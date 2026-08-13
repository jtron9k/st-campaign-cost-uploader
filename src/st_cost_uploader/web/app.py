"""FastAPI layer. Thin: it holds session state and renders, and defers every
decision to the engine package."""

from __future__ import annotations

import csv as csv_module
import io as io_module
import uuid
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from st_cost_uploader.aliases import AliasStore
from st_cost_uploader.audit import AuditLog
from st_cost_uploader.client import ServiceTitanClient
from st_cost_uploader.config import ConfigError, load_tenants
from st_cost_uploader.models import (
    Action,
    Campaign,
    MatchKind,
    ParseResult,
    PlannedWrite,
    Resolution,
    WriteOutcome,
)
from st_cost_uploader.parser import ParserError, parse_workbook
from st_cost_uploader.planner import plan as build_plan
from st_cost_uploader.resolver import resolve as resolve_names
from st_cost_uploader.writer import execute as execute_plan

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
        if not key.startswith("choice_") or value == "skip":
            # A skip is a no-op: whatever the resolver produced for this
            # row (including a FUZZY resolution's candidates) is left
            # exactly as-is, so a declined suggestion still carries the
            # "we suggested X, operator declined" information forward.
            # It is never recorded as an alias.
            continue
        try:
            index = int(key.removeprefix("choice_"))
            campaign_id = int(value)
        except ValueError:
            # A malformed key or value (hand-crafted or corrupted POST)
            # must not 500 and must not abort the rest of the submission.
            continue
        if index < 0 or index >= len(session.resolutions):
            continue
        campaign = by_id.get(campaign_id)
        if campaign is None:
            continue
        current = session.resolutions[index]
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
    session = _session(session_id)

    resolved_ids = [r.campaign_id for r in session.resolutions if r.is_resolved]
    client = get_client(session.tenant)
    try:
        existing = await client.list_costs_for_campaigns(resolved_ids)
    finally:
        await client.aclose()

    session.plans, session.unresolved = build_plan(session.resolutions, existing)

    will_write = sum(1 for p in session.plans if p.action is not Action.NO_CHANGE)
    overwrites = sum(1 for p in session.plans if p.overwrites_nonzero)
    net = sum((p.conversion.residual for p in session.plans), Decimal("0.00"))

    return TEMPLATES.TemplateResponse(
        request,
        "preview.html",
        {
            "step": 3,
            "session_id": session.id,
            "tenant": session.tenant,
            "filename": session.filename,
            "plans": session.plans,
            "unresolved": session.unresolved,
            "will_write": will_write,
            "overwrites": overwrites,
            "net_residual": f"${net}",
        },
    )


@app.get("/unmatched/{session_id}.csv")
def unmatched_csv(session_id: str) -> StreamingResponse:
    session = _session(session_id)
    buffer = io_module.StringIO()
    writer = csv_module.writer(buffer)
    writer.writerow(["campaign_name", "year", "month", "monthly_total", "source_row"])
    for res in session.unresolved:
        row = res.row
        writer.writerow(
            [row.campaign_name, row.year, row.month, row.monthly_total, row.source_row]
        )
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="unmatched-{session_id}.csv"'},
    )


@app.post("/write/{session_id}", response_class=HTMLResponse)
async def write_costs(request: Request, session_id: str) -> HTMLResponse:
    session = _session(session_id)
    if not session.plans:
        raise HTTPException(
            status_code=400,
            detail="Build a preview before writing. Nothing has been sent to ServiceTitan.",
        )

    client = get_client(session.tenant)
    try:
        session.outcomes = await execute_plan(
            session.plans, client, AuditLog(AUDIT_PATH), session.tenant
        )
    finally:
        await client.aclose()

    failed = [o for o in session.outcomes if not o.ok]
    return TEMPLATES.TemplateResponse(
        request,
        "results.html",
        {
            "step": 3,
            "session_id": session.id,
            "tenant": session.tenant,
            "filename": session.filename,
            "attempted": len(session.outcomes),
            "succeeded": len(session.outcomes) - len(failed),
            "failed": failed,
            "unchanged": sum(1 for p in session.plans if p.action is Action.NO_CHANGE),
            "unresolved": session.unresolved,
        },
    )
