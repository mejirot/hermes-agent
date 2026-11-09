"""A bounded delivery worker; model execution and browser lifetime are unrelated to Push.

The optional sender is a sealed, isolated program built outside the VPS. This module uses
only Hermes' existing dependencies, so notification support cannot make PM resolve a new
dependency generation at production startup.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from hermes_cli.web_push_store import PushStore

_log = logging.getLogger(__name__)
SENDER_PYTHON = Path("/opt/hermes-web-push/bin/python")
SENDER_SCRIPT = Path("/opt/hermes-web-push/send.py")
VAPID_SUBJECT = "https://github.com/mejirot/hermes-agent"


def configured(home: Path) -> bool:
    from hermes_cli.config_effective import load_user_config_effective

    cfg = load_user_config_effective(Path(home) / "config.yaml", fail_closed=True)
    webapp = cfg.get("webapp", {})
    block = webapp.get("push", {}) if isinstance(webapp, dict) else {}
    return isinstance(block, dict) and block.get("enabled") is True


def source_databases():
    from hermes_cli.profiles import profiles_to_serve

    paths = set()
    for _profile, home in profiles_to_serve(multiplex=True, include_standalone=True):
        paths.update((home / "state.db", home / "cron" / "executions.db"))
    return sorted(path for path in paths if path.is_file())


def public_event(event: dict) -> dict:
    return {key: event[key] for key in ("id", "kind", "title", "body", "target", "created_at", "expires_at")}


def event_is_current(event: dict, sources: set[Path]) -> bool:
    if event["expires_at"] <= time.time():
        return False
    if event["source_key"] == "test":
        return True
    source = Path(event["source_db"])
    if source not in sources:
        return False
    if event["kind"] in ("approval", "input"):
        from hermes_cli.web_push_events import pending_event_is_current
        return pending_event_is_current(event, dbpath=source)
    return True


async def run_sender(request: dict) -> dict:
    """Neither secrets nor subscription endpoints cross argv, shell or diagnostic output."""
    process = await asyncio.create_subprocess_exec(
        str(SENDER_PYTHON), "-I", "-B", str(SENDER_SCRIPT),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        limit=4096)
    async def exchange():
        process.stdin.write(json.dumps(request, allow_nan=False).encode() + b"\n")
        await process.stdin.drain()
        process.stdin.close()
        output = await process.stdout.readline()
        await process.wait()
        return output
    try:
        output = await asyncio.wait_for(exchange(), 15)
        if process.returncode or len(output) > 4096:
            return {"ok": False, "status": None, "error_code": "sender_failed"}
        result = json.loads(output)
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            return {"ok": False, "status": None, "error_code": "sender_protocol"}
        return result
    except (TimeoutError, asyncio.TimeoutError):
        return {"ok": False, "status": None, "error_code": "sender_timeout"}
    except (ValueError, UnicodeError, BrokenPipeError, ConnectionResetError):
        return {"ok": False, "status": None, "error_code": "sender_protocol"}
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class PushRuntime:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.store: PushStore | None = None
        self.key_path: Path | None = None
        self.public_key = ""
        self.task: asyncio.Task | None = None
        self.error_code: str | None = None
        self.source_warnings: dict[Path, float] = {}

    @property
    def available(self) -> bool:
        return bool(self.store is not None and self.public_key and self.error_code is None)

    def prepare(self):
        if not configured(self.home) or not SENDER_PYTHON.is_file() or not SENDER_SCRIPT.is_file():
            return
        from hermes_cli import browser_credential
        from hermes_cli.web_push_keys import prepare_key

        self.store = PushStore(self.home)
        self.key_path, self.public_key = prepare_key(self.store.directory)
        browser_credential.on_rotate(f"web-push:{self.home}", self.store.revoke_all)

    def collect(self):
        from hermes_cli import browser_credential
        from hermes_cli.web_push_events import iter_events, source_identity, SourceChangedError

        sources = source_databases()
        for source in sources:
            try:
                identity = source_identity(source)
                key = f"{source}:{identity}" if identity else str(source)
                rows = iter_events(source, self.store.cursor(key), limit=100, expected_identity=identity)
                self.store.import_events(key, source, rows, generation=browser_credential.generation())
            except (OSError, sqlite3.Error, SourceChangedError) as exc:
                now = time.monotonic()
                if now - self.source_warnings.get(source, -60) >= 60:
                    _log.warning("Web Push source unavailable (%s)", type(exc).__name__)
                    self.source_warnings[source] = now
        return set(sources)

    async def deliver(self, sources: set[Path]):
        from hermes_cli import browser_credential

        generation = browser_credential.generation()
        claimed = await asyncio.to_thread(self.store.claim, generation=generation)
        if claimed is None:
            return False
        event, device, delivery = claimed
        current = await asyncio.to_thread(event_is_current, event, sources)
        if not current:
            await asyncio.to_thread(self.store.discard, device["id"], event["id"])
            return True
        can_send = await asyncio.to_thread(self.store.can_send, device["id"], event["id"], generation)
        if not can_send or not browser_credential.is_current(generation):
            return True
        payload = {"v": 1, **public_event(event), "url": f"{device['origin']}/#/push/open/{event['id']}"}
        if not device["preview"]:
            payload["body"] = {"cron": "実行結果が届きました。", "reply": "新しい返信があります。",
                               "approval": "承認が必要です。", "input": "入力を待っています。"}[event["kind"]]
        ttl = min(86400, max(1, math.ceil(event["expires_at"] - time.time())))
        result = await run_sender({"subscription": device["subscription"], "payload": payload,
                                   "vapid_private_key_path": str(self.key_path), "vapid_subject": VAPID_SUBJECT, "ttl": ttl})
        await asyncio.to_thread(self.store.settle, device["id"], event["id"], result=result, attempts=delivery["attempts"])
        return True

    async def run(self):
        from hermes_cli.config_backend import ConfigBackendUnavailable

        prune_at = time.monotonic() + 3600
        while True:
            try:
                if not await asyncio.to_thread(configured, self.home):
                    self.error_code = "disabled"
                    await asyncio.sleep(1)
                    continue
                sources = await asyncio.to_thread(self.collect)
                # A single sender process bounds CPU, memory and outbound concurrency on a small VPS.
                await self.deliver(sources)
                if time.monotonic() >= prune_at:
                    await asyncio.to_thread(self.store.prune)
                    prune_at = time.monotonic() + 3600
                self.error_code = None
            except (OSError, ValueError, RuntimeError, sqlite3.Error, ConfigBackendUnavailable) as exc:
                # Exception text can contain a subscription endpoint; never log it.
                self.error_code = "delivery_unavailable"
                _log.warning("Web Push worker unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(0.5)


@asynccontextmanager
async def push_lifespan(app):
    from hermes_cli.web_server_surface import policy

    if not policy(app.state).own_origin_cookie_writes or not getattr(app.state, "auth_required", False):
        yield
        return
    home = getattr(app.state, "webapp_home", None)
    if home is None:
        yield
        return
    runtime = PushRuntime(Path(home))
    app.state.web_push_runtime = runtime
    try:
        from hermes_cli.config_backend import ConfigBackendUnavailable
        try:
            await asyncio.to_thread(runtime.prepare)
        except (OSError, ValueError, sqlite3.Error, ConfigBackendUnavailable) as exc:
            runtime.error_code = "setup_unavailable"
            _log.warning("Web Push setup unavailable (%s)", type(exc).__name__)
        if runtime.available:
            runtime.task = asyncio.create_task(runtime.run(), name="webapp-push-delivery")
        yield
    finally:
        if runtime.task is not None:
            runtime.task.cancel()
            await asyncio.gather(runtime.task, return_exceptions=True)
