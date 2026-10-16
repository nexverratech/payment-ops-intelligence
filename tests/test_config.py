"""Configuration validation tests."""

import pytest
from pydantic import ValidationError

from payops.config import Settings


def make(**overrides):
    base = {"postgres_password": "pw", "readonly_db_password": "ro-pw"}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_defaults_are_local():
    s = make()
    assert s.postgres_host in {"127.0.0.1", "localhost"}
    assert s.ollama_base_url.startswith("http://127.0.0.1")


def test_remote_ollama_url_is_rejected():
    with pytest.raises(ValidationError):
        make(ollama_base_url="https://ollama.com")


def test_remote_database_host_is_rejected():
    with pytest.raises(ValidationError):
        make(postgres_host="db.example.com")


def test_cloud_model_is_rejected():
    with pytest.raises(ValidationError):
        make(ollama_model="gpt-oss:120b-cloud")


def test_invalid_port_is_rejected():
    with pytest.raises(ValidationError):
        make(postgres_port=70000)


def test_password_is_hidden_when_printed():
    s = make(postgres_password="super-secret-value")
    assert "super-secret-value" not in repr(s)
    assert "super-secret-value" not in str(s.database_url())  # SQLAlchemy masks it as ***
    assert s.database_url().password == "super-secret-value"  # but the driver still gets it


def test_password_with_special_characters_builds_valid_url():
    s = make(postgres_password="p@ss:w/rd#1")
    assert s.database_url().password == "p@ss:w/rd#1"


def test_readonly_url_uses_readonly_account():
    s = make(readonly_db_user="payops_readonly", readonly_db_password="ro-secret")
    url = s.database_url(readonly=True)
    assert url.username == "payops_readonly"
    assert url.password == "ro-secret"
    assert s.database_url().username == s.postgres_user


def test_unsafe_readonly_role_name_is_rejected():
    with pytest.raises(ValidationError):
        make(readonly_db_user='evil"; DROP TABLE x; --')


def test_history_is_off_without_a_password(monkeypatch):
    monkeypatch.delenv("APP_DB_PASSWORD", raising=False)   # conftest copies your real one from .env
    s = make()
    assert s.history_enabled is False
    with pytest.raises(ValueError, match="APP_DB_PASSWORD"):
        s.database_url(history=True)


def test_history_url_uses_the_history_account():
    url = make(app_db_password="hist-pw").database_url(history=True)
    assert url.username == "payops_app" and url.password == "hist-pw"


def test_heartbeat_limit_is_longer_than_any_single_step():
    s = make(investigation_timeout_seconds=600, llm_timeout_seconds=180)
    assert s.stale_after_seconds > 600 + 2 * 180


@pytest.mark.parametrize("field, value", [("investigation_timeout_seconds", 5), ("max_concurrent_investigations", 0),
                                          ("app_db_user", "Robert'); DROP TABLE x;--")])
def test_unsafe_limits_and_names_are_rejected(field, value):
    with pytest.raises(ValidationError):
        make(**{field: value})


def test_docker_service_names_are_local_only_when_listed():
    with pytest.raises(ValidationError):
        make(postgres_host="db")
    s = make(postgres_host="db", ollama_base_url="http://host.docker.internal:11434",
             local_service_hosts="db, api ,host.docker.internal")
    assert s.service_hosts == ("db", "api", "host.docker.internal")


@pytest.mark.parametrize("hosts", ["ollama.com", "db,evil.example.org", "10.0.0.5"])
def test_only_single_word_names_may_be_listed(hosts):
    with pytest.raises(ValidationError):
        make(local_service_hosts=hosts)


def test_the_api_runs_without_the_owner_password():
    s = Settings(_env_file=None, postgres_password=None, readonly_db_password="ro-pw")
    assert s.database_url(readonly=True).username == "payops_readonly"
    with pytest.raises(ValueError, match="POSTGRES_PASSWORD"):
        s.database_url()
    with pytest.raises(SystemExit, match="POSTGRES_PASSWORD"):
        s.owner_password()
