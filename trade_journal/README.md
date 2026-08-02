# Trade Journal

The standalone Trade Journal reviews complete position lifecycles independently
from Signal Research and the TVL Monitor.

## Current behavior

- reads Tracker execution/event facts through the Journal synchronization path;
- reconstructs active and closed position lifecycles;
- keeps rapid fills as auditable execution batches;
- distinguishes entries, scale-ins, partial exits, full exits, and reversals;
- displays live unrealized PnL for active positions and full realized PnL for
  completed lifecycles;
- stores reasons, custom reasons, notes, decisions, and links in
  `data/trading_journal.db`;
- supports editing, text/status/direction/link filters, and click sorting;
- does not run a full ingestion simply because the browser page is refreshed.

Tracker facts and user journal annotations have separate ownership. The V2 plan
will replace the current full-scan synchronization with an incremental,
idempotent projection feed while preserving annotations.

## Run

From the repository root:

```powershell
& ".\.venv\Scripts\python.exe" -B -m trade_journal.app --host 127.0.0.1 --port 8811
```

Open <http://127.0.0.1:8811/>.

## V2 review mode

The V2 consumer is read-only and reversible. Enable it for the Journal with:

```powershell
$env:JOURNAL_UI_MODE = "v2"
& ".\.venv\Scripts\python.exe" -B -m trade_journal.app --host 127.0.0.1 --port 8811
```

Then open <http://127.0.0.1:8811/> and use **V2 review**. The legacy Journal
remains available through **Legacy view**. For a one-off preview, open
<http://127.0.0.1:8811/?ui=v2> without changing the environment. The V2 route
is `GET /api/v2/bootstrap`; it translates lifecycle rows into immutable V2
chart specs and does not write V2 tables or alter accounting.

Optional explicit paths:

```powershell
& ".\.venv\Scripts\python.exe" -B -m trade_journal.app `
  --command-db ".\data\command_center.db" `
  --journal-db ".\data\trading_journal.db"
```

The application performs background synchronization and also exposes an
explicit **Sync now** action. `GET /api/bootstrap` is read-only.

## Test

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -q tests/test_trade_journal.py
```

Lifecycle reconstruction coverage also lives in
`tests/test_command_center.py`, `tests/test_realization_repair.py`, and the
architecture V2 fixtures as they are added.
