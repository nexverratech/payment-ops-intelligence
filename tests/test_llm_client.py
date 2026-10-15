"""Ollama client, against a fake transport (no Ollama needed)."""

import json

import httpx2
import pytest

from payops.agents.schemas import PlanProposal
from payops.llm.client import LLMBudgetExceeded, LLMOutputError, LLMUnavailable, OllamaClient


def client_with(handler, max_calls=4):
    return OllamaClient("http://127.0.0.1:11434", "qwen3:4b", timeout_seconds=5, max_calls=max_calls,
                        transport=httpx2.MockTransport(handler))


def reply(content, status=200):
    return httpx2.Response(status, json={"message": {"role": "assistant", "content": content},
                                         "done": True, "prompt_eval_count": 120, "eval_count": 30})


GOOD = json.dumps({"week_containing": "2026-09-16", "provider_codes": ["KESTREL"], "reasoning": "Kestrel, mid-September."})


def test_request_is_local_structured_and_without_thinking():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return reply(GOOD)

    c = client_with(handler)
    result = c.generate("plan", "system text", "user text", PlanProposal)
    assert result.provider_codes == ["KESTREL"]
    body = seen["body"]
    assert seen["url"] == "http://127.0.0.1:11434/api/chat"
    assert body["think"] is False and body["stream"] is False
    assert body["format"] == PlanProposal.model_json_schema()
    assert body["options"]["temperature"] == 0 and body["options"]["num_ctx"] == 8192
    assert "JSON schema" in body["messages"][0]["content"]          # schema repeated in the prompt
    assert c.calls[0].status == "ok" and c.calls[0].prompt_tokens == 120 and c.calls[0].output_tokens == 30


def test_json_wrapped_in_code_fences_is_accepted():
    assert client_with(lambda r: reply("```json\n" + GOOD + "\n```")).generate("plan", "s", "u", PlanProposal).reasoning


def test_invalid_output_is_retried_once_then_rejected():
    replies = iter([reply("not json"), reply('{"provider_codes": "not-a-list"}')])
    c = client_with(lambda r: next(replies))
    with pytest.raises(LLMOutputError):
        c.generate("plan", "s", "u", PlanProposal)
    assert [x.status for x in c.calls] == ["invalid_output", "invalid_output"]


def test_retry_can_succeed():
    replies = iter([reply("oops"), reply(GOOD)])
    c = client_with(lambda r: next(replies))
    assert c.generate("plan", "s", "u", PlanProposal).week_containing == "2026-09-16"
    assert [x.status for x in c.calls] == ["invalid_output", "ok"]


def test_ollama_not_running_is_reported_as_unavailable():
    def handler(request):
        raise httpx2.ConnectError("refused", request=request)

    c = client_with(handler)
    with pytest.raises(LLMUnavailable, match="not reachable"):
        c.generate("plan", "s", "u", PlanProposal)
    assert c.calls[0].status == "unavailable"


def test_missing_model_is_reported_as_unavailable():
    with pytest.raises(LLMUnavailable, match="404"):
        client_with(lambda r: httpx2.Response(404, json={"error": "model 'qwen3:4b' not found"})).generate("plan", "s", "u", PlanProposal)


def test_call_budget_is_enforced():
    c = client_with(lambda r: reply(GOOD), max_calls=2)
    c.generate("plan", "s", "u", PlanProposal)
    c.generate("report", "s", "u", PlanProposal)
    with pytest.raises(LLMBudgetExceeded):
        c.generate("extra", "s", "u", PlanProposal)
    assert c.calls[-1].status == "budget_exceeded"


def test_non_local_ollama_address_is_refused():
    with pytest.raises(ValueError):
        OllamaClient("https://ollama.com", "qwen3:4b")
