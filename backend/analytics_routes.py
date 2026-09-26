"""First-party usage collection and an administrator-only aggregate report."""

import json
import os
import re
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from backend.usage_analytics import EVENTS, UsageAnalytics


NO_STORE = {"Cache-Control": "no-store"}
MAX_BODY_BYTES = 256


def _reject(status: int, message: str) -> None:
    raise HTTPException(status_code=status, detail=message, headers=NO_STORE)


def _same_origin(request: Request) -> None:
    # CORS alone does not prevent a cross-origin form or fetch from writing.
    if request.headers.get("sec-fetch-site") == "cross-site":
        _reject(403, "Cross-origin analytics requests are not allowed.")
    origin = request.headers.get("origin")
    if origin is not None:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            _reject(403, "Invalid request origin.")
        if (parsed.scheme, parsed.netloc.casefold()) != (
            request.url.scheme, request.url.netloc.casefold()
        ):
            _reject(403, "Cross-origin analytics requests are not allowed.")


def create_analytics_router(analytics: UsageAnalytics) -> APIRouter:
    router = APIRouter(prefix="/api/analytics", tags=["usage analytics"])

    @router.post("/events")
    async def record_event(request: Request):
        _same_origin(request)
        if request.query_params:
            _reject(422, "Analytics events do not accept query parameters.")
        if (
            not analytics.enabled
            or request.headers.get("dnt") == "1"
            or request.headers.get("sec-gpc") == "1"
        ):
            return Response(status_code=204, headers=NO_STORE)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            _reject(415, "Send an application/json event.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                _reject(413, "Analytics event is too large.")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            _reject(422, "Expected a usage event.")
        if (
            not isinstance(payload, dict)
            or set(payload) != {"event", "visitor_id"}
            or not isinstance(payload["event"], str)
            or payload["event"] not in EVENTS
            or not isinstance(payload["visitor_id"], str)
            or re.fullmatch(r"[0-9a-f]{32}", payload["visitor_id"]) is None
        ):
            _reject(422, "Expected a supported event type and a random browser identifier only.")

        # Storage in the embedding browser keeps IDs stable even where third-party
        # cookies are blocked. The service stores only a hash of this random ID.
        accepted = analytics.record(payload["visitor_id"], payload["event"])
        return JSONResponse({"accepted": accepted}, status_code=202, headers=NO_STORE)

    @router.get("/summary")
    async def summary(request: Request, days: int = Query(default=30, ge=1, le=365)):
        _same_origin(request)
        if set(request.query_params) - {"days"}:
            _reject(422, "Only the days query parameter is supported.")
        expected = os.environ.get("ANALYTICS_ADMIN_TOKEN", "")
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        if not expected:
            _reject(403, "Set ANALYTICS_ADMIN_TOKEN on the server to enable dashboard access.")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        ):
            _reject(403, "Invalid analytics access token.")
        try:
            data = await analytics.summary(days)
        except Exception:
            # Database exceptions may include DSNs. Keep them out of responses.
            _reject(503, "Usage analytics are temporarily unavailable. Please try again.")
        return JSONResponse(data, headers=NO_STORE)

    return router
