"""Safe DeepSeek Responses tool-call diagnostic contracts."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from oria.eval import diagnose_deepseek_responses_tools

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def test_diagnostic_distinguishes_adapter_input_shape_and_redacts_secret() -> None:
    secret = "sk-local-diagnostic-secret"
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if isinstance(payload["input"], str):
            return httpx.Response(
                200,
                headers={"x-request-id": "req-official"},
                json={
                    "id": "resp-official",
                    "model": "deepseek-v4-flash",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "oria_health_probe",
                            "arguments": "{}",
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            )
        return httpx.Response(
            400,
            headers={"x-request-id": f"req-{secret}"},
            json={
                "error": {
                    "code": "invalid_request",
                    "type": "invalid_request_error",
                    "param": "input",
                    "message": f"Bearer {secret} rejected sk-other-secret-value",
                }
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        card = await diagnose_deepseek_responses_tools(
            client=client,
            api_key=SecretStr(secret),
            model="deepseek-v4-flash",
        )

    assert card.conclusion == "adapter_input_shape_rejected"
    assert card.probes[0].outcome == "tool_call"
    assert card.probes[1].error_param == "input"
    assert "[REDACTED]" in (card.probes[1].error_summary or "")
    assert card.probes[1].request_id == "req-[REDACTED]"
    assert secret not in card.model_dump_json()
    assert "sk-other-secret-value" not in card.model_dump_json()
    assert len(seen) == 2
    assert all(payload["reasoning"] == {"effort": "none"} for payload in seen)


async def test_diagnostic_identifies_provider_rejection_of_both_documented_shapes() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "invalid_request",
                    "type": "invalid_request_error",
                    "param": "tools",
                    "message": "tools are unavailable",
                }
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        card = await diagnose_deepseek_responses_tools(
            client=client,
            api_key=SecretStr("test-only"),
            model="deepseek-v4-flash",
        )

    assert card.conclusion == "provider_rejected_documented_payload"
    assert all(probe.error_param == "tools" for probe in card.probes)
