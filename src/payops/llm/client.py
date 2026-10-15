"""Client for the local Ollama server.

Guard rails, all enforced here:
* Local only: the base URL must be 127.0.0.1/localhost (checked at construction).
* Bounded: every request has a timeout, and each investigation gets a fixed
  budget of LLM calls (a fresh client per investigation).
* Structured: the reply must be JSON matching a Pydantic schema. Ollama is asked
  to follow the schema (`format`), and the reply is validated again here.
  One retry on invalid output, then LLMOutputError.
* Fast: Qwen3's "thinking" is switched off (`think: false`); temperature 0 and a
  fixed seed make replies as repeatable as the model allows.
* Observable: every call is recorded as an LLMCall (purpose, time, tokens, outcome).
"""

from __future__ import annotations

import json
import time
from typing import Literal, Protocol, TypeVar

import httpx2
from pydantic import BaseModel, ValidationError

from payops.netpolicy import require_local_url

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """Ollama is not running, not reachable in time, or the model is not pulled."""


class LLMOutputError(RuntimeError):
    """The model replied, but not with valid JSON for the schema (after one retry)."""


class LLMBudgetExceeded(RuntimeError):
    """This investigation has used all its allowed LLM calls."""


class LLMCall(BaseModel):
    purpose: str
    model: str
    attempt: int
    status: Literal["ok", "invalid_output", "unavailable", "budget_exceeded"]
    duration_ms: int
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    from_checkpoint: bool = False   # True = made in an earlier run, carried over by a resume


class LLMClient(Protocol):
    """What the agents need from an LLM. Tests use a scripted fake with the same shape."""

    model: str
    calls: list[LLMCall]

    def generate(self, purpose: str, system: str, user: str, schema: type[T]) -> T: ...


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout_seconds: float = 120, max_calls: int = 4,
                 context_tokens: int = 8192, transport: httpx2.BaseTransport | None = None):
        self.base_url = require_local_url(base_url).rstrip("/")
        self.model = model
        self.max_calls = max_calls
        self.context_tokens = context_tokens
        self.calls: list[LLMCall] = []
        self._http = httpx2.Client(base_url=self.base_url, timeout=timeout_seconds, transport=transport)

    @classmethod
    def from_settings(cls, settings) -> OllamaClient:
        return cls(settings.ollama_base_url, settings.ollama_model, settings.llm_timeout_seconds,
                   settings.llm_max_calls, settings.llm_context_tokens)

    @property
    def used(self) -> int:
        return sum(1 for c in self.calls if c.status != "budget_exceeded")

    def generate(self, purpose: str, system: str, user: str, schema: type[T]) -> T:
        json_schema = schema.model_json_schema()
        # Ollama's docs recommend also showing the schema in the prompt.
        system = f"{system}\n\nReply with JSON only, matching this JSON schema:\n{json.dumps(json_schema)}"
        prompt = user
        for attempt in (1, 2):
            if self.used >= self.max_calls:
                self.calls.append(LLMCall(purpose=purpose, model=self.model, attempt=attempt, status="budget_exceeded", duration_ms=0))
                raise LLMBudgetExceeded(f"LLM call budget of {self.max_calls} used up")
            started = time.perf_counter()
            body = {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "stream": False,
                "think": False,
                "format": json_schema,
                "options": {"temperature": 0, "seed": 7, "num_ctx": self.context_tokens},
            }
            try:
                response = self._http.post("/api/chat", json=body)
            except httpx2.TimeoutException:
                self._record(purpose, attempt, "unavailable", started)
                raise LLMUnavailable("The local LLM did not answer in time.") from None
            except httpx2.TransportError:
                self._record(purpose, attempt, "unavailable", started)
                raise LLMUnavailable("The local LLM (Ollama) is not reachable.") from None
            if response.status_code != 200:
                self._record(purpose, attempt, "unavailable", started)
                raise LLMUnavailable(f"Ollama returned HTTP {response.status_code}: {_short(response.text)}")

            data = response.json()
            content = _strip_fences((data.get("message") or {}).get("content", ""))
            try:
                result = schema.model_validate_json(content)
            except ValidationError:
                self._record(purpose, attempt, "invalid_output", started, data)
                if attempt == 2:
                    raise LLMOutputError(f"The model's {purpose} reply was not valid for the schema (2 attempts).") from None
                prompt = user + "\n\nYour previous reply did not match the JSON schema. Reply again with JSON only."
                continue
            self._record(purpose, attempt, "ok", started, data)
            return result
        raise LLMOutputError("unreachable")  # pragma: no cover

    def _record(self, purpose: str, attempt: int, status: str, started: float, data: dict | None = None) -> None:
        self.calls.append(LLMCall(
            purpose=purpose, model=self.model, attempt=attempt, status=status,
            duration_ms=int((time.perf_counter() - started) * 1000),
            prompt_tokens=(data or {}).get("prompt_eval_count"), output_tokens=(data or {}).get("eval_count"),
        ))


def _strip_fences(text: str) -> str:
    """Some models wrap JSON in ```json fences despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _short(text: str) -> str:
    return " ".join(text.split())[:200]
