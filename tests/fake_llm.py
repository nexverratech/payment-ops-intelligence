"""A scripted stand-in for the local LLM, so LLM-dependent code is tested fast and repeatably."""

from payops.llm.client import LLMCall, LLMUnavailable


class FakeLLM:
    model = "fake-llm"

    def __init__(self, **responses):
        """responses: purpose -> dict (the JSON the model "returns"), an Exception to raise,
        or a function(user_prompt) -> dict."""
        self.responses = responses
        self.calls: list[LLMCall] = []
        self.prompts: dict[str, str] = {}

    def generate(self, purpose, system, user, schema):
        self.prompts[purpose] = user
        reply = self.responses.get(purpose, LLMUnavailable("fake: no scripted reply"))
        if callable(reply) and not isinstance(reply, type):
            reply = reply(user)
        if isinstance(reply, Exception):
            self.calls.append(LLMCall(purpose=purpose, model=self.model, attempt=1, status="unavailable", duration_ms=0))
            raise reply
        self.calls.append(LLMCall(purpose=purpose, model=self.model, attempt=1, status="ok", duration_ms=1))
        return schema.model_validate(reply)
