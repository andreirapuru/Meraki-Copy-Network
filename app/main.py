from __future__ import annotations

import asyncio
import os
import secrets
import time
from dataclasses import asdict
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .configuration import GROUPS
from .meraki import MerakiClient, MerakiError
from .models import JobState
from .security import production_cipher
from .service import MigrationService
from .storage import Store

ROOT = Path(__file__).parent
DATA_PATH = Path(os.environ.get("MERAKI_MIGRATOR_DATA", ROOT.parent / "data" / "migrator.sqlite3"))
store = Store(DATA_PATH, production_cipher())
service = MigrationService(store)
sessions: dict[str, tuple[str, float]] = {}
SESSION_TTL_SECONDS = 30 * 60

app = FastAPI(title="Meraki Switch Migrator", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


class ConnectionIn(BaseModel):
    api_key: str = Field(min_length=20)


class PlanIn(BaseModel):
    organization_id: str
    source_network_id: str
    destination_network_id: str
    serials: list[str] = Field(min_length=1)
    groups: list[str] = Field(default_factory=list)


def _session_key(session_id: str | None) -> str:
    if not session_id or session_id not in sessions:
        raise HTTPException(401, "Sessão expirada. Informe a chave API novamente.")
    api_key, expires_at = sessions[session_id]
    if time.monotonic() > expires_at:
        del sessions[session_id]
        raise HTTPException(401, "Sessão expirada. Informe a chave API novamente.")
    sessions[session_id] = (api_key, time.monotonic() + SESSION_TTL_SECONDS)
    return api_key


async def api_for_session(session_id: Annotated[str | None, Cookie()] = None) -> MerakiClient:
    return MerakiClient(_session_key(session_id))


def error(exc: Exception) -> HTTPException:
    return HTTPException(400, str(exc))


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"groups": list(GROUPS.values())})


@app.post("/api/session/connect")
async def connect(payload: ConnectionIn):
    api = MerakiClient(payload.api_key)
    try:
        organizations = await api.organizations()
    except MerakiError as exc:
        raise error(exc) from exc
    finally:
        await api.close()
    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = (payload.api_key, time.monotonic() + SESSION_TTL_SECONDS)
    response = {"organizations": organizations}
    from fastapi.responses import JSONResponse
    result = JSONResponse(response)
    result.set_cookie("session_id", session_id, httponly=True, samesite="strict", secure=False, max_age=SESSION_TTL_SECONDS)
    return result


@app.post("/api/session/logout")
async def logout(session_id: Annotated[str | None, Cookie()] = None):
    if session_id:
        sessions.pop(session_id, None)
    from fastapi.responses import JSONResponse
    result = JSONResponse({"ok": True})
    result.delete_cookie("session_id")
    return result


@app.get("/api/organizations")
async def organizations(api: MerakiClient = Depends(api_for_session)):
    try:
        return await api.organizations()
    finally:
        await api.close()


@app.get("/api/organizations/{organization_id}/networks")
async def networks(organization_id: str, api: MerakiClient = Depends(api_for_session)):
    try:
        return await service.inventory(api, organization_id)
    except MerakiError as exc:
        raise error(exc) from exc
    finally:
        await api.close()


@app.get("/api/networks/{network_id}/switches")
async def switches(network_id: str, api: MerakiClient = Depends(api_for_session)):
    try:
        devices, stacks = await api.devices(network_id), await api.stacks(network_id)
        members = {serial: stack for stack in stacks for serial in stack.get("serials", [])}
        def is_switch(device: dict) -> bool:
            # Older dashboard responses can omit productType. MS/Catalyst model
            # prefixes are a safe display fallback; the plan endpoint validates
            # the selection again before any mutation.
            product_type = str(device.get("productType", "")).lower()
            model = str(device.get("model", "")).upper()
            return "switch" in product_type or model.startswith(("MS", "C9", "C1"))
        return {
            "switches": [
                {**device, "stackId": members.get(device["serial"], {}).get("id"), "stackName": members.get(device["serial"], {}).get("name")}
                for device in devices if is_switch(device)
            ],
            "stacks": stacks,
            "detectedDevices": [
                {key: device.get(key) for key in ("serial", "name", "model", "productType")}
                for device in devices
            ],
        }
    except MerakiError as exc:
        raise error(exc) from exc
    finally:
        await api.close()


@app.get("/api/capabilities")
async def capabilities():
    return [asdict(group) for group in GROUPS.values()]


@app.post("/api/plans")
async def plans(payload: PlanIn, api: MerakiClient = Depends(api_for_session)):
    try:
        plan = await service.build_plan(
            api,
            organization_id=payload.organization_id,
            source_network_id=payload.source_network_id,
            destination_network_id=payload.destination_network_id,
            selected_serials=payload.serials,
            groups=payload.groups,
        )
        return {**plan.to_dict(), "groups": [{**asdict(GROUPS[g]), "selected": True} for g in plan.groups]}
    except MerakiError as exc:
        raise error(exc) from exc
    finally:
        await api.close()


@app.post("/api/jobs")
async def jobs(plan_id: str):
    try:
        job = await service.create_job(plan_id)
        return job.to_dict()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/jobs/{job_id}")
async def job(job_id: str):
    try:
        return store.get_job(job_id).to_dict()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/jobs/{job_id}/units/{unit_id}/confirm")
async def confirm(job_id: str, unit_id: str, api: MerakiClient = Depends(api_for_session)):
    try:
        # Execute in request context: the response only returns after a safe terminal/waiting state.
        return (await service.confirm_current(api, job_id, unit_id)).to_dict()
    except (KeyError, MerakiError) as exc:
        raise error(exc) from exc
    finally:
        await api.close()


@app.get("/api/jobs/{job_id}/events")
async def events(job_id: str):
    async def stream() -> AsyncIterator[str]:
        last_id = 0
        while True:
            entries = store.events_since(job_id, last_id)
            for entry in entries:
                last_id = entry["id"]
                import json
                yield f"id: {entry['id']}\nevent: migration\ndata: {json.dumps(entry)}\n\n"
            try:
                state = store.get_job(job_id).state
            except KeyError:
                yield "event: error\ndata: {\"message\": \"Job inexistente\"}\n\n"
                return
            if state in {JobState.COMPLETED, JobState.FAILED, JobState.INTERRUPTED}:
                yield f"event: end\ndata: {{\"state\": \"{state.value}\"}}\n\n"
                return
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def run() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8765, reload=False)
