"""Dashboard HTTP client, using a fake transport instead of a running API."""

import json
from datetime import datetime, timezone

import httpx2
import pytest

from payops.dashboard.api_client import ApiClient, ApiError, Filters

F = Filters(
    start=datetime(2026, 9, 21, tzinfo=timezone.utc), end=datetime(2026, 9, 28, tzinfo=timezone.utc),
    providers=("KESTREL", "ORBIT"), reasons=("TIMEOUT",),
)


def client_with(handler):
    return ApiClient("http://127.0.0.1:8000", transport=httpx2.MockTransport(handler))


def test_filters_become_repeated_query_parameters():
    seen = {}

    def handler(request):
        seen["url"] = request.url
        return httpx2.Response(200, json={"rows": []})

    client_with(handler).breakdown(F, by="provider")
    params = seen["url"].params
    assert seen["url"].path == "/api/v1/breakdown"
    assert params.get_list("provider") == ["KESTREL", "ORBIT"]
    assert params.get_list("reason") == ["TIMEOUT"]
    assert params["start"] == "2026-09-21T00:00:00+00:00"
    assert params["by"] == "provider"


def test_investigation_posts_json_and_accepts_201():
    def handler(request):
        assert request.method == "POST"
        assert json.loads(request.content) == {"question": "Why did failures rise?"}
        return httpx2.Response(201, json={"mode": "deterministic"})

    assert client_with(handler).investigate("Why did failures rise?") == {"mode": "deterministic"}


def test_health_body_is_returned_even_when_database_is_down():
    body = client_with(lambda r: httpx2.Response(503, json={"status": "down"})).health()
    assert body["status"] == "down"


def test_validation_errors_become_readable_messages():
    def handler(request):
        return httpx2.Response(422, json={"detail": ["provider_codes: 'kestrel' is not a valid code"]})

    with pytest.raises(ApiError, match="API error 422: provider_codes"):
        client_with(handler).kpis(F)


def test_api_not_running_gives_a_helpful_message():
    def handler(request):
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(ApiError, match="Cannot reach the API.*uvicorn"):
        client_with(handler).meta()


def test_client_refuses_a_non_local_api_address():
    with pytest.raises(ValueError):
        ApiClient("https://example.com")
