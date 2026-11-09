"""Opt-in Web Push for the authenticated browser, using the existing Webapp gate."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from hermes_cli import browser_credential, webapp_sign_in
from hermes_cli.dashboard_auth.cookies import detect_https
from hermes_cli.dashboard_auth.request_utils import _http_origin, cookie_origin_is_allowed
from hermes_cli.web_push_store import COOKIE_NAME, DEFAULT_KINDS
from hermes_cli.web_push_worker import event_is_current, public_event, source_databases
from hermes_cli.web_server_surface import policy

def _uncached(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/api/webapp/push", tags=["webapp-push"], dependencies=[Depends(_uncached)])


def cookie_name(request: Request) -> str:
    return f"{COOKIE_NAME}_{webapp_sign_in.cookie_gateway(request)}"


def request_origin(request: Request) -> str:
    from hermes_cli.dashboard_auth.prefix import resolve_public_url

    configured = resolve_public_url(request.app.state)
    if configured:
        url = urlsplit(configured)
        value = f"{url.scheme}://{url.netloc}"
    else:
        value = f"{'https' if detect_https(request) else request.url.scheme}://{request.url.netloc}"
    origin = _http_origin(value)
    if origin is None or origin[0] != "https":
        raise HTTPException(409, "Smartphone notifications require HTTPS")
    return value.rstrip("/")


def _identity(request: Request):
    if not policy(request.app.state).own_origin_cookie_writes:
        raise HTTPException(404, "Not Found")
    session = getattr(request.state, "session", None)
    generation = webapp_sign_in.sign_in_of(request)
    if session is None or generation is None or not browser_credential.is_current(generation):
        raise HTTPException(401, "Sign in to this Webapp to manage notifications")
    if request.method not in ("GET", "HEAD") and not cookie_origin_is_allowed(request):
        raise HTTPException(403, "Notification settings must be changed from this Webapp")
    origin = request_origin(request)
    owner = hashlib.sha256(json.dumps([session.provider, session.issuer, session.user_id], separators=(",", ":")).encode()).hexdigest()
    return {"owner": owner, "origin": origin, "generation": generation}


async def _context(request: Request, *, device_required=False, enabled_required=True):
    identity = _identity(request)
    runtime = getattr(request.app.state, "web_push_runtime", None)
    if enabled_required and (runtime is None or not runtime.available):
        raise HTTPException(503, "Smartphone notifications are not available on this server")
    device = None
    if runtime is not None and runtime.store is not None:
        device = await asyncio.to_thread(runtime.store.device, request.cookies.get(cookie_name(request)), **identity)
    if device_required and device is None:
        raise HTTPException(404, "Enable notifications on this device first")
    return runtime, identity, device


def _status(runtime, device):
    # The browser already owns its subscription keys. Status only needs the endpoint to
    # reconcile a revoked browser subscription, not a copy of the encryption secrets.
    subscription = {"endpoint": device["subscription"]["endpoint"]} if device else None
    return {"available": bool(runtime and runtime.available), "public_key": runtime.public_key if runtime else "",
            "device_id": device["id"] if device else None, "subscription": subscription,
            "kinds": device["kinds"] if device else dict(DEFAULT_KINDS), "preview": device["preview"] if device else True,
            "last_status": device["last_status"] if device else None,
            "last_error": device["last_error"] if device else None,
            "last_sent_at": device["last_sent_at"] if device else None}


@router.get("/status")
async def status(request: Request):
    runtime, _identity_value, device = await _context(request, enabled_required=False)
    return _status(runtime, device)


@router.put("/subscription")
async def subscribe(request: Request, response: Response, body: dict):
    runtime, identity, _device = await _context(request)
    if set(body) - {"subscription", "kinds", "preview"}:
        raise HTTPException(400, "Invalid notification subscription")
    try:
        device = await asyncio.to_thread(runtime.store.subscribe, request.cookies.get(cookie_name(request)),
            **identity, subscription=body.get("subscription"), kinds=body.get("kinds"), preview=body.get("preview"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    response.set_cookie(cookie_name(request), device["id"], max_age=365 * 86400, path="/",
                        secure=True, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return _status(runtime, device)


@router.delete("/subscription")
async def unsubscribe(request: Request, response: Response):
    runtime, _identity_value, device = await _context(request, enabled_required=False)
    if device is not None:
        await asyncio.to_thread(runtime.store.revoke, device["id"])
    response.delete_cookie(cookie_name(request), path="/", secure=True, httponly=True, samesite="strict")
    return {"ok": True}


@router.patch("/preferences")
async def update_preferences(request: Request, body: dict):
    runtime, identity, device = await _context(request, device_required=True)
    if not body or set(body) - {"kinds", "preview"}:
        raise HTTPException(400, "Invalid notification preferences")
    try:
        await asyncio.to_thread(runtime.store.update_preferences, device, kinds=body.get("kinds"), preview=body.get("preview"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    updated = await asyncio.to_thread(runtime.store.device, device["id"], **identity)
    return _status(runtime, updated)


@router.post("/test")
async def test_notification(request: Request):
    runtime, _identity_value, device = await _context(request, device_required=True)
    try:
        event_id = await asyncio.to_thread(runtime.store.test_event, device)
    except ValueError as exc:
        raise HTTPException(429, str(exc)) from exc
    return {"ok": True, "event_id": event_id}


@router.get("/events")
async def events(request: Request):
    runtime, _identity_value, device = await _context(request, device_required=True)
    rows = await asyncio.to_thread(runtime.store.visible_events, device["id"])
    sources = set(await asyncio.to_thread(source_databases))
    current = [public_event(row) for row in rows if await asyncio.to_thread(event_is_current, row, sources)]
    return {"events": current}


@router.post("/ack")
async def acknowledge(request: Request, body: dict):
    runtime, _identity_value, device = await _context(request, device_required=True)
    ids = body.get("event_ids")
    if (set(body) != {"event_ids"} or not isinstance(ids, list) or len(ids) > 100
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids)):
        raise HTTPException(400, "Invalid notification acknowledgement")
    await asyncio.to_thread(runtime.store.acknowledge, device["id"], ids)
    return {"ok": True}


@router.get("/notifications/{event_id}")
async def notification(request: Request, event_id: str):
    runtime, _identity_value, device = await _context(request, device_required=True)
    if not re.fullmatch(r"[0-9a-f]{64}", event_id):
        raise HTTPException(404, "Notification not found")
    event = await asyncio.to_thread(runtime.store.event_for_device, device["id"], event_id)
    sources = set(await asyncio.to_thread(source_databases))
    if event is None or not await asyncio.to_thread(event_is_current, event, sources):
        raise HTTPException(410, "This notification or request is no longer available")
    return public_event(event)


async def revoke_on_logout(request: Request, response: Response):
    """The existing logout path also closes this device's independent Push grant."""
    if not policy(request.app.state).own_origin_cookie_writes:
        return
    if not cookie_origin_is_allowed(request):
        return
    runtime = getattr(request.app.state, "web_push_runtime", None)
    device_id = request.cookies.get(cookie_name(request))
    if runtime is not None and runtime.store is not None and device_id:
        await asyncio.to_thread(runtime.store.revoke, device_id)
    response.delete_cookie(cookie_name(request), path="/", secure=True, httponly=True, samesite="strict")
