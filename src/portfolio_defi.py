"""Wallet lending positions from authoritative protocol APIs and contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable

import httpx
from eth_abi import decode, encode
from eth_utils import keccak


MORPHO_API = "https://api.morpho.org/graphql"
DEFILLAMA_PRICE_API = "https://coins.llama.fi/prices/current"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True)
class LendingDeployment:
    protocol: str
    market: str
    chain_key: str
    chain_name: str
    provider: str
    ui_provider: str


@dataclass(frozen=True)
class SparkAssetDeployment:
    product: str
    chain_key: str
    chain_name: str
    address: str
    position_type: str = "savings"
    underlying: str | None = None


AAVE_DEPLOYMENTS: tuple[LendingDeployment, ...] = (
    LendingDeployment("aave", "Aave V3 Core", "ethereum", "Ethereum", "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e", "0x2dAd8162A989cd99D673dE4425Bb2298Db1E1aA2"),
    LendingDeployment("aave", "Aave V3 Lido", "ethereum", "Ethereum", "0xcfBf336fe147D643B9Cb705648500e101504B16d", "0x2dAd8162A989cd99D673dE4425Bb2298Db1E1aA2"),
    LendingDeployment("aave", "Aave V3 EtherFi", "ethereum", "Ethereum", "0xeBa440B438Ad808101d1c451C1C5322c90BEFCdA", "0x2dAd8162A989cd99D673dE4425Bb2298Db1E1aA2"),
    LendingDeployment("aave", "Aave V3", "base", "Base", "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D", "0x0C6BC4a12039788be08F87e87Cff87FEDbd1D386"),
    LendingDeployment("aave", "Aave V3", "arbitrum", "Arbitrum", "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb", "0x91E04cf78e53aEBe609e8a7f2003e7EECD743F2B"),
    LendingDeployment("aave", "Aave V3", "optimism", "Optimism", "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb", "0x68100bD5345eA474D93577127C11F39FF8463e93"),
    LendingDeployment("aave", "Aave V3", "polygon", "Polygon", "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb", "0x66E1aBdb06e7363a618D65a910c540dfED23754f"),
    LendingDeployment("aave", "Aave V3", "bnb", "BNB Chain", "0xff75B6da14FfbbfD355Daf7a2731456b3562Ba6D", "0x68100bD5345eA474D93577127C11F39FF8463e93"),
    LendingDeployment("aave", "Aave V3", "avalanche", "Avalanche", "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb", "0xFBa4Df643205c5400BC3e05a1E67E0dFaEeeb41F"),
    LendingDeployment("aave", "Aave V3", "gnosis", "Gnosis", "0x36616cf17557639614c1cdDb356b1B83fc0B2132", "0x0C6BC4a12039788be08F87e87Cff87FEDbd1D386"),
    LendingDeployment("aave", "Aave V3", "celo", "Celo", "0x9F7Cf9417D5251C59fE94fB9147feEe1aAd9Cea5", "0xc851e6147dcE6A469CC33BE3121b6B2D4CaD2763"),
    LendingDeployment("aave", "Aave V3", "linea", "Linea", "0x89502c3731F69DDC95B65753708A07F8Cd0373F4", "0xc851e6147dcE6A469CC33BE3121b6B2D4CaD2763"),
    LendingDeployment("aave", "Aave V3", "scroll", "Scroll", "0x69850D0B276776781C063771b161bd8894BCdD04", "0xE28E2c8d240dd5eBd0adcab86fbD79df7a052034"),
    LendingDeployment("aave", "Aave V3", "zksync", "zkSync Era", "0x2A3948BB219D6B2Fa83D64100006391a96bE6cb7", "0x756Ff6722543F12d25396Ea646B0F2C96dA70c3e"),
    LendingDeployment("aave", "Aave V3", "mantle", "Mantle", "0xba50Cd2A20f6DA35D788639E581bca8d0B5d4D5f", "0xc851e6147dcE6A469CC33BE3121b6B2D4CaD2763"),
    LendingDeployment("aave", "Aave V3", "metis", "Metis", "0xB9FABd7500B2C6781c35Dd48d54f81fc2299D7AF", "0x5c5228aC8BC1528482514aF3e27E692495148717"),
    LendingDeployment("aave", "Aave V3", "sonic", "Sonic", "0x5C2e738F6E27bCE0F7558051Bf90605dD6176900", "0x4F3F69979ED28c962028582B1760E98B1a117097"),
)

SPARK_DEPLOYMENTS: tuple[LendingDeployment, ...] = (
    LendingDeployment("spark", "SparkLend", "ethereum", "Ethereum", "0x02C3eA4e34C0cBd694D2adFa2c690EECbC1793eE", "0xF028c2F4b19898718fD0F77b9b881CbfdAa5e8Bb"),
)

# Wallet-facing Spark products from sparkdotfi/spark-address-registry. The
# Liquidity Layer itself is intentionally absent: its ALMProxy owns protocol
# treasury assets, not positions attributable to the wallet being scanned.
SPARK_ASSET_DEPLOYMENTS: tuple[SparkAssetDeployment, ...] = (
    SparkAssetDeployment("Spark Savings sUSDC (legacy)", "ethereum", "Ethereum", "0xBc65ad17c5C0a2A4D159fa5a503f4992c7B545FE"),
    SparkAssetDeployment("Spark Savings sUSDS", "ethereum", "Ethereum", "0xa3931d71877C0E7a3148CB7Eb4463524FEc27fbD"),
    SparkAssetDeployment("Spark Savings spETH", "ethereum", "Ethereum", "0xfE6eb3b609a7C8352A241f7F3A21CEA4e9209B8f"),
    SparkAssetDeployment("Spark Savings spPYUSD", "ethereum", "Ethereum", "0x80128DbB9f07b93DDE62A6daeadb69ED14a7D354"),
    SparkAssetDeployment("Spark Savings spUSDC", "ethereum", "Ethereum", "0x28B3a8fb53B741A8Fd78c0fb9A6B2393d896a43d"),
    SparkAssetDeployment("Spark Savings spUSDT", "ethereum", "Ethereum", "0xe2e7a17dFf93280dec073C995595155283e3C372"),
    SparkAssetDeployment("Spark Savings sUSDS", "arbitrum", "Arbitrum", "0xdDb46999F8891663a8F2828d25298f70416d7610"),
    SparkAssetDeployment("Spark Savings spUSDT", "arbitrum", "Arbitrum", "0x45d91340B3B7B96985A72b5c678F7D9e8D664b62"),
    SparkAssetDeployment("Spark Savings spUSDC", "avalanche", "Avalanche", "0x28B3a8fb53B741A8Fd78c0fb9A6B2393d896a43d"),
    SparkAssetDeployment("Spark Savings sUSDS", "base", "Base", "0x5875eEE11Cf8398102FdAd704C9E96607675467a"),
    SparkAssetDeployment("Spark Savings sUSDS", "optimism", "Optimism", "0xb5B2dc7fd34C249F4be7fB1fCea07950784229e0"),
    SparkAssetDeployment("Spark Savings sUSDS", "unichain", "Unichain", "0xA06b10Db9F390990364A3984C04FaDf1c13691b5"),
    SparkAssetDeployment("Spark Savings spUSDT", "xlayer", "X Layer", "0xc358c90D32375721Cb3924320Fdc2F8B694347Ca"),
    SparkAssetDeployment("Spark Savings spUSDG", "robinhood", "Robinhood Chain", "0xde770c84FE66E063336b31737cFE9790f18c4087"),
    SparkAssetDeployment(
        "Spark Staking", "ethereum", "Ethereum", "0xc6132FAF04627c8d05d6E759FAbB331Ef2D8F8fD",
        position_type="staking", underlying="0xc20059e0317DE91738d13af027DfC4a50781b066",
    ),
)

DEFILLAMA_CHAIN_NAMES = {
    "ethereum": "ethereum", "arbitrum": "arbitrum", "avalanche": "avax",
    "base": "base", "optimism": "optimism", "unichain": "unichain",
    "xlayer": "xlayer", "robinhood": "robinhood",
}
STABLE_ASSET_SYMBOLS = {"DAI", "PYUSD", "USDC", "USDC.E", "USDG", "USDS", "USDT", "USDT0"}

MORPHO_CHAINS = {
    1: ("ethereum", "Ethereum"),
    10: ("optimism", "Optimism"),
    137: ("polygon", "Polygon"),
    999: ("hyperevm", "HyperEVM"),
    8453: ("base", "Base"),
    42161: ("arbitrum", "Arbitrum"),
}


def _selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def _call_data(signature: str, types: list[str] | None = None, values: list[Any] | None = None) -> str:
    payload = keccak(text=signature)[:4]
    if types:
        payload += encode(types, values or [])
    return "0x" + payload.hex()


def _hex_bytes(value: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) <= 2:
        raise ValueError("empty contract response")
    return bytes.fromhex(value[2:])


async def _rpc_batch(client: httpx.AsyncClient, rpc_url: str, calls: list[tuple[str, str]]) -> list[str]:
    if not calls:
        return []
    payload = [
        {"jsonrpc": "2.0", "id": i + 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]}
        for i, (to, data) in enumerate(calls)
    ]
    response = await client.post(rpc_url, json=payload)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, list):
        body = [body]
    by_id = {item.get("id"): item for item in body if isinstance(item, dict)}
    results: list[str] = []
    for i in range(len(calls)):
        item = by_id.get(i + 1) or {}
        if item.get("error"):
            message = (item.get("error") or {}).get("message") or "RPC call failed"
            raise RuntimeError(str(message))
        results.append(str(item.get("result") or "0x"))
    return results


async def _rpc_call(client: httpx.AsyncClient, rpc_url: str, to: str, data: str) -> str:
    return (await _rpc_batch(client, rpc_url, [(to, data)]))[0]


def _empty_protocol(key: str, name: str) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "ok": True,
        "total_usd": 0.0,
        "gross_assets_usd": 0.0,
        "supplied_usd": 0.0,
        "collateral_usd": 0.0,
        "borrowed_usd": 0.0,
        "positions": [],
        "receipt_tokens": [],
        "errors": [],
    }


def _sum_protocol(protocol: dict[str, Any]) -> dict[str, Any]:
    positions = protocol["positions"]
    for key in ("total_usd", "gross_assets_usd", "supplied_usd", "collateral_usd", "borrowed_usd"):
        protocol[key] = sum(float(position.get(key) or 0.0) for position in positions)
    protocol["ok"] = not protocol["errors"]
    return protocol


MORPHO_QUERY = """
query Portfolio($address: String!) {
  c1: userByAddress(address: $address, chainId: 1) { ...UserPositions }
  c10: userByAddress(address: $address, chainId: 10) { ...UserPositions }
  c137: userByAddress(address: $address, chainId: 137) { ...UserPositions }
  c999: userByAddress(address: $address, chainId: 999) { ...UserPositions }
  c8453: userByAddress(address: $address, chainId: 8453) { ...UserPositions }
  c42161: userByAddress(address: $address, chainId: 42161) { ...UserPositions }
}
fragment UserPositions on User {
  marketPositions {
    market { marketId loanAsset { address symbol decimals } collateralAsset { address symbol decimals } lltv }
    state { supplyAssets supplyAssetsUsd borrowAssets borrowAssetsUsd collateral collateralUsd }
  }
  vaultPositions {
    vault { address name symbol asset { address symbol decimals } }
    state { assets assetsUsd shares }
  }
  vaultV2Positions {
    vault { address name symbol asset { address symbol decimals } }
    assets assetsUsd shares
  }
}
"""


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def fetch_morpho(client: httpx.AsyncClient, address: str) -> dict[str, Any]:
    protocol = _empty_protocol("morpho", "Morpho")
    try:
        response = await client.post(MORPHO_API, json={"query": MORPHO_QUERY, "variables": {"address": address}})
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            raise RuntimeError("; ".join(str(error.get("message") or error) for error in body["errors"]))
        data = body.get("data") or {}
        for chain_id, (chain_key, chain_name) in MORPHO_CHAINS.items():
            user = data.get(f"c{chain_id}") or {}
            for item in user.get("marketPositions") or []:
                market = item.get("market") or {}
                state = item.get("state") or {}
                loan = market.get("loanAsset") or {}
                collateral = market.get("collateralAsset") or {}
                supplied_usd = _float(state.get("supplyAssetsUsd"))
                collateral_usd = _float(state.get("collateralUsd"))
                borrowed_usd = _float(state.get("borrowAssetsUsd"))
                if supplied_usd == collateral_usd == borrowed_usd == 0:
                    continue
                protocol["positions"].append({
                    "protocol": "Morpho", "market": "Morpho Blue", "position_type": "market",
                    "chain": chain_key, "chain_name": chain_name, "asset": loan.get("symbol") or "?",
                    "pair": f"{collateral.get('symbol') or '?'} / {loan.get('symbol') or '?'}",
                    "supplied": _float(state.get("supplyAssets")), "supplied_usd": supplied_usd,
                    "collateral": _float(state.get("collateral")), "collateral_usd": collateral_usd,
                    "borrowed": _float(state.get("borrowAssets")), "borrowed_usd": borrowed_usd,
                    "gross_assets_usd": supplied_usd + collateral_usd,
                    "total_usd": supplied_usd + collateral_usd - borrowed_usd,
                    "health_factor": None, "market_id": market.get("marketId"),
                })
            for field, version in (("vaultPositions", "vault"), ("vaultV2Positions", "vault-v2")):
                for item in user.get(field) or []:
                    vault = item.get("vault") or {}
                    state = (item.get("state") or {}) if field == "vaultPositions" else item
                    asset = vault.get("asset") or {}
                    supplied_usd = _float(state.get("assetsUsd"))
                    supplied = _float(state.get("assets"))
                    if supplied_usd == 0 and supplied == 0:
                        continue
                    vault_address = str(vault.get("address") or "").lower()
                    protocol["positions"].append({
                        "protocol": "Morpho", "market": vault.get("name") or vault.get("symbol") or "Morpho Vault",
                        "position_type": version, "chain": chain_key, "chain_name": chain_name,
                        "asset": asset.get("symbol") or "?", "pair": asset.get("symbol") or "?",
                        "supplied": supplied, "supplied_usd": supplied_usd, "collateral": 0.0,
                        "collateral_usd": 0.0, "borrowed": 0.0, "borrowed_usd": 0.0,
                        "gross_assets_usd": supplied_usd, "total_usd": supplied_usd,
                        "health_factor": None, "vault_address": vault_address,
                    })
                    if vault_address.startswith("0x") and len(vault_address) == 42:
                        protocol["receipt_tokens"].append({"chain": chain_key, "address": vault_address})
    except Exception as exc:
        protocol["errors"].append(f"Morpho API: {type(exc).__name__}: {exc}")
    return _sum_protocol(protocol)


def _decode_symbol(raw: str) -> str:
    payload = _hex_bytes(raw)
    try:
        return str(decode(["string"], payload)[0])
    except Exception:
        value = decode(["bytes32"], payload)[0]
        return value.rstrip(b"\x00").decode("utf-8", errors="replace")


def _decode_user_reserves(raw: str) -> list[tuple[str, int, bool, int, int]]:
    """Normalize both legacy (7-word) and Aave Origin (4-word) user structs."""
    payload = _hex_bytes(raw)
    if len(payload) < 96:
        raise ValueError("short user reserves response")
    count = int.from_bytes(payload[64:96])
    if count == 0:
        return []
    tuple_words = ((len(payload) // 32) - 3) // count
    if tuple_words == 4:
        values, _ = decode(["(address,uint256,uint256,bool)[]", "uint8"], payload)
        return [(str(v[0]), int(v[1]), bool(v[3]), int(v[2]), 0) for v in values]
    if tuple_words == 7:
        values, _ = decode(["(address,uint256,bool,uint256,uint256,uint256,uint256)[]", "uint8"], payload)
        return [(str(v[0]), int(v[1]), bool(v[2]), int(v[4]), int(v[5])) for v in values]
    raise ValueError(f"unsupported user reserve tuple width: {tuple_words}")


async def _fetch_aave_deployment(
    client: httpx.AsyncClient,
    address: str,
    deployment: LendingDeployment,
    rpc_url: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    user_reserves_raw = await _rpc_call(
        client, rpc_url, deployment.ui_provider,
        _call_data("getUserReservesData(address,address)", ["address", "address"], [deployment.provider, address]),
    )
    user_reserves = _decode_user_reserves(user_reserves_raw)
    active = [reserve for reserve in user_reserves if reserve[1] or reserve[3] or reserve[4]]
    if not active:
        return [], []

    provider_results = await _rpc_batch(client, rpc_url, [
        (deployment.provider, _selector("getPool()")),
        (deployment.provider, _selector("getPoolDataProvider()")),
        (deployment.provider, _selector("getPriceOracle()")),
    ])
    pool, data_provider, oracle = [str(decode(["address"], _hex_bytes(raw))[0]) for raw in provider_results]
    account_raw = await _rpc_call(client, rpc_url, pool, _call_data("getUserAccountData(address)", ["address"], [address]))
    account = decode(["uint256", "uint256", "uint256", "uint256", "uint256", "uint256"], _hex_bytes(account_raw))
    health_factor = (float(account[5]) / 1e18) if int(account[5]) < 2**255 else None
    try:
        base_unit_raw = await _rpc_call(client, rpc_url, oracle, _selector("BASE_CURRENCY_UNIT()"))
        base_unit = int(decode(["uint256"], _hex_bytes(base_unit_raw))[0]) or 100_000_000
    except Exception:
        base_unit = 100_000_000

    calls: list[tuple[str, str]] = []
    for reserve in active:
        asset = str(reserve[0])
        calls.extend([
            (data_provider, _call_data("getUserReserveData(address,address)", ["address", "address"], [asset, address])),
            (data_provider, _call_data("getReserveTokensAddresses(address)", ["address"], [asset])),
            (asset, _selector("decimals()")),
            (asset, _selector("symbol()")),
            (oracle, _call_data("getAssetPrice(address)", ["address"], [asset])),
        ])
    raw_results = await _rpc_batch(client, rpc_url, calls)
    positions: list[dict[str, Any]] = []
    receipt_tokens: list[dict[str, str]] = []
    for index, reserve in enumerate(active):
        raw = raw_results[index * 5:(index + 1) * 5]
        user_data = decode(["uint256", "uint256", "uint256", "uint256", "uint256", "uint256", "uint256", "uint40", "bool"], _hex_bytes(raw[0]))
        reserve_tokens = decode(["address", "address", "address"], _hex_bytes(raw[1]))
        decimals = int(decode(["uint8"], _hex_bytes(raw[2]))[0])
        symbol = _decode_symbol(raw[3])
        price = int(decode(["uint256"], _hex_bytes(raw[4]))[0]) / base_unit
        supplied = int(user_data[0]) / (10**decimals)
        borrowed = (int(user_data[1]) + int(user_data[2])) / (10**decimals)
        supplied_usd = supplied * price
        borrowed_usd = borrowed * price
        collateral_enabled = bool(reserve[2]) and supplied > 0
        positions.append({
            "protocol": "Aave" if deployment.protocol == "aave" else "Spark",
            "market": deployment.market, "position_type": "lending", "chain": deployment.chain_key,
            "chain_name": deployment.chain_name, "asset": symbol, "pair": symbol,
            "supplied": supplied, "supplied_usd": supplied_usd,
            "collateral": supplied if collateral_enabled else 0.0,
            "collateral_usd": supplied_usd if collateral_enabled else 0.0,
            "borrowed": borrowed, "borrowed_usd": borrowed_usd,
            "gross_assets_usd": supplied_usd, "total_usd": supplied_usd - borrowed_usd,
            "health_factor": health_factor, "underlying_address": str(reserve[0]).lower(),
        })
        a_token = str(reserve_tokens[0]).lower()
        if a_token != ZERO_ADDRESS:
            receipt_tokens.append({"chain": deployment.chain_key, "address": a_token})
    return positions, receipt_tokens


async def fetch_aave_compatible(
    client: httpx.AsyncClient,
    address: str,
    deployments: Iterable[LendingDeployment],
    rpc_by_chain: dict[str, str],
    *,
    key: str,
    name: str,
) -> dict[str, Any]:
    protocol = _empty_protocol(key, name)
    sem = asyncio.Semaphore(5)

    async def scan(deployment: LendingDeployment) -> tuple[LendingDeployment, Any]:
        rpc_url = rpc_by_chain.get(deployment.chain_key)
        if not rpc_url:
            return deployment, RuntimeError("no RPC configured")
        try:
            async with sem:
                return deployment, await _fetch_aave_deployment(client, address, deployment, rpc_url)
        except Exception as exc:
            return deployment, exc

    results = await asyncio.gather(*(scan(deployment) for deployment in deployments))
    for deployment, result in results:
        if isinstance(result, Exception):
            protocol["errors"].append(f"{deployment.market} on {deployment.chain_name}: {type(result).__name__}: {result}")
            continue
        positions, receipt_tokens = result
        protocol["positions"].extend(positions)
        protocol["receipt_tokens"].extend(receipt_tokens)
    return _sum_protocol(protocol)



async def _fetch_spark_asset_chain(
    client: httpx.AsyncClient,
    address: str,
    deployments: list[SparkAssetDeployment],
    rpc_url: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    balance_data = _call_data("balanceOf(address)", ["address"], [address])
    balance_results = await _rpc_batch(
        client, rpc_url, [(deployment.address, balance_data) for deployment in deployments]
    )
    active = [
        (deployment, int(decode(["uint256"], _hex_bytes(raw))[0]))
        for deployment, raw in zip(deployments, balance_results)
        if int(decode(["uint256"], _hex_bytes(raw))[0]) > 0
    ]
    if not active:
        return [], []

    details: list[tuple[SparkAssetDeployment, int, str, str, int, int]] = []
    for deployment, shares_raw in active:
        if deployment.position_type == "staking":
            raw = await _rpc_batch(client, rpc_url, [
                (deployment.address, _selector("decimals()")),
                (deployment.address, _selector("symbol()")),
            ])
            share_decimals = int(decode(["uint8"], _hex_bytes(raw[0]))[0])
            details.append((
                deployment, shares_raw, _decode_symbol(raw[1]),
                str(deployment.underlying), shares_raw, share_decimals,
            ))
            continue

        raw = await _rpc_batch(client, rpc_url, [
            (deployment.address, _selector("decimals()")),
            (deployment.address, _selector("symbol()")),
            (deployment.address, _selector("asset()")),
            (
                deployment.address,
                _call_data("convertToAssets(uint256)", ["uint256"], [shares_raw]),
            ),
        ])
        share_decimals = int(decode(["uint8"], _hex_bytes(raw[0]))[0])
        underlying = str(decode(["address"], _hex_bytes(raw[2]))[0])
        assets_raw = int(decode(["uint256"], _hex_bytes(raw[3]))[0])
        details.append((
            deployment, shares_raw, _decode_symbol(raw[1]),
            underlying, assets_raw, share_decimals,
        ))

    underlying_results = await _rpc_batch(client, rpc_url, [
        call
        for _, _, _, underlying, _, _ in details
        for call in ((underlying, _selector("decimals()")), (underlying, _selector("symbol()")))
    ])
    positions: list[dict[str, Any]] = []
    receipts: list[dict[str, str]] = []
    for index, (deployment, shares_raw, vault_symbol, underlying, assets_raw, share_decimals) in enumerate(details):
        underlying_decimals = int(decode(["uint8"], _hex_bytes(underlying_results[index * 2]))[0])
        asset_symbol = _decode_symbol(underlying_results[index * 2 + 1])
        supplied = assets_raw / (10**underlying_decimals)
        positions.append({
            "protocol": "Spark", "market": deployment.product,
            "position_type": deployment.position_type,
            "chain": deployment.chain_key, "chain_name": deployment.chain_name,
            "asset": asset_symbol, "pair": asset_symbol,
            "supplied": supplied, "supplied_usd": 0.0,
            "collateral": 0.0, "collateral_usd": 0.0,
            "borrowed": 0.0, "borrowed_usd": 0.0,
            "gross_assets_usd": 0.0, "total_usd": 0.0,
            "health_factor": None, "vault_address": deployment.address.lower(),
            "receipt_symbol": vault_symbol,
            "shares": shares_raw / (10**share_decimals),
            "underlying_address": underlying.lower(),
        })
        receipts.append({"chain": deployment.chain_key, "address": deployment.address.lower()})
    return positions, receipts


async def _fetch_defillama_prices(
    client: httpx.AsyncClient,
    positions: list[dict[str, Any]],
) -> dict[tuple[str, str], float]:
    coin_ids = {
        f"{DEFILLAMA_CHAIN_NAMES.get(str(position['chain']), position['chain'])}:{position['underlying_address']}"
        for position in positions
    }
    if not coin_ids:
        return {}
    response = await client.get(f"{DEFILLAMA_PRICE_API}/{','.join(sorted(coin_ids))}")
    response.raise_for_status()
    coins = (response.json() or {}).get("coins") or {}
    by_id = {str(key).lower(): _float((value or {}).get("price")) for key, value in coins.items()}
    return {
        (str(position["chain"]), str(position["underlying_address"]).lower()):
            by_id.get(
                f"{DEFILLAMA_CHAIN_NAMES.get(str(position['chain']), position['chain'])}:{position['underlying_address']}".lower(),
                0.0,
            )
        for position in positions
    }


async def fetch_spark_assets(
    client: httpx.AsyncClient,
    address: str,
    rpc_by_chain: dict[str, str],
) -> dict[str, Any]:
    protocol = _empty_protocol("spark", "Spark")
    by_chain: dict[str, list[SparkAssetDeployment]] = {}
    for deployment in SPARK_ASSET_DEPLOYMENTS:
        by_chain.setdefault(deployment.chain_key, []).append(deployment)

    sem = asyncio.Semaphore(5)

    async def scan(chain_key: str, deployments: list[SparkAssetDeployment]) -> tuple[str, Any]:
        rpc_url = rpc_by_chain.get(chain_key)
        if not rpc_url:
            return chain_key, RuntimeError("no RPC configured")
        try:
            async with sem:
                return chain_key, await _fetch_spark_asset_chain(client, address, deployments, rpc_url)
        except Exception as exc:
            return chain_key, exc

    results = await asyncio.gather(*(scan(chain_key, deployments) for chain_key, deployments in by_chain.items()))
    for chain_key, result in results:
        if isinstance(result, Exception):
            chain_name = by_chain[chain_key][0].chain_name
            protocol["errors"].append(f"Spark wallet products on {chain_name}: {type(result).__name__}: {result}")
            continue
        positions, receipts = result
        protocol["positions"].extend(positions)
        protocol["receipt_tokens"].extend(receipts)

    try:
        prices = await _fetch_defillama_prices(client, protocol["positions"])
    except Exception as exc:
        prices = {}
        if protocol["positions"]:
            protocol["errors"].append(f"Spark asset prices: {type(exc).__name__}: {exc}")

    for position in protocol["positions"]:
        price = prices.get((position["chain"], position["underlying_address"]), 0.0)
        if not price and str(position["asset"]).upper() in STABLE_ASSET_SYMBOLS:
            price = 1.0
            position["price_source"] = "stablecoin-parity-fallback"
        else:
            position["price_source"] = "defillama"
        value = float(position["supplied"]) * price
        position["price_usd"] = price
        position["supplied_usd"] = value
        position["gross_assets_usd"] = value
        position["total_usd"] = value
        if not price:
            protocol["errors"].append(
                f"{position['market']} on {position['chain_name']}: no underlying price for {position['asset']}"
            )
    return _sum_protocol(protocol)


async def fetch_spark(
    client: httpx.AsyncClient,
    address: str,
    rpc_by_chain: dict[str, str],
) -> dict[str, Any]:
    lending, wallet_assets = await asyncio.gather(
        fetch_aave_compatible(client, address, SPARK_DEPLOYMENTS, rpc_by_chain, key="spark", name="Spark"),
        fetch_spark_assets(client, address, rpc_by_chain),
    )
    protocol = _empty_protocol("spark", "Spark")
    protocol["positions"] = lending["positions"] + wallet_assets["positions"]
    protocol["receipt_tokens"] = lending["receipt_tokens"] + wallet_assets["receipt_tokens"]
    protocol["errors"] = lending["errors"] + wallet_assets["errors"]
    return _sum_protocol(protocol)


async def fetch_defi(client: httpx.AsyncClient, address: str, rpc_by_chain: dict[str, str]) -> dict[str, Any]:
    morpho, aave, spark = await asyncio.gather(
        fetch_morpho(client, address),
        fetch_aave_compatible(client, address, AAVE_DEPLOYMENTS, rpc_by_chain, key="aave", name="Aave"),
        fetch_spark(client, address, rpc_by_chain),
    )
    protocols = [morpho, aave, spark]
    errors = [error for protocol in protocols for error in protocol["errors"]]
    result = {
        "ok": not errors,
        "protocols": protocols,
        "positions": [position for protocol in protocols for position in protocol["positions"]],
        "receipt_tokens": [token for protocol in protocols for token in protocol["receipt_tokens"]],
        "errors": errors,
    }
    for key in ("total_usd", "gross_assets_usd", "supplied_usd", "collateral_usd", "borrowed_usd"):
        result[key] = sum(float(protocol.get(key) or 0.0) for protocol in protocols)
    return result


def remove_receipt_token_duplicates(chains: list[dict[str, Any]], receipt_tokens: list[dict[str, str]]) -> float:
    """Remove protocol share tokens from wallet rows and return removed USD value."""
    receipt_set = {(item.get("chain"), str(item.get("address") or "").lower()) for item in receipt_tokens}
    removed = 0.0
    for chain in chains:
        kept = []
        for token in chain.get("tokens") or []:
            identity = (chain.get("key"), str(token.get("contract") or token.get("address") or "").lower())
            if identity in receipt_set:
                removed += _float(token.get("value_usd"))
            else:
                kept.append(token)
        chain["tokens"] = kept
        chain["total_usd"] = _float(chain.get("total_usd")) - sum(
            _float(token.get("value_usd"))
            for token in (chain.get("tokens_removed") or [])
        )
    if removed:
        # Recompute from the surviving rows to avoid cumulative subtraction on reused payloads.
        for chain in chains:
            native_value = _float((chain.get("native") or {}).get("value_usd"))
            chain["total_usd"] = native_value + sum(_float(token.get("value_usd")) for token in chain.get("tokens") or [])
    return removed
