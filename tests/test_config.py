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
