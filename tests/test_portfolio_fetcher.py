import asyncio

import httpx

from src.portfolio_fetcher import (
    ChainConfig,
    LIGHTER_REST_BASE,
    TokenCatalog,
    LIT_COINGECKO_ID,
    _merge_core_token_targets,
    _parse_lighter_account,
    _rpc_batch,
    _parse_lighter_staking_entry,
    _summarize_lighter_lit,
    _token_price,
    build_token_targets,
    fetch_evm_chain,
    fetch_lighter,
    fetch_hyperliquid,
    group_targets_by_chain,
    mask_address,
    normalize_address,
)


def test_token_catalog_keeps_last_good_catalog_after_rate_limit():
    market_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal market_calls
        if request.url.path.endswith("/coins/markets"):
            market_calls += 1
            if market_calls > 1:
                return httpx.Response(429)
            return httpx.Response(200, json=[
                {"id": "ethereum", "symbol": "eth", "name": "Ethereum", "current_price": 2000, "market_cap_rank": 2},
                {"id": "usd-coin", "symbol": "usdc", "name": "USD Coin", "current_price": 1, "market_cap_rank": 7},
            ])
        if request.url.path.endswith("/simple/price"):
            return httpx.Response(200, json={})
        if request.url.path.endswith("/coins/list"):
            return httpx.Response(200, json=[
                {"id": "usd-coin", "platforms": {"ethereum": "0x0000000000000000000000000000000000000001"}},
            ])
        return httpx.Response(404)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            catalog = TokenCatalog(ttl_seconds=0)
            first = await catalog.get(client)
            second = await catalog.get(client)
            return first, second

    first, second = asyncio.run(_run())
    assert first["source"] == "coingecko_top_200"
    assert second["source"] == "coingecko_top_200"
    assert second["targets"] == first["targets"]
    assert second["stale"] is True
    assert any("429" in error for error in second["errors"])


def test_rpc_batch_retries_rate_limit_once():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        payload = __import__("json").loads(request.content.decode())
        return httpx.Response(200, json=[{
            "jsonrpc": "2.0",
            "id": payload[0]["id"],
            "result": "0x0",
        }])

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await _rpc_batch(client, "https://rpc.test", [{
                "id": "native",
                "method": "eth_getBalance",
                "params": ["0xabc", "latest"],
            }])

    result = asyncio.run(_run())
    assert calls == 2
    assert result["native"]["result"] == "0x0"




def test_rpc_batch_retries_ids_silently_omitted_by_public_rpc():
    batch_sizes = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content.decode())
        batch_sizes.append(len(payload))
        returned = payload[:2]
        return httpx.Response(200, json=[
            {"jsonrpc": "2.0", "id": item["id"], "result": "0x0"}
            for item in returned
        ])

    calls = [
        {"id": f"call:{index}", "method": "eth_getBalance", "params": ["0xabc", "latest"]}
        for index in range(7)
    ]

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await _rpc_batch(client, "https://rpc.test", calls)

    result = asyncio.run(_run())
    assert set(result) == {f"call:{index}" for index in range(7)}
    assert batch_sizes[0] == 7
    assert len(batch_sizes) > 1


def test_fetch_evm_chain_does_not_guess_failed_token_decimals():
    chain = ChainConfig("test", "Test", 1, "ETH", "ethereum", "https://rpc.test")
    target = {
        "id": "usd-coin",
        "symbol": "USDC",
        "name": "USD Coin",
        "contract": "0x0000000000000000000000000000000000000001",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content.decode())
        rows = []
        for call in payload:
            if call["id"] == "native":
                rows.append({"jsonrpc": "2.0", "id": call["id"], "result": "0x0"})
            elif call["id"].startswith("bal:"):
                rows.append({"jsonrpc": "2.0", "id": call["id"], "result": "0xf4240"})
            else:
                rows.append({"jsonrpc": "2.0", "id": call["id"], "error": {"code": -32000, "message": "reverted"}})
        return httpx.Response(200, json=rows)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_evm_chain(client, chain, "0xabc", [target], {"ethereum": 2000.0, "usd-coin": 1.0})

    result = asyncio.run(_run())
    assert result["tokens"] == []
    assert result["ok"] is True
    assert "decimals unavailable for USDC" in result["error"]


def test_token_price_uses_native_price_for_wrapped_eth_fallback():
    target = {"id": "weth", "symbol": "WETH", "price_usd": None}
    assert _token_price(target, {"ethereum": 2345.67}) == 2345.67


def test_fetch_evm_chain_marks_nonzero_unpriced_token_as_degraded():
    chain = ChainConfig("test", "Test", 1, "ETH", "ethereum", "https://rpc.test")
    target = {
        "id": "mystery",
        "symbol": "MYST",
        "name": "Mystery",
        "contract": "0x0000000000000000000000000000000000000001",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content.decode())
        rows = []
        for call in payload:
            result = "0x0"
            if call["id"].startswith("bal:"):
                result = "0xde0b6b3a7640000"
            elif call["id"].startswith("dec:"):
                result = "0x12"
            rows.append({"jsonrpc": "2.0", "id": call["id"], "result": result})
        return httpx.Response(200, json=rows)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_evm_chain(client, chain, "0xabc", [target], {"ethereum": 2000.0})

    result = asyncio.run(_run())
    assert result["tokens"][0]["balance"] == 1.0
    assert result["tokens"][0]["value_usd"] is None
    assert "price unavailable for MYST" in result["error"]


def test_normalize_address_extracts_from_stream_suffix():
    raw = "0x2222222222222222222222222222222222222222/stream"
    assert normalize_address(raw) == "0x2222222222222222222222222222222222222222"


def test_mask_address_is_stable():
    assert mask_address("0x2222222222222222222222222222222222222222") == "0x2222...2222"


def test_build_token_targets_keeps_supported_evm_platforms_only():
    markets = [
        {
            "id": "usd-coin",
            "symbol": "usdc",
            "name": "USD Coin",
            "current_price": 1,
            "market_cap_rank": 7,
            "price_change_percentage_24h": 0.01,
        },
        {
            "id": "solana",
            "symbol": "sol",
            "name": "Solana",
            "current_price": 100,
            "market_cap_rank": 5,
        },
    ]
    coin_list = [
        {
            "id": "usd-coin",
            "platforms": {
                "ethereum": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                "arbitrum-one": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                "base": "0x833589fcd6edb6e08f4c7c32d4f71b54bdA02913",
                "polygon-pos": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
                "hyperevm": "0xb88339cb7199b77e23db6e890353e22632ba630f",
                "solana": "not-evm",
            },
        },
        {
            "id": "solana",
            "platforms": {"solana": "So11111111111111111111111111111111111111112"},
        },
    ]

    targets = build_token_targets(markets, coin_list)
    assert {(t["chain"], t["symbol"]) for t in targets} == {
        ("ethereum", "USDC"),
        ("arbitrum", "USDC"),
        ("base", "USDC"),
        ("polygon", "USDC"),
        ("hyperevm", "USDC"),
    }
    assert all(t["price_usd"] == 1 for t in targets)




def test_build_token_targets_excludes_erc20_compatible_native_system_contracts():
    markets = [
        {"id": "polygon-ecosystem-token", "symbol": "pol", "name": "POL", "current_price": 0.1},
        {"id": "celo", "symbol": "celo", "name": "Celo", "current_price": 0.5},
    ]
    coin_list = [
        {
            "id": "polygon-ecosystem-token",
            "platforms": {"polygon-pos": "0x0000000000000000000000000000000000001010"},
        },
        {
            "id": "celo",
            "platforms": {"celo": "0x471ece3750da237f93b8e339c536989b8978a438"},
        },
    ]

    assert build_token_targets(markets, coin_list) == []




def test_merge_core_targets_fills_metadata_gaps_without_duplicates():
    base_usdc = {
        "chain": "base",
        "contract": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "id": "usd-coin",
        "symbol": "USDC",
        "name": "USD Coin",
        "price_usd": 0.999,
        "rank": 7,
        "change_24h": 0.0,
    }
    merged = _merge_core_token_targets([base_usdc])
    keys = [(target["chain"], target["contract"].lower()) for target in merged]

    assert len(keys) == len(set(keys))
    assert merged[0] is base_usdc
    assert ("hyperevm", "0xb88339cb7199b77e23db6e890353e22632ba630f") in keys
    assert ("polygon", "0xc2132d05d31c914a87c6611c10748aeb04b58e8f") in keys
    assert ("bnb", "0x55d398326f99059ff775485246999027b3197955") in keys


def test_group_targets_by_chain_includes_empty_supported_chains():
    grouped = group_targets_by_chain([
        {
            "chain": "ethereum",
            "contract": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "symbol": "USDC",
        }
    ])
    assert len(grouped["ethereum"]) == 1
    assert grouped["arbitrum"] == []
    assert grouped["bnb"] == []
    assert grouped["hyperevm"] == []


def test_parse_lighter_staking_entry_values_lit_when_price_known():
    entry = {
        "timestamp": 1760000000000,
        "staked_lit": 123.5,
        "staking_pnl": "4.25",
        "staking_inflow": "130",
        "staking_outflow": "6.5",
    }
    parsed = _parse_lighter_staking_entry(entry, lit_price_usd=2.0)
    assert parsed["staked_lit"] == 123.5
    assert parsed["staked_lit_value_usd"] == 247.0
    assert parsed["staking_pnl"] == 4.25
    assert parsed["staking_inflow"] == 130.0
    assert parsed["staking_outflow"] == 6.5

def test_parse_lighter_account_prices_lit_spot_and_locked_balances():
    parsed = _parse_lighter_account(
        {
            "assets": [
                {
                    "symbol": "LIT",
                    "asset_id": 2,
                    "balance": "10",
                    "locked_balance": "4",
                    "margin_balance": "0",
                    "margin_mode": "disabled",
                }
            ]
        },
        prices={LIT_COINGECKO_ID: 2.5},
    )

    asset = parsed["assets"][0]
    assert asset["balance"] == 10.0
    assert asset["spot_balance"] == 10.0
    assert asset["available_balance"] == 6.0
    assert asset["locked_balance"] == 4.0
    assert asset["price_usd"] == 2.5
    assert asset["value_usd"] == 25.0
    assert asset["spot_value_usd"] == 25.0
    assert asset["available_value_usd"] == 15.0
    assert asset["locked_value_usd"] == 10.0


def test_parse_lighter_account_shapes_pending_unlocks_for_lit_only():
    parsed = _parse_lighter_account(
        {
            "assets": [{"symbol": "LIT", "asset_id": 2, "balance": "100", "locked_balance": "40"}],
            "pending_unlocks": [
                {"unlock_timestamp": 1770000000, "asset_index": 2, "amount": "12.5"},
                {"unlock_timestamp": 1771000000, "asset_index": 2, "amount": "7.5"},
                {"unlock_timestamp": 1772000000, "asset_index": 3, "amount": "999"},  # USDC, dropped
                {"unlock_timestamp": 1773000000, "asset_index": 2, "amount": "0"},  # zero, dropped
            ],
        },
        prices={LIT_COINGECKO_ID: 2.0},
    )

    unlocks = parsed["pending_unlocks_lit"]
    assert unlocks == [
        {"unlock_timestamp": 1770000000, "amount": 12.5},
        {"unlock_timestamp": 1771000000, "amount": 7.5},
    ]
    # raw pending_unlocks preserved
    assert len(parsed["pending_unlocks"]) == 4


def test_parse_lighter_account_shapes_pool_deposits_from_shares():
    parsed = _parse_lighter_account(
        {
            "assets": [{"symbol": "USDC", "asset_id": 3, "balance": "0", "margin_balance": "15000"}],
            "shares": [
                {
                    "public_pool_index": 281474976624800,
                    "shares_amount": 1455601695,
                    "entry_usdc": "0",
                    "principal_amount": "14999.99999592",
                    "entry_timestamp": 0,
                },
                {"public_pool_index": 1, "shares_amount": 0, "principal_amount": "0"},  # empty, dropped
            ],
        },
        prices={LIT_COINGECKO_ID: 2.0},
    )

    deposits = parsed["pool_deposits"]
    assert len(deposits) == 1
    assert deposits[0]["public_pool_index"] == 281474976624800
    assert deposits[0]["principal_amount"] == 14999.99999592
    assert deposits[0]["shares_amount"] == 1455601695.0
    assert deposits[0]["entry_timestamp"] == 0


def test_summarize_lighter_lit_no_staking_when_nothing_locked():
    summary = _summarize_lighter_lit(
        [
            {
                "assets": [
                    {
                        "symbol": "LIT",
                        "balance": 10.0,
                        "spot_balance": 10.0,
                        "locked_balance": 0.0,
                    }
                ]
            }
        ],
        lit_price_usd=2.5,
    )

    assert summary["total_lit"] == 10.0
    assert summary["spot_lit"] == 10.0  # all free
    assert summary["free_lit"] == 10.0
    assert summary["locked_lit"] == 0.0
    assert summary["staked_lit"] == 0.0
    assert summary["total_value_usd"] == 25.0
    assert summary["staked_value_usd"] == 0.0
    assert summary["staking_source"] == "none"


def test_summarize_lighter_lit_staked_is_pool_shares_not_locked_balance():
    # Staked LIT = public-pool share value; it lives in the POOL account, not
    # in the user's balance, so it is additive. locked_balance is only an
    # informational slice of the spot balance (exchange lock, not staking).
    summary = _summarize_lighter_lit(
        [{"assets": [{"symbol": "LIT", "balance": 10.0, "locked_balance": 4.0}]}],
        lit_price_usd=2.5,
        pool_staked_lit=20.0,
    )

    assert summary["staked_lit"] == 20.0  # from pool shares
    assert summary["spot_lit"] == 10.0  # full balance (locked is inside it)
    assert summary["free_lit"] == 6.0
    assert summary["locked_lit"] == 4.0
    assert summary["total_lit"] == 30.0  # spot + pool staked, disjoint
    assert summary["staked_value_usd"] == 50.0
    assert summary["spot_value_usd"] == 25.0
    assert summary["total_value_usd"] == 75.0
    assert summary["staking_source"] == "public_pool_shares"


def test_summarize_lighter_lit_aggregates_multiple_accounts():
    summary = _summarize_lighter_lit(
        [
            {"assets": [{"symbol": "LIT", "balance": 10.0, "locked_balance": 4.0}]},
            {"assets": [{"symbol": "LIT", "balance": 5.0, "locked_balance": 5.0}]},
            {"assets": [{"symbol": "USDC", "balance": 100.0, "locked_balance": 0.0}]},
        ],
        lit_price_usd=2.0,
        pool_staked_lit=5.0,
    )

    assert summary["total_lit"] == 20.0  # 15 balance + 5 pool staked
    assert summary["staked_lit"] == 5.0
    assert summary["locked_lit"] == 9.0
    assert summary["free_lit"] == 6.0
    assert summary["total_value_usd"] == 40.0


# ---- fetch_lighter end-to-end (mocked transport, models live acct 7402) ----

_ACCT_7402 = {
    "index": 7402,
    "account_index": 7402,
    "name": "Main",
    "total_asset_value": "16012.065693",
    "collateral": "15502.005026",
    "available_balance": "40.0",
    "assets": [
        {"symbol": "LIT", "asset_id": 2, "balance": "4596.35725740",
         "locked_balance": "4596.35000000", "margin_balance": "0"},
        {"symbol": "USDC", "asset_id": 3, "balance": "49.64132536604",
         "locked_balance": "0", "margin_balance": "15502.005026415099"},
    ],
    "positions": [],
    "shares": [
        {"public_pool_index": 281474976624800, "shares_amount": 1455601695,
         "entry_usdc": "0", "principal_amount": "14999.99999592", "entry_timestamp": 0}
    ],
    "pending_unlocks": [
        {"unlock_timestamp": 1780000000, "asset_index": 2, "amount": "100.0"}
    ],
}


# The LIT staking pool (account_type 4), modeled on live pool 281474976624800:
# user 1,455,601,695 shares of 11,721,402,144,143 over 121,138,057.9 LIT
# = 15,043.316511928593 LIT (matches DeBank's "LIT Staking" exactly).
_POOL_LIT_STAKING = {
    "index": 281474976624800,
    "account_index": 281474976624800,
    "account_type": 4,
    "total_asset_value": "0",
    "collateral": "0",
    "assets": [
        {"symbol": "LIT", "asset_id": 2, "balance": "121138057.90665943",
         "locked_balance": "0", "margin_balance": "0"},
    ],
    "pool_info": {"total_shares": 11721402144143, "operator_shares": 10000000000},
    "shares": [],
    "pending_unlocks": [],
}

_EXPECTED_POOL_LIT = 1455601695 * 121138057.90665943 / 11721402144143  # 15043.3165...


def _lighter_mock_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/accountsByL1Address"):
        return httpx.Response(200, json={"sub_accounts": [{"index": 7402}], "next_cursor": None})
    if path.endswith("/account"):
        value = request.url.params.get("value")
        if value == "281474976624800":
            return httpx.Response(200, json={"accounts": [_POOL_LIT_STAKING]})
        return httpx.Response(200, json={"accounts": [_ACCT_7402]})
    if path.endswith("/pnl"):
        # /pnl 400s for this account (code 21100) -- must NOT zero staked.
        return httpx.Response(400, json={"code": 21100, "message": "account not found"})
    return httpx.Response(404, json={})


def _run_fetch_lighter():
    transport = httpx.MockTransport(_lighter_mock_handler)

    async def _run():
        async with httpx.AsyncClient(transport=transport, base_url="https://x") as client:
            return await fetch_lighter(
                client,
                "0x1234567890abcdef1234567890abcdef1234c0de",
                {LIT_COINGECKO_ID: 2.2},
            )

    return asyncio.run(_run())


def test_fetch_lighter_staked_lit_from_pool_shares_pnl_400_does_not_zero_it():
    result = _run_fetch_lighter()
    stk = result["staking"]
    # staked = pool share value (user_shares/total_shares x pool LIT balance),
    # unaffected by the /pnl 400.
    assert round(stk["staked_lit"], 2) == round(_EXPECTED_POOL_LIT, 2)  # ~15043.32
    assert stk["source"] == "public_pool_shares"
    assert stk["pnl_source"] == "unavailable"
    assert round(stk["staked_lit_value_usd"], 0) == round(_EXPECTED_POOL_LIT * 2.2, 0)
    # locked_balance is surfaced but is NOT the staked amount
    assert round(stk["locked_lit"], 2) == 4596.35
    # /pnl 400 is swallowed (400 not surfaced as an error)
    assert result["errors"] == []


def test_fetch_lighter_totals_no_double_count():
    result = _run_fetch_lighter()
    account_assets = result["account_assets_usd"]
    lit_usd = result["lit_assets_usd"]
    # account_assets_usd == total_asset_value (exchange USDC side; excludes
    # both the LIT balance and the pool share position).
    assert round(account_assets, 2) == 16012.07
    # lit_assets_usd covers spot balance + pool-staked LIT, each once.
    expected_lit_usd = (4596.35725740 + _EXPECTED_POOL_LIT) * 2.2
    assert round(lit_usd, 2) == round(expected_lit_usd, 2)
    # The only pool is the LIT staking pool -> no separate non-LIT pool USD.
    assert result["pool_deposits_usd"] == 0.0
    # total = exchange + LIT (spot+staked) + non-LIT pools; nothing overlaps.
    assert round(result["total_usd"], 4) == round(account_assets + lit_usd, 4)


def test_fetch_lighter_shapes_pending_unlocks_and_pool_deposits():
    result = _run_fetch_lighter()
    stk = result["staking"]
    assert stk["pending_unlocks"] == [{"unlock_timestamp": 1780000000, "amount": 100.0}]
    assert stk["pending_unstake_lit"] == 100.0
    assert round(stk["pending_unstake_lit_value_usd"], 1) == 220.0
    pools = result["pool_deposits"]
    assert len(pools) == 1
    assert pools[0]["public_pool_index"] == 281474976624800
    assert round(pools[0]["principal_amount"], 2) == 15000.0  # 15,000 LIT principal
    assert pools[0]["is_lit_staking"] is True
    assert round(pools[0]["staked_lit"], 2) == round(_EXPECTED_POOL_LIT, 2)
    assert pools[0]["underlying"][0]["symbol"] == "LIT"


def test_fetch_lighter_non_lit_pool_counts_as_pool_deposit_usd():
    # A USDC public pool must roll into pool_deposits_usd, not staked LIT.
    usdc_pool = {
        "index": 999, "account_index": 999, "account_type": 4,
        "assets": [{"symbol": "USDC", "asset_id": 3, "balance": "1000000", "locked_balance": "0", "margin_balance": "0"}],
        "pool_info": {"total_shares": 1000000},
    }
    acct = dict(_ACCT_7402)
    acct["shares"] = [{"public_pool_index": 999, "shares_amount": 5000, "principal_amount": "5000", "entry_timestamp": 0}]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/accountsByL1Address"):
            return httpx.Response(200, json={"sub_accounts": [{"index": 7402}], "next_cursor": None})
        if path.endswith("/account"):
            if request.url.params.get("value") == "999":
                return httpx.Response(200, json={"accounts": [usdc_pool]})
            return httpx.Response(200, json={"accounts": [acct]})
        if path.endswith("/pnl"):
            return httpx.Response(400, json={"code": 21100, "message": "account not found"})
        return httpx.Response(404, json={})

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x") as client:
            return await fetch_lighter(client, "0xabc", {LIT_COINGECKO_ID: 2.2})

    result = asyncio.run(_run())
    # 5000/1000000 x 1,000,000 USDC = 5000 USDC
    assert round(result["pool_deposits_usd"], 2) == 5000.0
    assert result["staking"]["staked_lit"] == 0.0
    assert result["pool_deposits"][0]["is_lit_staking"] is False
    # USDC pool value IS added to the lighter total (not inside account tav)
    expected = result["account_assets_usd"] + (result["lit_assets_usd"] or 0.0) + 5000.0
    assert round(result["total_usd"], 4) == round(expected, 4)





def test_fetch_lighter_account_style_pool_uses_total_asset_value():
    pool = {
        'index': 999,
        'account_index': 999,
        'account_type': 2,
        'name': 'NK pool',
        'assets': [{'symbol': 'USDC', 'asset_id': 3, 'balance': '0', 'margin_balance': '7000'}],
        'total_asset_value': '7000',
        'collateral': '7800',
        'pool_info': {'total_shares': 10000},
    }
    acct = {
        'index': 7402,
        'account_index': 7402,
        'assets': [{'symbol': 'USDC', 'asset_id': 3, 'balance': '0', 'margin_balance': '5000'}],
        'total_asset_value': '5000',
        'shares': [{'public_pool_index': 999, 'shares_amount': 2500, 'principal_amount': '3000', 'entry_timestamp': 0}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith('/accountsByL1Address'):
            return httpx.Response(200, json={'sub_accounts': [{'index': 7402}], 'next_cursor': None})
        if path.endswith('/account'):
            if request.url.params.get('value') == '999':
                return httpx.Response(200, json={'accounts': [pool]})
            return httpx.Response(200, json={'accounts': [acct]})
        if path.endswith('/pnl'):
            return httpx.Response(400, json={'code': 21100, 'message': 'account not found'})
        return httpx.Response(404, json={})

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://x') as client:
            return await fetch_lighter(client, '0xabc', {LIT_COINGECKO_ID: 2.2})

    result = asyncio.run(_run())
    dep = result['pool_deposits'][0]
    assert round(dep['value_usd'], 2) == 1750.0
    assert dep['pool_name'] == 'NK pool'
    assert dep['value_source'] == 'pool_total_asset_value'
    assert dep['underlying'] == [{'symbol': 'USDC', 'amount': 1750.0, 'value_usd': 1750.0, 'source': 'pool_total_asset_value'}]
    assert round(result['pool_deposits_usd'], 2) == 1750.0
    assert round(result['total_usd'], 2) == 6750.0


def test_fetch_hyperliquid_unified_mode_uses_spot_equity_and_surfaces_upnl_breakdown():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content.decode())
        if payload["type"] == "clearinghouseState":
            return httpx.Response(
                200,
                json={
                    "marginSummary": {
                        "accountValue": "15000",
                        "totalNtlPos": "50000",
                        "totalRawUsd": "12345",
                        "totalMarginUsed": "2500",
                    },
                    "withdrawable": "9000",
                    "assetPositions": [
                        {"position": {"coin": "BTC", "szi": "1", "entryPx": "50000", "positionValue": "60000", "unrealizedPnl": "10000", "returnOnEquity": "1", "marginUsed": "3000"}},
                        {"position": {"coin": "ETH", "szi": "-2", "entryPx": "2500", "positionValue": "4500", "unrealizedPnl": "-5000", "returnOnEquity": "-1", "marginUsed": "1000"}},
                    ],
                },
            )
        if payload["type"] == "spotClearinghouseState":
            return httpx.Response(200, json={"balances": [{"coin": "USDC", "total": "1000", "hold": "0", "entryNtl": "0"}]})
        if payload["type"] == "allMids":
            return httpx.Response(200, json={"@107": "20"})
        if payload["type"] == "spotMeta":
            return httpx.Response(200, json={"tokens": [], "universe": []})
        if payload["type"] == "userAbstraction":
            return httpx.Response(200, json="unifiedAccount")
        if payload["type"] == "userVaultEquities":
            return httpx.Response(200, json=[])
        if payload["type"] == "delegatorSummary":
            return httpx.Response(200, json={"delegated": "0", "undelegated": "0", "totalPendingWithdrawal": "0"})
        if payload["type"] == "userRole":
            return httpx.Response(200, json={"role": "user"})
        if payload["type"] == "perpDexs":
            return httpx.Response(200, json=[None])
        return httpx.Response(400, json={})

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x") as client:
            return await fetch_hyperliquid(client, "0xabc", {})

    result = asyncio.run(_run())
    perp = result["perp"]
    assert result["total_usd"] == 1000.0
    assert perp["account_value"] == 15000.0
    assert perp["total_unrealized_pnl"] == 5000.0
    assert perp["balance_without_upnl"] == 10000.0
    assert perp["equity_check"] == 15000.0
    assert perp["positions"][0]["margin_used"] == 3000.0
    assert result["spot"]["total_usd"] == 1000.0
    assert result["total_source"] == "unified_spot_balances"
    assert result["account_mode"] == "unifiedAccount"
    assert result["perp_equity_usd"] == 15000.0


def test_fetch_hyperliquid_falls_back_to_perp_equity_when_no_spot_balance():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content.decode())
        if payload["type"] == "clearinghouseState":
            return httpx.Response(200, json={"marginSummary": {"accountValue": "5000"}, "assetPositions": []})
        if payload["type"] == "spotClearinghouseState":
            return httpx.Response(200, json={"balances": []})
        if payload["type"] == "allMids":
            return httpx.Response(200, json={})
        if payload["type"] == "spotMeta":
            return httpx.Response(200, json={"tokens": [], "universe": []})
        if payload["type"] == "userAbstraction":
            return httpx.Response(200, json="disabled")
        if payload["type"] == "userVaultEquities":
            return httpx.Response(200, json=[])
        if payload["type"] == "delegatorSummary":
            return httpx.Response(200, json={"delegated": "0", "undelegated": "0", "totalPendingWithdrawal": "0"})
        if payload["type"] == "userRole":
            return httpx.Response(200, json={"role": "user"})
        if payload["type"] == "perpDexs":
            return httpx.Response(200, json=[None])
        return httpx.Response(400, json={})

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x") as client:
            return await fetch_hyperliquid(client, "0xabc", {})

    result = asyncio.run(_run())
    assert result["total_usd"] == 5000.0
    assert result["total_source"] == "perp_account_value"


def test_fetch_hyperliquid_standard_mode_adds_perp_spot_vault_and_staking():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content.decode())
        kind = payload["type"]
        if kind == "clearinghouseState":
            value = "250" if payload.get("dex") == "xyz" else "5000"
            return httpx.Response(200, json={"marginSummary": {"accountValue": value}, "assetPositions": []})
        if kind == "spotClearinghouseState":
            return httpx.Response(200, json={"balances": [
                {"coin": "USDC", "total": "1000", "hold": "0"},
                {"coin": "ABC", "total": "2", "hold": "0"},
            ]})
        if kind == "spotMeta":
            return httpx.Response(200, json={
                "tokens": [
                    {"name": "USDC", "index": 0},
                    {"name": "ABC", "index": 9},
                    {"name": "HYPE", "index": 150},
                ],
                "universe": [
                    {"name": "ABC/USDC", "index": 7, "tokens": [9, 0]},
                    {"name": "HYPE/USDC", "index": 107, "tokens": [150, 0]},
                ],
            })
        if kind == "allMids":
            return httpx.Response(200, json={"@7": "3", "@107": "20"})
        if kind == "userAbstraction":
            return httpx.Response(200, json="disabled")
        if kind == "userVaultEquities":
            return httpx.Response(200, json=[{"vaultAddress": "0xvault", "equity": "250"}])
        if kind == "delegatorSummary":
            return httpx.Response(200, json={"delegated": "10", "undelegated": "2", "totalPendingWithdrawal": "3"})
        if kind == "userRole":
            return httpx.Response(200, json={"role": "user"})
        if kind == "perpDexs":
            return httpx.Response(200, json=[None, {"name": "xyz"}])
        return httpx.Response(400, json={})

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x") as client:
            return await fetch_hyperliquid(client, "0xabc", {"hyperliquid": 99.0})

    result = asyncio.run(_run())
    assert result["account_mode"] == "disabled"
    assert result["spot"]["total_usd"] == 1006.0
    assert result["spot"]["balances"][1]["price_usd"] == 3.0
    assert result["direct_equity_usd"] == 6256.0
    assert result["perp"]["dexes"] == [
        {"dex": "default", "account_value": 5000.0},
        {"dex": "xyz", "account_value": 250.0},
    ]
    assert result["vaults"]["total_usd"] == 250.0
    assert result["staking"]["hype"] == 15.0
    assert result["staking"]["total_usd"] == 300.0
    assert result["total_usd"] == 6806.0
    assert result["total_source"] == "standard_spot_plus_perp+vaults+staking"


def test_fetch_hyperliquid_agent_address_and_unpriced_spot_are_visible():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content.decode())
        kind = payload["type"]
        if kind == "clearinghouseState":
            return httpx.Response(200, json={"marginSummary": {"accountValue": "0"}, "assetPositions": []})
        if kind == "spotClearinghouseState":
            return httpx.Response(200, json={"balances": [{"coin": "MYSTERY", "total": "10", "hold": "0"}]})
        if kind == "allMids":
            return httpx.Response(200, json={})
        if kind == "spotMeta":
            return httpx.Response(200, json={"tokens": [], "universe": []})
        if kind == "userAbstraction":
            return httpx.Response(200, json="default")
        if kind == "userVaultEquities":
            return httpx.Response(200, json=[])
        if kind == "delegatorSummary":
            return httpx.Response(200, json={"delegated": "0", "undelegated": "0", "totalPendingWithdrawal": "0"})
        if kind == "userRole":
            return httpx.Response(200, json={"role": "agent"})
        if kind == "perpDexs":
            return httpx.Response(200, json=[None])
        return httpx.Response(400, json={})

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x") as client:
            return await fetch_hyperliquid(client, "0xagent", {})

    result = asyncio.run(_run())
    assert result["user_role"] == "agent"
    assert result["unpriced_spot_symbols"] == ["MYSTERY"]
    assert any("agent wallet" in error for error in result["errors"])
    assert any("spot pricing unavailable for: MYSTERY" in error for error in result["errors"])


def test_fetch_lighter_bulk_loads_large_operator_and_counts_only_operator_share():
    account_requests = []
    pnl_indexes = []

    main = {"index": 1, "account_type": 0, "total_asset_value": "100", "assets": [], "positions": [], "shares": []}
    sub = {"index": 2, "account_type": 1, "total_asset_value": "50", "assets": [], "positions": [], "shares": []}
    pool = {
        "index": 1001,
        "account_type": 2,
        "name": "Owned pool",
        "total_asset_value": "1000",
        "assets": [{"symbol": "USDC", "balance": "1000"}],
        "positions": [],
        "shares": [],
        "pool_info": {"total_shares": 100, "operator_shares": 10},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/accountsByL1Address"):
            summaries = [{"index": 1, "account_type": 0}, {"index": 2, "account_type": 1}]
            summaries.extend({"index": 1000 + i, "account_type": 2} for i in range(23))
            return httpx.Response(200, json={"sub_accounts": summaries, "next_cursor": None})
        if path.endswith("/account"):
            account_requests.append(dict(request.url.params))
            if request.url.params.get("by") == "l1_address":
                return httpx.Response(200, json={"accounts": [main, sub, pool]})
            return httpx.Response(500, json={})
        if path.endswith("/pnl"):
            pnl_indexes.append(request.url.params.get("value"))
            return httpx.Response(400, json={"code": 21100})
        return httpx.Response(404, json={})

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x") as client:
            return await fetch_lighter(client, "0xabc", {})

    result = asyncio.run(_run())
    assert account_requests == [{"by": "l1_address", "value": "0xabc", "active_only": "true"}]
    assert pnl_indexes == ["1", "2"]
    assert result["account_count"] == 3
    assert result["account_assets_usd"] == 250.0
    owned = next(account for account in result["accounts"] if account["account_type"] == 2)
    assert owned["operator_share_value_usd"] == 100.0


def test_fetch_lighter_reports_cursor_cycle_instead_of_silent_truncation():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        path = request.url.path
        if path.endswith("/accountsByL1Address"):
            calls += 1
            cursor = request.url.params.get("cursor")
            next_cursor = "a" if cursor in (None, "b") else "b"
            return httpx.Response(200, json={
                "sub_accounts": [{"index": 1}],
                "next_cursor": next_cursor,
            })
        if path.endswith("/account"):
            return httpx.Response(200, json={"accounts": [{
                "index": 1,
                "total_asset_value": "100",
                "assets": [],
                "positions": [],
                "shares": [],
            }]})
        if path.endswith("/pnl"):
            return httpx.Response(400, json={"code": 21100})
        return httpx.Response(404, json={})

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x") as client:
            return await fetch_lighter(client, "0xabc", {})

    result = asyncio.run(_run())
    assert calls == 3
    assert any("repeated cursor" in error for error in result["errors"])


def test_fetch_lighter_reports_page_cap_and_deduplicates_detailed_accounts():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/accountsByL1Address"):
            cursor = int(request.url.params.get("cursor", "0"))
            return httpx.Response(200, json={
                "sub_accounts": [{"index": 1}, {"index": 2}],
                "next_cursor": str(cursor + 1),
            })
        if path.endswith("/account"):
            return httpx.Response(200, json={"accounts": [{
                "index": 42,
                "total_asset_value": "100",
                "assets": [],
                "positions": [],
                "shares": [],
            }]})
        if path.endswith("/pnl"):
            return httpx.Response(400, json={"code": 21100})
        return httpx.Response(404, json={})

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x") as client:
            return await fetch_lighter(client, "0xabc", {})

    result = asyncio.run(_run())
    assert result["account_count"] == 1
    assert result["total_usd"] == 100.0
    assert any("20 pages" in error for error in result["errors"])


def test_fetch_lighter_mixed_lit_usdc_pool_counts_each_asset_once():
    mixed_pool = {
        "index": 999,
        "account_index": 999,
        "account_type": 4,
        "assets": [
            {"symbol": "LIT", "asset_id": 2, "balance": "1000"},
            {"symbol": "USDC", "asset_id": 3, "balance": "5000"},
        ],
        "pool_info": {"total_shares": 1000},
    }
    account = {
        "index": 7,
        "account_index": 7,
        "total_asset_value": "100",
        "assets": [],
        "shares": [{"public_pool_index": 999, "shares_amount": 100, "principal_amount": "100"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accountsByL1Address"):
            return httpx.Response(200, json={"sub_accounts": [{"index": 7}]})
        if request.url.path.endswith("/account"):
            selected = mixed_pool if request.url.params.get("value") == "999" else account
            return httpx.Response(200, json={"accounts": [selected]})
        if request.url.path.endswith("/pnl"):
            return httpx.Response(400, json={})
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_lighter(client, "0xabc", {LIT_COINGECKO_ID: 2.0})

    result = asyncio.run(run())
    deposit = result["pool_deposits"][0]
    assert deposit["staked_lit"] == 100.0
    assert deposit["non_lit_value_usd"] == 500.0
    assert result["pool_deposits_usd"] == 500.0
    assert result["total_usd"] == 100.0 + 200.0 + 500.0


def test_fetch_lighter_rejects_impossible_pool_share_ratio():
    pool = {
        "index": 999,
        "assets": [{"symbol": "USDC", "balance": "1000"}],
        "pool_info": {"total_shares": 100},
    }
    account = {
        "index": 7,
        "total_asset_value": "10",
        "assets": [],
        "shares": [{"public_pool_index": 999, "shares_amount": 101, "principal_amount": "10"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accountsByL1Address"):
            return httpx.Response(200, json={"sub_accounts": [{"index": 7}]})
        if request.url.path.endswith("/account"):
            selected = pool if request.url.params.get("value") == "999" else account
            return httpx.Response(200, json={"accounts": [selected]})
        if request.url.path.endswith("/pnl"):
            return httpx.Response(400, json={})
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_lighter(client, "0xabc", {})

    result = asyncio.run(run())
    assert result["pool_deposits"][0]["value_usd"] is None
    assert result["pool_deposits_usd"] == 0.0
    assert any("invalid share ratio" in error for error in result["errors"])
