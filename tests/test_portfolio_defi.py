from __future__ import annotations

import asyncio

import httpx
from eth_abi import encode

from src import portfolio_defi


ADDRESS = "0x1111111111111111111111111111111111111111"


def test_morpho_parses_market_and_vault_positions():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {
            "c1": {
                "marketPositions": [{
                    "market": {
                        "marketId": "market-1",
                        "loanAsset": {"address": "0x1", "symbol": "USDC", "decimals": 6},
                        "collateralAsset": {"address": "0x2", "symbol": "WETH", "decimals": 18},
                        "lltv": "860000000000000000",
                    },
                    "state": {
                        "supplyAssets": "100", "supplyAssetsUsd": 100,
                        "borrowAssets": "25", "borrowAssetsUsd": 25,
                        "collateral": "2", "collateralUsd": 4000,
                    },
                }],
                "vaultPositions": [{
                    "vault": {
                        "address": "0x2222222222222222222222222222222222222222",
                        "name": "USDC Vault", "symbol": "mUSDC",
                        "asset": {"address": "0x3", "symbol": "USDC", "decimals": 6},
                    },
                    "state": {"assets": "500", "assetsUsd": 500, "shares": "490"},
                }],
                "vaultV2Positions": [],
            },
            "c10": None, "c137": None, "c999": None, "c8453": None, "c42161": None,
        }})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await portfolio_defi.fetch_morpho(client, ADDRESS)

    result = asyncio.run(run())
    assert result["ok"] is True
    assert result["gross_assets_usd"] == 4600
    assert result["borrowed_usd"] == 25
    assert result["total_usd"] == 4575
    assert result["receipt_tokens"] == [{
        "chain": "ethereum",
        "address": "0x2222222222222222222222222222222222222222",
    }]


def test_aave_empty_user_stops_after_ui_call(monkeypatch):
    calls = []

    async def fake_call(client, rpc_url, to, data):
        calls.append((to, data))
        return "0x" + encode(
            ["(address,uint256,uint256,bool)[]", "uint8"],
            [[], 0],
        ).hex()

    monkeypatch.setattr(portfolio_defi, "_rpc_call", fake_call)
    deployment = portfolio_defi.SPARK_DEPLOYMENTS[0]
    result = asyncio.run(portfolio_defi._fetch_aave_deployment(None, ADDRESS, deployment, "https://rpc"))
    assert result == ([], [])
    assert len(calls) == 1


def test_aave_active_position_uses_current_balances_and_debt(monkeypatch):
    asset = "0x3333333333333333333333333333333333333333"
    a_token = "0x4444444444444444444444444444444444444444"
    pool = "0x5555555555555555555555555555555555555555"
    data_provider = "0x6666666666666666666666666666666666666666"
    oracle = "0x7777777777777777777777777777777777777777"
    first = True

    async def fake_call(client, rpc_url, to, data):
        nonlocal first
        if first:
            first = False
            return "0x" + encode(
                ["(address,uint256,uint256,bool)[]", "uint8"],
                [[(asset, 1, 1, True)], 0],
            ).hex()
        if to.lower() == pool.lower():
            return "0x" + encode(["uint256"] * 6, [0, 0, 0, 0, 0, 2 * 10**18]).hex()
        if to.lower() == oracle.lower():
            return "0x" + encode(["uint256"], [10**8]).hex()
        raise AssertionError((to, data))

    async def fake_batch(client, rpc_url, calls):
        if len(calls) == 3:
            return ["0x" + encode(["address"], [value]).hex() for value in (pool, data_provider, oracle)]
        assert len(calls) == 5
        return [
            "0x" + encode(
                ["uint256", "uint256", "uint256", "uint256", "uint256", "uint256", "uint256", "uint40", "bool"],
                [100 * 10**6, 10 * 10**6, 5 * 10**6, 0, 0, 0, 0, 0, True],
            ).hex(),
            "0x" + encode(["address", "address", "address"], [a_token, portfolio_defi.ZERO_ADDRESS, portfolio_defi.ZERO_ADDRESS]).hex(),
            "0x" + encode(["uint8"], [6]).hex(),
            "0x" + encode(["string"], ["USDC"]).hex(),
            "0x" + encode(["uint256"], [10**8]).hex(),
        ]

    monkeypatch.setattr(portfolio_defi, "_rpc_call", fake_call)
    monkeypatch.setattr(portfolio_defi, "_rpc_batch", fake_batch)
    deployment = portfolio_defi.SPARK_DEPLOYMENTS[0]
    positions, receipts = asyncio.run(
        portfolio_defi._fetch_aave_deployment(None, ADDRESS, deployment, "https://rpc")
    )
    assert positions[0]["supplied_usd"] == 100
    assert positions[0]["borrowed_usd"] == 15
    assert positions[0]["total_usd"] == 85
    assert positions[0]["collateral_usd"] == 100
    assert positions[0]["health_factor"] == 2
    assert receipts == [{"chain": "ethereum", "address": a_token}]


def test_receipt_token_dedup_recomputes_chain_total():
    chains = [{
        "key": "ethereum",
        "total_usd": 125,
        "native": {"value_usd": 20},
        "tokens": [
            {"contract": "0xaaa", "symbol": "aUSDC", "value_usd": 100},
            {"contract": "0xbbb", "symbol": "USDC", "value_usd": 5},
        ],
    }]
    removed = portfolio_defi.remove_receipt_token_duplicates(
        chains, [{"chain": "ethereum", "address": "0xAAA"}]
    )
    assert removed == 100
    assert chains[0]["total_usd"] == 25
    assert [token["symbol"] for token in chains[0]["tokens"]] == ["USDC"]


def test_protocol_failure_isolated_from_other_protocol(monkeypatch):
    async def bad_morpho(client, address):
        result = portfolio_defi._empty_protocol("morpho", "Morpho")
        result["errors"] = ["offline"]
        return portfolio_defi._sum_protocol(result)

    async def compatible(client, address, deployments, rpc_by_chain, *, key, name):
        result = portfolio_defi._empty_protocol(key, name)
        if key == "aave":
            result["positions"] = [{
                "total_usd": 75, "gross_assets_usd": 100, "supplied_usd": 100,
                "collateral_usd": 100, "borrowed_usd": 25,
            }]
        return portfolio_defi._sum_protocol(result)

    async def empty_spark_assets(client, address, rpc_by_chain):
        return portfolio_defi._sum_protocol(portfolio_defi._empty_protocol("spark", "Spark"))

    monkeypatch.setattr(portfolio_defi, "fetch_morpho", bad_morpho)
    monkeypatch.setattr(portfolio_defi, "fetch_aave_compatible", compatible)
    monkeypatch.setattr(portfolio_defi, "fetch_spark_assets", empty_spark_assets)
    result = asyncio.run(portfolio_defi.fetch_defi(None, ADDRESS, {}))
    assert result["ok"] is False
    assert result["total_usd"] == 75
    assert result["errors"] == ["offline"]



def test_spark_erc4626_position_uses_redeemable_underlying(monkeypatch):
    deployment = portfolio_defi.SparkAssetDeployment(
        "Spark Savings test", "ethereum", "Ethereum",
        "0x2222222222222222222222222222222222222222",
    )
    underlying = "0x3333333333333333333333333333333333333333"
    batches = iter([
        ["0x" + encode(["uint256"], [2 * 10**18]).hex()],
        [
            "0x" + encode(["uint8"], [18]).hex(),
            "0x" + encode(["string"], ["spUSDC"]).hex(),
            "0x" + encode(["address"], [underlying]).hex(),
            "0x" + encode(["uint256"], [2_200_000]).hex(),
        ],
        [
            "0x" + encode(["uint8"], [6]).hex(),
            "0x" + encode(["string"], ["USDC"]).hex(),
        ],
    ])

    async def fake_batch(client, rpc_url, calls):
        return next(batches)

    monkeypatch.setattr(portfolio_defi, "_rpc_batch", fake_batch)
    positions, receipts = asyncio.run(
        portfolio_defi._fetch_spark_asset_chain(None, ADDRESS, [deployment], "https://rpc")
    )
    assert positions[0]["shares"] == 2
    assert positions[0]["supplied"] == 2.2
    assert positions[0]["asset"] == "USDC"
    assert positions[0]["position_type"] == "savings"
    assert receipts == [{"chain": "ethereum", "address": deployment.address.lower()}]


def test_spark_staking_is_one_to_one_with_spk(monkeypatch):
    deployment = portfolio_defi.SparkAssetDeployment(
        "Spark Staking", "ethereum", "Ethereum",
        "0x2222222222222222222222222222222222222222",
        position_type="staking",
        underlying="0x3333333333333333333333333333333333333333",
    )
    batches = iter([
        ["0x" + encode(["uint256"], [125 * 10**18]).hex()],
        [
            "0x" + encode(["uint8"], [18]).hex(),
            "0x" + encode(["string"], ["stSPK"]).hex(),
        ],
        [
            "0x" + encode(["uint8"], [18]).hex(),
            "0x" + encode(["string"], ["SPK"]).hex(),
        ],
    ])

    async def fake_batch(client, rpc_url, calls):
        return next(batches)

    monkeypatch.setattr(portfolio_defi, "_rpc_batch", fake_batch)
    positions, _ = asyncio.run(
        portfolio_defi._fetch_spark_asset_chain(None, ADDRESS, [deployment], "https://rpc")
    )
    assert positions[0]["supplied"] == 125
    assert positions[0]["shares"] == 125
    assert positions[0]["asset"] == "SPK"
    assert positions[0]["position_type"] == "staking"


def test_spark_stablecoin_price_fallback(monkeypatch):
    deployment = portfolio_defi.SparkAssetDeployment(
        "Spark Savings test", "ethereum", "Ethereum",
        "0x2222222222222222222222222222222222222222",
    )
    position = {
        "market": deployment.product, "position_type": "savings",
        "chain": "ethereum", "chain_name": "Ethereum", "asset": "USDC",
        "underlying_address": "0x3333333333333333333333333333333333333333",
        "supplied": 250.0, "supplied_usd": 0.0, "collateral_usd": 0.0,
        "borrowed_usd": 0.0, "gross_assets_usd": 0.0, "total_usd": 0.0,
    }

    async def fake_chain(client, address, deployments, rpc_url):
        return [dict(position)], [{"chain": "ethereum", "address": deployment.address.lower()}]

    async def no_prices(client, positions):
        return {}

    monkeypatch.setattr(portfolio_defi, "SPARK_ASSET_DEPLOYMENTS", (deployment,))
    monkeypatch.setattr(portfolio_defi, "_fetch_spark_asset_chain", fake_chain)
    monkeypatch.setattr(portfolio_defi, "_fetch_defillama_prices", no_prices)
    result = asyncio.run(
        portfolio_defi.fetch_spark_assets(None, ADDRESS, {"ethereum": "https://rpc"})
    )
    assert result["ok"] is True
    assert result["total_usd"] == 250
    assert result["positions"][0]["price_source"] == "stablecoin-parity-fallback"
