# Invariants — things that must never be violated

These are the non-negotiable properties. If a change breaks one, it is a P0/P1
bug regardless of why it seemed fine.

## Security

1. **HL wallet address** → `.env` only (`HL_ADDRESS*`). Never in config, chat,
   Telegram, filenames, or logs. Masked `0x…4c74` if it must appear. Source ids
   are sha256 hashes, never the address.
2. **`PRIVACY_SECRET_KEY`** → env only, never shown. The price/size blur is
   reversible if the salt leaks.
3. **Binance creds** → `.env` only, READ-ONLY keys.
4. **`footer_url`** → only a public website (e.g. enkapital.xyz). Never a
   wallet/account/explorer URL.
5. **No secrets in logs**: the Telegram token is redacted by
   `_SecretRedactionFilter`; `_safe_telegram_error` strips URLs.

## Privacy (HL)

6. PnL $ / % / win-rate stay EXACT. Price/size/notional/timestamps are fuzzed.
7. Master switch `privacy_enabled: false` restores 100% real (rollback path).
8. **No real HL price/size may appear on any public surface** — dashboard grid,
   events table, open orders, raw WS payload, or chart. The events-table leak
   was fixed 2026-08-04 (`_recent_event_payload`).
9. The frozen anchor is seeded at OPEN and reused open→close so a scaled-into
   position looks identical across all surfaces.

## Accounting

10. Each fill's PnL is recorded **at most once**. (TOCTOU dedup race fixed
    2026-08-04 — keys claimed synchronously before any await.)
11. `PARTIAL` rows keep a round-trip open; any non-`PARTIAL` row closes it.
12. A closed trade and a later reopen of the same ticker are NEVER summed.
13. Unknown PnL is never silently treated as zero.
14. One display card/tile/chart bar/stats entry per fully-closed round-trip.
15. The FULL close card's figure = the whole round-trip total and matches the
    tile + chart + stats.
16. HL close cards re-sum from exchange `closedPnl` when scale-outs exist.
17. Stats use CLOSED trades only (non-None pnl).
18. Filter fills by window FIRST, then aggregate round-trips.
19. Reconcile `--apply` is scoped (only rows `ts >= T0`), backup-first,
    idempotent, and requires `bootstrap_markets()` first.
20. `architecture_v2/` must stay inert — no production import/write path, no
    consumer activation, no migration run without explicit approval.

## Delivery

21. A failed execution chart or album send must still deliver the PnL card
    (and a failed card still delivers the text alert) — exactly one safe fallback.
22. Notification states are recoverable: `pending`/`failed` reclaimable after
    5 min, `sent` never re-sent.
23. Telegram captions/logs/errors never contain secrets or wallet addresses.

## Operations

24. `sudo systemctl restart lighterbot` requires explicit user OK.
25. Every deployment is backup-first and reversible.
26. Four service health endpoints must return 200 (8080/healthz, 8810, 8811,
    8800/api/status).
27. SQLite integrity checks must stay `ok`.
