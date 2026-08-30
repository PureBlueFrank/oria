"""Execute the explicit DeepSeek Provider Live smoke and save a redacted card."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from pydantic import SecretStr

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import (
    ChatOptions,
    Done,
    Message,
    Principal,
    ProviderError,
    TextDelta,
    ToolSpec,
    Usage,
    UsageDelta,
)
from oria.providers.errors import AuthenticationError
from oria.providers.openai_compat import OpenAICompatProvider


@dataclass
class LiveProgress:
    request_count: int = 0
    request_ids: list[str] = field(default_factory=list)
    usages: list[Usage] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    provider: str | None = None
    model: str | None = None
    api_dialect: str | None = None
    config_fingerprint: str | None = None


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, choices=("deepseek",))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/eval/provider-live.json"),
    )
    return parser.parse_args()


def _usage_dict(usage: Usage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens or 0,
        "cache_read_tokens": usage.cache_read_tokens or 0,
    }


async def _run(args: argparse.Namespace, progress: LiveProgress) -> dict[str, object]:
    environ = dict(os.environ)
    environ.update({"ORIA_ENVIRONMENT": "test", "ORIA_EMBEDDING_PROFILE": "fixture"})
    resolved = resolve_runtime_config(
        runtime_profile="standard",
        llm_profile=args.target,
        data_dir=Path(".artifacts/eval/provider-live-runtime"),
        environ=environ,
    )
    progress.provider = resolved.llm.provider
    progress.model = resolved.llm.model
    progress.api_dialect = resolved.llm.api_dialect
    progress.config_fingerprint = resolved.config_fingerprint
    runtime = await build_runtime(resolved)
    principal = Principal(
        subject_id="provider-live",
        tenant_id="eval-synthetic",
        kind="service",
        roles=("eval",),
        authn_method="workflow_secret",
    )
    request_ids = progress.request_ids
    usages = progress.usages
    checks = progress.checks
    started = time.perf_counter()
    async with runtime:
        ctx = runtime.new_context(
            actor=principal,
            executor=principal,
            session_id="provider-live",
            thread_id="provider-live",
            run_id=f"provider-live-{int(time.time())}",
        )
        if runtime.llm is None:
            raise RuntimeError("selected provider is unavailable")

        progress.request_count += 1
        text_result = await runtime.llm.chat(
            [Message(role="user", content="只回复 ORIA_LIVE_OK")],
            ctx,
            options=ChatOptions(max_output_tokens=64, timeout_seconds=120.0),
        )
        if text_result.request_id is None or "ORIA_LIVE_OK" not in text_result.text:
            raise RuntimeError("text smoke failed")
        raw_model = (text_result.internal_raw_response() or {}).get("model")
        if raw_model != resolved.llm.model:
            raise RuntimeError("provider response model mismatch")
        request_ids.append(text_result.request_id)
        usages.append(text_result.usage)
        checks["text"] = True

        stream_text: list[str] = []
        stream_usage: Usage | None = None
        stream_request_id: str | None = None
        stream_done = False
        progress.request_count += 1
        async for event in runtime.llm.chat_stream(
            [Message(role="user", content="只回复 ORIA_STREAM_OK")],
            ctx,
            options=ChatOptions(max_output_tokens=64, timeout_seconds=120.0),
        ):
            if isinstance(event, TextDelta):
                stream_text.append(event.text)
            elif isinstance(event, UsageDelta):
                stream_usage = event.usage
                stream_request_id = event.request_id
            elif isinstance(event, Done):
                stream_done = True
                stream_request_id = event.request_id or stream_request_id
            elif isinstance(event, ProviderError):
                raise RuntimeError(f"stream smoke failed: {event.code}")
        if (
            not stream_done
            or stream_usage is None
            or stream_request_id is None
            or "ORIA_STREAM_OK" not in "".join(stream_text)
        ):
            raise RuntimeError("stream smoke is incomplete")
        request_ids.append(stream_request_id)
        usages.append(stream_usage)
        checks["stream"] = True

        tool = ToolSpec(
            name="oria_health_probe",
            schema_version=1,
            description="Emit a no-argument provider health probe.",
            json_schema={
                "type": "object",
                "properties": {},
            },
        )
        progress.request_count += 1
        tool_result = await runtime.llm.chat(
            [Message(role="user", content="请调用一次 oria_health_probe 工具")],
            ctx,
            tools=[tool],
            options=ChatOptions(
                max_output_tokens=512,
                tool_choice="required",
                timeout_seconds=120.0,
            ),
        )
        if (
            tool_result.request_id is None
            or len(tool_result.tool_calls) != 1
            or tool_result.tool_calls[0].name != tool.name
            or tool_result.tool_calls[0].args != {}
        ):
            raise RuntimeError("tool-call smoke failed")
        request_ids.append(tool_result.request_id)
        usages.append(tool_result.usage)
        checks["tool_call"] = True

        invalid_profile = resolved.llm.model_copy(
            update={"api_key": SecretStr("oria-deliberately-invalid-live-key")}
        )
        async with httpx.AsyncClient(
            base_url=resolved.llm.base_url or "https://api.deepseek.com",
            timeout=httpx.Timeout(30.0, connect=10.0),
        ) as client:
            invalid_provider = OpenAICompatProvider(invalid_profile, client)
            try:
                progress.request_count += 1
                await invalid_provider.chat(
                    [Message(role="user", content="authentication mapping probe")],
                    ctx,
                    options=ChatOptions(max_output_tokens=1, timeout_seconds=30.0),
                )
            except AuthenticationError:
                checks["authentication_error_mapping"] = True
            else:
                raise RuntimeError("authentication error mapping smoke failed")

    if len(request_ids) != 3 or len(set(request_ids)) != 3 or not all(checks.values()):
        raise RuntimeError("provider live evidence is incomplete")
    return {
        "schema_version": 1,
        "task_id": "V0.2-T06",
        "target_id": args.target,
        "status": "passed",
        "provider": resolved.llm.provider,
        "model": resolved.llm.model,
        "api_dialect": resolved.llm.api_dialect,
        "config_fingerprint": resolved.config_fingerprint,
        "request_count": 4,
        "successful_request_count": 3,
        "request_ids": request_ids,
        "usage": {
            "input_tokens": sum(item.input_tokens for item in usages),
            "output_tokens": sum(item.output_tokens for item in usages),
            "reasoning_tokens": sum(item.reasoning_tokens or 0 for item in usages),
            "cache_read_tokens": sum(item.cache_read_tokens or 0 for item in usages),
        },
        "checks": checks,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
    }


def main() -> int:
    args = _arguments()
    progress = LiveProgress()
    try:
        card = asyncio.run(_run(args, progress))
    except Exception as exc:
        request_count = progress.request_count
        card = {
            "schema_version": 1,
            "task_id": "V0.2-T06",
            "target_id": args.target,
            "status": "blocked" if request_count == 0 else "failed",
            "request_count": request_count,
            "successful_request_count": len(progress.request_ids),
            "provider": progress.provider,
            "model": progress.model,
            "api_dialect": progress.api_dialect,
            "config_fingerprint": progress.config_fingerprint,
            "request_ids": progress.request_ids,
            "usage": {
                "input_tokens": sum(item.input_tokens for item in progress.usages),
                "output_tokens": sum(item.output_tokens for item in progress.usages),
                "reasoning_tokens": sum(item.reasoning_tokens or 0 for item in progress.usages),
                "cache_read_tokens": sum(item.cache_read_tokens or 0 for item in progress.usages),
            },
            "checks": progress.checks,
            "reason": type(exc).__name__,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": card["status"],
                "target_id": card["target_id"],
                "request_count": card["request_count"],
            },
            sort_keys=True,
        )
    )
    return 0 if card["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
