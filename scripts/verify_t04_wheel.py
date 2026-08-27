"""Verify T04 Provider and Embedder behavior from an installed wheel."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx

import oria
from oria.config import resolve_runtime_config
from oria.config.models import ResolvedLLMProfile
from oria.core.runtime import build_runtime
from oria.core.types import ChatOptions, Message, ResponseSchema
from oria.permission.local import local_cli_executor, local_operator
from oria.providers.embeddings import FixtureEmbedder
from oria.providers.mock import MockLLMProvider
from oria.providers.openai_compat import OpenAICompatProvider


async def _verify(data_dir: Path) -> None:
    runtime = await build_runtime(resolve_runtime_config(environ={}, data_dir=data_dir))
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id="wheel-session",
        thread_id="wheel-thread",
        run_id="wheel-run",
    )
    try:
        if not isinstance(ctx.llm, MockLLMProvider):
            raise AssertionError("installed wheel did not assemble MockLLMProvider")
        if not isinstance(ctx.embedder, FixtureEmbedder):
            raise AssertionError("installed wheel did not assemble FixtureEmbedder")
        first = await ctx.embedder.embed(["招商规则"], ctx)
        second = await ctx.embedder.embed(["招商规则"], ctx)
        if first != second or len(first[0]) != ctx.embedder.dim:
            raise AssertionError("installed FixtureEmbedder is not deterministic")

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "wheel-response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": '{"ok":true}'}],
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        profile = ResolvedLLMProfile(
            profile_id="deepseek",
            provider="deepseek",
            api_dialect="responses",
            model="deepseek-v4-flash",
            api_key="wheel-fixture-key",
            base_url="https://api.deepseek.com",
            structured_output_mode="native_json_schema",
        )
        schema = ResponseSchema(
            name="wheel_result",
            json_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        )
        async with httpx.AsyncClient(
            base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
        ) as client:
            result = await OpenAICompatProvider(profile, client).chat(
                [Message(role="user", content="fixture")],
                ctx,
                options=ChatOptions(response_schema=schema),
            )
        payload = json.loads(captured[0].content)
        if captured[0].url.path != "/responses" or "response_format" in payload:
            raise AssertionError("installed DeepSeek adapter did not use Responses dialect")
        if payload.get("text", {}).get("format", {}).get("type") != "json_schema":
            raise AssertionError("installed DeepSeek adapter did not map text.format")
        if result.structured_output != {"ok": True}:
            raise AssertionError("installed DeepSeek adapter did not validate structured output")
    finally:
        await runtime.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    package_file = Path(oria.__file__).resolve()
    if "site-packages" not in package_file.parts:
        raise AssertionError("Oria was not imported from an installed wheel environment")
    asyncio.run(_verify(args.data_dir.resolve(strict=False)))
    print(f"verified installed T04 providers and fixture embedder from {package_file}")


if __name__ == "__main__":
    main()
