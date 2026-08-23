from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession

from hzcu_agent.config import Settings
from hzcu_agent.models import RuntimeModelConfiguration, utc_now

ModelProtocol = Literal["demo", "openai_responses", "anthropic_messages"]
ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]


class StoredModelConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelEndpointConfig:
    protocol: ModelProtocol
    api_key: str | None
    base_url: str | None
    agent_model: str
    utility_model: str
    reasoning_effort: ReasoningEffort
    utility_reasoning_effort: ReasoningEffort
    timeout_seconds: float
    source: Literal["environment", "database"] = "environment"
    updated_at: datetime | None = None

    @property
    def provider(self) -> str:
        return {
            "demo": "demo",
            "openai_responses": "openai",
            "anthropic_messages": "anthropic",
        }[self.protocol]

    @property
    def configured(self) -> bool:
        return self.protocol == "demo" or bool(self.api_key)

    @property
    def api_key_hint(self) -> str | None:
        if not self.api_key:
            return None
        return f"••••{self.api_key[-4:]}"


def model_config_from_settings(settings: Settings) -> ModelEndpointConfig:
    protocol: ModelProtocol = {
        "demo": "demo",
        "openai": "openai_responses",
        "anthropic": "anthropic_messages",
    }[settings.model_provider]
    if settings.model_provider == "openai":
        api_key = (
            settings.openai_api_key.get_secret_value()
            if settings.openai_api_key is not None
            else None
        )
        base_url = settings.openai_base_url
    elif settings.model_provider == "anthropic":
        api_key = (
            settings.anthropic_api_key.get_secret_value()
            if settings.anthropic_api_key is not None
            else None
        )
        base_url = settings.anthropic_base_url
    else:
        api_key = None
        base_url = None
    return ModelEndpointConfig(
        protocol=protocol,
        api_key=api_key,
        base_url=base_url,
        agent_model=settings.agent_model,
        utility_model=settings.utility_model,
        reasoning_effort=settings.reasoning_effort,
        utility_reasoning_effort=settings.utility_reasoning_effort,
        timeout_seconds=settings.model_timeout_seconds,
    )


class ModelConfigurationStore:
    """Persist one server-level endpoint without returning its API key to clients."""

    record_id = "primary"

    def __init__(self, settings: Settings) -> None:
        secret = settings.effective_model_config_secret
        self._fernet: Fernet | None = None
        if secret is not None:
            digest = hashlib.sha256(secret.get_secret_value().encode("utf-8")).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    async def load(self, session: AsyncSession) -> ModelEndpointConfig | None:
        row = await session.get(RuntimeModelConfiguration, self.record_id)
        if row is None:
            return None
        if self._fernet is None:
            raise StoredModelConfigurationError(
                "读取后台模型配置需要 CA 会话密钥或独立模型配置密钥。"
            )
        try:
            api_key = self._fernet.decrypt(row.encrypted_api_key.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise StoredModelConfigurationError("后台模型 API 密钥无法解密。") from exc
        return ModelEndpointConfig(
            protocol=row.protocol,  # type: ignore[arg-type]
            api_key=api_key,
            base_url=row.base_url,
            agent_model=row.agent_model,
            utility_model=row.utility_model,
            reasoning_effort=row.reasoning_effort,  # type: ignore[arg-type]
            utility_reasoning_effort=row.utility_reasoning_effort,  # type: ignore[arg-type]
            timeout_seconds=row.timeout_seconds,
            source="database",
            updated_at=row.updated_at,
        )

    async def save(
        self,
        session: AsyncSession,
        config: ModelEndpointConfig,
        *,
        actor_user_id: str,
    ) -> ModelEndpointConfig:
        if not config.api_key:
            raise StoredModelConfigurationError("公用模型端点必须提供 API 密钥。")
        if self._fernet is None:
            raise StoredModelConfigurationError(
                "保存后台模型配置需要 CA 会话密钥或独立模型配置密钥。"
            )
        now = utc_now()
        encrypted = self._fernet.encrypt(config.api_key.encode("utf-8")).decode("ascii")
        row = await session.get(RuntimeModelConfiguration, self.record_id)
        if row is None:
            row = RuntimeModelConfiguration(
                id=self.record_id,
                protocol=config.protocol,
                base_url=config.base_url,
                encrypted_api_key=encrypted,
                api_key_hint=config.api_key_hint or "••••",
                agent_model=config.agent_model,
                utility_model=config.utility_model,
                reasoning_effort=config.reasoning_effort,
                utility_reasoning_effort=config.utility_reasoning_effort,
                timeout_seconds=config.timeout_seconds,
                updated_by_user_id=actor_user_id,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.protocol = config.protocol
            row.base_url = config.base_url
            row.encrypted_api_key = encrypted
            row.api_key_hint = config.api_key_hint or "••••"
            row.agent_model = config.agent_model
            row.utility_model = config.utility_model
            row.reasoning_effort = config.reasoning_effort
            row.utility_reasoning_effort = config.utility_reasoning_effort
            row.timeout_seconds = config.timeout_seconds
            row.updated_by_user_id = actor_user_id
            row.updated_at = now
        await session.flush()
        return replace(config, source="database", updated_at=now)
