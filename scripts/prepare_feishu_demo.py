"""Discover one approved demo chat/operator without printing private identifiers."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote

import httpx

from oria.chat.feishu_demo import FeishuDemoSettings, require_feishu_demo_target


async def prepare(credentials: Path, output: Path, chat_name: str, operator_name: str) -> None:
    require_feishu_demo_target(os.environ)
    if output.exists() or output.is_symlink():
        raise ValueError("private scope output already exists")
    if credentials.is_symlink() or credentials.stat().st_mode & 0o077:
        raise ValueError("credentials must be private (0600)")
    values = json.loads(credentials.read_bytes())
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        print("Discovery phase: application authentication", flush=True)
        response = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": values["app_id"], "app_secret": values["app_secret"]},
        )
        response.raise_for_status()
        authentication = response.json()
        if authentication.get("code") != 0:
            raise ValueError("authentication rejected")
        print(f"Authentication HTTP {response.status_code}, code 0", flush=True)
        headers = {"Authorization": "Bearer " + authentication["tenant_access_token"]}

        async def get(path: str, **params: str | int) -> dict:
            print("Discovery phase: " + path.split("/")[0], flush=True)
            response = await client.get(
                "https://open.feishu.cn/open-apis/" + path, headers=headers, params=params
            )
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 0:
                raise ValueError("discovery rejected")
            print(f"Discovery HTTP {response.status_code}, code 0", flush=True)
            return result

        bot = (await get("bot/v3/info"))["bot"]
        if bot.get("app_name") != "OriaTest" or bot.get("activate_status") != 2:
            raise ValueError("only the active, independent OriaTest application is allowed")
        chats = (await get("im/v1/chats", page_size=100))["data"]
        matches = [i for i in chats.get("items", []) if i.get("name") == chat_name]
        if chats.get("has_more") or len(matches) != 1:
            raise ValueError("demo chat must match uniquely in a complete first page")
        chat_id = matches[0]["chat_id"]
        members = (
            await get(
                "im/v1/chats/" + quote(chat_id, safe="") + "/members",
                member_id_type="open_id",
                page_size=100,
            )
        )["data"]
        matches = [
            i
            for i in members.get("items", [])
            if i.get("name") == operator_name and i.get("member_id_type") == "open_id"
        ]
        if (
            members.get("has_more")
            or members.get("trigger_security_conf_limit")
            or len(matches) != 1
            or not matches[0].get("tenant_key")
        ):
            raise ValueError("operator must match uniquely in a complete first page")
        settings = FeishuDemoSettings(
            **{
                key: values[key]
                for key in (
                    "app_id",
                    "app_secret",
                    "encrypt_key",
                    "verification_token",
                )
            },
            tenant_key=matches[0]["tenant_key"],
            operator_open_id=matches[0]["member_id"],
            chat_id=chat_id,
            chat_name=chat_name,
            app_name="OriaTest",
        )
        document = settings.model_dump(mode="json")
        document.update(
            {
                key: values[key]
                for key in (
                    "app_secret",
                    "encrypt_key",
                    "verification_token",
                )
            }
        )
        # Exclusive creation avoids replacing any existing credential/configuration.
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(document, file, ensure_ascii=False, indent=2)
            file.write("\n")
        print(
            "Four read-only discovery calls passed; unique OriaTest/chat/operator. "
            "Private scope saved (0600)."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chat-name", required=True)
    parser.add_argument("--operator-name", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(prepare(args.credentials, args.output, args.chat_name, args.operator_name))
    except Exception as error:
        raise SystemExit(
            "Discovery failed ("
            + type(error).__name__
            + "); no private response values are printed."
        ) from None
