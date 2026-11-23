"""Tests de la page graphique : archive des prix, appariement des trades."""

import json
from datetime import datetime, timezone

from perps_engine import graphique as gq

T0 = 1_800_000_000.0


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _state(path, points, sym="BTC"):
    json.dump({"saved_at": T0, "markets": {sym: {"points": points}}},
              open(path, "w", encoding="utf-8"))


def test_archive_merges_copies_without_duplicates(tmp_path):
    csv_path = str(tmp_path / "prix.csv")
    a, b = str(tmp_path / "a.json"), str(tmp_path / "b.json")
    _state(a, [[T0, 80_000.0], [T0 + 60, 80_010.0]])
    _state(b, [[T0 + 60, 80_010.0], [T0 + 120, 80_020.0]])
    assert gq.archive_prices([a, b, str(tmp_path / "absent.json")], csv_path) == 3
    assert gq.archive_prices([a, b], csv_path) == 0           # rien de neuf
    prices = gq.load_prices(csv_path)
    assert [p for _, p in prices["BTC"]] == [80_000.0, 80_010.0, 80_020.0]


def test_candles_are_true_ohlc():
    pts = [(T0 + i * 60, p) for i, p in enumerate([10, 12, 9, 11, 10, 20])]
    bars = gq.candles(pts, bucket=300)
    first = bars[0]
    assert (first["open"], first["high"], first["low"], first["close"]) == (10, 12, 9, 10)
    assert bars[1]["open"] == 20 and len(bars) == 2


def _log(path, events):
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_entries_are_paired_with_their_closes_in_order(tmp_path):
    p = str(tmp_path / "trades.log")
    _log(p, [
        {"event": "start", "account": "FP-1", "ts": _iso(T0)},
        {"event": "entry_order", "sym": "BTC", "side": "buy", "status": "filled",
         "fill_price": 100.0, "stop": 95.0, "tp": 110.0, "ts": _iso(T0 + 60)},
        {"event": "position_closed", "sym": "BTC", "side": "buy", "entry": 100.5,
         "exit": 95.0, "close_reason": "stop_loss", "net": -50.0,
         "hold_min": 30, "ts": _iso(T0 + 1900)},
        {"event": "entry_order", "sym": "BTC", "side": "sell", "status": "filled",
         "fill_price": 96.0, "stop": 99.0, "tp": 90.0, "ts": _iso(T0 + 4000)},
        {"event": "entry_order", "sym": "BTC", "side": "buy", "status": "rejected",
         "ts": _iso(T0 + 5000)},
    ])
    trades, account = gq.load_trades(p)
    assert account == "FP-1" and len(trades) == 2
    first, second = trades
    assert first["reason"] == "Stop" and first["net"] == -50.0
    assert first["entry"] == 100.5                     # prix reel de la plateforme
    assert first["exit_ts"] == first["entry_ts"] + 30 * 60
    assert second["open"] is True and second["side"] == "sell"
    st = gq.stats(trades)
    assert (st["closed"], st["open"], st["losses"], st["net"]) == (1, 1, 1, -50.0)


def test_close_without_known_entry_is_still_listed(tmp_path):
    p = str(tmp_path / "trades.log")
    _log(p, [{"event": "position_closed", "sym": "GOLD", "side": "sell",
              "entry": 4000.0, "exit": 3990.0, "close_reason": "take_profit",
              "net": 400.0, "hold_min": 10, "ts": _iso(T0 + 600)}])
    trades, _ = gq.load_trades(p)
    assert trades[0]["reason"] == "Objectif"
    assert trades[0]["entry_ts"] == T0


def test_page_is_self_contained_and_safe(tmp_path):
    prices = {"BTC": [(T0 + i * 60, 100.0 + i) for i in range(30)]}
    trades = [{"sym": "BTC", "side": "buy", "entry_ts": T0 + 300, "entry": 105.0,
               "stop": 100.0, "tp": 115.0, "open": False, "exit": 115.0,
               "exit_ts": T0 + 1500, "reason": "Objectif</script>", "net": 99.0,
               "hold_min": 20}]
    page = gq.build_html(prices, trades, "FP-1</script>", now=T0)
    assert page.count("</script>") == page.count("<script")   # pas d'injection
    assert "LightweightCharts" in page
    data = json.loads(page.split("const DATA = ", 1)[1].split(";\n", 1)[0]
                      .replace("<\\/", "</"))
    t = data["markets"]["BTC"]["trades"][0]
    assert t["entry_time"] <= T0 + 300 < t["entry_time"] + 300
    assert t["exit_time"] <= T0 + 1500


def test_trade_without_archived_prices_is_drawn_on_empty_time():
    prices = {"BTC": [(T0 + 86_400 + i * 60, 100.0) for i in range(10)]}
    trades = [{"sym": "BTC", "side": "buy", "entry_ts": T0 + 10, "entry": 100.0,
               "stop": 99.0, "tp": 102.0, "open": False, "exit": 102.0,
               "exit_ts": T0 + 610, "reason": "Objectif", "net": 10.0}]
    m = gq.chart_data(prices, trades)["BTC"]
    t = m["trades"][0]
    assert (t["entry_time"], t["exit_time"]) == (T0, T0 + 600)
    empty = [c["time"] for c in m["candles"] if "open" not in c]
    assert empty[-3:] == [T0, T0 + 300, T0 + 600]  # la duree du trade existe
    assert empty[:6] == [T0 - k * 300 for k in range(6, 0, -1)]   # marge
    times = [c["time"] for c in m["candles"]]
    assert times == sorted(set(times))              # croissant, sans doublon
    assert m["missing"] == 1
    assert "Objectif" in gq.build_html(prices, trades, None, now=T0)


def test_trade_inside_archived_prices_adds_no_empty_case():
    prices = {"BTC": [(T0 + i * 60, 100.0 + i) for i in range(30)]}
    trades = [{"sym": "BTC", "side": "sell", "entry_ts": T0 + 400, "entry": 106.0,
               "stop": 110.0, "tp": 98.0, "open": False, "exit": 110.0,
               "exit_ts": T0 + 1300, "reason": "Stop", "net": -40.0}]
    m = gq.chart_data(prices, trades)["BTC"]
    assert all("open" in c for c in m["candles"][6:]) and m["missing"] == 0
