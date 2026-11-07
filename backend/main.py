"""
FastAPI application for Blast Intelligence AI (local only).

Endpoints
---------
GET  /api/health      service, database and mapping status (no data)
GET  /api/ai/status   local Ollama status (no cloud fallback)
POST /api/chat        {"question": "...", "session_id": "optional"} -> grounded answer + evidence
POST /api/chat/clear  {"session_id": "..."} -> forget the in-memory session
POST /api/analytics/{section}  read-only dashboard data from the existing analytics functions
     sections: overview, drilling, rigs, sites, blasts, compare-blasts, trends, problems, anomalies,
     prediction-readiness. Body: {"date_from", "date_to", "rigs", "sites", "blasts", "blast_ids", "grain"}
     (filters travel in the body so identifier values never appear in access-log URLs).

Protections: binds to 127.0.0.1 by default, request-body size limit, per-client
rate limit on /api/chat, validation errors that never echo the input, generic
500 errors, CORS limited to local front-end origins, read-only hardened
DuckDB connection. Stop this server before running ingestion or typed_views.py
(they need write access to the database file).

Run
---
    python backend\\main.py
    # or: python -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field, StringConstraints  # noqa: E402

import anomaly_detection  # noqa: E402
import config  # noqa: E402
import duckdb  # noqa: E402
import predictions  # noqa: E402
from analytics import AnalyticsEngine, Filters, load_or_create_salt  # noqa: E402
from analytics_mapping import approved_views, resolve_mapping  # noqa: E402
from chat_service import ChatService, ChatSettings  # noqa: E402
from llm_service import OllamaClient  # noqa: E402
from logging_setup import safe_error, setup_logging  # noqa: E402
from report_io import ReportError, load_json_report  # noqa: E402
from safe_query import Deadline, open_hardened_connection  # noqa: E402

API_VERSION = "0.8.0"
FilterValue = Annotated[str, StringConstraints(min_length=1, max_length=64,
                                               pattern=r"^[A-Za-z0-9][A-Za-z0-9 _\-./#]{0,63}$")]

# section -> handler(engine, filters, request). Each calls an existing analytics function unchanged.
ANALYTICS_SECTIONS: dict[str, Callable[[AnalyticsEngine, Filters, "AnalyticsRequest"], dict[str, Any]]] = {
    "overview": lambda e, f, r: {"metrics": e.overview(f)},
    "drilling": lambda e, f, r: {"metrics": e.drilling_performance(f) + [e.completion_time_analysis(f)]},
    "rigs": lambda e, f, r: {"metrics": [e.rig_performance(f)]},
    "sites": lambda e, f, r: {"metrics": [e.site_comparison(f)]},
    "blasts": lambda e, f, r: {"metrics": [e.blast_analysis(f)]},
    "compare-blasts": lambda e, f, r: {"metrics": [e.compare_blasts(list(r.blast_ids), f)]},
    "trends": lambda e, f, r: {"metrics": [e.trends(f, r.grain)]},
    "problems": lambda e, f, r: {"metrics": [e.problem_frequency(f)]},
    "anomalies": lambda e, f, r: {"anomalies": anomaly_detection.detect_all(e, f)},
    "prediction-readiness": lambda e, f, r: {"readiness": predictions.assess_readiness(e, f)},
}
log = logging.getLogger("api")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=config.CHAT_MAX_QUESTION_CHARS)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")


class ClearRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")


class AnalyticsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date_from: date | None = None
    date_to: date | None = None
    rigs: list[FilterValue] = Field(default_factory=list, max_length=10)
    sites: list[FilterValue] = Field(default_factory=list, max_length=10)
    blasts: list[FilterValue] = Field(default_factory=list, max_length=10)
    blast_ids: list[FilterValue] = Field(default_factory=list, max_length=10)
    grain: Literal["month", "week"] = "month"


class RateLimiter:
    """Sliding one-minute window per client host (in memory)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            if len(self._hits) > 10_000:
                self._hits = defaultdict(deque, {k: v for k, v in self._hits.items() if v})
            return True


def create_app(*, db_path: Path | None = None, mapping_path: Path | None = None, llm: OllamaClient | None = None,
               settings: ChatSettings | None = None, salt: str | None = None,
               rate_limit_per_minute: int | None = None, max_body_bytes: int | None = None,
               analytics_rate_limit_per_minute: int | None = None,
               analytics_timeout: float | None = None) -> FastAPI:
    db_path = Path(db_path or config.DB_PATH)
    mapping_path = Path(mapping_path or config.ANALYTICS_MAPPING_PATH)
    max_body = max_body_bytes or config.CHAT_MAX_BODY_BYTES
    limiter = RateLimiter(rate_limit_per_minute or config.CHAT_RATE_LIMIT_PER_MINUTE)
    analytics_limiter = RateLimiter(analytics_rate_limit_per_minute or config.ANALYTICS_API_RATE_LIMIT_PER_MINUTE)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state: dict[str, Any] = {"db_error": None, "mapping_error": None, "con": None, "mapping": None}
        try:
            state["con"] = open_hardened_connection(db_path)
        except FileNotFoundError:
            state["db_error"] = "database not found - run ingestion and typed_views.py first"
        except Exception as exc:  # duckdb lock, corruption ...
            state["db_error"] = f"database unavailable ({safe_error(exc)}) - is another process writing to it?"
        views: dict[str, dict[str, str]] = {}
        if state["con"] is not None:
            views = approved_views(state["con"])
            try:
                state["mapping"] = resolve_mapping(load_json_report(mapping_path, what="Analytics mapping"), views)
            except (ReportError, ValueError) as exc:
                state["mapping_error"] = str(exc).split("\n")[0]
        client = llm
        if client is None:
            try:
                client = OllamaClient()
            except ValueError as exc:          # non-local URL configured -> refuse, never fall back
                state["llm_error"] = str(exc)
        app.state.svc = ChatService(state["con"], state["mapping"], client,
                                    salt=salt or load_or_create_salt(config.LABEL_SALT_PATH),
                                    approved_views=set(views), settings=settings)
        app.state.info = state
        app.state.llm = client
        try:
            yield
        finally:
            if state["con"] is not None:
                state["con"].close()

    app = FastAPI(title="Blast Intelligence AI", version=API_VERSION, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.add_middleware(CORSMiddleware, allow_origins=config.API_CORS_ORIGINS, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"], allow_credentials=False)

    @app.middleware("http")
    async def limits(request: Request, call_next):
        if request.method == "POST":
            length = request.headers.get("content-length")
            if length is None or not length.isdigit():
                return JSONResponse({"error": "length_required", "detail": "Content-Length header required"}, 411)
            if int(length) > max_body:
                return JSONResponse({"error": "payload_too_large", "detail": f"max {max_body} bytes"}, 413)
            client = request.client.host if request.client else "unknown"
            path = request.url.path
            if ((path.startswith("/api/analytics/") and not analytics_limiter.allow(client))
                    or (path == "/api/chat" and not limiter.allow(client))):
                # Drain the (size-checked) body first: replying with unread bytes in the socket can make
                # the OS reset the connection, so the client would never see the 429.
                await request.body()
                return JSONResponse({"error": "rate_limited", "detail": "too many requests - try again shortly"},
                                    429, headers={"Retry-After": "60"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Never echo the submitted input back (it may contain confidential text).
        problems = [{"field": ".".join(str(p) for p in e.get("loc", [])[1:]) or "body", "problem": e.get("type")}
                    for e in exc.errors()]
        return JSONResponse({"error": "invalid_request", "problems": problems}, 422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.error("unhandled error on %s (%s)", request.url.path, safe_error(exc))
        return JSONResponse({"error": "internal_error", "detail": "unexpected server error"}, 500)

    @app.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        info, svc = request.app.state.info, request.app.state.svc
        mapping = svc.mapping
        ok = info["con"] is not None and mapping is not None and bool(mapping.entities)
        return {
            "status": "ok" if ok else "degraded",
            "version": API_VERSION,
            "database": {"available": info["con"] is not None, "read_only": True, "error": info["db_error"]},
            "analytics_mapping": {"available": mapping is not None,
                                  "confirmed_entities": sorted(mapping.entities) if mapping else [],
                                  "error": info["mapping_error"]},
            "approved_views": len(svc.approved_views),
            "active_sessions": len(svc.sessions),
        }

    @app.get("/api/ai/status")
    def ai_status(request: Request) -> dict[str, Any]:
        client: OllamaClient | None = request.app.state.llm
        if client is None:
            return {"provider": "ollama", "reachable": False, "model_available": False, "cloud_fallback": False,
                    "local_only": True, "error": request.app.state.info.get("llm_error", "not configured"),
                    "chat_uses_llm": False}
        st = client.status()
        return {**st, "chat_uses_llm": request.app.state.svc.settings.use_llm,
                "fallback_when_unavailable": "deterministic template answer (local)"}

    @app.post("/api/chat")
    def chat(body: ChatRequest, request: Request) -> JSONResponse:
        try:
            return JSONResponse(request.app.state.svc.ask(body.question, body.session_id))
        except ValueError as exc:
            return JSONResponse({"error": "invalid_question", "detail": str(exc)}, 422)

    @app.post("/api/chat/clear")
    def clear(body: ClearRequest, request: Request) -> dict[str, Any]:
        return {"session_id": body.session_id, "cleared": request.app.state.svc.sessions.clear(body.session_id)}

    @app.post("/api/analytics/{section}")
    def analytics_section(section: str, body: AnalyticsRequest, request: Request) -> JSONResponse:
        handler = ANALYTICS_SECTIONS.get(section)
        if handler is None:
            return JSONResponse({"error": "unknown_section", "detail": "unknown analytics section",
                                 "sections": sorted(ANALYTICS_SECTIONS)}, 404)
        svc: ChatService = request.app.state.svc
        if svc.con is None or svc.mapping is None or not svc.mapping.entities:
            return JSONResponse({"error": "analytics_unavailable",
                                 "detail": "database or confirmed analytics mapping is not available"}, 503)
        if body.date_from and body.date_to and body.date_from > body.date_to:
            return JSONResponse({"error": "invalid_request", "problems": [{"field": "date_from",
                                                                           "problem": "after date_to"}]}, 422)
        if section == "compare-blasts" and not body.blast_ids:
            return JSONResponse({"error": "invalid_request", "problems": [{"field": "blast_ids",
                                                                           "problem": "missing"}]}, 422)
        f = Filters(body.date_from, body.date_to, tuple(body.rigs), tuple(body.sites), tuple(body.blasts))
        cur = svc.con.cursor()
        try:
            engine = AnalyticsEngine(cur, svc.mapping, salt=svc.salt, reveal_labels=svc.settings.reveal_labels)
            with Deadline(cur, analytics_timeout or config.ANALYTICS_API_TIMEOUT_SECONDS) as dl:
                try:
                    result = handler(engine, f, body)
                except duckdb.Error as exc:
                    if dl.expired:
                        return JSONResponse({"error": "timeout", "detail": "analytics query time limit reached"}, 504)
                    log.error("analytics %s failed (%s)", section, safe_error(exc))
                    return JSONResponse({"error": "analytics_failed", "detail": "analytics query failed"}, 500)
            if dl.expired:
                return JSONResponse({"error": "timeout", "detail": "analytics query time limit reached"}, 504)
        finally:
            cur.close()
        payload = {"section": section, "filters": {**f.describe(), "grain": body.grain,
                                                   "blast_ids_requested": len(body.blast_ids)},
                   "result": result,
                   "privacy": {"labels": "real" if svc.settings.reveal_labels else "pseudonymised",
                               "min_group_size": svc.mapping.min_group_size, "raw_records_returned": False}}
        # default=str converts numpy/pandas scalars from anomaly detection safely.
        return JSONResponse(json.loads(json.dumps(payload, default=str)))

    return app


setup_logging("api")
app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT, log_level="info")
