"""Standalone Trade Journal application."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from aiohttp import web

from command_center.ingest import WorkspaceIngestor
from command_center.store import CommandStore


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORKSPACE_ROOT = ROOT.parent
STATIC = HERE / "static"
DEFAULT_COMMAND_DB = ROOT / "data" / "command_center.db"
DEFAULT_JOURNAL_DB = ROOT / "data" / "trading_journal.db"

STORE_KEY = web.AppKey("journal_store", CommandStore)
INGESTOR_KEY = web.AppKey("journal_ingestor", WorkspaceIngestor)
SYNC_LOCK_KEY = web.AppKey("journal_sync_lock", asyncio.Lock)
SYNC_TASK_KEY = web.AppKey("journal_sync_task", asyncio.Task[Any])


def _store(request: web.Request) -> CommandStore:
    return request.app[STORE_KEY]


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _sync(request: web.Request) -> dict[str, int]:
    lock = request.app[SYNC_LOCK_KEY]
    async with lock:
        return await asyncio.to_thread(request.app[INGESTOR_KEY].sync_trading)


def _journal_summary(store: CommandStore) -> dict[str, Any]:
    trades = store.list_trades(limit=500)
    decisions = store.list_decisions(limit=500)
    positions = store.list_positions()
    live_values = [
        float(row["unrealized_pnl"])
        for row in positions
        if row.get("unrealized_pnl") is not None
    ]
    live_pnl = sum(live_values) if live_values else None
    realized = sum(
        float(row.get("pnl") or 0)
        for row in trades
        if row.get("status") in {"closed", "reversed"}
    )
    return {
        "trades": len(trades),
        "open_trades": sum(
            1 for row in trades if row.get("status") not in {"closed", "reversed"}
        ),
        "journaled": sum(1 for row in trades if row.get("decision_id")),
        "active_decisions": sum(
            1
            for row in decisions
            if (row.get("effective_status") or row.get("status")) == "active"
        ),
        "realized_pnl": realized,
        "live_pnl": live_pnl,
        "live_marks": len(live_values),
        "last_sync": store.last_journal_sync(),
    }


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def health(request: web.Request) -> web.Response:
    return web.json_response(
        {"status": "ok", "journal": _journal_summary(_store(request))}
    )


async def bootstrap(request: web.Request) -> web.Response:
    """Read-only page snapshot. Loading the site never runs ingestion."""
    store = _store(request)
    return web.json_response(
        {
            "summary": _journal_summary(store),
            "trades": store.list_trades(),
            "decisions": store.list_decisions(),
            "positions": store.list_positions(),
            "reasons": store.list_reasons(),
            "evaluation": store.lifecycle_evaluation(),
        }
    )


async def sync_now(request: web.Request) -> web.Response:
    try:
        result = await _sync(request)
        return web.json_response(
            {"result": result, "summary": _journal_summary(_store(request))}
        )
    except Exception as exc:
        return _error(f"Sync failed: {exc}", 500)


async def create_decision(request: web.Request) -> web.Response:
    try:
        return web.json_response(
            _store(request).create_decision(await request.json()), status=201
        )
    except (TypeError, ValueError) as exc:
        return _error(str(exc))


async def update_decision(request: web.Request) -> web.Response:
    try:
        result = _store(request).update_decision(
            int(request.match_info["decision_id"]), await request.json()
        )
    except (TypeError, ValueError) as exc:
        return _error(str(exc))
    return web.json_response(result) if result else _error("Journal entry not found", 404)


async def reasons(request: web.Request) -> web.Response:
    store = _store(request)
    if request.method == "GET":
        return web.json_response(store.list_reasons())
    payload = await request.json()
    try:
        return web.json_response(
            store.create_reason(
                str(payload.get("category", "")), str(payload.get("label", ""))
            ),
            status=201,
        )
    except ValueError as exc:
        return _error(str(exc))


async def link_trade(request: web.Request) -> web.Response:
    payload = await request.json()
    decision_id = int(request.match_info["decision_id"])
    try:
        if payload.get("lifecycle_id") is not None:
            result = _store(request).link_lifecycle(
                decision_id, int(payload["lifecycle_id"])
            )
        elif payload.get("trade_id") is not None:
            result = _store(request).link_trade(
                decision_id, int(payload["trade_id"])
            )
        else:
            raise ValueError("lifecycle_id or trade_id is required")
        return web.json_response(result)
    except KeyError as exc:
        return _error(str(exc), 404)
    except (TypeError, ValueError) as exc:
        return _error(str(exc))


async def startup(app: web.Application) -> None:
    await asyncio.to_thread(app[STORE_KEY].init)
    try:
        async with app[SYNC_LOCK_KEY]:
            await asyncio.to_thread(app[INGESTOR_KEY].sync_trading)
    except Exception:
        pass
    app[SYNC_TASK_KEY] = asyncio.create_task(_background_sync(app))


async def _background_sync(app: web.Application) -> None:
    while True:
        await asyncio.sleep(60)
        if app[SYNC_LOCK_KEY].locked():
            continue
        try:
            async with app[SYNC_LOCK_KEY]:
                await asyncio.to_thread(app[INGESTOR_KEY].sync_trading)
        except asyncio.CancelledError:
            raise
        except Exception:
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
    *,
    command_db: Path | None = None,
    journal_db: Path | None = None,
    workspace_root: Path | None = None,
) -> web.Application:
    store = CommandStore(
        command_db or DEFAULT_COMMAND_DB,
        journal_path=journal_db or DEFAULT_JOURNAL_DB,
    )
    app = web.Application(client_max_size=1024 * 1024)
    app[STORE_KEY] = store
    app[INGESTOR_KEY] = WorkspaceIngestor(
        store, workspace_root or WORKSPACE_ROOT
    )
    app[SYNC_LOCK_KEY] = asyncio.Lock()
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_get("/", index)
    app.router.add_static("/static/", STATIC, show_index=False)
    app.router.add_get("/health", health)
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_post("/api/sync", sync_now)
    app.router.add_post("/api/decisions", create_decision)
    app.router.add_patch("/api/decisions/{decision_id:\\d+}", update_decision)
    app.router.add_post("/api/decisions/{decision_id:\\d+}/trades", link_trade)
    app.router.add_route("*", "/api/reasons", reasons)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crypto Scientist Trade Journal")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8811)
    parser.add_argument("--command-db", type=Path, default=DEFAULT_COMMAND_DB)
    parser.add_argument("--journal-db", type=Path, default=DEFAULT_JOURNAL_DB)
    args = parser.parse_args(argv)
    web.run_app(
        create_app(command_db=args.command_db, journal_db=args.journal_db),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
