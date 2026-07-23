# Telegram Command Bot

Status: owner-only read-only commands implemented on 2026-07-23 in the
production `src.dashboard` entry point. Group/multi-user access remains a
future option and is intentionally disabled for privacy.

## Vision

A Telegram bot that any member of a designated group or channel can command.
It fetches live data from the dashboard backend and relays insights back to the chat.

Implemented commands:

| Command | Response |
|---------|----------|
| `/stats` | Full stats summary card |
| `/pnl [today\|7d\|30d\|all] [source]` | P&L and win-rate summary |
| `/positions [source]` | Current cached open positions |
| `/orders [source]` | Current cached open orders |
| `/trades [n] [source]` | Last completed round trips (default 10) |
| `/fills [n] [source]` | Last executions/events (default 10) |
| `/risk [source]` | Exposure and concentration summary |
| `/sources` | Active/disabled source status |
| `/health` | Component health summary |
| `/dashboard` | Public dashboard link |
| `/version` | Deployed commit and process start time |
| `/help` | Command list with short descriptions |

---

## Why the Current Architecture Already Supports This

The analytics layer was built channel-agnostic from the start:

- **`src/stats.py`** — `compute_stats()` is a pure function; its output dict is independent of Telegram, WebSocket, or HTTP. Any delivery channel can call it.
- **`format_stats_summary()`** — produces a compact, Telegram-ready multi-line string. Already used by `/api/send_stats`; identical output can be sent to any `chat_id`.
- **`render_stats_card()`** — returns raw PNG bytes; can be sent to any recipient chat, not just the broadcast channel.
- **`/api/send_stats`** — already proves the relay path end-to-end: receive a trigger → compute stats → deliver text + image to Telegram. A bot command is the same pattern with a different trigger source.
- **`stats_state`** — the cached stats dict in `_run()` is updated after every close; a command handler can read it without recomputing.

The existing `tg_send` / `tg_send_photo` helpers accept any `chat_id` if adapted to take it as a parameter, avoiding a full Telegram SDK dependency.

---

## Implementation

### 1. Command listener (asyncio task)

`telegram_command_listener()` runs inside `_run()` alongside the existing
dashboard tasks.

It uses long-poll `getUpdates`; the last processed update cursor is persisted in
SQLite so restarts cannot replay old commands.

### 2. Command dispatch table

```python
COMMAND_HANDLERS = {
    "/stats":     handle_stats,
    "/pnl":       handle_pnl,
    "/positions": handle_positions,
    "/trades":    handle_trades,
    "/help":      handle_help,
}
```

Each handler is an `async def` that accepts `(chat_id, args, stats_state, sources, …)` and calls the appropriate builder + Telegram send helper, replying to `chat_id` rather than the broadcast channel.

### 3. Per-chat reply helpers

Extend (or wrap) `tg_send` / `tg_send_photo` to accept an explicit `chat_id` parameter, defaulting to the broadcast `tg_channel` for existing callers. No breaking change.

---

## Multi-Tenant / Safety Notes

- **Read-only**: the bot must never accept trade commands, position modifications, or any instruction that touches exchange APIs. Accept only informational queries.
- **No wallet address exposure**: source names ("HL", "My NK pool") and ticker symbols are safe; raw wallet addresses, API keys, and internal IDs must never appear in responses. The existing masking in `sources.py` / `db.py` already enforces this — command handlers must not circumvent it.
- **Per-chat rate limiting**: maintain a `{chat_id: last_command_time}` dict; reject commands issued faster than a configurable floor (e.g. 5 seconds per chat) with a friendly throttle message.
- **Chat allowlist**: only respond in chats listed in a `telegram_allowed_chats` config setting (or the broadcast channel itself). Silently ignore messages from unlisted chats to avoid information leakage.
- **Dedup scoping**: the existing `_tg_sent` dedup guard is keyed on message hash globally. For command replies that go to multiple different chats, dedup should be scoped per `chat_id` to avoid one chat's reply suppressing another's.
- **Error isolation**: command handler failures must be caught and logged without crashing the main event loop or triggering false dedup cache entries.
