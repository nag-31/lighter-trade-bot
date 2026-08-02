from __future__ import annotations

import json

from command_center.marks import PublicMarkProvider


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_public_mark_provider_uses_native_exchange_marks_then_gate_fallback(monkeypatch):
    def fake_urlopen(request, timeout):
        url = request.full_url
        if "zklighter" in url:
            return _Response({
                "order_book_details": [
                    {"symbol": "BTC", "mark_price": "63000"},
                ]
            })
        if "hyperliquid.xyz" in url:
            body = json.loads(request.data.decode("utf-8"))
            assert body["type"] == "metaAndAssetCtxs"
            return _Response([
                {"universe": [{"name": "BTC"}]},
                [{"markPx": "63010"}],
            ])
        if "gateio.ws" in url:
            return _Response([
                {"contract": "SOL_USDT", "mark_price": "200"},
            ])
        raise AssertionError(url)

    monkeypatch.setattr("command_center.marks.urlopen", fake_urlopen)
    rows = [
        {"source": "Lighter Wallet", "symbol": "BTC", "side": "long"},
        {"source": "HL", "symbol": "BTC", "side": "long"},
        {"source": "Other", "symbol": "SOL", "side": "short"},
    ]
    marks = PublicMarkProvider().fetch(rows)
    assert marks["Lighter Wallet:BTC:long"].price == 63000
    assert marks["Lighter Wallet:BTC:long"].source.startswith("lighter_")
    assert marks["HL:BTC:long"].price == 63010
    assert marks["Other:SOL:short"].price == 200
