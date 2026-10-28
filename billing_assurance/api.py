"""Minimal HTTP API for billing-exception review."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from billing_assurance.investigate import InvestigationError, analyze_amendment
from billing_assurance.reconcile import DEFAULT_DB_PATH
from billing_assurance.review import (
    ReviewError,
    audit_history,
    get_exception,
    list_exceptions,
    record_decision,
    summary,
)


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str
    reason: str


def create_app(db_path: Path = DEFAULT_DB_PATH, *, completer=None) -> FastAPI:
    app = FastAPI(title="Intelligent Billing Assurance", version="0.1.0")
    app.state.db_path = Path(db_path)
    app.state.completer = completer

    @app.get("/summary")
    def read_summary() -> dict:
        return _call(summary, app.state.db_path)

    @app.get("/exceptions")
    def read_exceptions(status: str | None = None, exception_type: str | None = None) -> dict:
        items = _call(
            list_exceptions,
            app.state.db_path,
            status=status,
            exception_type=exception_type,
        )
        return {"count": len(items), "exceptions": items}

    @app.get("/exceptions/{exception_id}")
    def read_exception(exception_id: int) -> dict:
        return _call(get_exception, app.state.db_path, exception_id)

    @app.get("/exceptions/{exception_id}/audit")
    def read_audit(exception_id: int) -> dict:
        return _call(audit_history, app.state.db_path, exception_id)

    @app.post("/exceptions/{exception_id}/analyze")
    def analyze_exception(exception_id: int) -> dict:
        return _call(
            analyze_amendment,
            app.state.db_path,
            exception_id,
            completer=app.state.completer,
        )

    @app.post("/exceptions/{exception_id}/approve")
    def approve_exception(exception_id: int, body: DecisionBody) -> dict:
        return _decide(app.state.db_path, exception_id, "approve", body)

    @app.post("/exceptions/{exception_id}/reject")
    def reject_exception(exception_id: int, body: DecisionBody) -> dict:
        return _decide(app.state.db_path, exception_id, "reject", body)

    return app


def _decide(db_path: Path, exception_id: int, decision: str, body: DecisionBody) -> dict:
    decided_at = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _call(
        record_decision,
        db_path,
        exception_id,
        decision,
        body.reviewer,
        body.reason,
        decided_at=decided_at,
    )


def _call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except (ReviewError, InvestigationError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


app = create_app()
