from argparse import Namespace

from hzcu_agent.cli import _settings_for_serve
from hzcu_agent.config import get_settings
from hzcu_agent.local_model_config import load_local_openai_config


def test_settings_load_file_backed_production_secrets(tmp_path) -> None:
    model_secret = tmp_path / "model.secret"
    auth_secret = tmp_path / "auth.secret"
    model_secret.write_text("model-secret-with-at-least-32-characters", encoding="utf-8")
    auth_secret.write_text("auth-secret-with-at-least-32-characters", encoding="utf-8")

    from hzcu_agent.config import Settings

    settings = Settings(
        environment="production",
        auth_mode="anonymous",
        local_admin_enabled=True,
        public_api_base_url="https://agent.example.edu",
        web_app_url="https://agent.example.edu",
        model_config_secret_file=str(model_secret),
        auth_session_secret_file=str(auth_secret),
    )

    assert settings.model_config_secret is not None
    assert settings.model_config_secret.get_secret_value() == model_secret.read_text()
    assert settings.auth_session_secret is not None
    assert settings.auth_session_secret.get_secret_value() == auth_secret.read_text()


def test_load_local_openai_config_supports_named_values(tmp_path) -> None:
    config_path = tmp_path / "model.txt"
    config_path.write_text(
        "HZCU_OPENAI_BASE_URL=https://relay.example/v1\nHZCU_OPENAI_API_KEY=test-secret\n",
        encoding="utf-8",
    )

    config = load_local_openai_config(config_path)

    assert config.base_url == "https://relay.example/v1"
    assert config.api_key == "test-secret"


def test_serve_settings_enable_real_model_without_copying_config(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "model.txt"
    config_path.write_text(
        "https://relay.example/v1\ntest-secret\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()

    settings = _settings_for_serve(
        Namespace(
            model_config=str(config_path),
            anonymous_campus_mirror=True,
            model_timeout=180,
        )
    )

    assert settings.model_provider == "openai"
    assert settings.openai_base_url == "https://relay.example/v1"
    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "test-secret"
    assert settings.pilot_anonymous_campus_mirror is True
    assert settings.model_timeout_seconds == 180
    assert not (tmp_path / ".env").exists()
    get_settings.cache_clear()
