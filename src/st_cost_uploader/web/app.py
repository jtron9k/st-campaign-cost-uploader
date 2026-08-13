"""FastAPI layer. Thin: it holds session state and renders, and defers every
decision to the engine package."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from st_cost_uploader.config import ConfigError, load_tenants
from st_cost_uploader.models import ParseResult, PlannedWrite, Resolution, WriteOutcome
from st_cost_uploader.parser import ParserError, parse_workbook

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="ST Campaign Cost Uploader")


@dataclass
class UploadSession:
    id: str
    tenant: str
    filename: str
    parse_result: ParseResult
    resolutions: list[Resolution] = field(default_factory=list)
    plans: list[PlannedWrite] = field(default_factory=list)
    unresolved: list[Resolution] = field(default_factory=list)
    outcomes: list[WriteOutcome] = field(default_factory=list)


SESSIONS: dict[str, UploadSession] = {}


def _tenant_names() -> list[str]:
    try:
        return list(load_tenants())
    except ConfigError:
        return []


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "upload.html", {"step": 1, "tenants": _tenant_names()}
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload(
    request: Request,
    tenant: str = Form(...),
    layout: str = Form(""),
    file: UploadFile = File(...),  # noqa: B008 - FastAPI's required DI idiom, not a mutable default
) -> HTMLResponse:
    tenants = _tenant_names()

    def fail(message: str) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "upload.html",
            {"step": 1, "tenants": tenants, "error": message},
            status_code=400,
        )

    if tenant not in tenants:
        return fail(f"'{tenant}' is not a configured tenant.")

    try:
        result = parse_workbook(
            await file.read(), file.filename or "upload.xlsx", layout or None
        )
    except ParserError as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - any unreadable file lands here
        return fail(f"Could not read that file: {exc}")

    session = UploadSession(
        id=uuid.uuid4().hex,
        tenant=tenant,
        filename=file.filename or "upload.xlsx",
        parse_result=result,
    )
    SESSIONS[session.id] = session

    return TEMPLATES.TemplateResponse(
        request,
        "upload.html",
        {
            "step": 1,
            "tenants": tenants,
            "result": result,
            "filename": session.filename,
            "session_id": session.id,
        },
    )
