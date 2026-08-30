"""Run a local-only, synthetic DeepSeek Responses tool-call diagnostic."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
from pydantic import SecretStr

from oria.eval import diagnose_deepseek_responses_tools


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/eval/deepseek-tool-diagnostic.json"),
    )
    parser.add_argument("--model", default="deepseek-v4-flash")
    return parser.parse_args()


async def _run(args: argparse.Namespace, secret: str) -> dict[str, object]:
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com",
        timeout=httpx.Timeout(60.0, connect=10.0),
    ) as client:
        card = await diagnose_deepseek_responses_tools(
            client=client,
            api_key=SecretStr(secret),
            model=args.model,
        )
    return card.model_dump(mode="json")


def main() -> int:
    args = _arguments()
    secret = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not secret:
        result: dict[str, object] = {
            "schema_version": 1,
            "target_id": "deepseek",
            "status": "blocked",
            "request_count": 0,
            "reason": "credential_missing",
        }
        exit_code = 2
    else:
        try:
            result = asyncio.run(_run(args, secret))
            exit_code = 0
        except Exception as exc:
            result = {
                "schema_version": 1,
                "target_id": "deepseek",
                "status": "failed",
                "request_count": None,
                "reason": type(exc).__name__,
            }
            exit_code = 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "request_count": result["request_count"],
                "conclusion": result.get("conclusion"),
            },
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
