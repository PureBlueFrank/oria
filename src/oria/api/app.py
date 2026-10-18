"""FastAPI v1 foundation with fail-closed authentication and ingress handoff."""

# ruff: noqa: RUF001

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field
from starlette.exceptions import HTTPException

from oria.api.approvals import ApprovalAPI, ApprovalDecisionBody
from oria.api.identity import AuthenticationError, IdentityBinding, JWTAuthenticator
from oria.core.approvals import ApprovalConflict
from oria.core.types import InboundRequest, ValueModel
from oria.ingress.feishu import FeishuIngress, VerifiedFeishuEvent
from oria.ingress.local import IngressVerificationError
from oria.ingress.receipts import IngressConflict, IngressUnavailable
from oria.presentation.im import IMViewModel, render_card


class APIError(ValueModel):
    code: str
    message: str
    request_id: str
    retryable: bool = False
    details: dict[str, str] = Field(default_factory=dict)


class FeishuReceiver(Protocol):
    """Atomically accept and deduplicate in the platform DB.

    Replay/source/action keys and the submitted business request share a transaction.
    A duplicate returns the original acceptance without executing a second action.
    Approval action_ref must resolve server-side and reauthorize the current actor.
    Never retain raw callback bodies, headers or bearer tokens.
    """

    async def accept(self, event: VerifiedFeishuEvent) -> IMViewModel | None: ...


@dataclass(frozen=True, slots=True)
class APIServices:
    """Services sharing one application-owned runtime and database lifecycle."""

    approvals: ApprovalAPI
    receiver: FeishuReceiver | None = None


def create_app(
    *,
    authenticator: JWTAuthenticator | None = None,
    local_identity: IdentityBinding | None = None,
    feishu: FeishuIngress | None = None,
    receiver: FeishuReceiver | None = None,
    approvals: ApprovalAPI | None = None,
    close: Callable[[], Awaitable[None]] | None = None,
    approval_factory: Callable[[], AbstractAsyncContextManager[ApprovalAPI]] | None = None,
    services_factory: Callable[[], AbstractAsyncContextManager[APIServices]] | None = None,
) -> FastAPI:
    if (authenticator is None) == (local_identity is None):
        raise ValueError("select exactly one explicit authentication profile")
    if local_identity is not None and feishu is not None:
        raise ValueError("Feishu requires the authenticated server profile")

    if approvals is not None and approval_factory is not None:
        raise ValueError("select one approval service source")
    if services_factory is not None and any(
        service is not None for service in (approvals, approval_factory, receiver)
    ):
        raise ValueError("application services cannot be mixed with injected services")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal approvals, receiver
        del app
        try:
            async with AsyncExitStack() as stack:
                if approval_factory is not None:
                    approvals = await stack.enter_async_context(approval_factory())
                if services_factory is not None:
                    services = await stack.enter_async_context(services_factory())
                    approvals, receiver = services.approvals, services.receiver
                yield
        finally:
            if approval_factory is not None or services_factory is not None:
                approvals = None
            if services_factory is not None:
                receiver = None
            if close is not None:
                await close()

    app = FastAPI(
        title="Oria API",
        version="1.0.0",
        lifespan=lifespan,
        responses={code: {"model": APIError} for code in (401, 404, 409, 413, 422, 500, 503)},
    )

    def error(request: Request, status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content=APIError(
                code=code,
                message=message,
                request_id=request.state.request_id,
                retryable=status == 503,
            ).model_dump(),
            headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
        )

    @app.middleware("http")
    async def request_boundary(request: Request, call_next: Any) -> Any:
        request.state.request_id = uuid.uuid4().hex
        # Request IDs and proxy identity headers are never accepted as auth facts.
        try:
            response = await call_next(request)
        except Exception:
            # Never serialize exception strings: providers/validation may embed secrets.
            response = error(request, 500, "internal_error", "服务暂时不可用，请联系管理员。")
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AuthenticationError)
    async def authentication_error(request: Request, exc: AuthenticationError) -> JSONResponse:
        return error(request, 401, "unauthenticated", "请重新登录或联系管理员开通访问权限。")

    @app.exception_handler(IngressVerificationError)
    async def ingress_error(request: Request, exc: IngressVerificationError) -> JSONResponse:
        return error(request, 401, "invalid_callback", "回调验证失败，请检查应用配置。")

    @app.exception_handler(PermissionError)
    async def permission_error(request: Request, exc: PermissionError) -> JSONResponse:
        return error(request, 404, "not_found", "资源不存在或不可访问。")

    @app.exception_handler(ApprovalConflict)
    async def approval_conflict(request: Request, exc: ApprovalConflict) -> JSONResponse:
        return error(request, 409, "approval_conflict", "审批已变化或不可操作，请刷新详情。")

    @app.exception_handler(IngressConflict)
    async def ingress_conflict(request: Request, exc: IngressConflict) -> JSONResponse:
        return error(request, 409, "ingress_conflict", "重复回调内容不一致，请联系管理员。")

    @app.exception_handler(IngressUnavailable)
    async def ingress_unavailable(request: Request, exc: IngressUnavailable) -> JSONResponse:
        return error(
            request, 503, "unavailable", "暂时无法确认处理结果，请稍后重试或查询当前状态。"
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error(request, 422, "invalid_request", "请求格式不正确，请检查参数。")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = "unavailable" if exc.status_code == 503 else "invalid_request"
        if exc.status_code == 404:
            code = "not_found"
        return error(request, exc.status_code, code, "请求无法处理，请检查参数或稍后重试。")

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "api_version": "v1", "job_service": "not_configured"}

    async def authenticate(request: Request) -> IdentityBinding:
        if local_identity is not None:
            if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
                raise AuthenticationError("local profile requires loopback")
            binding = local_identity
        else:
            assert authenticator is not None
            binding = await authenticator.authenticate(request.headers.get("authorization"))
        return binding

    @app.get("/v1/me")
    async def me(request: Request) -> dict[str, Any]:
        binding = await authenticate(request)
        return {"actor": binding.actor.model_dump(), "executor": binding.executor.model_dump()}

    @app.get("/v1/approvals/{approval_id}")
    async def approval_detail(
        approval_id: str,
        request: Request,
        page: int = Query(default=1, ge=1),
    ) -> dict[str, Any]:
        identity = await authenticate(request)
        if approvals is None:
            raise HTTPException(503)
        return await approvals.detail(approval_id, identity, request.state.request_id, page=page)

    @app.post("/v1/approvals/{approval_id}/decision")
    async def approval_decide(
        approval_id: str,
        body: ApprovalDecisionBody,
        request: Request,
    ) -> dict[str, str]:
        identity = await authenticate(request)
        if approvals is None:
            raise HTTPException(503)
        return await approvals.decide(approval_id, body, identity, request.state.request_id)

    @app.post("/v1/ingress/feishu/events")
    async def feishu_event(request: Request) -> dict[str, Any]:
        if feishu is None:
            raise HTTPException(503)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1_048_576:
                raise HTTPException(413)
        result = feishu.verify(
            InboundRequest(
                raw_body=bytes(body),
                headers=dict(request.headers),
                received_at=datetime.now(UTC),
                request_id=request.state.request_id,
            )
        )
        if isinstance(result, str):
            return {"challenge": result}
        if receiver is None:
            # No acknowledgement until an actual durable receiver is installed.
            raise HTTPException(503)
        view = await receiver.accept(result)
        if result.kind == "action" and view is not None:
            return {"card": {"type": "raw", "data": render_card(view, shared_update=True)}}
        return {} if result.kind == "action" else {"code": 0}

    return app
