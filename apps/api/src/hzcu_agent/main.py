import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import update

from hzcu_agent.api.routes import (
    admin,
    agent,
    auth,
    community,
    conversations,
    health,
    product,
    sources,
)
from hzcu_agent.auth.campus_access import CampusAccessBroker
from hzcu_agent.auth.contributor import LocalContributorAuthenticator
from hzcu_agent.auth.local_admin import LocalAdminAuthenticator
from hzcu_agent.auth.product_identity import ProductIdentityService
from hzcu_agent.auth.service import AuthService
from hzcu_agent.config import Settings, ensure_local_auth_session_secret, get_settings
from hzcu_agent.db import Database
from hzcu_agent.ingestion.catalog import SourceRegistry
from hzcu_agent.ingestion.service import IngestionService
from hzcu_agent.models import AgentTask, utc_now
from hzcu_agent.observability import RequestContextMiddleware, configure_logging
from hzcu_agent.runtime import TaskEventBroker
from hzcu_agent.services.agent_admission import AgentAdmissionService
from hzcu_agent.services.agent_policy import AgentPolicyService
from hzcu_agent.services.coordinator import AgentCoordinator
from hzcu_agent.services.image_reader import CampusImageReader
from hzcu_agent.services.model_gateway import ManagedModelGateway
from hzcu_agent.services.model_runtime import (
    ModelConfigurationStore,
    model_config_from_settings,
)
from hzcu_agent.services.task_scheduler import AgentTaskScheduler
from hzcu_agent.services.tool_gateway import ToolGateway
from hzcu_agent.tools.campus_document import CampusDocumentExplorer
from hzcu_agent.tools.campus_memory import CampusMemorySearchTool
from hzcu_agent.tools.campus_notices import CampusNoticeSearchTool
from hzcu_agent.tools.hzcu_official import HzcuOfficialSearchTool


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = ensure_local_auth_session_secret(settings or get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(resolved_settings)
        database = Database(resolved_settings)
        await database.initialize()
        source_registry = SourceRegistry(
            database,
            resolved_settings.resolved_source_registry_path,
        )
        await source_registry.sync_definitions()
        auth_service = AuthService(
            settings=resolved_settings,
            database=database,
        )
        local_admin = LocalAdminAuthenticator(
            settings=resolved_settings,
            database=database,
        )
        product_identity = ProductIdentityService(
            settings=resolved_settings,
            database=database,
        )
        contributors = LocalContributorAuthenticator(
            settings=resolved_settings,
            database=database,
        )
        policy = AgentPolicyService(settings=resolved_settings, database=database)
        await policy.initialize()
        admission = AgentAdmissionService(database=database, policy=policy)
        await admission.cleanup()
        model_configuration_store = ModelConfigurationStore(resolved_settings)
        async with database.session_factory() as session:
            await session.execute(
                update(AgentTask)
                .where(AgentTask.status == "running")
                .values(
                    status="failed",
                    error_code="SERVICE_RESTARTED",
                    updated_at=utc_now(),
                )
            )
            stored_model_config = await model_configuration_store.load(session)
            await session.commit()
        ingestion = IngestionService(
            settings=resolved_settings,
            database=database,
            registry=source_registry,
        )
        campus_access = CampusAccessBroker(
            settings=resolved_settings,
            registry=source_registry,
        )
        broker = TaskEventBroker()
        model_gateway = ManagedModelGateway(
            stored_model_config or model_config_from_settings(resolved_settings),
            call_gate=policy,
        )
        campus_documents = CampusDocumentExplorer(database)
        campus_memory = CampusMemorySearchTool(
            database,
            strategy=resolved_settings.retrieval_strategy,
        )
        await campus_memory.initialize()
        campus_notices = CampusNoticeSearchTool(
            access=campus_access,
            registry=source_registry,
            ingestion=ingestion,
            memory=campus_memory,
            image_reader=CampusImageReader(resolved_settings),
        )
        tool_gateway = ToolGateway(
            HzcuOfficialSearchTool(),
            campus_memory,
            campus_notices,
            ingestion,
            campus_documents=campus_documents,
        )
        coordinator = AgentCoordinator(
            settings=resolved_settings,
            database=database,
            broker=broker,
            models=model_gateway,
            tools=tool_gateway,
            policy=policy,
        )
        app.state.settings = resolved_settings
        app.state.database = database
        app.state.auth = auth_service
        app.state.local_admin = local_admin
        app.state.contributors = contributors
        app.state.product_identity = product_identity
        app.state.campus_access = campus_access
        app.state.source_registry = source_registry
        app.state.ingestion = ingestion
        app.state.broker = broker
        app.state.models = model_gateway
        app.state.model_configuration_store = model_configuration_store
        app.state.tools = tool_gateway
        app.state.coordinator = coordinator
        app.state.background_tasks = {}
        scheduler = AgentTaskScheduler(
            database=database,
            coordinator=coordinator,
            broker=broker,
            policy=policy,
            admission=admission,
            background_tasks=app.state.background_tasks,
        )
        app.state.policy = policy
        app.state.admission = admission
        app.state.scheduler = scheduler
        scheduler.start()
        yield
        await scheduler.stop()
        running_tasks = tuple(app.state.background_tasks.values())
        for task in running_tasks:
            task.cancel()
        if running_tasks:
            # Drain canceled coroutines before disposing the database.  Without
            # this, a task interrupted during an async SQLAlchemy commit can
            # enter the generic failure handler after its connection is closed,
            # producing noisy errors and hanging TestClient/worker shutdown.
            await asyncio.gather(*running_tasks, return_exceptions=True)
        await tool_gateway.close()
        await campus_access.close()
        await auth_service.close()
        await model_gateway.close()
        await database.close()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router, prefix=resolved_settings.api_prefix)
    app.include_router(agent.router, prefix=resolved_settings.api_prefix)
    app.include_router(auth.router, prefix=resolved_settings.api_prefix)
    app.include_router(community.router, prefix=resolved_settings.api_prefix)
    app.include_router(community.admin_router, prefix=resolved_settings.api_prefix)
    app.include_router(admin.router, prefix=resolved_settings.api_prefix)
    app.include_router(conversations.router, prefix=resolved_settings.api_prefix)
    app.include_router(product.router, prefix=resolved_settings.api_prefix)
    app.include_router(sources.router, prefix=resolved_settings.api_prefix)
    return app


app = create_app()
