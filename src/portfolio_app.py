"""Local portfolio overview webapp.

Run with:
    python -m src.portfolio_app
Then open:
    http://127.0.0.1:8790/
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import getpass
import hashlib
import hmac
import io
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from .portfolio_db import (
    aggregate_history,
    disable_address,
    get_address,
    init_portfolio_db,
    latest_snapshots,
    list_addresses,
    save_snapshot,
    set_address_fields,
    snapshot_history,
    upsert_address,
)
from .portfolio_fetcher import (
    ADDRESS_RE,
    TokenCatalog,
    fetch_address_portfolio,
    mask_address,
    normalize_address,
    utc_now_iso,
)


DB_PATH = Path(__file__).parent.parent / "data" / "portfolio.db"
STATIC_DIR = Path(__file__).parent / "portfolio_static"
REFRESH_CONCURRENCY = 4
GUEST_MAX_ADDRESSES = 25
GUEST_RATE_LIMIT_PER_MINUTE = 12
PRIVATE_LOGIN_RATE_LIMIT = 5
PRIVATE_SESSION_SECONDS = 12 * 60 * 60
AUTO_REFRESH_MAX_AGE_SECONDS = 24 * 60 * 60
AUTH_COOKIE = "portfolio_session"


_FRONTEND_NOT_BUILT_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Portfolio â€” frontend not built</title></head>
<body style="font-family: system-ui, sans-serif; background:#08090c; color:#f3f5f9; padding:48px;">
<h1>Frontend not built</h1>
<p>Expected <code>src/portfolio_static/index.html</code> was not found on disk.</p>
<p>The API is running; build the static frontend to view the dashboard.</p>
</body>
</html>
"""


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def hash_password(password: str, *, iterations: int = 600_000) -> str:
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join((
        "pbkdf2_sha256",
        str(iterations),
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    ))


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _password_hash_valid(encoded: str) -> bool:
    try:
        scheme, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        iterations = int(raw_iterations)
        return (
            scheme == "pbkdf2_sha256"
            and 100_000 <= iterations <= 2_000_000
            and len(_b64decode(raw_salt)) >= 16
            and len(_b64decode(raw_digest)) == 32
        )
    except (TypeError, ValueError):
        return False


def _password_matches(password: str, encoded: str) -> bool:
    if not _password_hash_valid(encoded):
        return False
    _, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), _b64decode(raw_salt), int(raw_iterations)
    )
    return hmac.compare_digest(actual, _b64decode(raw_digest))


def _session_token(secret: str, expires_at: int) -> str:
    payload = str(expires_at)
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return payload + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def _session_valid(secret: str, token: str | None) -> bool:
    try:
        raw_expiry, raw_signature = str(token or "").split(".", 1)
        expires_at = int(raw_expiry)
        if expires_at < int(time.time()):
            return False
        expected = hmac.new(secret.encode(), raw_expiry.encode(), hashlib.sha256).digest()
        return hmac.compare_digest(expected, _b64decode(raw_signature))
    except (TypeError, ValueError):
        return False


def _client_key(request: web.Request) -> str:
    forwarded = str(request.headers.get("X-Forwarded-For") or "")
    return (forwarded.split(",", 1)[0].strip() if forwarded else request.remote) or "unknown"


def _rate_allowed(
    store: dict[str, list[float]], key: str, *, limit: int, window: float
) -> bool:
    now = time.monotonic()
    recent = [stamp for stamp in store.get(key, []) if stamp >= now - window]
    if len(recent) >= limit:
        store[key] = recent
        return False
    recent.append(now)
    store[key] = recent
    return True


def _is_authenticated(request: web.Request) -> bool:
    if request.app["storage_mode"] != "private":
        return True
    return _session_valid(
        request.app["session_secret"], request.cookies.get(AUTH_COOKIE)
    )


@web.middleware
async def _private_auth(request: web.Request, handler: Any) -> web.StreamResponse:
    if request.app["storage_mode"] != "private":
        return await handler(request)
    public = (
        request.path == "/login"
        or request.path.startswith("/static/")
        or request.path in {"/api/auth/status", "/api/auth/login", "/api/auth/logout"}
    )
    if public or _is_authenticated(request):
        return await handler(request)
    if request.path.startswith("/api/"):
        return _json_error("authentication required", status=401)
    raise web.HTTPFound("/login")


@web.middleware
async def _privacy_headers(request: web.Request, handler: Any) -> web.StreamResponse:
    response = await handler(request)
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    elif request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


def _request_origin_allowed(request: web.Request, configured: set[str]) -> bool:
    origin = str(request.headers.get("Origin") or "").rstrip("/")
    if not origin:
        return True
    if configured:
        return origin in configured
    try:
        return urlsplit(origin).netloc.lower() == request.host.lower()
    except ValueError:
        return False


def _guest_rate_allowed(app: web.Application, request: web.Request) -> bool:
    return _rate_allowed(
        app["guest_rate"],
        _client_key(request),
        limit=int(app["guest_rate_limit"]),
        window=60.0,
    )


def _parse_limit(request: web.Request, *, default: int) -> int:
    raw = request.query.get("limit")
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _parse_wallet_import(raw: str, default_label: str | None = None) -> tuple[list[dict[str, str | None]], int]:
    """Parse CSV, address-per-line, or pasted text into unique EVM wallets."""
    rows = csv.reader(io.StringIO(str(raw or "")))
    imported: dict[str, dict[str, str | None]] = {}
    skipped = 0
    header: dict[str, int] | None = None

    for raw_row in rows:
        cells = [str(cell or "").strip() for cell in raw_row]
        if cells:
            cells[0] = cells[0].lstrip("\ufeff")
        if not any(cells):
            continue
        lowered = [cell.lower() for cell in cells]
        if header is None and not any(ADDRESS_RE.search(cell) for cell in cells):
            names = {"address", "wallet", "wallet_address", "evm_address"}
            if any(cell in names for cell in lowered):
                header = {name: index for index, name in enumerate(lowered)}
                continue

        candidate = ""
        if header:
            for key in ("address", "wallet", "wallet_address", "evm_address"):
                index = header.get(key)
                if index is not None and index < len(cells):
                    candidate = cells[index]
                    break
        if not candidate:
            candidate = next((cell for cell in cells if ADDRESS_RE.search(cell)), "")

        try:
            address = normalize_address(candidate)
        except ValueError:
            skipped += 1
            continue

        label: str | None = None
        if header and "label" in header and header["label"] < len(cells):
            label = cells[header["label"]].strip() or None
        elif len(cells) > 1:
            label = next(
                (cell for cell in cells if cell != candidate and not ADDRESS_RE.search(cell) and cell.strip()),
                None,
            )
        label = label or default_label
        existing = imported.get(address)
        if existing is None or (label and not existing.get("label")):
            imported[address] = {"address": address, "label": label}

    return list(imported.values()), skipped


def _error_payload(address: str, error: str) -> dict[str, Any]:
    normalized = normalize_address(address)
    return {
        "address": normalized,
        "address_masked": mask_address(normalized),
        "timestamp": utc_now_iso(),
        "status": "error",
        "totals": {
            "total_usd": 0.0,
            "chains_usd": 0.0,
            "lighter_usd": 0.0,
            "hyperliquid_usd": 0.0,
            "defi_usd": 0.0,
            "defi_gross_assets_usd": 0.0,
            "defi_supplied_usd": 0.0,
            "defi_collateral_usd": 0.0,
            "defi_borrowed_usd": 0.0,
            "lit_staked": 0.0,
            "lit_staked_usd": 0.0,
            "lit_spot": 0.0,
            "lit_spot_usd": 0.0,
            "lit_locked": 0.0,
            "lit_locked_usd": 0.0,
            "lit_total": 0.0,
            "lit_total_usd": 0.0,
        },
        "chains": [],
        "lighter": {"ok": False, "errors": [], "total_usd": 0.0, "accounts": []},
        "hyperliquid": {"ok": False, "errors": [], "total_usd": 0.0},
        "defi": {"ok": False, "errors": [], "total_usd": 0.0, "protocols": [], "positions": []},
        "token_catalog": {},
        "errors": [error],
    }


async def _refresh_address(app: web.Application, row: dict[str, Any]) -> dict[str, Any]:
    db_path: Path = app["db_path"]
    catalog: TokenCatalog = app["catalog"]
    try:
        payload = await fetch_address_portfolio(row["address"], catalog=catalog)
        status = payload.get("status") or "ok"
        errors = payload.get("errors") or []
        error = "\n".join(str(e) for e in errors) if errors else None
    except Exception as exc:
        payload = _error_payload(row["address"], f"{type(exc).__name__}: {exc}")
        status = "error"
        error = payload["errors"][0]

    await save_snapshot(
        db_path,
        address_id=int(row["id"]),
        ts=str(payload.get("timestamp") or utc_now_iso()),
        status=status,
        total_usd=payload.get("totals", {}).get("total_usd"),
        payload=payload,
        error=error,
    )
    return payload


async def _summary(db_path: Path) -> dict[str, Any]:
    addresses = await list_addresses(db_path)
    snapshots = await latest_snapshots(db_path)
    successful_snapshots = await latest_snapshots(db_path, successful_only=True)
    rows: list[dict[str, Any]] = []
    totals = {
        "total_usd": 0.0,
        "chains_usd": 0.0,
        "lighter_usd": 0.0,
        "hyperliquid_usd": 0.0,
        "defi_usd": 0.0,
        "defi_gross_assets_usd": 0.0,
        "defi_supplied_usd": 0.0,
        "defi_collateral_usd": 0.0,
        "defi_borrowed_usd": 0.0,
        "lit_staked": 0.0,
        "lit_staked_usd": 0.0,
        "lit_spot": 0.0,
        "lit_spot_usd": 0.0,
        "lit_locked": 0.0,
        "lit_locked_usd": 0.0,
        "lit_total": 0.0,
        "lit_total_usd": 0.0,
    }
    totals_included = dict.fromkeys(totals, 0.0)
    last_refresh: str | None = None
    statuses: list[str] = []
    token_catalog: dict[str, Any] = {}

    for address in addresses:
        address_id = int(address["id"])
        attempt = snapshots.get(address_id)
        last_good = successful_snapshots.get(address_id)
        valuation = attempt
        if attempt and attempt.get("status") == "error" and last_good:
            valuation = last_good
        payload = valuation.get("payload") if valuation else None
        excluded = bool(address.get("excluded"))
        stale = bool(attempt and attempt.get("status") == "error" and last_good)
        latest = {
            "status": attempt.get("status") if attempt else "idle",
            "ts": attempt.get("ts") if attempt else None,
            "total_usd": valuation.get("total_usd") if valuation else None,
            "attempted_total_usd": attempt.get("total_usd") if attempt else None,
            "error": attempt.get("error") if attempt else None,
            "stale": stale,
            "last_good_ts": last_good.get("ts") if last_good else None,
        }
        if payload:
            p_totals = payload.get("totals") or {}
            for key in totals:
                value = float(p_totals.get(key) or 0.0)
                totals[key] += value
                if not excluded:
                    totals_included[key] += value
            statuses.append(str(latest["status"] or payload.get("status") or "idle"))
            refresh_ts = str(latest.get("ts") or payload.get("timestamp") or "")
            if refresh_ts and (not last_refresh or refresh_ts > last_refresh):
                last_refresh = refresh_ts
            token_catalog = payload.get("token_catalog") or token_catalog
        else:
            statuses.append("idle")

        rows.append({
            "id": address["id"],
            "address": address["address"],
            "address_masked": mask_address(address["address"]),
            "label": address.get("label"),
            "excluded": excluded,
            "latest": latest,
            "snapshot": payload,
        })

    if not rows:
        status = "idle"
    elif any(s == "error" for s in statuses):
        status = "error"
    elif any(s == "degraded" for s in statuses):
        status = "degraded"
    elif all(s == "idle" for s in statuses):
        status = "idle"
    else:
        status = "ok"

    return {
        "ok": True,
        "status": status,
        "addresses": rows,
        "totals": totals,
        "totals_included": totals_included,
        "last_refresh": last_refresh,
        "token_catalog": token_catalog,
    }


def _add_static(app: web.Application) -> None:
    """Serve src/portfolio_static/ at /static/. Create the directory if missing
    so aiohttp's add_static does not raise before the frontend agent lands."""
    static_dir: Path = app["static_dir"]
    try:
        static_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if static_dir.is_dir():
        app.router.add_static("/static/", str(static_dir), show_index=False)


def _reset_refresh_job(job: dict[str, Any]) -> None:
    job.clear()
    job.update({
        "running": False,
        "total": 0,
        "completed": 0,
        "current_address_id": None,
        "current_address_ids": [],
        "started_at": None,
        "finished_at": None,
        "results": [],
    })


def create_app(
    db_path: Path | None = None,
    *,
    seed_addresses: list[str] | None = None,
    static_dir: Path | None = None,
    storage_mode: str | None = None,
    allowed_origins: set[str] | None = None,
    auth_password_hash: str | None = None,
    session_secret: str | None = None,
) -> web.Application:
    mode = str(storage_mode or os.getenv("PORTFOLIO_STORAGE_MODE", "local")).strip().lower()
    if mode not in {"local", "guest", "private"}:
        raise ValueError("storage_mode must be local, guest, or private")
    password_hash = auth_password_hash or os.getenv("PORTFOLIO_PASSWORD_HASH", "")
    secret = session_secret or os.getenv("PORTFOLIO_SESSION_SECRET", "")
    if mode == "private" and not _password_hash_valid(password_hash):
        raise ValueError("private mode requires a valid PORTFOLIO_PASSWORD_HASH")
    if mode == "private" and len(secret) < 32:
        raise ValueError("private mode requires a 32+ character PORTFOLIO_SESSION_SECRET")
    app = web.Application(
        client_max_size=128 * 1024,
        middlewares=[_privacy_headers, _private_auth],
    )
    app["db_path"] = db_path or DB_PATH
    app["static_dir"] = static_dir or STATIC_DIR
    app["catalog"] = TokenCatalog()
    app["refresh_lock"] = asyncio.Lock()
    app["refresh_job"] = {}
    _reset_refresh_job(app["refresh_job"])
    # Single-slot mutable holder so we never reassign an app key after startup.
    app["refresh_task"] = {"task": None}
    app["seed_addresses"] = seed_addresses or []
    app["storage_mode"] = mode
    configured_origins = allowed_origins
    if configured_origins is None:
        configured_origins = {
            value.strip().rstrip("/")
            for value in os.getenv("PORTFOLIO_ALLOWED_ORIGINS", "").split(",")
            if value.strip()
        }
    app["allowed_origins"] = configured_origins
    app["guest_rate"] = {}
    app["guest_rate_limit"] = int(
        os.getenv("PORTFOLIO_GUEST_RATE_LIMIT", str(GUEST_RATE_LIMIT_PER_MINUTE))
    )
    app["password_hash"] = password_hash
    app["session_secret"] = secret
    app["login_rate"] = {}

    async def on_startup(app_: web.Application) -> None:
        if app_["storage_mode"] == "guest":
            return
        await init_portfolio_db(app_["db_path"])
        for raw in app_["seed_addresses"]:
            try:
                await upsert_address(app_["db_path"], normalize_address(raw), None)
            except ValueError:
                pass

    async def index(_request: web.Request) -> web.Response:
        index_path = app["static_dir"] / "index.html"
        try:
            text = await asyncio.to_thread(index_path.read_text, "utf-8")
        except (FileNotFoundError, OSError):
            return web.Response(
                text=_FRONTEND_NOT_BUILT_HTML,
                content_type="text/html",
                status=503,
                headers={"Cache-Control": "no-cache, must-revalidate"},
            )
        return web.Response(
            text=text,
            content_type="text/html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    async def login_page(request: web.Request) -> web.Response:
        if app["storage_mode"] != "private":
            raise web.HTTPFound("/")
        if _is_authenticated(request):
            raise web.HTTPFound("/")
        path = app["static_dir"] / "login.html"
        try:
            text = await asyncio.to_thread(path.read_text, "utf-8")
        except (FileNotFoundError, OSError):
            return web.Response(text="Login page unavailable", status=503)
        return web.Response(
            text=text,
            content_type="text/html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    async def auth_status(request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "private_mode": app["storage_mode"] == "private",
            "authenticated": _is_authenticated(request),
        })

    async def auth_login(request: web.Request) -> web.Response:
        if app["storage_mode"] != "private":
            return _json_error("private mode is disabled", status=404)
        if not _request_origin_allowed(request, app["allowed_origins"]):
            return _json_error("origin not allowed", status=403)
        client = _client_key(request)
        if not _rate_allowed(
            app["login_rate"],
            client,
            limit=PRIVATE_LOGIN_RATE_LIMIT,
            window=300.0,
        ):
            return _json_error("too many login attempts", status=429)
        try:
            data = await request.json()
        except Exception:
            return _json_error("invalid JSON")
        if not _password_matches(str(data.get("password") or ""), app["password_hash"]):
            await asyncio.sleep(0.35)
            return _json_error("incorrect password", status=401)
        app["login_rate"].pop(client, None)
        expires_at = int(time.time()) + PRIVATE_SESSION_SECONDS
        response = web.json_response({"ok": True, "expires_at": expires_at})
        forwarded_proto = str(request.headers.get("X-Forwarded-Proto") or "").lower()
        response.set_cookie(
            AUTH_COOKIE,
            _session_token(app["session_secret"], expires_at),
            max_age=PRIVATE_SESSION_SECONDS,
            httponly=True,
            secure=forwarded_proto == "https" or request.secure,
            samesite="Strict",
            path="/",
        )
        return response

    async def auth_logout(request: web.Request) -> web.Response:
        if not _request_origin_allowed(request, app["allowed_origins"]):
            return _json_error("origin not allowed", status=403)
        response = web.json_response({"ok": True})
        response.del_cookie(AUTH_COOKIE, path="/")
        return response

    async def runtime_config(_request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "storage_mode": app["storage_mode"],
            "history_location": "browser" if app["storage_mode"] == "guest" else "server",
            "max_addresses_per_refresh": GUEST_MAX_ADDRESSES,
            "auto_refresh_on_load": True,
            "auto_refresh_max_age_seconds": AUTO_REFRESH_MAX_AGE_SECONDS,
        })

    async def guest_refresh(request: web.Request) -> web.Response:
        if app["storage_mode"] != "guest":
            return _json_error("guest mode is disabled", status=404)
        if not _request_origin_allowed(request, app["allowed_origins"]):
            return _json_error("origin not allowed", status=403)
        if not _guest_rate_allowed(app, request):
            return _json_error("refresh rate limit exceeded", status=429)
        try:
            data = await request.json()
        except Exception:
            return _json_error("invalid JSON")
        raw_addresses = data.get("addresses")
        if not isinstance(raw_addresses, list):
            return _json_error("addresses must be a list")
        addresses: list[str] = []
        seen: set[str] = set()
        for raw in raw_addresses:
            try:
                address = normalize_address(str(raw or ""))
            except ValueError as exc:
                return _json_error(str(exc))
            if address not in seen:
                seen.add(address)
                addresses.append(address)
        if not addresses:
            return _json_error("at least one address is required")
        if len(addresses) > GUEST_MAX_ADDRESSES:
            return _json_error(
                f"refresh is limited to {GUEST_MAX_ADDRESSES} unique addresses"
            )

        catalog: TokenCatalog = app["catalog"]

        async def fetch_one(address: str) -> dict[str, Any]:
            try:
                payload = await fetch_address_portfolio(address, catalog=catalog)
                return {"address": address, "ok": True, "payload": payload}
            except Exception as exc:
                payload = _error_payload(address, f"{type(exc).__name__}: {exc}")
                return {"address": address, "ok": False, "payload": payload}

        results: list[dict[str, Any]] = []
        remaining = list(addresses)
        if remaining:
            results.append(await fetch_one(remaining.pop(0)))
        semaphore = asyncio.Semaphore(REFRESH_CONCURRENCY)

        async def limited(address: str) -> dict[str, Any]:
            async with semaphore:
                return await fetch_one(address)

        if remaining:
            results.extend(await asyncio.gather(*(limited(address) for address in remaining)))
        by_address = {item["address"]: item for item in results}
        ordered = [by_address[address] for address in addresses]
        return web.json_response({"ok": True, "results": ordered})

    async def summary(_request: web.Request) -> web.Response:
        return web.json_response(await _summary(app["db_path"]))

    async def patch_address(request: web.Request) -> web.Response:
        try:
            address_id = int(request.match_info["id"])
        except (KeyError, ValueError):
            return _json_error("invalid address id")
        try:
            data = await request.json()
        except Exception:
            return _json_error("invalid JSON")

        kwargs: dict[str, Any] = {}
        if "label" in data:
            raw_label = data.get("label")
            kwargs["label"] = str(raw_label).strip() if raw_label is not None else None
            if kwargs["label"] == "":
                kwargs["label"] = None
        if "excluded" in data:
            kwargs["excluded"] = bool(data.get("excluded"))
        if not kwargs:
            return _json_error("no fields to update")

        row = await set_address_fields(app["db_path"], address_id, **kwargs)
        if not row:
            return _json_error("address not found", status=404)
        return web.json_response({"ok": True, "address": row})

    async def address_history(request: web.Request) -> web.Response:
        try:
            address_id = int(request.match_info["id"])
        except (KeyError, ValueError):
            return _json_error("invalid address id")
        limit = _parse_limit(request, default=300)
        rows = await snapshot_history(app["db_path"], address_id, limit)
        history = [
            {
                "id": r.get("id"),
                "ts": r.get("ts"),
                "status": r.get("status"),
                "total_usd": r.get("total_usd"),
            }
            for r in reversed(rows)  # snapshot_history returns id DESC; emit ascending
        ]
        return web.json_response({"ok": True, "history": history})

    async def history(request: web.Request) -> web.Response:
        limit = _parse_limit(request, default=1000)
        raw_ids = request.query.get("address_ids")
        address_ids: list[int] | None = None
        if raw_ids is not None:
            try:
                address_ids = sorted({int(value) for value in raw_ids.split(",") if value})
            except ValueError:
                return _json_error("invalid address_ids")
        points = await aggregate_history(app["db_path"], limit, address_ids)
        return web.json_response({"ok": True, "history": points, "address_ids": address_ids})

    async def add_address(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return _json_error("invalid JSON")
        try:
            address = normalize_address(data.get("address", ""))
        except ValueError as exc:
            return _json_error(str(exc))
        label = str(data.get("label") or "").strip() or None
        row = await upsert_address(app["db_path"], address, label)
        return web.json_response({"ok": True, "address": row})

    async def import_addresses(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return _json_error("invalid JSON")
        default_label = str(data.get("label") or "").strip() or None
        wallets, skipped = _parse_wallet_import(data.get("text", ""), default_label)
        if not wallets:
            return _json_error("no valid EVM addresses found")
        if len(wallets) > 1000:
            return _json_error("import is limited to 1000 unique wallets")
        rows = [
            await upsert_address(app["db_path"], wallet["address"], wallet["label"])
            for wallet in wallets
        ]
        return web.json_response({
            "ok": True,
            "addresses": rows,
            "imported": len(rows),
            "skipped": skipped,
        })

    async def delete_address(request: web.Request) -> web.Response:
        try:
            address_id = int(request.match_info["id"])
        except (KeyError, ValueError):
            return _json_error("invalid address id")
        ok = await disable_address(app["db_path"], address_id)
        if not ok:
            return _json_error("address not found", status=404)
        return web.json_response({"ok": True})

    async def refresh_one(request: web.Request) -> web.Response:
        try:
            address_id = int(request.match_info["id"])
        except (KeyError, ValueError):
            return _json_error("invalid address id")
        row = await get_address(app["db_path"], address_id)
        if not row:
            return _json_error("address not found", status=404)
        async with app["refresh_lock"]:
            payload = await _refresh_address(app, row)
        return web.json_response({"ok": True, "snapshot": payload})

    async def _run_refresh_job(rows: list[dict[str, Any]]) -> None:
        job = app["refresh_job"]

        def sync_current() -> None:
            current = sorted(job["current_address_ids"])
            job["current_address_ids"] = current
            job["current_address_id"] = current[0] if current else None

        async def process(row: dict[str, Any]) -> None:
            address_id = int(row["id"])
            job["current_address_ids"].append(address_id)
            sync_current()
            try:
                payload = await _refresh_address(app, row)
                status = str(payload.get("status") or "ok")
            except Exception:
                # _refresh_address persists fetch failures itself; this guard
                # also covers an unexpected snapshot persistence failure.
                status = "error"
            finally:
                job["current_address_ids"].remove(address_id)
                sync_current()
            job["results"].append({"address_id": address_id, "status": status})
            job["results"].sort(key=lambda result: result["address_id"])
            job["completed"] += 1

        async def worker(queue: asyncio.Queue[dict[str, Any]]) -> None:
            while True:
                try:
                    row = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await process(row)
                finally:
                    queue.task_done()

        try:
            # Hold the lock for the whole job so a per-address refresh cannot
            # interleave. Refresh one wallet first to populate TokenCatalog,
            # then fan out without racing CoinGecko cache initialization.
            async with app["refresh_lock"]:
                remaining = list(rows)
                if remaining:
                    await process(remaining.pop(0))
                queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
                for row in remaining:
                    queue.put_nowait(row)
                workers = min(REFRESH_CONCURRENCY, len(remaining))
                if workers:
                    await asyncio.gather(*(worker(queue) for _ in range(workers)))
        finally:
            job["current_address_ids"] = []
            job["current_address_id"] = None
            job["running"] = False
            job["finished_at"] = utc_now_iso()
            app["refresh_task"]["task"] = None

    async def refresh_all(_request: web.Request) -> web.Response:
        job = app["refresh_job"]
        if job["running"]:
            return web.json_response(
                {"ok": False, "error": "refresh already running"}, status=409
            )
        rows = await list_addresses(app["db_path"])
        _reset_refresh_job(job)
        job["running"] = True
        job["total"] = len(rows)
        job["started_at"] = utc_now_iso()
        app["refresh_task"]["task"] = asyncio.create_task(_run_refresh_job(rows))
        return web.json_response({"ok": True, "started": True})

    async def refresh_status(_request: web.Request) -> web.Response:
        job = app["refresh_job"]
        return web.json_response({
            "ok": True,
            "running": job["running"],
            "total": job["total"],
            "completed": job["completed"],
            "current_address_id": job["current_address_id"],
            "current_address_ids": job["current_address_ids"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "results": job["results"],
        })

    async def on_cleanup(app_: web.Application) -> None:
        holder = app_.get("refresh_task") or {}
        task = holder.get("task")
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/login", login_page)
    app.router.add_get("/api/auth/status", auth_status)
    app.router.add_post("/api/auth/login", auth_login)
    app.router.add_post("/api/auth/logout", auth_logout)
    app.router.add_get("/api/config", runtime_config)
    if app["storage_mode"] == "guest":
        app.router.add_post("/api/guest/refresh", guest_refresh)
    else:
        app.router.add_get("/api/summary", summary)
        app.router.add_get("/api/history", history)
        app.router.add_get("/api/addresses/{id}/history", address_history)
        app.router.add_post("/api/addresses", add_address)
        app.router.add_post("/api/addresses/import", import_addresses)
        app.router.add_patch("/api/addresses/{id}", patch_address)
        app.router.add_delete("/api/addresses/{id}", delete_address)
        app.router.add_post("/api/addresses/{id}/refresh", refresh_one)
        app.router.add_post("/api/refresh", refresh_all)
        app.router.add_get("/api/refresh/status", refresh_status)
    _add_static(app)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local portfolio overview webapp")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8790")))
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument(
        "--storage-mode",
        choices=("local", "guest", "private"),
        default=os.getenv("PORTFOLIO_STORAGE_MODE", "local"),
    )
    parser.add_argument("--seed-address", action="append", default=[])
    parser.add_argument(
        "--hash-password",
        action="store_true",
        help="prompt for a password, print a PBKDF2 hash, and exit",
    )
    args = parser.parse_args(argv)

    if args.hash_password:
        password = getpass.getpass("Portfolio password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            parser.error("passwords do not match")
        print(hash_password(password))
        return 0

    app = create_app(
        args.db_path,
        seed_addresses=args.seed_address,
        storage_mode=args.storage_mode,
    )
    web.run_app(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


