import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_prefix="HZCU_",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "HZCU Campus Agent"
    environment: Literal["development", "test", "production"] = "development"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite+aiosqlite:///./data/hzcu_agent.db"
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    model_provider: Literal["demo", "openai", "anthropic"] = "demo"
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str | None = None
    model_config_secret: SecretStr | None = None
    model_config_secret_file: str | None = None
    agent_model: str = "gpt-5.6-sol"
    utility_model: str = "gpt-5.6-terra"
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "medium"
    utility_reasoning_effort: Literal[
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ] = "low"
    model_timeout_seconds: float = 60.0

    max_tool_rounds: int = 3
    max_tool_calls: int = 8
    source_registry_path: str | None = None
    snapshot_directory: str = "./data/snapshots"
    sync_max_concurrency: int = 4
    max_download_bytes: int = 25 * 1024 * 1024
    ingestion_allowed_visibilities: str = "public"

    # Campus identity is an optional enhancement: anonymous users keep Public
    # access, while a validated CAS identity adds the Campus visibility scope.
    auth_mode: Literal["anonymous", "optional_cas", "required_cas"] = "anonymous"
    public_api_base_url: str = "http://localhost:8000"
    web_app_url: str = "http://localhost:3000"
    cas_browser_base_url: str = "http://ca.hzcu.edu.cn/cas"
    cas_server_base_url: str = "http://ca.hzcu.edu.cn/cas"
    cas_validation_path: str = "/serviceValidate"
    cas_service_registered: bool = False
    cas_http_transport_approved: bool = False
    auth_session_secret: SecretStr | None = None
    auth_session_secret_file: str | None = None
    auth_session_hours: int = 8
    auth_state_minutes: int = 10
    auth_cookie_name: str = "hzcu_session"
    auth_state_cookie_name: str = "hzcu_cas_state"
    auth_csrf_cookie_name: str = "hzcu_csrf"
    auth_login_csrf_cookie_name: str = "hzcu_login_csrf"
    visitor_cookie_name: str = "hzcu_visitor"
    visitor_session_days: int = 180
    auth_cookie_secure: bool | None = None
    admin_cas_subjects: SecretStr | None = None
    # An independently verified server administrator can sign in without CAS.
    # The first credential is created through the loopback-only development UI
    # and the password is persisted only as a slow hash.
    local_admin_enabled: bool = False
    local_auth_secret_path: str = "./data/local_auth.secret"
    max_concurrent_agent_tasks: int = 4
    max_active_tasks_per_subject: int = 1
    # A Stage 6 pilot may expose already-approved, read-only Campus mirror
    # records to anonymous device subjects. This never grants a Campus
    # identity, enables live campus routing, or exposes restricted data.
    pilot_anonymous_campus_mirror: bool = False

    # Campus notice queries are strictly read-only. A campus-hosted API can use
    # direct routing; an off-campus API can delegate to an approved Windows
    # aTrust + headless Edge sidecar. Student credentials are accepted only by
    # the explicit VPN handoff and are never persisted by this service.
    campus_query_route: Literal["disabled", "direct", "vpn_sidecar", "auto"] = "disabled"
    campus_direct_probe_url: str = "http://hzcujwb.hzcu.edu.cn/index.php?c=main&a=tlist&id=3"
    campus_direct_probe_seconds: int = 30
    credential_vpn_enabled: bool = False
    vpn_sidecar_base_url: str | None = None
    vpn_sidecar_api_token: SecretStr | None = None
    vpn_sidecar_timeout_seconds: float = 60.0
    vpn_session_minutes: int = 15

    @field_validator(
        "openai_api_key",
        "anthropic_api_key",
        "model_config_secret",
        mode="before",
    )
    @classmethod
    def empty_secret_is_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("openai_base_url", "anthropic_base_url", mode="before")
    @classmethod
    def empty_url_is_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("auth_session_secret", mode="before")
    @classmethod
    def empty_auth_secret_is_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator(
        "model_config_secret_file",
        "auth_session_secret_file",
        mode="before",
    )
    @classmethod
    def empty_secret_file_is_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("admin_cas_subjects", mode="before")
    @classmethod
    def empty_admin_subjects_is_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("vpn_sidecar_api_token", mode="before")
    @classmethod
    def empty_sidecar_secret_is_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("vpn_sidecar_base_url", mode="before")
    @classmethod
    def empty_sidecar_url_is_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator(
        "public_api_base_url",
        "web_app_url",
        "cas_browser_base_url",
        "cas_server_base_url",
    )
    @classmethod
    def normalize_origin_or_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("authentication URLs must be absolute HTTP(S) URLs")
        return normalized

    @field_validator("cas_validation_path")
    @classmethod
    def validation_path_must_be_relative(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/") or "://" in normalized or "?" in normalized:
            raise ValueError("CAS validation path must be an absolute URL path")
        return normalized

    @field_validator("campus_direct_probe_url")
    @classmethod
    def valid_direct_probe_url(cls, value: str) -> str:
        normalized = value.strip()
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not parsed.hostname.endswith((".hzcu.edu.cn", ".zucc.edu.cn"))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("campus direct probe must be an HZCU HTTP(S) URL")
        return normalized

    @field_validator("vpn_sidecar_base_url")
    @classmethod
    def normalize_sidecar_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("VPN sidecar base URL must be an absolute HTTP(S) URL")
        return normalized

    @field_validator("auth_session_hours")
    @classmethod
    def valid_session_lifetime(cls, value: int) -> int:
        if not 1 <= value <= 168:
            raise ValueError("auth session lifetime must be between 1 and 168 hours")
        return value

    @field_validator("visitor_session_days")
    @classmethod
    def valid_visitor_lifetime(cls, value: int) -> int:
        if not 1 <= value <= 365:
            raise ValueError("visitor session lifetime must be between 1 and 365 days")
        return value

    @field_validator("max_concurrent_agent_tasks")
    @classmethod
    def valid_global_agent_concurrency(cls, value: int) -> int:
        if not 1 <= value <= 16:
            raise ValueError("global Agent concurrency must be between 1 and 16")
        return value

    @field_validator("max_active_tasks_per_subject")
    @classmethod
    def valid_subject_agent_concurrency(cls, value: int) -> int:
        if not 1 <= value <= 4:
            raise ValueError("per-subject Agent concurrency must be between 1 and 4")
        return value

    @field_validator("ingestion_allowed_visibilities")
    @classmethod
    def valid_ingestion_visibilities(cls, value: str) -> str:
        scopes = {item.strip().lower() for item in value.split(",") if item.strip()}
        if not scopes or "public" not in scopes or not scopes <= {"public", "campus"}:
            raise ValueError(
                "ingestion visibilities must contain public and may additionally contain campus"
            )
        return ",".join(sorted(scopes))

    @field_validator("auth_state_minutes")
    @classmethod
    def valid_state_lifetime(cls, value: int) -> int:
        if not 2 <= value <= 30:
            raise ValueError("CAS state lifetime must be between 2 and 30 minutes")
        return value

    @field_validator("campus_direct_probe_seconds")
    @classmethod
    def valid_direct_probe_interval(cls, value: int) -> int:
        if not 5 <= value <= 300:
            raise ValueError("campus direct probe interval must be between 5 and 300 seconds")
        return value

    @field_validator("vpn_session_minutes")
    @classmethod
    def valid_vpn_session_lifetime(cls, value: int) -> int:
        if not 5 <= value <= 60:
            raise ValueError("VPN query session lifetime must be between 5 and 60 minutes")
        return value

    @model_validator(mode="after")
    def load_file_backed_secrets(self) -> "Settings":
        if self.model_config_secret is None and self.model_config_secret_file is not None:
            self.model_config_secret = SecretStr(
                _read_secret_file(self.model_config_secret_file, "model configuration")
            )
        if self.auth_session_secret is None and self.auth_session_secret_file is not None:
            self.auth_session_secret = SecretStr(
                _read_secret_file(self.auth_session_secret_file, "authentication session")
            )
        return self

    @model_validator(mode="after")
    def validate_authentication_configuration(self) -> "Settings":
        if self.credential_vpn_enabled:
            if self.auth_mode == "anonymous":
                raise ValueError("credential VPN handoff requires optional_cas or required_cas")
            if self.vpn_sidecar_base_url is None or self.vpn_sidecar_api_token is None:
                raise ValueError("credential VPN handoff requires a sidecar URL and API token")
            if len(self.vpn_sidecar_api_token.get_secret_value()) < 32:
                raise ValueError("VPN sidecar API token must contain at least 32 characters")
            if (
                self.environment == "production"
                and urlsplit(self.vpn_sidecar_base_url).scheme != "https"
            ):
                raise ValueError("production VPN sidecar handoff requires HTTPS")
        if self.campus_query_route == "vpn_sidecar" and not self.credential_vpn_enabled:
            raise ValueError("vpn_sidecar query route requires credential VPN handoff")
        authentication_enabled = self.auth_mode != "anonymous" or self.local_admin_enabled
        if not authentication_enabled:
            return self
        if self.auth_session_secret is None:
            if self.local_admin_enabled and self.auth_mode == "anonymous":
                if self.environment == "production":
                    raise ValueError("production local admin requires HZCU_AUTH_SESSION_SECRET")
                return self
            raise ValueError("authenticated sessions require HZCU_AUTH_SESSION_SECRET")
        if len(self.auth_session_secret.get_secret_value()) < 32:
            raise ValueError("HZCU_AUTH_SESSION_SECRET must contain at least 32 characters")
        if (
            self.environment == "production"
            and urlsplit(self.public_api_base_url).scheme != "https"
            and not self.cas_http_transport_approved
        ):
            raise ValueError("production CAS callbacks require an HTTPS public API URL")
        if (
            self.environment == "production"
            and urlsplit(self.web_app_url).scheme != "https"
            and not self.cas_http_transport_approved
        ):
            raise ValueError("production authenticated Web sessions require HTTPS")
        if (
            self.cas_is_enabled
            and self.environment == "production"
            and urlsplit(self.cas_browser_base_url).scheme != "https"
            and not self.cas_http_transport_approved
        ):
            raise ValueError("production login is blocked while the browser-facing CAS URL is HTTP")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def model_is_configured(self) -> bool:
        if self.model_provider == "demo":
            return True
        if self.model_provider == "anthropic":
            return self.anthropic_api_key is not None
        return self.openai_api_key is not None

    @property
    def effective_model_config_secret(self) -> SecretStr | None:
        return self.model_config_secret or self.auth_session_secret

    @property
    def cas_is_enabled(self) -> bool:
        return self.auth_mode in {"optional_cas", "required_cas"}

    @property
    def cas_login_ready(self) -> bool:
        return self.cas_is_enabled and self.cas_service_registered

    @property
    def resolved_auth_cookie_secure(self) -> bool:
        if self.auth_cookie_secure is not None:
            return self.auth_cookie_secure
        return self.environment == "production"

    def is_admin_cas_subject(self, subject: str) -> bool:
        if self.admin_cas_subjects is None:
            return False
        allowed = {
            item.strip()
            for item in self.admin_cas_subjects.get_secret_value().split(",")
            if item.strip()
        }
        return subject in allowed

    @property
    def has_admin_cas_subjects(self) -> bool:
        if self.admin_cas_subjects is None:
            return False
        return any(item.strip() for item in self.admin_cas_subjects.get_secret_value().split(","))

    @property
    def local_admin_setup_allowed(self) -> bool:
        return self.local_admin_enabled and self.environment in {"development", "test"}

    @property
    def ingestion_visibility_set(self) -> frozenset[str]:
        return frozenset(self.ingestion_allowed_visibilities.split(","))

    def local_mirror_visibility_scopes(
        self,
        identity_scopes: frozenset[str],
        *,
        authenticated: bool,
    ) -> frozenset[str]:
        """Return scopes usable only for the already-ingested local mirror."""

        if self.pilot_anonymous_campus_mirror and not authenticated:
            return identity_scopes | {"campus"}
        return identity_scopes

    @property
    def effective_sync_max_concurrency(self) -> int:
        # SQLite permits only one writer at a time. Resource ingestion opens
        # independent transactions, so parallel writes make a local/demo
        # deployment intermittently fail with "database is locked". Production
        # PostgreSQL deployments retain the configured concurrency.
        if self.database_url.startswith("sqlite"):
            return 1
        return max(1, self.sync_max_concurrency)

    @property
    def resolved_source_registry_path(self) -> Path:
        if self.source_registry_path:
            return Path(self.source_registry_path).expanduser().resolve()
        return Path(__file__).parent / "resources" / "sources.yaml"

    @property
    def resolved_snapshot_directory(self) -> Path:
        return Path(self.snapshot_directory).expanduser().resolve()

    @property
    def resolved_local_auth_secret_path(self) -> Path:
        return Path(self.local_auth_secret_path).expanduser().resolve()

    def ensure_local_data_directories(self) -> None:
        if self.database_url.startswith("sqlite"):
            database_path = self.database_url.rsplit("///", maxsplit=1)[-1]
            if database_path != ":memory:":
                Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self.resolved_snapshot_directory.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _read_secret_file(path_value: str, purpose: str) -> str:
    path = Path(path_value).expanduser().resolve()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"unable to read {purpose} secret file: {path}") from exc
    if not value:
        raise ValueError(f"{purpose} secret file is empty: {path}")
    return value


def ensure_local_auth_session_secret(settings: Settings) -> Settings:
    """Create one stable development-only secret for password-backed sessions."""

    if settings.auth_session_secret is not None or not settings.local_admin_enabled:
        return settings
    if settings.environment == "production":
        raise ValueError("production local admin requires HZCU_AUTH_SESSION_SECRET")

    path = settings.resolved_local_auth_secret_path
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        value = path.read_text(encoding="utf-8").strip()
    else:
        value = secrets.token_urlsafe(48)
        try:
            os.write(descriptor, value.encode("utf-8"))
        finally:
            os.close(descriptor)
        try:
            path.chmod(0o600)
        except OSError:
            # Some mounted Windows volumes do not expose POSIX mode bits.
            pass
    if len(value) < 32:
        raise ValueError("local authentication secret file is invalid")
    return settings.model_copy(update={"auth_session_secret": SecretStr(value)})
