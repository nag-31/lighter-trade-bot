"""App-level tests for the portfolio webapp using aiohttp's in-process TestClient.

Network fetches are stubbed so refresh jobs run fully offline.
"""

import asyncio

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

import src.portfolio_app as portfolio_app
from src.portfolio_app import create_app


VALID_ADDR = "0x2222222222222222222222222222222222222222"
VALID_ADDR_2 = "0x1111111111111111111111111111111111111111"


def _run(coro):
    return asyncio.run(coro)


def _fake_payload(address, total=100.0, status="ok"):
    return {
        "address": address,
        "address_masked": address[:6] + "…" + address[-4:],
        "timestamp": "2026-01-01T00:00:00+00:00",
        "status": status,
        "totals": {
            "total_usd": total,
            "chains_usd": total,
            "lighter_usd": 0.0,
            "hyperliquid_usd": 0.0,
            "lit_staked": 0.0,
            "lit_staked_usd": 0.0,
        },
        "chains": [],
        "lighter": {"ok": True, "errors": [], "total_usd": 0.0, "accounts": []},
        "hyperliquid": {"ok": True, "errors": [], "total_usd": 0.0},
        "token_catalog": {"source": "test", "target_count": 1},
        "errors": [],
    }


async def _client(tmp_path, monkeypatch):
    async def fake_fetch(address, *, catalog=None):
        return _fake_payload(address)

    monkeypatch.setattr(portfolio_app, "fetch_address_portfolio", fake_fetch)

    db = tmp_path / "portfolio.db"
    static = tmp_path / "portfolio_static"
    app = create_app(db, static_dir=static)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


def test_index_returns_503_when_frontend_missing(tmp_path, monkeypatch):
    async def go():
        client = await _client(tmp_path, monkeypatch)
        try:
            resp = await client.get("/")
            assert resp.status == 503
            assert resp.headers["Cache-Control"] == "no-cache, must-revalidate"
            body = await resp.text()
            assert "frontend not built" in body.lower()
        finally:
            await client.close()
    _run(go())


def test_index_serves_disk_file_when_present(tmp_path, monkeypatch):
    async def go():
        client = await _client(tmp_path, monkeypatch)
        try:
            (tmp_path / "portfolio_static" / "index.html").write_text(
                "<h1>hello dashboard</h1>", encoding="utf-8"
            )
            resp = await client.get("/")
            assert resp.status == 200
            assert "hello dashboard" in await resp.text()
        finally:
            await client.close()
    _run(go())


def test_static_serving(tmp_path, monkeypatch):
    async def go():
        client = await _client(tmp_path, monkeypatch)
        try:
            (tmp_path / "portfolio_static" / "app.js").write_text(
                "console.log('ok');", encoding="utf-8"
            )
            resp = await client.get("/static/app.js")
            assert resp.status == 200
            assert "console.log" in await resp.text()
        finally:
            await client.close()
    _run(go())


def test_post_address_reenables_disabled_and_accepts_suffix(tmp_path, monkeypatch):
    async def go():
        client = await _client(tmp_path, monkeypatch)
        try:
            r = await client.post("/api/addresses", json={"address": VALID_ADDR, "label": "main"})
            first = (await r.json())["address"]
            aid = first["id"]

            r = await client.delete("/api/addresses/%s" % aid)
            assert r.status == 200

            r = await client.get("/api/summary")
            assert (await r.json())["addresses"] == []

            r = await client.post("/api/addresses", json={"address": VALID_ADDR + "/stream", "label": "stream"})
            data = await r.json()
            assert r.status == 200
            assert data["address"]["id"] == aid
            assert data["address"]["enabled"] == 1
            assert data["address"]["address"] == VALID_ADDR.lower()
            assert data["address"]["label"] == "stream"

            r = await client.get("/api/summary")
            rows = (await r.json())["addresses"]
            assert [row["id"] for row in rows] == [aid]
        finally:
            await client.close()
    _run(go())

def test_patch_label_and_excluded(tmp_path, monkeypatch):
    async def go():
        client = await _client(tmp_path, monkeypatch)
        try:
            r = await client.post("/api/addresses", json={"address": VALID_ADDR, "label": "main"})
            addr = (await r.json())["address"]
            aid = addr["id"]

            r = await client.patch(f"/api/addresses/{aid}", json={"excluded": True, "label": "renamed"})
            data = await r.json()
            assert r.status == 200
            assert data["address"]["excluded"] is True
            assert data["address"]["label"] == "renamed"

            # 404 for unknown id.
            r = await client.patch("/api/addresses/99999", json={"excluded": True})
            assert r.status == 404
        finally:
            await client.close()
    _run(go())


def test_summary_totals_included_excludes_excluded(tmp_path, monkeypatch):
    async def go():
        client = await _client(tmp_path, monkeypatch)
        try:
            r = await client.post("/api/addresses", json={"address": VALID_ADDR})
            a1 = (await r.json())["address"]["id"]
            r = await client.post("/api/addresses", json={"address": VALID_ADDR_2})
            a2 = (await r.json())["address"]["id"]

            await client.post(f"/api/addresses/{a1}/refresh")
            await client.post(f"/api/addresses/{a2}/refresh")

            # Exclude the second address.
            await client.patch(f"/api/addresses/{a2}", json={"excluded": True})

            r = await client.get("/api/summary")
            s = await r.json()
            assert s["totals"]["total_usd"] == 200.0
            assert s["totals_included"]["total_usd"] == 100.0
            by_id = {row["id"]: row for row in s["addresses"]}
            assert by_id[a1]["excluded"] is False
            assert by_id[a2]["excluded"] is True
        finally:
            await client.close()
    _run(go())


def test_address_history_ascending(tmp_path, monkeypatch):
    async def go():
        client = await _client(tmp_path, monkeypatch)
        try:
            r = await client.post("/api/addresses", json={"address": VALID_ADDR})
            aid = (await r.json())["address"]["id"]
            await client.post(f"/api/addresses/{aid}/refresh")
            await client.post(f"/api/addresses/{aid}/refresh")

            r = await client.get(f"/api/addresses/{aid}/history?limit=300")
            data = await r.json()
            assert data["ok"] is True
            assert len(data["history"]) == 2
            ids = [h["id"] for h in data["history"]]
            assert ids == sorted(ids)  # ascending
            assert "payload" not in data["history"][0]
            assert data["history"][0]["total_usd"] == 100.0
        finally:
            await client.close()
    _run(go())


def test_aggregate_history_endpoint(tmp_path, monkeypatch):
    async def go():
        client = await _client(tmp_path, monkeypatch)
        try:
            r = await client.post("/api/addresses", json={"address": VALID_ADDR})
            aid = (await r.json())["address"]["id"]
            await client.post(f"/api/addresses/{aid}/refresh")
            r = await client.get("/api/history?limit=1000")
            data = await r.json()
            assert data["ok"] is True
            assert data["history"][-1]["total_usd"] == 100.0
        finally:
            await client.close()
    _run(go())


def test_refresh_job_lifecycle_and_409(tmp_path, monkeypatch):
    async def go():
        client = await _client(tmp_path, monkeypatch)
        try:
            r = await client.post("/api/addresses", json={"address": VALID_ADDR})
            await r.json()

            r = await client.post("/api/refresh")
            data = await r.json()
            assert r.status == 200
            assert data == {"ok": True, "started": True}

            # Poll status until finished.
            for _ in range(100):
                r = await client.get("/api/refresh/status")
                st = await r.json()
                if not st["running"] and st["finished_at"]:
                    break
                await asyncio.sleep(0.02)

            assert st["ok"] is True
            assert st["running"] is False
            assert st["total"] == 1
            assert st["completed"] == 1
            assert st["current_address_id"] is None
            assert st["started_at"] is not None
            assert st["finished_at"] is not None
            assert st["results"] == [{"address_id": 1, "status": "ok"}]
        finally:
            await client.close()
    _run(go())


def test_refresh_job_409_when_running(tmp_path, monkeypatch):
    async def go():
        # Slow fetch so the job stays running while we hit it a second time.
        async def slow_fetch(address, *, catalog=None):
            await asyncio.sleep(0.3)
            return _fake_payload(address)

        monkeypatch.setattr(portfolio_app, "fetch_address_portfolio", slow_fetch)
        db = tmp_path / "portfolio.db"
        app = create_app(db, static_dir=tmp_path / "portfolio_static")
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            r = await client.post("/api/addresses", json={"address": VALID_ADDR})
            await r.json()

            r1 = await client.post("/api/refresh")
            assert (await r1.json())["started"] is True

            r2 = await client.post("/api/refresh")
            assert r2.status == 409
            assert (await r2.json()) == {"ok": False, "error": "refresh already running"}
        finally:
            await client.close()
    _run(go())


def test_import_pasted_csv_wallets(tmp_path, monkeypatch):
    async def go():
        client = await _client(tmp_path, monkeypatch)
        try:
            pasted = (
                "address,label\n"
                f"{VALID_ADDR},Main\n"
                f"{VALID_ADDR_2},Trading\n"
                f"{VALID_ADDR},Duplicate\n"
                "not-an-address,Bad\n"
            )
            response = await client.post(
                "/api/addresses/import",
                json={"text": pasted},
            )
            data = await response.json()
            assert response.status == 200
            assert data["imported"] == 2
            assert data["skipped"] == 1
            assert [row["label"] for row in data["addresses"]] == ["Main", "Trading"]

            summary = await client.get("/api/summary")
            assert len((await summary.json())["addresses"]) == 2
        finally:
            await client.close()
    _run(go())


def test_failed_refresh_keeps_last_good_value_and_marks_stale(tmp_path, monkeypatch):
    async def go():
        calls = 0

        async def sometimes_fails(address, *, catalog=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _fake_payload(address, total=250.0)
            raise RuntimeError("upstream unavailable")

        monkeypatch.setattr(portfolio_app, "fetch_address_portfolio", sometimes_fails)
        app = create_app(tmp_path / "portfolio.db", static_dir=tmp_path / "static")
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            added = await client.post("/api/addresses", json={"address": VALID_ADDR})
            address_id = (await added.json())["address"]["id"]
            await client.post(f"/api/addresses/{address_id}/refresh")
            await client.post(f"/api/addresses/{address_id}/refresh")

            response = await client.get("/api/summary")
            summary = await response.json()
            wallet = summary["addresses"][0]
            assert wallet["latest"]["status"] == "error"
            assert wallet["latest"]["stale"] is True
            assert wallet["snapshot"]["totals"]["total_usd"] == 250.0
            assert summary["totals_included"]["total_usd"] == 250.0

            history = await client.get(f"/api/addresses/{address_id}/history")
            assert [point["total_usd"] for point in (await history.json())["history"]] == [250.0]
        finally:
            await client.close()
    _run(go())


def test_refresh_job_reports_degraded_payload_status(tmp_path, monkeypatch):
    async def degraded_fetch(address, *, catalog=None):
        return _fake_payload(address, total=90.0, status="degraded")

    async def go():
        monkeypatch.setattr(portfolio_app, "fetch_address_portfolio", degraded_fetch)
        app = create_app(tmp_path / "portfolio.db", static_dir=tmp_path / "static")
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await client.post("/api/addresses", json={"address": VALID_ADDR})
            await client.post("/api/refresh")
            for _ in range(100):
                state = await (await client.get("/api/refresh/status")).json()
                if not state["running"]:
                    break
                await asyncio.sleep(0.01)
            assert state["results"] == [{"address_id": 1, "status": "degraded"}]
        finally:
            await client.close()
    _run(go())


def test_csv_import_handles_bom_quotes_and_is_atomic_over_limit(tmp_path, monkeypatch):
    async def go():
        client = await _client(tmp_path, monkeypatch)
        try:
            pasted = (
                "\ufeffaddress,label\n"
                f'"{VALID_ADDR}","Main, Treasury"\n'
                "\n"
            )
            response = await client.post("/api/addresses/import", json={"text": pasted})
            body = await response.json()
            assert response.status == 200
            assert body["skipped"] == 0
            assert body["addresses"][0]["label"] == "Main, Treasury"

            too_many = "address,label\n" + "\n".join(
                f"0x{index:040x},Wallet {index}" for index in range(1, 1002)
            )
            response = await client.post("/api/addresses/import", json={"text": too_many})
            assert response.status == 400
            summary = await (await client.get("/api/summary")).json()
            assert len(summary["addresses"]) == 1
        finally:
            await client.close()
    _run(go())


def test_history_empty_and_unknown_selection_contribute_nothing(tmp_path, monkeypatch):
    async def go():
        client = await _client(tmp_path, monkeypatch)
        try:
            added = await client.post("/api/addresses", json={"address": VALID_ADDR})
            address_id = (await added.json())["address"]["id"]
            await client.post(f"/api/addresses/{address_id}/refresh")
            empty = await (await client.get("/api/history?address_ids=")).json()
            unknown = await (await client.get("/api/history?address_ids=999999")).json()
            assert empty["history"] == []
            assert unknown["history"] == []
        finally:
            await client.close()
    _run(go())


def test_refresh_100_wallets_uses_bounded_concurrency_and_persists_all(tmp_path, monkeypatch):
    active = 0
    max_active = 0
    calls = []

    async def measured_fetch(address, *, catalog=None):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        calls.append(address)
        try:
            await asyncio.sleep(0.005)
            return _fake_payload(address, total=1.0)
        finally:
            active -= 1

    async def go():
        monkeypatch.setattr(portfolio_app, "fetch_address_portfolio", measured_fetch)
        app = create_app(tmp_path / "portfolio.db", static_dir=tmp_path / "static")
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            csv_text = "address,label\n" + "\n".join(
                f"0x{index:040x},Wallet {index}" for index in range(1, 101)
            )
            imported = await client.post("/api/addresses/import", json={"text": csv_text})
            assert imported.status == 200
            assert (await imported.json())["imported"] == 100

            started = await client.post("/api/refresh")
            assert (await started.json())["started"] is True
            for _ in range(500):
                state = await (await client.get("/api/refresh/status")).json()
                assert len(state["current_address_ids"]) <= portfolio_app.REFRESH_CONCURRENCY
                if not state["running"]:
                    break
                await asyncio.sleep(0.01)

            assert state["running"] is False
            assert state["total"] == 100
            assert state["completed"] == 100
            assert [item["address_id"] for item in state["results"]] == list(range(1, 101))
            assert len(calls) == 100
            assert 1 < max_active <= portfolio_app.REFRESH_CONCURRENCY

            summary = await (await client.get("/api/summary")).json()
            assert len(summary["addresses"]) == 100
            assert summary["totals_included"]["total_usd"] == 100.0
        finally:
            await client.close()

    _run(go())



def test_guest_mode_is_stateless_and_hides_legacy_routes(tmp_path, monkeypatch):
    async def go():
        async def fake_fetch(address, *, catalog=None):
            return _fake_payload(address)

        monkeypatch.setattr(portfolio_app, "fetch_address_portfolio", fake_fetch)
        db = tmp_path / "must-not-exist.db"
        app = create_app(db, storage_mode="guest", static_dir=tmp_path / "static")
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            config_response = await client.get("/api/config")
            config = await config_response.json()
            assert config["storage_mode"] == "guest"
            assert config["history_location"] == "browser"
            assert config_response.headers["Cache-Control"] == "private, no-store, max-age=0"
            assert config_response.headers["Referrer-Policy"] == "no-referrer"
            assert not db.exists()

            assert (await client.get("/api/summary")).status == 404
            assert (await client.post("/api/addresses", json={"address": VALID_ADDR})).status == 404
            assert not db.exists()
        finally:
            await client.close()

    _run(go())


def test_guest_refresh_normalizes_deduplicates_and_never_persists(tmp_path, monkeypatch):
    calls = []

    async def fake_fetch(address, *, catalog=None):
        calls.append(address)
        return _fake_payload(address, total=321.0)

    async def go():
        monkeypatch.setattr(portfolio_app, "fetch_address_portfolio", fake_fetch)
        db = tmp_path / "must-not-exist.db"
        app = create_app(db, storage_mode="guest", static_dir=tmp_path / "static")
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/api/guest/refresh",
                json={"addresses": [VALID_ADDR + "/stream", VALID_ADDR]},
            )
            body = await response.json()
            assert response.status == 200
            assert body["ok"] is True
            assert len(body["results"]) == 1
            assert body["results"][0]["address"] == VALID_ADDR
            assert body["results"][0]["payload"]["totals"]["total_usd"] == 321.0
            assert calls == [VALID_ADDR]
            assert not db.exists()
        finally:
            await client.close()

    _run(go())


def test_guest_refresh_rejects_cross_origin_and_large_batches(tmp_path, monkeypatch):
    async def fake_fetch(address, *, catalog=None):
        return _fake_payload(address)

    async def go():
        monkeypatch.setattr(portfolio_app, "fetch_address_portfolio", fake_fetch)
        app = create_app(
            tmp_path / "unused.db",
            storage_mode="guest",
            static_dir=tmp_path / "static",
            allowed_origins={"https://portfolio.example"},
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            blocked = await client.post(
                "/api/guest/refresh",
                json={"addresses": [VALID_ADDR]},
                headers={"Origin": "https://attacker.example"},
            )
            assert blocked.status == 403

            addresses = ["0x" + format(index, "040x") for index in range(1, 27)]
            oversized = await client.post(
                "/api/guest/refresh",
                json={"addresses": addresses},
                headers={"Origin": "https://portfolio.example"},
            )
            assert oversized.status == 400
            assert "25" in (await oversized.json())["error"]
        finally:
            await client.close()

    _run(go())


def test_password_hash_round_trip_and_private_configuration_validation(tmp_path):
    encoded = portfolio_app.hash_password("a-long-private-password")
    assert encoded.startswith("pbkdf2_sha256$")
    assert portfolio_app._password_matches("a-long-private-password", encoded)
    assert not portfolio_app._password_matches("wrong-password", encoded)

    try:
        create_app(tmp_path / "missing.db", storage_mode="private")
    except ValueError as exc:
        assert "PASSWORD_HASH" in str(exc)
    else:
        raise AssertionError("private mode accepted missing credentials")


def test_private_mode_requires_login_and_persists_server_data(tmp_path, monkeypatch):
    async def go():
        async def fake_fetch(address, *, catalog=None):
            return _fake_payload(address, total=432.1)

        monkeypatch.setattr(portfolio_app, "fetch_address_portfolio", fake_fetch)
        static = tmp_path / "static"
        static.mkdir()
        (static / "index.html").write_text("<h1>private dashboard</h1>", encoding="utf-8")
        (static / "login.html").write_text("<h1>sign in</h1>", encoding="utf-8")
        encoded = portfolio_app.hash_password("correct-horse-battery-staple")
        app = create_app(
            tmp_path / "private.db",
            storage_mode="private",
            static_dir=static,
            auth_password_hash=encoded,
            session_secret="s" * 48,
        )
        client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            redirect = await client.get("/", allow_redirects=False)
            assert redirect.status == 302
            assert redirect.headers["Location"] == "/login"
            assert (await client.get("/api/summary")).status == 401
            assert "sign in" in await (await client.get("/login")).text()

            denied = await client.post(
                "/api/auth/login", json={"password": "not-the-password"}
            )
            assert denied.status == 401

            accepted = await client.post(
                "/api/auth/login",
                json={"password": "correct-horse-battery-staple"},
            )
            assert accepted.status == 200
            cookie = accepted.headers["Set-Cookie"]
            assert "HttpOnly" in cookie
            assert "SameSite=Strict" in cookie

            config = await (await client.get("/api/config")).json()
            assert config["storage_mode"] == "private"
            assert config["history_location"] == "server"
            assert config["auto_refresh_max_age_seconds"] == 86400

            added = await client.post("/api/addresses", json={"address": VALID_ADDR})
            assert added.status == 200
            refreshed = await client.post(
                "/api/addresses/1/refresh"
            )
            assert refreshed.status == 200
            summary = await (await client.get("/api/summary")).json()
            assert summary["totals"]["total_usd"] == 432.1
            assert (tmp_path / "private.db").exists()

            assert (await client.post("/api/auth/logout")).status == 200
            assert (await client.get("/api/summary")).status == 401
        finally:
            await client.close()

    _run(go())


def test_private_login_rejects_cross_origin(tmp_path):
    async def go():
        static = tmp_path / "static"
        static.mkdir()
        app = create_app(
            tmp_path / "private.db",
            storage_mode="private",
            static_dir=static,
            allowed_origins={"https://private-portfolio.example"},
            auth_password_hash=portfolio_app.hash_password("correct-horse-battery-staple"),
            session_secret="s" * 48,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/api/auth/login",
                json={"password": "correct-horse-battery-staple"},
                headers={"Origin": "https://attacker.example"},
            )
            assert response.status == 403
        finally:
            await client.close()

    _run(go())
