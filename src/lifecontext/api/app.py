"""FastAPI factory and fail-closed local HTTP guards."""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import stat
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, get_args
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from lifecontext import __version__
from lifecontext.api.models import ErrorResponse, RetainRequest
from lifecontext.api.security import local_token, protected_path

DOCUMENT_MAX_BYTES = 32 * 1024 * 1024
from lifecontext.application import ApplicationError, JournalApplication
from lifecontext.config import JournalLimits
from lifecontext.custody import CustodyError
from lifecontext.hindsight_adapter import HindsightAdapterError
from lifecontext.paths import LifeContextPaths
from lifecontext.relationships import RelationshipError
from lifecontext.vault import VaultError


class PublicAPIError(Exception):
    """Internal carrier for an HTTP status and a stable public error code."""

    def __init__(self, status_code: int, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


def _claim_home(paths: LifeContextPaths) -> int:
    """Hold a private, no-follow process claim on one LifeContext HOME."""
    try:
        import fcntl
    except ImportError:
        raise PublicAPIError(503, "internal_error") from None

    try:
        paths.ensure_private_home()
        lock_path = paths.service_lock_path
        before = lock_path.lstat()
    except FileNotFoundError:
        before = None
    except OSError:
        raise PublicAPIError(403, "unsafe_path") from None
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise PublicAPIError(403, "unsafe_path")

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise PublicAPIError(500, "internal_error")
    flags |= nofollow
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError:
        raise PublicAPIError(403, "unsafe_path") from None

    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (os.name == "posix" and opened.st_uid != os.geteuid())
            or before is not None
            and (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise PublicAPIError(403, "unsafe_path")
        if before is None and os.name == "posix":
            os.fchmod(descriptor, 0o600)
            opened = os.fstat(descriptor)
        if os.name == "posix" and stat.S_IMODE(opened.st_mode) != 0o600:
            raise PublicAPIError(403, "unsafe_permissions")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublicAPIError(409, "conflict") from None
        except OSError:
            raise PublicAPIError(503, "internal_error") from None
        return descriptor
    except PublicAPIError:
        os.close(descriptor)
        raise
    except OSError:
        os.close(descriptor)
        raise PublicAPIError(403, "unsafe_path") from None


def _release_home_claim(descriptor: int) -> None:
    try:
        import fcntl
    except ImportError:
        os.close(descriptor)
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _is_loopback_host(value: str) -> bool:
    if not value or len(value) > 255 or any(char.isspace() for char in value) or "," in value:
        return False
    try:
        parsed = urlsplit("//" + value)
        port = parsed.port
    except (ValueError, TypeError):
        return False
    safe_authority = (
        parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.username is None
        and parsed.password is None
        and (port is None or 1 <= port <= 65535)
    )
    if not safe_authority:
        return False
    hostname = (parsed.hostname or "").casefold()
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class LocalRequestGuard:
    """Bound incoming and outgoing HTTP bytes and enforce the local-only policy."""

    def __init__(self, app: Any, *, limits: JournalLimits, app_state: Any = None):
        self.app = app
        self.limits = limits
        self.app_state = app_state

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "websocket":
            # No route speaks WebSocket; refuse before the handshake is accepted.
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = secrets.token_hex(16)
        application = getattr(self.app_state, "application", None)
        limits = getattr(application, "limits", self.limits)
        # A JSON string can expand to six bytes per source byte when escaped.
        path = scope.get("path", "")
        binary_upload = path == "/v1/documents" and scope.get("method") == "POST"
        binary_download = path.startswith("/v1/documents/") and path.endswith("/original")
        if binary_upload:
            max_request_bytes = DOCUMENT_MAX_BYTES
        elif scope.get("path") == "/v1/imports":
            max_request_bytes = limits.max_file_bytes
        else:
            max_request_bytes = min(limits.max_file_bytes, limits.max_text_bytes * 6 + 65_536)
        max_response_bytes = (
            1_048_576
            if path == "/openapi.json"
            else limits.max_file_bytes
            if path == "/v1/exports"
            else limits.max_response_bytes
        )
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        headers: dict[bytes, list[bytes]] = {}
        for name, value in scope.get("headers", ()):
            headers.setdefault(name.lower(), []).append(value)
        hosts = headers.get(b"host", ())
        if len(hosts) != 1 or not _is_loopback_host(hosts[0].decode("latin-1")):
            await self._send_error(send, request_id, 403, "host_not_allowed", False)
            return
        if headers.get(b"origin"):
            await self._send_error(send, request_id, 403, "origin_not_allowed", False)
            return

        if protected_path(path):
            token = getattr(self.app_state, "api_token", None)
            authorization = headers.get(b"authorization", ())
            expected = ("Bearer " + token).encode("ascii") if isinstance(token, str) else b""
            if not expected or len(authorization) != 1 or not secrets.compare_digest(authorization[0], expected):
                await self._send_error(send, request_id, 401, "unauthorized", False)
                return

        lengths = headers.get(b"content-length", ())
        if lengths:
            try:
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                    raise ValueError
                if int(lengths[0]) > max_request_bytes:
                    raise OverflowError
            except OverflowError:
                await self._send_error(send, request_id, 413, "body_too_large", False)
                return
            except ValueError:
                await self._send_error(send, request_id, 400, "invalid_request", False)
                return

        if binary_upload or binary_download:
            request_size = 0

            async def bounded_receive():
                nonlocal request_size
                message = await receive()
                if message["type"] == "http.request":
                    request_size += len(message.get("body", b""))
                    if request_size > max_request_bytes:
                        raise PublicAPIError(413, "body_too_large")
                return message

            async def streaming_send(message):
                if message["type"] == "http.response.start":
                    message = dict(message)
                    message["headers"] = [*message.get("headers", ()), (b"x-request-id", request_id.encode("ascii"))]
                await send(message)

            await self.app(scope, bounded_receive, streaming_send)
            return

        messages: list[dict[str, Any]] = []
        request_size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                request_size += len(message.get("body", b""))
                if request_size > max_request_bytes:
                    await self._send_error(send, request_id, 413, "body_too_large", False)
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                return

        async def replay_receive():
            if messages:
                return messages.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        output: list[dict[str, Any]] = []
        response_size = 0
        response_exceeded = False

        async def capture_send(message):
            nonlocal response_size, response_exceeded
            if message["type"] == "http.response.body":
                response_size += len(message.get("body", b""))
                if response_size > max_response_bytes:
                    response_exceeded = True
                    output.clear()
                elif not response_exceeded:
                    output.append(message)
            elif not response_exceeded:
                output.append(message)

        await self.app(scope, replay_receive, capture_send)
        if response_size > max_response_bytes:
            await self._send_error(send, request_id, 500, "response_too_large", False)
            return
        for message in output:
            if message["type"] == "http.response.start":
                message = dict(message)
                response_headers = list(message.get("headers", ()))
                response_headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = response_headers
            await send(message)

    @staticmethod
    async def _send_error(send, request_id: str, status: int, code: str, retryable: bool) -> None:
        error = ErrorResponse(
            request_id=request_id,
            error_code=code,
            retryable=retryable,
        ).model_dump(mode="json")
        body = json.dumps(error, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"x-request-id", request_id.encode("ascii")),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


def _error_details(code: str) -> tuple[int, bool, str]:
    """Return status, retryability, and allowlisted error code."""
    if code in {"invalid_request", "invalid_source_text"}:
        return 422, False, code
    if code in {"conflict", "source_conflict"}:
        return 409, False, code
    if code in {"source_not_found", "evidence_unavailable"}:
        return 404, False, code
    if code == "body_too_large":
        return 413, False, code
    if code in {"host_not_allowed", "origin_not_allowed", "unsafe_path", "unsafe_permissions"}:
        return 403, False, code
    if code in {
        "provider_unavailable", "retain_failed", "recall_failed", "reflect_failed",
        "operation_status_unavailable", "curation_unavailable", "transfer_unavailable",
        "relationship_unavailable", "vault_unavailable", "catalog_busy",
    }:
        return 503, code in {"provider_unavailable", "retain_failed", "recall_failed", "reflect_failed", "catalog_busy"}, code
    if code in {"response_too_large", "invalid_provider_response", "unattributed_evidence"}:
        return 500 if code == "response_too_large" else 502, False, code
    if code == "result_limit_exceeded":
        return 422, False, code
    known = set(get_args(ErrorResponse.model_fields["error_code"].annotation))
    return (500, False, code) if code in known else (500, False, "internal_error")


def create_app(
    application: JournalApplication | None = None,
    *,
    application_factory: Callable[[], JournalApplication] | None = None,
) -> FastAPI:
    """Build an injectable ASGI app; one factory or one existing application owns it."""
    if application is not None and application_factory is not None:
        raise ValueError("provide application or application_factory")
    if application is None and application_factory is None:
        raise ValueError("application is required")

    owned = application

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal owned
        paths = getattr(application, "paths", None) or LifeContextPaths.current()
        claim_fd = _claim_home(paths)
        try:
            app.state.api_token = local_token(paths, create=True)
            if owned is None:
                owned = application_factory()
                app.state.application = owned
            owned_paths = getattr(owned, "paths", paths)
            if getattr(owned_paths, "lifeweave_home", paths.lifeweave_home) != paths.lifeweave_home:
                raise PublicAPIError(403, "unsafe_path")
            recover = getattr(owned, "recover_startup", None)
            if callable(recover):
                await recover()
            yield
        finally:
            try:
                if owned is not None:
                    owned.close()
            finally:
                _release_home_claim(claim_fd)

    limits = getattr(application, "limits", JournalLimits())
    app = FastAPI(
        title="LifeContext Local API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.application = application
    app.add_middleware(LocalRequestGuard, limits=limits, app_state=app.state)

    def openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        retain_schema = RetainRequest.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components.update(retain_schema.pop("$defs", {}))
        components["RetainRequest"] = retain_schema
        app.openapi_schema = schema
        return schema

    app.openapi = openapi

    @app.exception_handler(PublicAPIError)
    async def public_error_handler(request: Request, exc: PublicAPIError):
        return _json_error(request, exc.status_code, exc.code, exc.retryable)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return _json_error(request, 422, "invalid_request", False)

    @app.exception_handler(ApplicationError)
    async def application_error_handler(request: Request, exc: ApplicationError):
        status, retryable, code = _error_details(exc.code)
        return _json_error(request, status, code, retryable)

    @app.exception_handler(HindsightAdapterError)
    async def provider_error_handler(request: Request, exc: HindsightAdapterError):
        status, retryable, code = _error_details(exc.code)
        if code == "internal_error":
            status, code = 503, "provider_unavailable"
        return _json_error(request, status, code, retryable)

    async def storage_error_handler(request: Request, exc: Exception):
        status, retryable, code = _error_details(getattr(exc, "code", "internal_error"))
        return _json_error(request, status, code, retryable)

    async def custody_error_handler(request: Request, exc: CustodyError):
        return _json_error(request, 404, "evidence_unavailable", False)

    app.add_exception_handler(CustodyError, custody_error_handler)
    for error_type in (VaultError, RelationshipError):
        app.add_exception_handler(error_type, storage_error_handler)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        code = "invalid_request" if exc.status_code < 500 else "internal_error"
        return _json_error(request, exc.status_code, code, False)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        return _json_error(request, 500, "internal_error", False)

    from lifecontext.api.routes import router

    app.include_router(router)
    return app


def _json_error(request: Request, status: int, code: str, retryable: bool) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or secrets.token_hex(16)
    model = ErrorResponse(request_id=request_id, error_code=code, retryable=retryable)
    return JSONResponse(status_code=status, content=model.model_dump(mode="json"))


def run(
    application: JournalApplication,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Run the supported Uvicorn server after checking the bind address."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("bind address must be a loopback IP literal") from None
    if not address.is_loopback:
        raise ValueError("bind address must be a loopback IP literal")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid port")
    import uvicorn

    uvicorn.run(create_app(application), host=str(address), port=port, access_log=False)
