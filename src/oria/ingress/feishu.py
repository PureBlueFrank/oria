"""Feishu v2 verification and normalization; never logs or persists raw payloads."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from typing import Any, Literal

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import Field, SecretStr

from oria.api.identity import IdentityBinding, IdentityDirectory
from oria.core.types import InboundMessage, InboundRequest, IngressContext, ValueModel
from oria.ingress.local import IngressVerificationError


class FeishuSettings(ValueModel):
    app_id: str = Field(min_length=1)
    encrypt_key: SecretStr = Field(min_length=1)
    verification_token: SecretStr = Field(min_length=1)
    max_age_seconds: int = Field(default=300, ge=1, le=300)


class CardAction(ValueModel):
    version: Literal[1] = 1
    # Opaque reference resolved against server-owned approval/resource/version/wait.
    # Neither this reference nor a valid platform signature grants approval permission.
    action_ref: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class VerifiedFeishuEvent(ValueModel):
    kind: Literal["message", "action"]
    binding: IdentityBinding
    message: InboundMessage
    action: CardAction | None = None
    replay_key: str
    expires_at: float


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def _text(value: Any, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError("invalid text")
    return value


def _digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


class FeishuIngress:
    name = "feishu"

    def __init__(
        self, settings: FeishuSettings, directory: Callable[[], IdentityDirectory]
    ) -> None:
        self.settings = settings
        self.directory = directory

    def _decode(self, raw: bytes) -> dict[str, Any]:
        outer = _object(json.loads(raw))
        if "encrypt" not in outer:
            return outer
        encrypted = base64.b64decode(_text(outer["encrypt"], limit=1_048_576), validate=True)
        key = hashlib.sha256(self.settings.encrypt_key.get_secret_value().encode()).digest()
        decryptor = Cipher(algorithms.AES(key), modes.CBC(encrypted[:16])).decryptor()
        padded = decryptor.update(encrypted[16:]) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return _object(json.loads(unpadder.update(padded) + unpadder.finalize()))

    def verify(self, request: InboundRequest) -> VerifiedFeishuEvent | str:
        """Return a challenge or a verified event for an atomic, durable receiver.

        The receiver must atomically deduplicate replay_key and message.dedupe_key
        with business acceptance before returning success. Verification alone must
        never acknowledge a message. T03 owns that database transaction.
        """
        try:
            headers = {key.lower(): value for key, value in request.headers.items()}
            # URL verification uses the verification token (official challenge flow).
            # If signature headers are present, even challenges must pass verification.
            signed = any(
                key in headers
                for key in ("x-lark-request-timestamp", "x-lark-request-nonce", "x-lark-signature")
            )
            stamp = 0
            nonce = ""
            if signed:
                timestamp = _text(headers.get("x-lark-request-timestamp"), limit=20)
                nonce = _text(headers.get("x-lark-request-nonce"))
                signature = _text(headers.get("x-lark-signature"), limit=64)
                stamp = int(timestamp)
                now = request.received_at.timestamp()
                if abs(now - stamp) > self.settings.max_age_seconds:
                    raise ValueError("stale request")
                prefix = timestamp + nonce + self.settings.encrypt_key.get_secret_value()
                expected = hashlib.sha256(prefix.encode() + request.raw_body).hexdigest()
                if not hmac.compare_digest(expected, signature):
                    raise ValueError("invalid signature")
            document = self._decode(request.raw_body)
            header = _object(document.get("header", {}))
            token = (
                document.get("token")
                if document.get("type") == "url_verification"
                else header.get("token")
            )
            if not hmac.compare_digest(
                _text(token), self.settings.verification_token.get_secret_value()
            ):
                raise ValueError("invalid verification token")
            if document.get("type") == "url_verification":
                return _text(document.get("challenge"))
            if not signed or document.get("schema") != "2.0":
                raise ValueError("signed v2 event required")
            if header.get("app_id") != self.settings.app_id:
                raise ValueError("wrong application")
            _text(header.get("event_id"))
            tenant = _text(header.get("tenant_key"))
            event = _object(document.get("event"))
            action = None
            if header.get("event_type") == "im.message.receive_v1":
                sender = _object(event.get("sender"))
                if sender.get("sender_type") != "user" or sender.get("tenant_key") != tenant:
                    raise ValueError("untrusted sender")
                subject = _text(_object(sender.get("sender_id")).get("open_id"))
                message = _object(event.get("message"))
                if message.get("message_type") != "text":
                    raise ValueError("unsupported message type")
                content = _object(json.loads(_text(message.get("content"), limit=32_768)))
                text = _text(content.get("text"), limit=16_384)
                source_id = _text(message.get("message_id"))
                chat = _text(message.get("chat_id"))
                kind: Literal["message", "action"] = "message"
            elif header.get("event_type") == "card.action.trigger":
                operator = _object(event.get("operator"))
                if operator.get("tenant_key") != tenant:
                    raise ValueError("untrusted operator")
                subject = _text(operator.get("open_id"))
                action = CardAction.model_validate(_object(event.get("action")).get("value"))
                context = _object(event.get("context"))
                chat = _text(context.get("open_chat_id"))
                source_id = _text(context.get("open_message_id"))
                text, kind = "", "action"
            else:
                raise ValueError("unsupported event type")
            issuer = f"feishu:{self.settings.app_id}:{tenant}"
            binding = self.directory().resolve(issuer, subject)
            dedupe = _digest(
                issuer,
                kind,
                source_id,
                subject if action else "",
                action.action_ref if action else "",
            )
            return VerifiedFeishuEvent(
                kind=kind,
                binding=binding,
                message=InboundMessage(
                    source="feishu",
                    source_message_id=source_id,
                    mapped_tenant_id=binding.actor.tenant_id,
                    mapped_subject_id=binding.actor.subject_id,
                    sender_ref=subject,
                    target_ref=chat,
                    text=text,
                    received_at=request.received_at,
                    verified=True,
                    dedupe_key=dedupe,
                ),
                action=action,
                replay_key=_digest(self.settings.app_id, str(stamp), nonce),
                expires_at=stamp + self.settings.max_age_seconds,
            )
        except (ValueError, TypeError, KeyError, PermissionError):
            raise IngressVerificationError("invalid Feishu callback") from None

    async def verify_and_normalize(
        self, request: InboundRequest, ingress_ctx: IngressContext
    ) -> InboundMessage:
        result = self.verify(request)
        if (
            isinstance(result, str)
            or result.kind != "message"
            or ingress_ctx.request_id != request.request_id
            or ingress_ctx.executor != result.binding.executor
        ):
            raise IngressVerificationError("invalid Feishu message context")
        return result.message
