"""Aiohttp application for the Crypto Scientist Command Center."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from aiohttp import web

from .ingest import WorkspaceIngestor
from .store import CommandStore


HERE = Path(__file__).resolve().parent
LIGHTER_ROOT = HERE.parent
WORKSPACE_ROOT = LIGHTER_ROOT.parent
STATIC = HERE / "static"
DEFAULT_DB = LIGHTER_ROOT / "data" / "command_center.db"
STORE_KEY = web.AppKey("store", CommandStore)
INGESTOR_KEY = web.AppKey("ingestor", WorkspaceIngestor)
SYNC_LOCK_KEY = web.AppKey("sync_lock", asyncio.Lock)
SYNC_TASK_KEY = web.AppKey("sync_task", asyncio.Task[Any])


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _store(request: web.Request) -> CommandStore:
    return request.app[STORE_KEY]


async def _sync(request: web.Request) -> dict[str, int]:
    lock = request.app[SYNC_LOCK_KEY]
    async with lock:
        ingestor = request.app[INGESTOR_KEY]
        return await asyncio.to_thread(ingestor.sync)


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "summary": _store(request).summary()})


async def bootstrap(request: web.Request) -> web.Response:
    # Page loads are read-only. Ingestion runs at service startup, in the
    # background worker, or through the explicit POST /api/sync action.
    store = _store(request)
    return web.json_response(
        {
            "summary": store.summary(),
            "signals": store.list_signals(limit=80),
            "decisions": store.list_decisions(),
            "trades": store.list_trades(),
            "evaluation": store.lifecycle_evaluation(),
            "positions": store.list_positions(),
            "reasons": store.list_reasons(),
            "edge": store.edge_report(),
            "weekly": store.weekly_review(),
            "settings": store.settings(),
        }
    )


async def sync_now(request: web.Request) -> web.Response:
    try:
        result = await _sync(request)
        return web.json_response({"result": result, "summary": _store(request).summary()})
    except Exception as exc:
        return _json_error(f"Sync failed: {exc}", 500)


async def list_signals(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError:
        limit = 100
    return web.json_response(
        _store(request).list_signals(
            status=request.query.get("status"),
            source=request.query.get("source"),
            limit=limit,
        )
    )


async def signal_detail(request: web.Request) -> web.Response:
    signal = _store(request).get_signal(int(request.match_info["signal_id"]))
    return web.json_response(signal) if signal else _json_error("Signal not found", 404)


async def signal_status(request: web.Request) -> web.Response:
    payload = await request.json()
    try:
        ok = _store(request).set_signal_status(
            int(request.match_info["signal_id"]), str(payload.get("status", ""))
        )
    except ValueError as exc:
        return _json_error(str(exc))
    return web.json_response({"ok": ok}) if ok else _json_error("Signal not found", 404)


async def create_decision(request: web.Request) -> web.Response:
    try:
        return web.json_response(_store(request).create_decision(await request.json()), status=201)
    except (ValueError, TypeError) as exc:
        return _json_error(str(exc))


async def update_decision(request: web.Request) -> web.Response:
    try:
        result = _store(request).update_decision(
            int(request.match_info["decision_id"]), await request.json()
        )
    except (ValueError, TypeError) as exc:
        return _json_error(str(exc))
    return web.json_response(result) if result else _json_error("Decision not found", 404)


async def list_decisions(request: web.Request) -> web.Response:
    return web.json_response(_store(request).list_decisions())


async def list_trades(request: web.Request) -> web.Response:
    return web.json_response(_store(request).list_trades())


async def lifecycle_evaluation(request: web.Request) -> web.Response:
    return web.json_response(_store(request).lifecycle_evaluation())


async def reasons(request: web.Request) -> web.Response:
    store = _store(request)
    if request.method == "GET":
        return web.json_response(store.list_reasons())
    payload = await request.json()
    try:
        created = store.create_reason(
            str(payload.get("category", "")), str(payload.get("label", ""))
        )
        return web.json_response(created, status=201)
    except ValueError as exc:
        return _json_error(str(exc))


async def link_trade(request: web.Request) -> web.Response:
    payload = await request.json()
    try:
        decision_id = int(request.match_info["decision_id"])
        if payload.get("lifecycle_id") is not None:
            result = _store(request).link_lifecycle(
                decision_id, int(payload["lifecycle_id"])
            )
        else:
            result = _store(request).link_trade(decision_id, int(payload["trade_id"]))
        return web.json_response(result)
    except KeyError as exc:
        return _json_error(str(exc), 404)
    except (ValueError, TypeError):
        return _json_error("trade_id or lifecycle_id is required")


async def summary(request: web.Request) -> web.Response:
    return web.json_response(_store(request).summary())


async def edge(request: web.Request) -> web.Response:
    return web.json_response(_store(request).edge_report())


async def weekly(request: web.Request) -> web.Response:
    return web.json_response(_store(request).weekly_review())


async def settings(request: web.Request) -> web.Response:
    store = _store(request)
    if request.method == "GET":
        return web.json_response(store.settings())
    return web.json_response(store.update_settings(await request.json()))


async def startup(app: web.Application) -> None:
    store = app[STORE_KEY]
    await asyncio.to_thread(store.init)
    try:
        await asyncio.to_thread(app[INGESTOR_KEY].sync)
    except Exception:
        pass
    app[SYNC_TASK_KEY] = asyncio.create_task(_background_sync(app))


async def _background_sync(app: web.Application) -> None:
    """Keep signals, trades, and due outcome windows current while the app runs."""
    while True:
        await asyncio.sleep(60)
        lock = app[SYNC_LOCK_KEY]
        if lock.locked():
            continue
        try:
            async with lock:
                await asyncio.to_thread(app[INGESTOR_KEY].sync)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Sync failures are persisted by the ingestor and surfaced in the UI.
            continue


async def cleanup(app: web.Application) -> None:
    task = app.get(SYNC_TASK_KEY)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_app(
    *, db_path: Path | None = None, workspace_root: Path | None = None
) -> web.Application:
    store = CommandStore(db_path or DEFAULT_DB)
    app = web.Application(client_max_size=1024 * 1024)
    app[STORE_KEY] = store
    app[INGESTOR_KEY] = WorkspaceIngestor(store, workspace_root or WORKSPACE_ROOT)
    app[SYNC_LOCK_KEY] = asyncio.Lock()
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_get("/", index)
    app.router.add_static("/static/", STATIC, show_index=False)
    app.router.add_get("/health", health)
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_post("/api/sync", sync_now)
    app.router.add_get("/api/summary", summary)
    app.router.add_get("/api/signals", list_signals)
    app.router.add_get("/api/signals/{signal_id:\\d+}", signal_detail)
    app.router.add_patch("/api/signals/{signal_id:\\d+}/status", signal_status)
    app.router.add_get("/api/decisions", list_decisions)
    app.router.add_post("/api/decisions", create_decision)
    app.router.add_patch("/api/decisions/{decision_id:\\d+}", update_decision)
    app.router.add_post("/api/decisions/{decision_id:\\d+}/trades", link_trade)
    app.router.add_get("/api/trades", list_trades)
    app.router.add_get("/api/evaluation", lifecycle_evaluation)
    app.router.add_route("*", "/api/reasons", reasons)
    app.router.add_get("/api/edge", edge)
    app.router.add_get("/api/weekly", weekly)
    app.router.add_route("*", "/api/settings", settings)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crypto Scientist Command Center")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8810)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    web.run_app(create_app(db_path=args.db), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
