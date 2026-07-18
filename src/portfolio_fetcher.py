"""Public-data fetchers for the local portfolio dashboard."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .portfolio_defi import fetch_defi, remove_receipt_token_duplicates


ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
LIGHTER_REST_BASE = "https://mainnet.zklighter.elliot.ai/api/v1"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
LIT_COINGECKO_ID = "lighter"
LIT_SYMBOL = "LIT"
LIT_ASSET_ID = 2  # Lighter asset_id for LIT (staking + pending_unlocks target)
# LIT ERC-20 lives only on Ethereum mainnet (CoinGecko `lighter` platforms).
LIT_ETH_CONTRACT = "0x232ce3bd40fcd6f80f3d55a522d03f25df784ee2"

BALANCE_OF_SELECTOR = "0x70a08231"
DECIMALS_SELECTOR = "0x313ce567"


@dataclass(frozen=True)
class ChainConfig:
    key: str
    name: str
    chain_id: int
    native_symbol: str
    native_price_id: str
    rpc_url: str


CHAINS: tuple[ChainConfig, ...] = (
    ChainConfig("ethereum", "Ethereum", 1, "ETH", "ethereum", "https://ethereum.publicnode.com"),
    ChainConfig("base", "Base", 8453, "ETH", "ethereum", "https://base-rpc.publicnode.com"),
    ChainConfig("arbitrum", "Arbitrum", 42161, "ETH", "ethereum", "https://arbitrum-one-rpc.publicnode.com"),
    ChainConfig("optimism", "Optimism", 10, "ETH", "ethereum", "https://optimism-rpc.publicnode.com"),
    ChainConfig("polygon", "Polygon", 137, "POL", "polygon-ecosystem-token", "https://polygon-bor-rpc.publicnode.com"),
    ChainConfig("bnb", "BNB Chain", 56, "BNB", "binancecoin", "https://bsc-dataseed.binance.org"),
    ChainConfig("avalanche", "Avalanche", 43114, "AVAX", "avalanche-2", "https://avalanche-c-chain-rpc.publicnode.com"),
    ChainConfig("gnosis", "Gnosis", 100, "xDAI", "xdai", "https://gnosis-rpc.publicnode.com"),
    ChainConfig("celo", "Celo", 42220, "CELO", "celo", "https://celo-rpc.publicnode.com"),
    ChainConfig("linea", "Linea", 59144, "ETH", "ethereum", "https://linea-rpc.publicnode.com"),
    ChainConfig("scroll", "Scroll", 534352, "ETH", "ethereum", "https://scroll-rpc.publicnode.com"),
    ChainConfig("zksync", "zkSync Era", 324, "ETH", "ethereum", "https://mainnet.era.zksync.io"),
    ChainConfig("mantle", "Mantle", 5000, "MNT", "mantle", "https://mantle-rpc.publicnode.com"),
    ChainConfig("blast", "Blast", 81457, "ETH", "ethereum", "https://blast-rpc.publicnode.com"),
    ChainConfig("fantom", "Fantom", 250, "FTM", "fantom", "https://rpcapi.fantom.network"),
    ChainConfig("cronos", "Cronos", 25, "CRO", "crypto-com-chain", "https://cronos-evm-rpc.publicnode.com"),
    ChainConfig("moonbeam", "Moonbeam", 1284, "GLMR", "moonbeam", "https://moonbeam-rpc.publicnode.com"),
    ChainConfig("metis", "Metis", 1088, "METIS", "metis-token", "https://metis-rpc.publicnode.com"),
    ChainConfig("opbnb", "opBNB", 204, "BNB", "binancecoin", "https://opbnb-rpc.publicnode.com"),
    ChainConfig("kava", "Kava", 2222, "KAVA", "kava", "https://kava-evm-rpc.publicnode.com"),
    ChainConfig("sonic", "Sonic", 146, "S", "sonic-3", "https://sonic-rpc.publicnode.com"),
    ChainConfig("hyperevm", "HyperEVM", 999, "HYPE", "hyperliquid", "https://rpc.hyperliquid.xyz/evm"),
    ChainConfig("unichain", "Unichain", 130, "ETH", "ethereum", "https://unichain-rpc.publicnode.com"),
    ChainConfig("xlayer", "X Layer", 196, "OKB", "okb", "https://rpc.xlayer.tech"),
    ChainConfig("robinhood", "Robinhood Chain", 4663, "ETH", "ethereum", "https://rpc.mainnet.chain.robinhood.com"),
)

NATIVE_PRICE_IDS = sorted({chain.native_price_id for chain in CHAINS} | {LIT_COINGECKO_ID})
NATIVE_COIN_ID_BY_CHAIN = {chain.key: chain.native_price_id for chain in CHAINS}
PRICE_ID_ALIASES = {
    "weth": "ethereum",
}

SUPPORTED_COINGECKO_PLATFORMS = {
    "ethereum": "ethereum",
    "base": "base",
    "arbitrum-one": "arbitrum",
    "optimistic-ethereum": "optimism",
    "polygon-pos": "polygon",
    "binance-smart-chain": "bnb",
    "avalanche": "avalanche",
    "xdai": "gnosis",
    "celo": "celo",
    "linea": "linea",
    "scroll": "scroll",
    "zksync": "zksync",
    "mantle": "mantle",
    "blast": "blast",
    "fantom": "fantom",
    "cronos": "cronos",
    "moonbeam": "moonbeam",
    "metis-andromeda": "metis",
    "opbnb": "opbnb",
    "kava": "kava",
    "sonic": "sonic",
    "hyperevm": "hyperevm",
    "unichain": "unichain",
    "x-layer": "xlayer",
    "robinhood": "robinhood",
}

FALLBACK_TOKEN_TARGETS: tuple[dict[str, Any], ...] = (
    {
        "chain": "ethereum",
        "contract": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "id": "usd-coin",
        "symbol": "USDC",
        "name": "USD Coin",
        "price_usd": 1.0,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "ethereum",
        "contract": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "id": "tether",
        "symbol": "USDT",
        "name": "Tether",
        "price_usd": 1.0,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "ethereum",
        "contract": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "id": "weth",
        "symbol": "WETH",
        "name": "Wrapped Ether",
        "price_usd": None,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "ethereum",
        "contract": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
        "id": "wrapped-bitcoin",
        "symbol": "WBTC",
        "name": "Wrapped Bitcoin",
        "price_usd": None,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "ethereum",
        "contract": "0x6b175474e89094c44da98b954eedeac495271d0f",
        "id": "dai",
        "symbol": "DAI",
        "name": "Dai",
        "price_usd": 1.0,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "arbitrum",
        "contract": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
        "id": "usd-coin",
        "symbol": "USDC",
        "name": "USD Coin",
        "price_usd": 1.0,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "arbitrum",
        "contract": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
        "id": "tether",
        "symbol": "USDT",
        "name": "Tether",
        "price_usd": 1.0,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "arbitrum",
        "contract": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        "id": "weth",
        "symbol": "WETH",
        "name": "Wrapped Ether",
        "price_usd": None,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "arbitrum",
        "contract": "0x912ce59144191c1204e64559fe8253a0e49e6548",
        "id": "arbitrum",
        "symbol": "ARB",
        "name": "Arbitrum",
        "price_usd": None,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "bnb",
        "contract": "0x55d398326f99059ff775485246999027b3197955",
        "id": "tether",
        "symbol": "USDT",
        "name": "Tether",
        "price_usd": 1.0,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "bnb",
        "contract": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
        "id": "usd-coin",
        "symbol": "USDC",
        "name": "USD Coin",
        "price_usd": 1.0,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "bnb",
        "contract": "0x2170ed0880ac9a755fd29b2688956bd959f933f8",
        "id": "ethereum",
        "symbol": "ETH",
        "name": "Ethereum Token",
        "price_usd": None,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "bnb",
        "contract": "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c",
        "id": "bitcoin",
        "symbol": "BTCB",
        "name": "BTCB Token",
        "price_usd": None,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "base",
        "contract": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "id": "usd-coin",
        "symbol": "USDC",
        "name": "USD Coin",
        "price_usd": 1.0,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "polygon",
        "contract": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
        "id": "usd-coin",
        "symbol": "USDC",
        "name": "USD Coin",
        "price_usd": 1.0,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "polygon",
        "contract": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
        "id": "tether",
        "symbol": "USDT0",
        "name": "Tether USD",
        "price_usd": 1.0,
        "rank": None,
        "change_24h": None,
    },
    {
        "chain": "hyperevm",
        "contract": "0xb88339cb7199b77e23db6e890353e22632ba630f",
        "id": "usd-coin",
        "symbol": "USDC",
        "name": "USD Coin",
        "price_usd": 1.0,
        "rank": None,
        "change_24h": None,
    },
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_verify() -> object:
    # Use Python/httpx's configured default context. Creating another
    # truststore context after a site-level injection can recurse on Windows.
    return True


def normalize_address(raw: str) -> str:
    match = ADDRESS_RE.search(str(raw or ""))
    if not match:
        raise ValueError("expected an EVM address")
    return match.group(0).lower()


def mask_address(address: str) -> str:
    address = normalize_address(address)
    return f"{address[:6]}...{address[-4:]}"


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_nonzero(value: Any) -> bool:
    val = _as_float(value, 0.0) or 0.0
    return abs(val) > 0


def _hex_to_int(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    try:
        return int(value, 16)
    except ValueError:
        return 0


def _wei_to_float(value: Any) -> float:
    return _hex_to_int(value) / 1e18


def _erc20_balance_call(contract: str, address: str) -> dict[str, str]:
    padded_address = address.removeprefix("0x").rjust(64, "0")
    return {"to": contract, "data": BALANCE_OF_SELECTOR + padded_address}


def _decimals_call(contract: str) -> dict[str, str]:
    return {"to": contract, "data": DECIMALS_SELECTOR}


def _token_price(target: dict[str, Any], prices: dict[str, float]) -> float | None:
    direct = _as_float(target.get("price_usd"))
    if direct is not None:
        return direct
    token_id = target.get("id")
    if token_id:
        price_id = str(token_id)
        return prices.get(price_id, prices.get(PRICE_ID_ALIASES.get(price_id, "")))
    return None


def build_token_targets(
    markets: list[dict[str, Any]],
    coin_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    coin_by_id = {str(c.get("id")): c for c in coin_list if c.get("id")}
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for market in markets[:200]:
        coin = coin_by_id.get(str(market.get("id")), {})
        platforms = coin.get("platforms") or market.get("platforms") or {}
        if not isinstance(platforms, dict):
            continue

        for platform_key, chain_key in SUPPORTED_COINGECKO_PLATFORMS.items():
            contract = str(platforms.get(platform_key) or "").lower()
            if not ADDRESS_RE.fullmatch(contract):
                continue
            if contract == "0x0000000000000000000000000000000000000000":
                continue
            # Some chains expose their native coin through an ERC-20-compatible
            # system contract (Polygon POL at 0x...1010, Celo CELO, etc.).
            # eth_getBalance already counts it, so scanning that contract would
            # duplicate the native balance.
            if str(market.get("id") or "") == NATIVE_COIN_ID_BY_CHAIN.get(chain_key):
                continue
            dedup = (chain_key, contract)
            if dedup in seen:
                continue
            seen.add(dedup)
            targets.append({
                "chain": chain_key,
                "contract": contract,
                "id": market.get("id"),
                "symbol": str(market.get("symbol") or "").upper(),
                "name": market.get("name") or market.get("id") or "Token",
                "price_usd": _as_float(market.get("current_price")),
                "rank": market.get("market_cap_rank"),
                "change_24h": _as_float(market.get("price_change_percentage_24h")),
            })

    return targets


def _merge_core_token_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = list(targets)
    seen = {
        (str(target.get("chain") or ""), str(target.get("contract") or "").lower())
        for target in merged
    }
    for target in FALLBACK_TOKEN_TARGETS:
        key = (str(target["chain"]), str(target["contract"]).lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(target))
    return merged


class TokenCatalog:
    """Caches a public CoinGecko top-200 EVM token target list."""

    def __init__(self, ttl_seconds: int = 900):
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, Any] | None = None
        self._cache_time = 0.0

    async def get(self, client: httpx.AsyncClient) -> dict[str, Any]:
        now = time.monotonic()
        if self._cache is not None and now - self._cache_time < self.ttl_seconds:
            return self._cache

        errors: list[str] = []
        prices: dict[str, float] = {}
        stale_cache = False
        try:
            markets_resp = await client.get(
                f"{COINGECKO_BASE}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": "200",
                    "page": "1",
                    "sparkline": "false",
                    "price_change_percentage": "24h",
                },
            )
            markets_resp.raise_for_status()
            markets = markets_resp.json()
            if not isinstance(markets, list):
                raise ValueError("CoinGecko markets response was not a list")

            for row in markets:
                token_id = row.get("id")
                price = _as_float(row.get("current_price"))
                if token_id and price is not None:
                    prices[str(token_id)] = price

            missing_price_ids = [pid for pid in NATIVE_PRICE_IDS if pid not in prices]
            if missing_price_ids:
                try:
                    price_resp = await client.get(
                        f"{COINGECKO_BASE}/simple/price",
                        params={
                            "ids": ",".join(missing_price_ids),
                            "vs_currencies": "usd",
                        },
                    )
                    price_resp.raise_for_status()
                    price_data = price_resp.json()
                    if isinstance(price_data, dict):
                        for pid in missing_price_ids:
                            price = _as_float((price_data.get(pid) or {}).get("usd"))
                            if price is not None:
                                prices[pid] = price
                except Exception as exc:
                    errors.append(f"coingecko native prices: {type(exc).__name__}: {exc}")

            list_resp = await client.get(
                f"{COINGECKO_BASE}/coins/list",
                params={"include_platform": "true"},
            )
            list_resp.raise_for_status()
            coin_list = list_resp.json()
            if not isinstance(coin_list, list):
                raise ValueError("CoinGecko coin list response was not a list")

            targets = _merge_core_token_targets(build_token_targets(markets, coin_list))
            if not targets:
                raise ValueError("CoinGecko returned no supported EVM token targets")

            payload = {
                "source": "coingecko_top_200",
                "updated_at": utc_now_iso(),
                "top_market_count": len(markets[:200]),
                "targets": targets,
                "core_target_count": len(FALLBACK_TOKEN_TARGETS),
                "prices": prices,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(f"coingecko: {type(exc).__name__}: {exc}")
            if self._cache is not None and self._cache.get("targets"):
                payload = dict(self._cache)
                payload["errors"] = list(self._cache.get("errors") or []) + errors
                payload["stale"] = True
                payload["stale_at"] = utc_now_iso()
                stale_cache = True
            else:
                payload = {
                    "source": "fallback_common_tokens",
                    "updated_at": utc_now_iso(),
                    "top_market_count": 0,
                    "targets": list(FALLBACK_TOKEN_TARGETS),
                    "prices": prices,
                    "errors": errors,
                    "stale": False,
                }

        self._cache = payload
        if stale_cache:
            retry_seconds = min(60.0, max(0.0, float(self.ttl_seconds)))
            self._cache_time = now - max(0.0, float(self.ttl_seconds) - retry_seconds)
        else:
            self._cache_time = now
        return payload


def group_targets_by_chain(targets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {c.key: [] for c in CHAINS}
    for target in targets:
        chain = str(target.get("chain") or "")
        if chain in grouped:
            grouped[chain].append(target)
    return grouped


async def _rpc_batch(
    client: httpx.AsyncClient,
    url: str,
    calls: list[dict[str, Any]],
    *,
    chunk_size: int = 80,
) -> dict[Any, Any]:
    async def request_chunk(chunk: list[dict[str, Any]]) -> dict[Any, Any]:
        body = [
            {
                "jsonrpc": "2.0",
                "id": call["id"],
                "method": call["method"],
                "params": call.get("params", []),
            }
            for call in chunk
        ]
        resp = None
        for attempt in range(3):
            resp = await client.post(url, json=body)
            if resp.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                break
            retry_after = _as_float(resp.headers.get("Retry-After"))
            delay = retry_after if retry_after is not None else 0.25 * (2 ** attempt)
            await asyncio.sleep(max(0.0, min(delay, 5.0)))
        assert resp is not None
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ValueError("RPC batch response was not a list")

        expected_ids = {call["id"] for call in chunk}
        received = {
            item["id"]: item
            for item in data
            if isinstance(item, dict) and item.get("id") in expected_ids
        }
        missing = [call for call in chunk if call["id"] not in received]
        if not missing:
            return received
        if len(chunk) == 1:
            raise ValueError(f"RPC omitted response id {chunk[0]['id']!r}")

        # Some public RPCs silently truncate large, expensive eth_call batches
        # while still returning HTTP 200. Retry only omitted calls in smaller
        # groups so balances cannot silently become zero.
        midpoint = max(1, len(missing) // 2)
        received.update(await request_chunk(missing[:midpoint]))
        if midpoint < len(missing):
            received.update(await request_chunk(missing[midpoint:]))
        return received

    out: dict[Any, Any] = {}
    safe_chunk_size = max(1, int(chunk_size))
    for i in range(0, len(calls), safe_chunk_size):
        out.update(await request_chunk(calls[i:i + safe_chunk_size]))
    return out


async def fetch_evm_chain(
    client: httpx.AsyncClient,
    chain: ChainConfig,
    address: str,
    targets: list[dict[str, Any]],
    prices: dict[str, float],
) -> dict[str, Any]:
    calls: list[dict[str, Any]] = [
        {
            "id": "native",
            "method": "eth_getBalance",
            "params": [address, "latest"],
        }
    ]
    for idx, target in enumerate(targets):
        calls.append({
            "id": f"bal:{idx}",
            "method": "eth_call",
            "params": [_erc20_balance_call(target["contract"], address), "latest"],
        })
        calls.append({
            "id": f"dec:{idx}",
            "method": "eth_call",
            "params": [_decimals_call(target["contract"]), "latest"],
        })

    try:
        results = await _rpc_batch(client, chain.rpc_url, calls)
    except Exception as exc:
        return {
            "key": chain.key,
            "name": chain.name,
            "chain_id": chain.chain_id,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "native": {
                "symbol": chain.native_symbol,
                "balance": 0.0,
                "price_usd": prices.get(chain.native_price_id),
                "value_usd": 0.0,
            },
            "tokens": [],
            "total_usd": 0.0,
            "coverage": {"tokens_checked": len(targets), "tokens_nonzero": 0},
        }

    chain_errors: list[str] = []

    def valid_hex_result(item: Any) -> bool:
        if not isinstance(item, dict) or item.get("error") or not isinstance(item.get("result"), str):
            return False
        try:
            int(item["result"], 16)
            return item["result"].lower().startswith("0x")
        except (TypeError, ValueError):
            return False

    native_result = results.get("native", {})
    if valid_hex_result(native_result):
        native_balance = _wei_to_float(native_result.get("result"))
    else:
        native_balance = 0.0
        chain_errors.append(f"native balance unavailable for {chain.native_symbol}")
    native_price = prices.get(chain.native_price_id)
    native_value = native_balance * native_price if native_price is not None else None
    if native_balance and native_price is None:
        chain_errors.append(f"price unavailable for {chain.native_symbol}")

    tokens: list[dict[str, Any]] = []
    for idx, target in enumerate(targets):
        symbol = str(target.get("symbol") or target.get("id") or f"token {idx}")
        bal_item = results.get(f"bal:{idx}", {})
        dec_item = results.get(f"dec:{idx}", {})
        if not valid_hex_result(bal_item):
            chain_errors.append(f"balance unavailable for {symbol}")
            continue
        raw_balance = _hex_to_int(bal_item.get("result"))
        if raw_balance <= 0:
            continue
        if not valid_hex_result(dec_item):
            chain_errors.append(f"decimals unavailable for {symbol}")
            continue
        decimals = _hex_to_int(dec_item.get("result"))
        if decimals < 0 or decimals > 36:
            chain_errors.append(f"invalid decimals for {symbol}: {decimals}")
            continue
        balance = raw_balance / (10 ** decimals)
        price = _token_price(target, prices)
        value_usd = balance * price if price is not None else None
        if price is None:
            chain_errors.append(f"price unavailable for {symbol}")
        tokens.append({
            "symbol": target.get("symbol"),
            "name": target.get("name"),
            "id": target.get("id"),
            "contract": target.get("contract"),
            "balance": balance,
            "decimals": decimals,
            "price_usd": price,
            "value_usd": value_usd,
            "rank": target.get("rank"),
            "change_24h": target.get("change_24h"),
        })

    token_total = sum(t["value_usd"] for t in tokens if t.get("value_usd") is not None)
    total = token_total + (native_value or 0.0)
    return {
        "key": chain.key,
        "name": chain.name,
        "chain_id": chain.chain_id,
        "ok": True,
        "error": "; ".join(chain_errors) if chain_errors else None,
        "errors": chain_errors,
        "native": {
            "symbol": chain.native_symbol,
            "balance": native_balance,
            "price_usd": native_price,
            "value_usd": native_value,
        },
        "tokens": tokens,
        "total_usd": total,
        "coverage": {"tokens_checked": len(targets), "tokens_nonzero": len(tokens)},
    }


async def _lighter_get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = await client.get(f"{LIGHTER_REST_BASE}{path}", params=params)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"Lighter {path} returned non-object JSON")
    return data


def _parse_lighter_account(raw: dict[str, Any], prices: dict[str, float] | None = None) -> dict[str, Any]:
    lit_price_usd = (prices or {}).get(LIT_COINGECKO_ID)
    positions = []
    for pos in raw.get("positions") or []:
        if not isinstance(pos, dict) or not _is_nonzero(pos.get("position")):
            continue
        sign = int(_as_float(pos.get("sign"), 1) or 1)
        imf = _as_float(pos.get("initial_margin_fraction"))
        leverage = 100.0 / imf if imf and imf > 0 else None
        positions.append({
            "market_id": pos.get("market_id"),
            "symbol": pos.get("symbol") or f"M{pos.get('market_id')}",
            "side": "long" if sign > 0 else "short",
            "size": _as_float(pos.get("position"), 0.0),
            "entry_price": _as_float(pos.get("avg_entry_price")),
            "position_value": _as_float(pos.get("position_value")),
            "unrealized_pnl": _as_float(pos.get("unrealized_pnl")),
            "realized_pnl": _as_float(pos.get("realized_pnl")),
            "liquidation_price": _as_float(pos.get("liquidation_price")),
            "leverage": leverage,
        })

    assets = []
    for asset in raw.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        if not (
            _is_nonzero(asset.get("balance"))
            or _is_nonzero(asset.get("locked_balance"))
            or _is_nonzero(asset.get("margin_balance"))
        ):
            continue
        symbol = str(asset.get("symbol") or "").upper()
        balance = _as_float(asset.get("balance"), 0.0) or 0.0
        locked_balance = _as_float(asset.get("locked_balance"), 0.0) or 0.0
        margin_balance = _as_float(asset.get("margin_balance"), 0.0) or 0.0
        spot_balance = balance
        available_balance = max(balance - locked_balance, 0.0)
        if symbol == LIT_SYMBOL:
            price_usd = (prices or {}).get(LIT_COINGECKO_ID)
        elif symbol in {"USDC", "USDT", "USD"}:
            price_usd = 1.0
        else:
            price_usd = None
        value_usd = balance * price_usd if price_usd is not None else None
        assets.append({
            "symbol": symbol,
            "asset_id": asset.get("asset_id"),
            "balance": balance,
            "locked_balance": locked_balance,
            "spot_balance": spot_balance,
            "available_balance": available_balance,
            "margin_balance": margin_balance,
            "margin_mode": asset.get("margin_mode"),
            "price_usd": price_usd,
            "value_usd": value_usd,
            "locked_value_usd": locked_balance * price_usd if price_usd is not None else None,
            "spot_value_usd": spot_balance * price_usd if price_usd is not None else None,
            "available_value_usd": available_balance * price_usd if price_usd is not None else None,
        })

    # LIT is asset_id 2 on Lighter. pending_unlocks[] holds in-flight unstake
    # requests keyed by asset_index; keep the LIT-relevant ones normalized.
    lit_asset_id = LIT_ASSET_ID
    pending_unlocks: list[dict[str, Any]] = []
    for unlock in raw.get("pending_unlocks") or []:
        if not isinstance(unlock, dict):
            continue
        asset_index = unlock.get("asset_index")
        # Keep unlocks for the LIT asset (or any where asset_index is unspecified).
        if asset_index is not None:
            try:
                if int(asset_index) != lit_asset_id:
                    continue
            except (TypeError, ValueError):
                continue
        amount = _as_float(unlock.get("amount"), 0.0) or 0.0
        if amount <= 0:
            continue
        pending_unlocks.append({
            "unlock_timestamp": unlock.get("unlock_timestamp"),
            "amount": amount,
        })

    # shares[] are public-pool deposits. The pool's underlying asset is NOT
    # implied here -- it can be USDC (classic LLP) or LIT (the LIT staking
    # pool, e.g. pool 281474976624800, which is how "LIT Staking" positions
    # are held). principal_amount is denominated in the pool's underlying.
    # Current value is computed later by reading the pool account itself
    # (user shares_amount / pool_info.total_shares x pool asset balances).
    pool_deposits: list[dict[str, Any]] = []
    for share in raw.get("shares") or []:
        if not isinstance(share, dict):
            continue
        principal = _as_float(share.get("principal_amount"), 0.0) or 0.0
        shares_amount = _as_float(share.get("shares_amount"), 0.0) or 0.0
        if principal <= 0 and shares_amount <= 0:
            continue
        pool_deposits.append({
            "public_pool_index": share.get("public_pool_index"),
            "principal_amount": principal,
            "shares_amount": shares_amount,
            "entry_timestamp": share.get("entry_timestamp"),
        })

    account_index = raw.get("index")
    if account_index is None:
        account_index = raw.get("account_index")
    pool_info = raw.get("pool_info") if isinstance(raw.get("pool_info"), dict) else {}
    pool_total_shares = _as_float(pool_info.get("total_shares"), 0.0) or 0.0
    operator_shares = _as_float(pool_info.get("operator_shares"), 0.0) or 0.0
    pool_total_value = _as_float(raw.get("total_asset_value"), 0.0) or 0.0
    is_public_pool = bool(pool_info)
    operator_share_value_usd = 0.0
    if is_public_pool and pool_total_shares > 0 and 0 <= operator_shares <= pool_total_shares:
        operator_share_value_usd = pool_total_value * operator_shares / pool_total_shares

    return {
        "index": account_index,
        "name": raw.get("name"),
        "status": raw.get("status"),
        "account_type": raw.get("account_type"),
        "trading_mode": raw.get("account_trading_mode"),
        "is_public_pool": is_public_pool,
        "pool_total_shares": pool_total_shares,
        "operator_shares": operator_shares,
        "operator_share_value_usd": operator_share_value_usd,
        "available_balance": _as_float(raw.get("available_balance"), 0.0),
        "collateral": _as_float(raw.get("collateral"), 0.0),
        "total_asset_value": _as_float(raw.get("total_asset_value")),
        "cross_asset_value": _as_float(raw.get("cross_asset_value")),
        "cross_initial_margin_requirement": _as_float(raw.get("cross_initial_margin_requirement")),
        "cross_maintenance_margin_requirement": _as_float(raw.get("cross_maintenance_margin_requirement")),
        "assets": assets,
        "positions": positions,
        "shares": raw.get("shares") or [],
        "pool_deposits": pool_deposits,
        "pending_unlocks": raw.get("pending_unlocks") or [],
        "pending_unlocks_lit": pending_unlocks,
    }



def _parse_lighter_staking_entry(
    entry: dict[str, Any] | None,
    *,
    lit_price_usd: float | None = None,
) -> dict[str, Any]:
    entry = entry if isinstance(entry, dict) else {}
    staked_lit = _as_float(entry.get("staked_lit"), 0.0) or 0.0
    value_usd = staked_lit * lit_price_usd if lit_price_usd is not None else None
    return {
        "staked_lit": staked_lit,
        "staked_lit_value_usd": value_usd,
        "lit_price_usd": lit_price_usd,
        "staking_pnl": _as_float(entry.get("staking_pnl"), 0.0) or 0.0,
        "staking_inflow": _as_float(entry.get("staking_inflow"), 0.0) or 0.0,
        "staking_outflow": _as_float(entry.get("staking_outflow"), 0.0) or 0.0,
        "timestamp": entry.get("timestamp"),
    }


def _summarize_lighter_lit(
    accounts: list[dict[str, Any]],
    *,
    lit_price_usd: float | None,
    pool_staked_lit: float = 0.0,
) -> dict[str, Any]:
    # LIT staking on Lighter = shares in a LIT-denominated public pool (e.g.
    # pool 281474976624800, verified live: DeBank's "LIT Staking" position is
    # exactly user_shares x pool_LIT_balance / pool_total_shares). That staked
    # LIT lives in the POOL's account, NOT in the user's asset balance, so it
    # is additive:
    #   spot_lit   = sum(balance)          (full spot ledger, incl. locked slice)
    #   staked_lit = pool_staked_lit       (from LIT public-pool shares)
    #   total_lit  = spot_lit + staked_lit (disjoint -> no double count)
    #   locked_lit = sum(locked_balance)   (informational slice of spot;
    #                                       exchange-side lock, NOT staking)
    #   free_lit   = sum(balance - locked_balance)
    # /pnl is NEVER the source of staked_lit (it has inconsistent account
    # coverage and 400s for some accounts), so a /pnl failure can't zero it.
    lit_balance = 0.0
    lit_locked = 0.0
    lit_free = 0.0
    for account in accounts:
        for asset in account.get("assets") or []:
            if str(asset.get("symbol") or "").upper() != LIT_SYMBOL:
                continue
            balance = asset.get("balance") or 0.0
            locked = asset.get("locked_balance") or 0.0
            lit_balance += balance
            lit_locked += locked
            lit_free += max(balance - locked, 0.0)

    staked_lit = pool_staked_lit
    free_lit = lit_free
    total_lit = lit_balance + staked_lit
    price_known = lit_price_usd is not None

    def value(amount: float) -> float | None:
        return amount * lit_price_usd if price_known else None

    return {
        "total_lit": total_lit,
        "asset_balance_lit": lit_balance,
        # spot_lit == the full asset balance (locked is a slice of it); the
        # staked (pool) LIT is outside the balance, so spot + staked == total.
        "spot_lit": lit_balance,
        "free_lit": free_lit,
        "locked_lit": lit_locked,
        "staked_lit": staked_lit,
        "lit_price_usd": lit_price_usd,
        "total_value_usd": value(total_lit),
        "spot_value_usd": value(lit_balance),
        "free_value_usd": value(free_lit),
        "locked_value_usd": value(lit_locked),
        "staked_value_usd": value(staked_lit),
        "staking_source": "public_pool_shares" if staked_lit > 0 else "none",
    }


async def fetch_lighter_staking(
    client: httpx.AsyncClient,
    account_index: int,
    *,
    lit_price_usd: float | None = None,
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 370 * 24 * 60 * 60 * 1000
    data = await _lighter_get(
        client,
        "/pnl",
        {
            "by": "index",
            "value": str(account_index),
            "resolution": "1d",
            "start_timestamp": str(start_ms),
            "end_timestamp": str(now_ms),
            "count_back": "1",
            "ignore_transfers": "false",
        },
    )
    entries = data.get("pnl") or []
    latest = entries[-1] if isinstance(entries, list) and entries else None
    return _parse_lighter_staking_entry(latest, lit_price_usd=lit_price_usd)


async def fetch_lighter_pool(
    client: httpx.AsyncClient,
    pool_index: int,
) -> dict[str, Any]:
    """Read a Lighter public pool's account to value user share positions.

    Public pools are exposed as accounts on zkLighter and are readable via
    GET /account?by=index. The pool carries `pool_info.total_shares` and
    its asset balances; a user's position value is
        user_shares_amount / total_shares * pool_asset_balance
    per pool asset. Verified live against pool 281474976624800 (the LIT
    staking pool: 121.1M LIT, total_shares 11,721,402,144,143), reproducing
    DeBank's "LIT Staking" amount exactly.
    """
    data = await _lighter_get(
        client,
        "/account",
        {"by": "index", "value": str(pool_index)},
    )
    rows = data.get("accounts") or []
    pool_acc = rows[0] if rows and isinstance(rows[0], dict) else {}
    info = pool_acc.get("pool_info") or {}
    total_shares = _as_float(info.get("total_shares"), 0.0) or 0.0
    assets: dict[str, float] = {}
    for asset in pool_acc.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        symbol = str(asset.get("symbol") or "").upper()
        balance = _as_float(asset.get("balance"), 0.0) or 0.0
        if balance > 0:
            assets[symbol] = assets.get(symbol, 0.0) + balance
    return {
        "pool_index": pool_index,
        "name": pool_acc.get("name"),
        "account_type": pool_acc.get("account_type"),
        "total_shares": total_shares,
        "assets": assets,
        "total_asset_value": _as_float(pool_acc.get("total_asset_value"), 0.0) or 0.0,
        "collateral": _as_float(pool_acc.get("collateral"), 0.0) or 0.0,
    }


async def fetch_lighter(
    client: httpx.AsyncClient,
    address: str,
    prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    sub_accounts: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    try:
        for _ in range(20):
            params = {"l1_address": address}
            if cursor:
                params["cursor"] = cursor
            data = await _lighter_get(client, "/accountsByL1Address", params)
            rows = data.get("sub_accounts") or []
            if isinstance(rows, list):
                sub_accounts.extend(r for r in rows if isinstance(r, dict))
            next_cursor = data.get("next_cursor")
            if not next_cursor:
                break
            next_cursor = str(next_cursor)
            if next_cursor in seen_cursors:
                errors.append(f"accountsByL1Address: repeated cursor {next_cursor!r}")
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            errors.append("accountsByL1Address: pagination truncated after 20 pages")
    except Exception as exc:
        if not (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 400):
            errors.append(f"accountsByL1Address: {type(exc).__name__}: {exc}")

    detailed: list[dict[str, Any]] = []
    account_indexes: list[int] = []
    seen_indexes: set[int] = set()
    for account in sub_accounts:
        idx = account.get("index")
        if idx is None:
            idx = account.get("account_index")
        try:
            idx_int = int(idx)
        except (TypeError, ValueError):
            continue
        if idx_int in seen_indexes:
            continue
        seen_indexes.add(idx_int)
        account_indexes.append(idx_int)

    if len(account_indexes) > 20:
        try:
            data = await _lighter_get(
                client,
                "/account",
                {"by": "l1_address", "value": address, "active_only": "true"},
            )
            rows = data.get("accounts") or []
            detailed.extend(a for a in rows if isinstance(a, dict))
        except Exception as exc:
            if not (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 400):
                errors.append(f"account by l1_address: {type(exc).__name__}: {exc}")
    else:
        for idx_int in account_indexes:
            try:
                data = await _lighter_get(
                    client,
                    "/account",
                    {"by": "index", "value": str(idx_int), "active_only": "true"},
                )
                rows = data.get("accounts") or []
                detailed.extend(a for a in rows if isinstance(a, dict))
            except Exception as exc:
                if not (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 400):
                    errors.append(f"account {idx_int}: {type(exc).__name__}: {exc}")

    if not detailed and not sub_accounts:
        try:
            data = await _lighter_get(
                client,
                "/account",
                {"by": "l1_address", "value": address, "active_only": "true"},
            )
            accounts = data.get("accounts") or []
            detailed.extend(a for a in accounts if isinstance(a, dict))
        except Exception as exc:
            if not (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 400):
                errors.append(f"account by l1_address: {type(exc).__name__}: {exc}")

    deduped_detailed: list[dict[str, Any]] = []
    seen_detailed_indexes: set[int] = set()
    for account in detailed:
        idx = account.get("index")
        if idx is None:
            idx = account.get("account_index")
        try:
            idx_int = int(idx) if idx is not None else None
        except (TypeError, ValueError):
            idx_int = None
        if idx_int is not None:
            if idx_int in seen_detailed_indexes:
                continue
            seen_detailed_indexes.add(idx_int)
        deduped_detailed.append(account)
    detailed = deduped_detailed

    lit_price_usd = (prices or {}).get(LIT_COINGECKO_ID)
    accounts = [_parse_lighter_account(a, prices=prices) for a in detailed]
    total_asset_value = 0.0
    collateral = 0.0
    available = 0.0
    # /pnl-sourced staking metadata is BEST-EFFORT only. It never feeds
    # staked_lit (that comes from public-pool shares), so a /pnl 400/failure
    # cannot zero out the staked amount.
    staking_pnl = 0.0
    staking_inflow = 0.0
    staking_outflow = 0.0
    pnl_reported = False
    pending_unlocks: list[dict[str, Any]] = []
    pending_unstake_lit = 0.0
    pool_deposits: list[dict[str, Any]] = []
    pool_deposits_usd = 0.0
    operator_pool_value_usd = 0.0
    personal_accounts: list[dict[str, Any]] = []

    for account in accounts:
        idx = account.get("index")
        is_public_pool = bool(account.get("is_public_pool"))
        # Attach best-effort /pnl staking metadata to each account for detail
        # views; staked_lit here is informational, not the source of truth.
        pnl_entry = _parse_lighter_staking_entry(None, lit_price_usd=lit_price_usd)
        if not is_public_pool and idx is not None:
            try:
                pnl_entry = await fetch_lighter_staking(
                    client,
                    int(idx),
                    lit_price_usd=lit_price_usd,
                )
                pnl_reported = True
            except Exception as exc:
                if not (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 400):
                    errors.append(f"staking {idx}: {type(exc).__name__}: {exc}")
        account["staking_pnl_meta"] = pnl_entry

        if is_public_pool:
            operator_value = account.get("operator_share_value_usd") or 0.0
            operator_pool_value_usd += operator_value
            total_asset_value += operator_value
            continue

        personal_accounts.append(account)
        account_value = account.get("total_asset_value")
        total_asset_value += account_value if account_value is not None else (account.get("collateral") or 0.0)
        collateral += account.get("collateral") or 0.0
        available += account.get("available_balance") or 0.0
        staking_pnl += pnl_entry.get("staking_pnl") or 0.0
        staking_inflow += pnl_entry.get("staking_inflow") or 0.0
        staking_outflow += pnl_entry.get("staking_outflow") or 0.0

        for unlock in account.get("pending_unlocks_lit") or []:
            pending_unlocks.append(unlock)
            pending_unstake_lit += unlock.get("amount") or 0.0
        for deposit in account.get("pool_deposits") or []:
            pool_deposits.append(deposit)

    # ---- Value public-pool shares by reading the pool accounts -----------
    # LIT staking IS a public-pool position: the "LIT Staking" amount equals
    # user_shares / pool_total_shares x pool LIT balance (pool
    # 281474976624800; verified live to 10 decimal places against DeBank).
    # LIT-pool value feeds staking.staked_lit; non-LIT pools (USDC LLP etc.)
    # roll up into pool_deposits_usd.
    pool_cache: dict[int, dict[str, Any]] = {}
    pool_staked_lit = 0.0
    for deposit in pool_deposits:
        pidx = deposit.get("public_pool_index")
        try:
            pidx_int = int(pidx)
        except (TypeError, ValueError):
            continue
        if pidx_int not in pool_cache:
            try:
                pool_cache[pidx_int] = await fetch_lighter_pool(client, pidx_int)
            except Exception as exc:
                pool_cache[pidx_int] = {"total_shares": 0.0, "assets": {}}
                if not (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 400):
                    errors.append(f"pool {pidx_int}: {type(exc).__name__}: {exc}")
        pool = pool_cache[pidx_int]
        total_shares = pool.get("total_shares") or 0.0
        shares_amount = deposit.get("shares_amount") or 0.0
        underlying: list[dict[str, Any]] = []
        deposit_usd = 0.0
        deposit_lit = 0.0
        non_lit_value_usd = 0.0
        valid_ratio = total_shares > 0 and shares_amount > 0
        if shares_amount > 0 and total_shares <= 0:
            errors.append(f"pool {pidx_int}: invalid total shares")
            valid_ratio = False
        elif total_shares > 0 and shares_amount > total_shares * (1.0 + 1e-9):
            errors.append(f"pool {pidx_int}: invalid share ratio")
            valid_ratio = False
        if valid_ratio:
            ratio = shares_amount / total_shares
            for symbol, pool_balance in (pool.get("assets") or {}).items():
                amount = ratio * pool_balance
                if amount <= 0:
                    continue
                if symbol == LIT_SYMBOL:
                    price = lit_price_usd
                    deposit_lit += amount
                elif symbol in {"USDC", "USDT", "USD"}:
                    price = 1.0
                else:
                    price = None
                value_usd = amount * price if price is not None else None
                if value_usd is not None:
                    deposit_usd += value_usd
                    if symbol != LIT_SYMBOL:
                        non_lit_value_usd += value_usd
                underlying.append({"symbol": symbol, "amount": amount, "value_usd": value_usd, "source": "pool_asset_balance"})
            if not underlying:
                pool_value = pool.get("total_asset_value") or 0.0
                if pool_value <= 0:
                    pool_value = pool.get("collateral") or 0.0
                if pool_value > 0:
                    amount = ratio * pool_value
                    deposit_usd += amount
                    non_lit_value_usd += amount
                    underlying.append({"symbol": "USDC", "amount": amount, "value_usd": amount, "source": "pool_total_asset_value"})
                    deposit["pool_value_usd"] = pool_value
                    deposit["pool_name"] = pool.get("name")
                    deposit["value_source"] = "pool_total_asset_value"
        deposit["underlying"] = underlying
        deposit["is_lit_staking"] = deposit_lit > 0
        deposit["staked_lit"] = deposit_lit
        deposit["non_lit_value_usd"] = non_lit_value_usd
        deposit["value_usd"] = deposit_usd if underlying else None
        pool_staked_lit += deposit_lit
        # Count every valued non-LIT component, including mixed LIT/USDC pools.
        pool_deposits_usd += non_lit_value_usd

    lit_summary = _summarize_lighter_lit(
        personal_accounts,
        lit_price_usd=lit_price_usd,
        pool_staked_lit=pool_staked_lit,
    )
    lit_value_usd = lit_summary.get("total_value_usd") or 0.0
    staked_lit = lit_summary.get("staked_lit") or 0.0
    staked_lit_value_usd = lit_summary.get("staked_value_usd")
    locked_lit = lit_summary.get("locked_lit") or 0.0
    pending_unstake_lit_value_usd = (
        pending_unstake_lit * lit_price_usd if lit_price_usd is not None else None
    )
    pnl_source = "pnl" if pnl_reported else "unavailable"

    # ---- Totals formula (verified live vs acct 7402 + DeBank app_list) ----
    # account_assets_usd covers personal exchange USDC/perp equity plus only
    # the operator's pro-rata equity in pools they own. Full owned-pool TVL is
    # display metadata and is never counted as operator wealth. It excludes:
    #   - LIT asset balances (priced separately as spot/staked LIT), and
    #   - pool deposits represented by shares[] on personal accounts.
    # So each component is added exactly once:
    #   total_usd = account_assets_usd            (personal accounts + operator equity)
    #             + lit_value_usd                 (spot LIT + pool-staked LIT)
    #             + pool_deposits_usd             (non-LIT share deposits)
    # operator_pool_value_usd is a disclosed subset of account_assets_usd and
    # must not be added again. locked_lit is a slice of spot, not another asset.
    return {
        "ok": not errors or bool(accounts),
        "errors": errors,
        "account_count": len(accounts),
        "total_usd": total_asset_value + lit_value_usd + pool_deposits_usd,
        "account_assets_usd": total_asset_value,
        "operator_pool_value_usd": operator_pool_value_usd,
        "lit_assets_usd": lit_summary.get("total_value_usd"),
        "collateral": collateral,
        "available_balance": available,
        "lit": lit_summary,
        "pool_deposits": pool_deposits,
        "pool_deposits_usd": pool_deposits_usd,
        "staking": {
            # staked_lit = LIT public-pool share value (user_shares /
            # pool_total_shares x pool LIT balance). Sourced from /account
            # only -- a /pnl outage never zeroes it.
            "staked_lit": staked_lit,
            "staked_lit_value_usd": staked_lit_value_usd,
            "pool_staked_lit": pool_staked_lit,
            "locked_lit": locked_lit,
            "lit_price_usd": lit_price_usd,
            "source": lit_summary.get("staking_source"),
            # Best-effort staking flow metrics from /pnl (None-ish when /pnl
            # doesn't cover this account); never used to derive staked_lit.
            "staking_pnl": staking_pnl,
            "staking_inflow": staking_inflow,
            "staking_outflow": staking_outflow,
            "pnl_source": pnl_source,
            # In-flight unstake requests (3-day lockup) for LIT.
            "pending_unlocks": pending_unlocks,
            "pending_unstake_lit": pending_unstake_lit,
            "pending_unstake_lit_value_usd": pending_unstake_lit_value_usd,
        },
        "accounts": accounts,
    }


async def _hl_info(client: httpx.AsyncClient, payload: dict[str, Any]) -> Any:
    resp = await client.post(HL_INFO_URL, json=payload)
    resp.raise_for_status()
    return resp.json()


def _hl_spot_prices(
    spot_meta: dict[str, Any],
    mids: dict[str, Any],
    catalog_prices: dict[str, float],
) -> dict[str, float]:
    """Resolve Hyperliquid spot token prices through spotMeta @index markets."""
    stable_symbols = {"USD", "USDC", "USDT", "USDT0", "USDH"}
    prices = {symbol: 1.0 for symbol in stable_symbols}

    tokens_by_index: dict[int, str] = {}
    for token in spot_meta.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        try:
            index = int(token.get("index"))
        except (TypeError, ValueError):
            continue
        name = str(token.get("name") or "").upper()
        if name:
            tokens_by_index[index] = name

    markets: list[tuple[str, str, float]] = []
    for market in spot_meta.get("universe") or []:
        if not isinstance(market, dict):
            continue
        token_indexes = market.get("tokens")
        if not isinstance(token_indexes, list) or len(token_indexes) < 2:
            continue
        try:
            base = tokens_by_index[int(token_indexes[0])]
            quote = tokens_by_index[int(token_indexes[1])]
            market_index = int(market.get("index"))
        except (KeyError, TypeError, ValueError):
            continue
        mid = _as_float(mids.get(f"@{market_index}"))
        if mid is not None and mid > 0:
            markets.append((base, quote, mid))

    # Most markets are quoted in USDC, but iterating also supports token/token
    # routes such as a token quoted in HYPE.
    for _ in range(max(1, len(markets) + 1)):
        changed = False
        for base, quote, mid in markets:
            if quote in prices and base not in prices:
                prices[base] = mid * prices[quote]
                changed = True
            if base in prices and quote not in prices:
                prices[quote] = prices[base] / mid
                changed = True
        if not changed:
            break

    # Prefer Hyperliquid's own spot market. CoinGecko is only a fallback when
    # HYPE has no resolvable venue mid, then another pass can price HYPE-quoted
    # tokens from that fallback.
    hype_price = _as_float(catalog_prices.get("hyperliquid"))
    if "HYPE" not in prices and hype_price is not None and hype_price > 0:
        prices["HYPE"] = hype_price
        for _ in range(max(1, len(markets) + 1)):
            changed = False
            for base, quote, mid in markets:
                if quote in prices and base not in prices:
                    prices[base] = mid * prices[quote]
                    changed = True
                if base in prices and quote not in prices:
                    prices[quote] = prices[base] / mid
                    changed = True
            if not changed:
                break
    return prices


def _hl_symbol_price(
    mids: dict[str, Any],
    symbol: str,
    catalog_prices: dict[str, float],
    spot_prices: dict[str, float] | None = None,
) -> float | None:
    sym = symbol.upper()
    if spot_prices and sym in spot_prices:
        return spot_prices[sym]
    if sym in {"USDC", "USDT", "USDT0", "USD", "USDH"}:
        return 1.0
    if sym == "HYPE":
        price = catalog_prices.get("hyperliquid")
        if price is not None:
            return price
    # Named mids are perpetual markets. Spot markets use @<spot pair index>
    # and are resolved through _hl_spot_prices above.
    for key in (sym, f"{sym}-PERP"):
        if key in mids:
            return _as_float(mids.get(key))
    return None


async def fetch_hyperliquid(
    client: httpx.AsyncClient,
    address: str,
    catalog_prices: dict[str, float],
) -> dict[str, Any]:
    errors: list[str] = []

    async def info(kind: str) -> Any:
        try:
            return await _hl_info(client, {"type": kind, "user": address})
        except Exception as exc:
            errors.append(f"{kind}: {type(exc).__name__}: {exc}")
            return None

    # allMids and spotMeta ignore the user field, which the API tolerates.
    (
        perp_state,
        spot_state,
        mids,
        spot_meta,
        abstraction,
        vault_data,
        staking_data,
        role_data,
        perp_dex_data,
    ) = await asyncio.gather(
        info("clearinghouseState"),
        info("spotClearinghouseState"),
        info("allMids"),
        info("spotMeta"),
        info("userAbstraction"),
        info("userVaultEquities"),
        info("delegatorSummary"),
        info("userRole"),
        info("perpDexs"),
    )
    perp_state = perp_state if isinstance(perp_state, dict) else {}
    spot_state = spot_state if isinstance(spot_state, dict) else {}
    mids = mids if isinstance(mids, dict) else {}
    spot_meta = spot_meta if isinstance(spot_meta, dict) else {}
    account_mode = abstraction if isinstance(abstraction, str) else None
    role = role_data.get("role") if isinstance(role_data, dict) else None
    if role == "agent":
        errors.append("userRole: address is an agent wallet; import its master or subaccount address")

    perp_states: list[tuple[str, dict[str, Any]]] = [("default", perp_state)]
    if account_mode == "disabled" and isinstance(perp_dex_data, list):
        dex_names = [
            str(row.get("name"))
            for row in perp_dex_data
            if isinstance(row, dict) and row.get("name")
        ]

        async def fetch_dex_state(dex: str) -> tuple[str, dict[str, Any]]:
            try:
                data = await _hl_info(
                    client,
                    {"type": "clearinghouseState", "user": address, "dex": dex},
                )
                return dex, data if isinstance(data, dict) else {}
            except Exception as exc:
                errors.append(f"clearinghouseState[{dex}]: {type(exc).__name__}: {exc}")
                return dex, {}

        if dex_names:
            perp_states.extend(await asyncio.gather(*(fetch_dex_state(dex) for dex in dex_names)))

    perp_total = 0.0
    margin_raw_usd = 0.0
    total_margin_used = 0.0
    total_notional_position = 0.0
    withdrawable_total = 0.0
    perp_dexes = []
    positions = []
    total_unrealized_pnl = 0.0
    for dex, state in perp_states:
        margin = state.get("marginSummary") or state.get("crossMarginSummary") or {}
        account_value = _as_float(margin.get("accountValue"), 0.0) or 0.0
        perp_total += account_value
        margin_raw_usd += _as_float(margin.get("totalRawUsd"), 0.0) or 0.0
        total_margin_used += _as_float(margin.get("totalMarginUsed"), 0.0) or 0.0
        total_notional_position += _as_float(margin.get("totalNtlPos"), 0.0) or 0.0
        withdrawable_total += _as_float(state.get("withdrawable"), 0.0) or 0.0
        state_positions = state.get("assetPositions") or []
        if dex == "default" or account_value or state_positions:
            perp_dexes.append({"dex": dex, "account_value": account_value})
        for row in state_positions:
            pos = row.get("position") if isinstance(row, dict) else None
            if not isinstance(pos, dict):
                continue
            szi = _as_float(pos.get("szi"), 0.0) or 0.0
            if abs(szi) <= 0:
                continue
            unrealized_pnl = _as_float(pos.get("unrealizedPnl"), 0.0) or 0.0
            total_unrealized_pnl += unrealized_pnl
            positions.append({
                "dex": dex,
                "coin": str(pos.get("coin") or "").upper(),
                "side": "long" if szi > 0 else "short",
                "size": abs(szi),
                "entry_price": _as_float(pos.get("entryPx")),
                "position_value": _as_float(pos.get("positionValue")),
                "margin_used": _as_float(pos.get("marginUsed")),
                "unrealized_pnl": unrealized_pnl,
                "return_on_equity": _as_float(pos.get("returnOnEquity")),
                "liquidation_price": _as_float(pos.get("liquidationPx")),
                "leverage": pos.get("leverage"),
            })

    spot_prices = _hl_spot_prices(spot_meta, mids, catalog_prices)
    spot_balances = []
    spot_total = 0.0
    unpriced_spot_symbols: list[str] = []
    for bal in spot_state.get("balances") or []:
        if not isinstance(bal, dict):
            continue
        total = _as_float(bal.get("total"), 0.0) or 0.0
        hold = _as_float(bal.get("hold"), 0.0) or 0.0
        if abs(total) <= 0 and abs(hold) <= 0:
            continue
        coin = str(bal.get("coin") or "").upper()
        price = _hl_symbol_price(mids, coin, catalog_prices, spot_prices)
        value = total * price if price is not None else None
        if value is not None:
            spot_total += value
        elif abs(total) > 0:
            unpriced_spot_symbols.append(coin)
        spot_balances.append({
            "coin": coin,
            "total": total,
            "hold": hold,
            "entry_ntl": _as_float(bal.get("entryNtl")),
            "price_usd": price,
            "value_usd": value,
        })
    if unpriced_spot_symbols:
        symbols = ", ".join(sorted(set(unpriced_spot_symbols)))
        errors.append(f"spot pricing unavailable for: {symbols}")

    if account_mode == "disabled":
        direct_total = perp_total + spot_total
        if spot_total:
            total_source = "standard_spot_plus_perp"
        else:
            total_source = "perp_account_value"
    elif account_mode in {"unifiedAccount", "portfolioMargin"}:
        if spot_state:
            direct_total = spot_total
            total_source = "unified_spot_balances"
        else:
            direct_total = perp_total
            total_source = "perp_account_value_fallback"
            if perp_total:
                errors.append(f"{account_mode}: spot state unavailable; using perp account value fallback")
    else:
        # Without the abstraction state there is no provably correct way to
        # distinguish separate standard balances from unified collateral.
        direct_total = max(perp_total, spot_total)
        total_source = "unknown_mode_max_equity"
        if perp_total or spot_total:
            errors.append("userAbstraction: unavailable; using the larger of spot and perp equity")

    vault_rows = []
    vault_total = 0.0
    if isinstance(vault_data, list):
        for row in vault_data:
            if not isinstance(row, dict):
                continue
            equity = _as_float(row.get("equity"))
            if equity is None:
                continue
            vault_total += equity
            vault_rows.append({
                "vault_address": row.get("vaultAddress"),
                "equity_usd": equity,
                "locked_until_timestamp": row.get("lockedUntilTimestamp"),
            })

    staking = staking_data if isinstance(staking_data, dict) else {}
    delegated = _as_float(staking.get("delegated"), 0.0) or 0.0
    undelegated = _as_float(staking.get("undelegated"), 0.0) or 0.0
    pending = _as_float(staking.get("totalPendingWithdrawal"), 0.0) or 0.0
    staked_hype = delegated + undelegated + pending
    hype_price = _hl_symbol_price(mids, "HYPE", catalog_prices, spot_prices)
    staking_total = staked_hype * hype_price if hype_price is not None else None
    if staked_hype and staking_total is None:
        errors.append("delegatorSummary: HYPE price unavailable; staking excluded from total")

    portfolio_total = direct_total + vault_total + (staking_total or 0.0)
    if vault_total:
        total_source += "+vaults"
    if staking_total:
        total_source += "+staking"

    return {
        "ok": any(bool(state) for _, state in perp_states) or bool(spot_state) or bool(vault_rows) or bool(staking_data),
        "errors": errors,
        "total_usd": portfolio_total,
        "total_source": total_source,
        "account_mode": account_mode,
        "user_role": role,
        "direct_equity_usd": direct_total,
        "perp_equity_usd": perp_total,
        "spot_usd": spot_total,
        "unpriced_spot_symbols": sorted(set(unpriced_spot_symbols)),
        "vaults": {
            "total_usd": vault_total,
            "positions": vault_rows,
        },
        "staking": {
            "delegated_hype": delegated,
            "undelegated_hype": undelegated,
            "pending_withdrawal_hype": pending,
            "hype": staked_hype,
            "hype_price_usd": hype_price,
            "total_usd": staking_total,
        },
        "perp": {
            # Hyperliquid accountValue already includes open unrealized PnL.
            "account_value": perp_total,
            "balance_without_upnl": perp_total - total_unrealized_pnl,
            "total_unrealized_pnl": total_unrealized_pnl,
            "equity_check": (perp_total - total_unrealized_pnl) + total_unrealized_pnl,
            "withdrawable": withdrawable_total,
            "margin_summary": {
                "total_margin_used": total_margin_used,
                "total_notional_position": total_notional_position,
                "total_raw_usd": margin_raw_usd,
            },
            "dexes": perp_dexes,
            "positions": positions,
        },
        "spot": {
            "total_usd": spot_total,
            "balances": spot_balances,
        },
    }


async def fetch_address_portfolio(
    address: str,
    *,
    catalog: TokenCatalog | None = None,
) -> dict[str, Any]:
    normalized = normalize_address(address)
    catalog = catalog or TokenCatalog()
    headers = {"User-Agent": "lighter-trade-bot-portfolio/0.1"}
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers, verify=_http_verify()) as client:
        token_catalog = await catalog.get(client)
        targets_by_chain = group_targets_by_chain(token_catalog.get("targets") or [])
        prices = {str(k): float(v) for k, v in (token_catalog.get("prices") or {}).items() if v is not None}
        for target in token_catalog.get("targets") or []:
            token_id = target.get("id")
            price = _as_float(target.get("price_usd"))
            if token_id and price is not None:
                prices.setdefault(str(token_id), price)

        chain_sem = asyncio.Semaphore(5)

        async def fetch_chain_limited(chain: ChainConfig) -> dict[str, Any]:
            async with chain_sem:
                return await fetch_evm_chain(
                    client,
                    chain,
                    normalized,
                    targets_by_chain.get(chain.key, []),
                    prices,
                )

        chain_tasks = [fetch_chain_limited(chain) for chain in CHAINS]
        lighter_task = fetch_lighter(client, normalized, prices)
        hl_task = fetch_hyperliquid(client, normalized, prices)
        defi_task = fetch_defi(
            client,
            normalized,
            {chain.key: chain.rpc_url for chain in CHAINS},
        )

        chains, lighter, hyperliquid, defi = await asyncio.gather(
            asyncio.gather(*chain_tasks),
            lighter_task,
            hl_task,
            defi_task,
        )

    remove_receipt_token_duplicates(chains, defi.get("receipt_tokens") or [])

    errors: list[str] = []
    for chain in chains:
        if chain.get("error"):
            errors.append(f"{chain.get('name')}: {chain.get('error')}")
    for err in lighter.get("errors") or []:
        errors.append(f"Lighter: {err}")
    for err in hyperliquid.get("errors") or []:
        errors.append(f"Hyperliquid: {err}")
    for err in defi.get("errors") or []:
        errors.append(f"DeFi: {err}")
    for err in token_catalog.get("errors") or []:
        errors.append(f"Token catalog: {err}")

    chains_total = sum(c.get("total_usd") or 0.0 for c in chains)
    lighter_total = lighter.get("total_usd") or 0.0
    lighter_staking = lighter.get("staking") or {}
    lighter_lit = lighter.get("lit") or {}
    lit_staked = lighter_staking.get("staked_lit") or 0.0
    lit_staked_usd = lighter_staking.get("staked_lit_value_usd")
    lit_spot = lighter_lit.get("spot_lit") or 0.0
    lit_spot_usd = lighter_lit.get("spot_value_usd")
    lit_locked = lighter_lit.get("locked_lit") or 0.0
    lit_locked_usd = lighter_lit.get("locked_value_usd")
    lit_total = lighter_lit.get("total_lit") or (lit_spot + lit_staked)
    lit_total_usd = lighter_lit.get("total_value_usd")
    hl_total = hyperliquid.get("total_usd") or 0.0
    defi_total = defi.get("total_usd") or 0.0
    any_ok = any(c.get("ok") for c in chains) or lighter.get("ok") or hyperliquid.get("ok") or defi.get("ok")
    status = "ok" if not errors else ("degraded" if any_ok else "error")

    return {
        "address": normalized,
        "address_masked": mask_address(normalized),
        "timestamp": utc_now_iso(),
        "status": status,
        "totals": {
            "total_usd": chains_total + lighter_total + hl_total + defi_total,
            "chains_usd": chains_total,
            "lighter_usd": lighter_total,
            "hyperliquid_usd": hl_total,
            "defi_usd": defi_total,
            "defi_gross_assets_usd": defi.get("gross_assets_usd") or 0.0,
            "defi_supplied_usd": defi.get("supplied_usd") or 0.0,
            "defi_collateral_usd": defi.get("collateral_usd") or 0.0,
            "defi_borrowed_usd": defi.get("borrowed_usd") or 0.0,
            "lit_staked": lit_staked,
            "lit_staked_usd": lit_staked_usd,
            "lit_spot": lit_spot,
            "lit_spot_usd": lit_spot_usd,
            "lit_locked": lit_locked,
            "lit_locked_usd": lit_locked_usd,
            "lit_total": lit_total,
            "lit_total_usd": lit_total_usd,
        },
        "chains": chains,
        "lighter": lighter,
        "hyperliquid": hyperliquid,
        "defi": defi,
        "token_catalog": {
            "source": token_catalog.get("source"),
            "updated_at": token_catalog.get("updated_at"),
            "top_market_count": token_catalog.get("top_market_count"),
            "target_count": len(token_catalog.get("targets") or []),
            "targets_by_chain": {
                key: len(value)
                for key, value in targets_by_chain.items()
            },
        },
        "errors": errors,
    }





