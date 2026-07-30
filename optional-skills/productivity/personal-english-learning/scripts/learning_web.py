#!/usr/bin/env python3
"""Loopback-only web UI for the personal English learning service."""

from __future__ import annotations

import argparse
import hmac
import os
import secrets
import sqlite3
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from learning_core import LearningDatabase, LearningService, ReportService


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SESSION_HEADER = "X-Learning-Session"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def default_database_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    return hermes_home / "personal-english-learning" / "learning.db"


def _host_without_port(value: str) -> str:
    value = value.strip().casefold()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


class AssessmentSampleRequest(BaseModel):
    per_band: int = Field(default=5, ge=1, le=25)
    seed: int = 0
    collection_id: str | None = None


class AssessmentRecordRequest(BaseModel):
    sense_id: str
    response: str
    frequency_band: str
    event_id: str | None = None


class DailyPlanRequest(BaseModel):
    review_limit: int = Field(default=30, ge=0, le=100)
    new_limit: int = Field(default=8, ge=0, le=20)
    collection_id: str | None = None


class ReviewRequest(BaseModel):
    rating: str
    idempotency_key: str
    response_time_ms: int | None = Field(default=None, ge=0)
    hint_count: int = Field(default=0, ge=0)
    answer_text: str | None = None


def create_app(
    database_path: str | Path | None = None,
    *,
    session_token: str | None = None,
) -> FastAPI:
    database = LearningDatabase(database_path or default_database_path())
    service = LearningService(database)
    token = session_token or secrets.token_urlsafe(32)
    app = FastAPI(
        title="Personal English Learning",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.learning_service = service
    app.state.session_token = token

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        host = _host_without_port(request.headers.get("host", ""))
        if host not in LOOPBACK_HOSTS:
            return JSONResponse({"detail": "Host is not allowed"}, status_code=400)
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            supplied = request.headers.get(SESSION_HEADER, "")
            if not supplied or not hmac.compare_digest(supplied, token):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(sqlite3.Error)
    async def sqlite_error_handler(_request: Request, _exc: sqlite3.Error):
        return JSONResponse({"detail": "Learning database operation failed"}, status_code=500)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        template = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(template.replace("__LEARNING_SESSION_TOKEN__", token))

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "service": "personal-english-learning"}

    @app.get("/api/stats")
    async def stats() -> dict:
        return {"ok": True, **service.stats()}

    @app.get("/api/reports/weekly")
    async def weekly_report(days: int = Query(default=7, ge=1, le=90)) -> dict:
        return {"ok": True, **ReportService(database).weekly_report(days=days)}

    @app.post("/api/assessment/sample")
    async def assessment_sample(body: AssessmentSampleRequest) -> dict:
        return {
            "ok": True,
            **service.assessment_sample(
                per_band=body.per_band,
                seed=body.seed,
                collection_id=body.collection_id,
            ),
        }

    @app.post("/api/assessment/record")
    async def assessment_record(body: AssessmentRecordRequest) -> dict:
        return {
            "ok": True,
            **service.record_assessment(
                body.sense_id,
                body.response,
                body.frequency_band,
                event_id=body.event_id or str(uuid.uuid4()),
            ),
        }

    @app.post("/api/plans/today")
    async def today(body: DailyPlanRequest) -> dict:
        return {
            "ok": True,
            **service.daily_plan(
                review_limit=body.review_limit,
                new_limit=body.new_limit,
                collection_id=body.collection_id,
            ),
        }

    @app.post("/api/reviews/{card_id}")
    async def review(card_id: str, body: ReviewRequest) -> dict:
        return {
            "ok": True,
            **service.record_review(
                card_id,
                body.rating,
                body.idempotency_key,
                response_time_ms=body.response_time_ms,
                hint_count=body.hint_count,
                answer_text=body.answer_text,
            ),
        }

    @app.get("/api/vocabulary/search")
    async def vocabulary_search(
        q: str = Query(min_length=1, max_length=80),
        collection_id: str | None = None,
        limit: int = Query(default=30, ge=1, le=100),
    ) -> dict:
        return {
            "ok": True,
            **service.search_vocabulary(
                q, collection_id=collection_id, limit=limit
            ),
        }

    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="learning-assets")
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9121)
    parser.add_argument("--db", type=Path, default=default_database_path())
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.host.casefold() not in LOOPBACK_HOSTS:
        print(
            "Refusing non-loopback bind. Use 127.0.0.1 and SSH local forwarding.",
            file=sys.stderr,
        )
        return 2
    if not 1 <= args.port <= 65535:
        print("Port must be between 1 and 65535.", file=sys.stderr)
        return 2
    import uvicorn

    uvicorn.run(create_app(args.db), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
