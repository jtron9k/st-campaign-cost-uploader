"""FastAPI layer. Thin: it holds session state and renders, and defers every
decision to the engine package."""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from st_cost_uploader.aliases import AliasStore
from st_cost_uploader.audit import AuditLog  # noqa: F401 -- unused until Task 16
from st_cost_uploader.client import ServiceTitanClient
from st_cost_uploader.config import ConfigError, load_tenants
from st_cost_uploader.models import (
    Campaign,
    MatchKind,
    ParseResult,
    PlannedWrite,
    Resolution,
    WriteOutcome,
)
from st_cost_uploader.parser import ParserError, parse_workbook
from st_cost_uploader.resolver import resolve as resolve_names

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
    campaigns: list[Campaign] = field(default_factory=list)


SESSIONS: dict[str, UploadSession] = {}

ALIAS_DIR = Path("aliases")
AUDIT_PATH = Path("logs") / "writes.jsonl"


def get_client(tenant_name: str) -> ServiceTitanClient:
    """Seam for tests. Monkeypatched to a stub so no test touches the network."""
    return ServiceTitanClient(load_tenants()[tenant_name])


def _session(session_id: str) -> UploadSession:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="That upload session has expired.")
    return session


def _render_resolve(request: Request, session: UploadSession, campaigns: list[Campaign]):
    pending = [(i, r) for i, r in enumerate(session.resolutions) if not r.is_resolved]
    counts = Counter(r.kind.value for r in session.resolutions)
    return TEMPLATES.TemplateResponse(
        request,
        "resolve.html",
        {
            "step": 2,
            "session_id": session.id,
            "tenant": session.tenant,
            "total": len(session.resolutions),
            "auto": sum(1 for r in session.resolutions if r.is_resolved),
            "pending": pending,
            "counts": {
                "exact": counts.get("exact", 0),
                "alias": counts.get("alias", 0),
                "fuzzy": counts.get("fuzzy", 0),
                "none": counts.get("none", 0),
            },
            "all_campaigns": sorted(campaigns, key=lambda c: c.name),
        },
    )


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


@app.post("/resolve/{session_id}", response_class=HTMLResponse)
async def resolve_screen(request: Request, session_id: str) -> HTMLResponse:
    session = _session(session_id)
    client = get_client(session.tenant)
    try:
        campaigns = await client.list_campaigns()
    finally:
        await client.aclose()

    store = AliasStore.load(session.tenant, ALIAS_DIR)
    session.resolutions = resolve_names(list(session.parse_result.rows), campaigns, store)
    session.campaigns = campaigns
    return _render_resolve(request, session, campaigns)


@app.post("/confirm/{session_id}", response_class=HTMLResponse)
async def confirm_choices(request: Request, session_id: str) -> HTMLResponse:
    session = _session(session_id)
    form = await request.form()
    store = AliasStore.load(session.tenant, ALIAS_DIR)
    by_id = {c.id: c for c in session.campaigns}

    for key, value in form.items():
        if not key.startswith("choice_"):
            continue
        index = int(key.removeprefix("choice_"))
        if index < 0 or index >= len(session.resolutions):
            continue
        current = session.resolutions[index]

        if value == "skip":
            # A chosen skip, not a no-op: it must force the row to
            # unresolved (even one that auto-resolved exact/alias) so it
            # lands in the unmatched CSV, and it must never be recorded
            # as an alias.
            session.resolutions[index] = Resolution(
                row=current.row, kind=MatchKind.NONE, campaign_id=None, campaign_name=None
            )
            continue

        campaign = by_id.get(int(value))
        if campaign is None:
            continue
        session.resolutions[index] = Resolution(
            row=current.row,
            kind=MatchKind.ALIAS,
            campaign_id=campaign.id,
            campaign_name=campaign.name,
        )
        store.set(current.row.campaign_name, campaign.id)

    store.save()
    return await preview_screen(request, session_id)


@app.post("/preview/{session_id}", response_class=HTMLResponse)
async def preview_screen(request: Request, session_id: str) -> HTMLResponse:
    _session(session_id)
    return HTMLResponse("preview pending")
