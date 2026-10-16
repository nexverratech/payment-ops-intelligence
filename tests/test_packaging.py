"""Security properties of the Docker and CI files, checked as text so a careless edit fails a test."""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")   # PyYAML comes with LangChain; skip cleanly if ever absent

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_every_published_port_is_bound_to_this_computer_only():
    ports = [p for s in COMPOSE["services"].values() for p in s.get("ports", [])]
    assert ports and all(str(p).startswith("127.0.0.1:") for p in ports), ports


def test_the_api_and_dashboard_never_receive_the_owner_password():
    api, dashboard = COMPOSE["services"]["api"]["environment"], COMPOSE["services"]["dashboard"]["environment"]
    assert "POSTGRES_PASSWORD" not in api
    assert not any("PASSWORD" in k for k in dashboard)          # the dashboard only talks to the API


def test_only_container_names_are_declared_local():
    from payops.netpolicy import valid_service_host

    for service in COMPOSE["services"].values():
        hosts = (service.get("environment") or {}).get("LOCAL_SERVICE_HOSTS", "")
        assert all(valid_service_host(h.strip()) for h in hosts.split(",") if h.strip())


def test_the_image_runs_as_an_unprivileged_user_and_holds_no_secrets():
    assert "\nUSER payops" in DOCKERFILE
    assert "PASSWORD=" not in DOCKERFILE and "ARG " not in DOCKERFILE        # no secrets baked in or passed at build
    assert "STREAMLIT_BROWSER_GATHER_USAGE_STATS=false" in DOCKERFILE and "LANGSMITH_TRACING=false" in DOCKERFILE


def test_secrets_and_data_stay_out_of_the_build():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert {".env", "data", ".git", ".venv"} <= set(ignored)


def test_ci_token_is_read_only_and_needs_no_repository_secrets():
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert "secrets." not in (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def test_ci_runs_python_311_and_fails_on_skipped_database_tests():
    steps = WORKFLOW["jobs"]["tests"]["steps"]
    assert any(s.get("with", {}).get("python-version") == "3.11" for s in steps)
    assert any("check_test_report.py" in s.get("run", "") for s in steps)
