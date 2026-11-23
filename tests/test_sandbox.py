"""
Tests du moteur sandbox (v2.0).

Aucun test ne parle au vrai serveur : une fausse plateforme en memoire
rejoue les reponses documentees de l'API (/v1/orders, /v1/positions, ...).
"""

import itertools
import json
import os
from urllib.parse import unquote

import pytest
import requests

from perps_engine import executor as ex
from perps_engine import sandbox_run as sr
from perps_engine.risk import (AccountRules, AccountState, OrderIntent,
                               PlatformLimits, RiskPolicy, evaluate_order)
from perps_engine.signals import SignalConfig
from perps_engine.strategy import MomentumStrategy

KEY = "fp_test_0123456789abcdef"
BASE = ex.SANDBOX_BASE

POLICY = {
    "account_rules": [
        {"id": "max-daily-loss", "parameters": {"pct": 3}},
        {"id": "max-drawdown", "parameters": {"pct": 3, "type": "static"}},
        {"id": "profit-target", "parameters": {"pct": 9}},
    ],
    "limits": {"min_order_notional_usd": 10, "max_open_positions": 15,
               "maintenance_margin_ratio_bps": 1500,
               "max_book_staleness_ms": 2500},
}

MARKETS_LIST = [
    {"market_id": "binance|BTCUSDT", "symbol": "BTC", "max_leverage": 10,
     "min_notional": 10, "min_size": 0.001, "size_step": 0.001,
     "size_decimals": 3},
    {"market_id": "binance|XAUUSDT", "symbol": "XAU", "max_leverage": 10,
     "min_notional": 10, "min_size": 0.001, "size_step": 0.001,
     "size_decimals": 3},
]

# Fenetres courtes pour les tests ; plancher de cout neutralise.
FAST = SignalConfig(lookback_minutes=5, vol_window_minutes=20,
                    warmup_minutes=15, max_staleness_seconds=180,
                    entry_z=2.0, exit_z=1.0, round_trip_cost_pct=0.0,
                    min_stop_cost_multiple=0.0)


# --------------------------------------------------------------------------
# Fausses reponses HTTP
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("pas de json")
        return self._payload


class ScriptedSession:
    """Rend les reponses d'une liste, dans l'ordre. Enregistre les appels."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, headers=None, json=None, params=None,
                timeout=None):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "json": json,
                           "params": params})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeExchange:
    """Une plateforme sandbox minimale, fidele aux formes documentees."""

    def __init__(self, clock, prices=None, starting=100_000.0):
        self.clock = clock
        self.prices = dict(prices or {"binance|BTCUSDT": 80_000.0,
                                      "binance|XAUUSDT": 4_000.0})
        self.starting = starting
        self.balance = starting
        self.status = "active"
        self.open = {}
        self.closed = []
        self.orders = {}
        self.calls = []
        self.fail_quotes = set()
        self.ids = itertools.count(1)
        self.close_policy = "real"
        self.close_bodies = []

    def account(self):
        upnl = sum((self.prices[p["market_id"]] - p["entry_price"]) * p["size"]
                   * (1 if p["side"] == "long" else -1)
                   for p in self.open.values())
        eq = self.balance + upnl
        floor = self.starting * 0.97
        return {"id": "acc1", "account_number": "FP-TEST", "status": self.status,
                "balance": self.balance, "starting_balance": self.starting,
                "risk": {"equity": eq, "daily_loss_floor": floor,
                         "daily_loss_room": eq - floor}}

    def add_manual_position(self, market_id, side="long", size=1.0):
        pid = f"manual{next(self.ids)}"
        self.open[pid] = {"id": pid, "market_id": market_id, "side": side,
                          "size": size, "entry_price": self.prices[market_id],
                          "opened_at": self.clock() * 1000, "leverage": 2}
        return pid

    def _ok(self, data, status=200):
        return FakeResponse(status, {"data": data})

    def request(self, method, url, headers=None, json=None, params=None,
                timeout=None):
        assert url.startswith(BASE), url
        path = url[len(BASE):]
        headers = dict(headers or {})
        self.calls.append((method, path, json, headers, params))
        parts = path.strip("/").split("/")

        if method == "GET" and path == "/accounts":
            return self._ok([self.account()])
        if method == "GET" and parts[0] == "accounts" and len(parts) == 2:
            return self._ok(self.account())
        if method == "GET" and path.endswith("/trading-policy"):
            return self._ok(POLICY)
        if method == "GET" and path == "/markets":
            return self._ok(MARKETS_LIST)
        if method == "GET" and parts[0] == "markets" and parts[-1] == "quote":
            mid = unquote(parts[1])
            if mid in self.fail_quotes:
                return FakeResponse(503, {"error": {"code": "unavailable",
                                                    "message": "x",
                                                    "request_id": "r"}})
            p = self.prices[mid]
            return self._ok({"market_id": mid, "bid": p - 0.05,
                             "ask": p + 0.05, "mid": p,
                             "time": int(self.clock() * 1000)})
        if method == "GET" and path == "/positions":
            if params.get("status") == "closed":
                return self._ok(list(reversed(self.closed)))
            return self._ok(list(self.open.values()))
        if method == "GET" and parts[0] == "orders" and len(parts) == 2:
            return self._ok(self.orders[parts[1]])
        if method == "POST" and path == "/orders":
            assert headers.get("Idempotency-Key"), "cle d'idempotence absente"
            assert json["stop_loss_price"] > 0, "ordre sans stop"
            mid = json["market_id"]
            oid = f"ord{next(self.ids)}"
            pid = f"pos{next(self.ids)}"
            side = "long" if json["side"] == "buy" else "short"
            self.open[pid] = {"id": pid, "market_id": mid, "side": side,
                              "size": json["size"],
                              "entry_price": self.prices[mid],
                              "opened_at": self.clock() * 1000,
                              "leverage": json["leverage"]}
            order = {"id": oid, "status": "filled", "fill_price": self.prices[mid],
                     "market_id": mid, "side": json["side"],
                     "size": json["size"], "leverage": json["leverage"],
                     "stop_loss_order_id": f"sl-{oid}",
                     "take_profit_order_id": f"tp-{oid}"}
            self.orders[oid] = order
            return self._ok(order, 201)
        if method == "POST" and parts[0] == "positions" and parts[-1] == "close":
            assert headers.get("Idempotency-Key")
            self.close_bodies.append(dict(json))
            invalid = FakeResponse(400, {"error": {
                "code": "invalid_position_close",
                "message": "One or more request fields are invalid.",
                "request_id": "r", "details": [{
                    "field": "expected_price",
                    "issue": "is required for market closes"}]}})
            # Regle reelle de la plateforme, relevee dans sandbox-orders.log
            # le 5 nov. : pas de fermeture au marche sans expected_price.
            if json.get("type", "market") == "market" and \
                    not json.get("expected_price"):
                return invalid
            if self.close_policy == "minimal_only" and \
                    set(json) != {"type", "expected_price"}:
                return invalid
            if self.close_policy == "always_reject":
                return invalid
            pid = parts[1]
            if pid not in self.open:
                return FakeResponse(404, {"error": {"code": "not_found",
                                                    "message": "x",
                                                    "request_id": "r"}})
            part = json.get("size")
            if part and part < self.open[pid]["size"] - 1e-9:
                p = self.open[pid]
                px = self.prices[p["market_id"]]
                pnl = (px - p["entry_price"]) * part * (1 if p["side"] == "long" else -1)
                self.balance += pnl
                p["size"] = round(p["size"] - part, 8)
                oid = f"ord{next(self.ids)}"
                order = {"id": oid, "status": "filled", "fill_price": px,
                         "realized_pnl": pnl, "reduce_only": True}
                self.orders[oid] = order
                return self._ok(order, 201)
            p = self.open.pop(pid)
            exit_px = self.prices[p["market_id"]]
            sign = 1 if p["side"] == "long" else -1
            pnl = (exit_px - p["entry_price"]) * p["size"] * sign
            self.balance += pnl
            self.closed.append(dict(p, exit_price=exit_px,
                                    closed_at=self.clock() * 1000,
                                    realized_pnl=pnl, fees=0.0, funding=0.0,
                                    close_reason="user", status="closed"))
            oid = f"ord{next(self.ids)}"
            order = {"id": oid, "status": "filled", "fill_price": exit_px,
                     "realized_pnl": pnl, "reduce_only": True}
            self.orders[oid] = order
            return self._ok(order, 201)
        raise AssertionError(f"route inconnue : {method} {path}")


# --------------------------------------------------------------------------
# L'executeur ne peut pas sortir de la sandbox
# --------------------------------------------------------------------------

def test_live_key_is_refused():
    with pytest.raises(ex.SandboxOnlyError):
        ex.SandboxExecutor("fp_live_abcdef0123456789")


def test_missing_or_odd_key_is_refused():
    for bad in ("", None, "abc", "FP_TEST_x"):
        with pytest.raises(ex.SandboxOnlyError):
            ex.SandboxExecutor(bad)


def test_live_host_is_refused_even_with_a_test_key():
    with pytest.raises(ex.SandboxOnlyError):
        ex.SandboxExecutor(KEY, base_url="https://developers.myfundedperpetuals.com/v1")


def test_key_is_masked_for_display():
    api = ex.SandboxExecutor(KEY, session=ScriptedSession([]))
    assert KEY not in api.key_display
    assert api.key_display.startswith("fp_test_")


def test_observation_engine_client_stays_read_only():
    """L'ecriture vit dans executor.py, jamais dans client.py."""
    from perps_engine import client
    src = open(client.__file__, encoding="utf-8").read()
    for verb in ("session.post", ".post(", ".delete(", ".put("):
        assert verb not in src


# --------------------------------------------------------------------------
# Ordres : corps, en-tetes, idempotence
# --------------------------------------------------------------------------

def _filled(oid="o1"):
    return FakeResponse(201, {"data": {"id": oid, "status": "filled",
                                       "fill_price": 80_000.0}})


def _open(api, **kw):
    args = dict(account_id="acc1", market_id="binance|BTCUSDT", side="buy",
                size=0.5, leverage=3, stop_loss_price=79_600.0,
                take_profit_price=80_800.0, expected_price=80_000.0)
    args.update(kw)
    return api.open_market(**args)


def test_open_sends_stop_target_and_idempotency_key(tmp_path):
    s = ScriptedSession([_filled()])
    api = ex.SandboxExecutor(KEY, session=s, sleep=lambda _: None,
                             journal_path=str(tmp_path / "orders.log"))
    order = _open(api)
    assert order["status"] == "filled"
    call = s.calls[0]
    assert call["method"] == "POST" and call["url"] == f"{BASE}/orders"
    body = call["json"]
    assert body["stop_loss_price"] == 79_600.0
    assert body["take_profit_price"] == 80_800.0
    assert body["margin_mode"] == "isolated" and body["type"] == "market"
    assert body["size"] == 0.5 and body["leverage"] == 3
    assert call["headers"]["Idempotency-Key"].startswith("open-")
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    # Le journal contient l'intention AVANT le resultat.
    events = [json.loads(l)["event"] for l in open(tmp_path / "orders.log")]
    assert events == ["open_send", "open_result"]


def test_lost_response_is_retried_with_the_same_key_and_body():
    s = ScriptedSession([requests.Timeout("coupure"), _filled()])
    api = ex.SandboxExecutor(KEY, session=s, sleep=lambda _: None)
    _open(api)
    assert len(s.calls) == 2
    k1 = s.calls[0]["headers"]["Idempotency-Key"]
    k2 = s.calls[1]["headers"]["Idempotency-Key"]
    assert k1 == k2
    assert s.calls[0]["json"] == s.calls[1]["json"]


def test_429_waits_retry_after_then_retries_same_key():
    waits = []
    s = ScriptedSession([FakeResponse(429, {"error": {"code": "rate_limited",
                                                      "message": "x",
                                                      "request_id": "r"}},
                                      {"Retry-After": "4"}),
                         _filled()])
    api = ex.SandboxExecutor(KEY, session=s, sleep=waits.append)
    _open(api)
    assert waits == [4.0]
    assert (s.calls[0]["headers"]["Idempotency-Key"]
            == s.calls[1]["headers"]["Idempotency-Key"])


def test_trading_rule_rejection_is_not_retried():
    s = ScriptedSession([FakeResponse(422, {"error": {
        "code": "rule_violation", "message": "daily loss", "request_id": "r",
        "rule_id": "max-daily-loss"}})])
    api = ex.SandboxExecutor(KEY, session=s, sleep=lambda _: None)
    with pytest.raises(ex.ApiError) as e:
        _open(api)
    assert e.value.status == 422 and e.value.rule_id == "max-daily-loss"
    assert len(s.calls) == 1


def test_uncertain_outcome_is_recovered_by_client_order_id():
    timeouts = [requests.Timeout("x")] * 3
    found = FakeResponse(200, {"data": [{"id": "o9", "status": "filled"}]})
    s = ScriptedSession(timeouts + [found])
    api = ex.SandboxExecutor(KEY, session=s, sleep=lambda _: None)
    order = _open(api, client_order_id="sbx-abc")
    assert order["id"] == "o9"
    assert s.calls[-1]["params"] == {"client_order_id": "sbx-abc"}


def test_pending_order_is_followed_until_filled():
    s = ScriptedSession([
        FakeResponse(201, {"data": {"id": "o1", "status": "pending"}}),
        FakeResponse(200, {"data": {"id": "o1", "status": "pending"}}),
        FakeResponse(200, {"data": {"id": "o1", "status": "filled",
                                    "fill_price": 80_001.0}})])
    api = ex.SandboxExecutor(KEY, session=s, sleep=lambda _: None)
    order = _open(api)
    assert order["status"] == "filled" and order["fill_price"] == 80_001.0


@pytest.mark.parametrize("kw", [
    dict(stop_loss_price=0),
    dict(stop_loss_price=80_100.0),              # stop au-dessus d'un achat
    dict(side="sell", stop_loss_price=79_000.0,  # stop sous une vente
         take_profit_price=79_000.0),
    dict(take_profit_price=79_000.0),            # objectif sous un achat
])
def test_orders_without_a_valid_stop_never_leave(kw):
    s = ScriptedSession([])
    api = ex.SandboxExecutor(KEY, session=s)
    with pytest.raises(ValueError):
        _open(api, **kw)
    assert s.calls == []


def test_close_position_sends_expected_price_and_key():
    s = ScriptedSession([FakeResponse(201, {"data": {"id": "c1",
                                                     "status": "filled"}})])
    api = ex.SandboxExecutor(KEY, session=s, sleep=lambda _: None)
    api.close_position("pos42", expected_price=80_000.0)
    call = s.calls[0]
    assert call["url"] == f"{BASE}/positions/pos42/close"
    assert "size" not in call["json"]
    # La plateforme exige le prix observe pour fermer au marche.
    assert call["json"]["expected_price"] == 80_000.0
    assert call["json"]["type"] == "market"
    assert "slippage_tolerance_bps" not in call["json"]
    assert call["headers"]["Idempotency-Key"].startswith("close-")


def test_close_without_expected_price_never_leaves():
    s = ScriptedSession([])
    api = ex.SandboxExecutor(KEY, session=s)
    for bad in (None, 0, -1):
        with pytest.raises(ValueError):
            api.close_position("pos42", expected_price=bad)
    assert s.calls == []


def test_minimal_close_sends_only_type_and_price():
    s = ScriptedSession([FakeResponse(201, {"data": {"id": "c1",
                                                     "status": "filled"}})])
    api = ex.SandboxExecutor(KEY, session=s, sleep=lambda _: None)
    api.close_position("pos42", expected_price=80_000.0, minimal=True)
    assert s.calls[0]["json"] == {"type": "market", "expected_price": 80_000.0}
    assert s.calls[0]["headers"]["Idempotency-Key"]


def test_quote_url_encodes_the_pipe():
    s = ScriptedSession([FakeResponse(200, {"data": {"mid": 4000.0}})])
    api = ex.SandboxExecutor(KEY, session=s)
    api.quote("hyperliquid|xyz:GOLD")
    assert s.calls[0]["url"] == f"{BASE}/markets/hyperliquid%7Cxyz%3AGOLD/quote"


# --------------------------------------------------------------------------
# Outils purs du lanceur
# --------------------------------------------------------------------------

def test_dotenv_reads_bom_and_quotes_without_overriding(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text('﻿PERPS_TEST_KEY="fp_test_x"\n# note\nOTHER=1\n',
                 encoding="utf-8")
    monkeypatch.delenv("PERPS_TEST_KEY", raising=False)
    monkeypatch.setenv("OTHER", "deja")
    loaded = sr.load_dotenv(str(p))
    assert os.environ["PERPS_TEST_KEY"] == "fp_test_x"
    assert os.environ["OTHER"] == "deja" and loaded == ["PERPS_TEST_KEY"]
    monkeypatch.delenv("PERPS_TEST_KEY", raising=False)


def test_pick_account_by_number_or_single_active():
    accts = [{"id": "a", "account_number": "FP-1", "status": "failed"},
             {"id": "b", "account_number": "FP-2", "status": "active"}]
    assert sr.pick_account(accts)["id"] == "b"
    assert sr.pick_account(accts, "FP-2")["id"] == "b"
    with pytest.raises(RuntimeError):
        sr.pick_account(accts, "FP-1")             # pas actif
    with pytest.raises(RuntimeError):
        sr.pick_account(accts, "FP-9")             # introuvable
    two = accts + [{"id": "c", "account_number": "FP-3", "status": "active"}]
    with pytest.raises(RuntimeError):
        sr.pick_account(two)                       # ambigu


def test_pick_markets_falls_back_to_the_next_venue():
    listed = [{"market_id": "binance|BTCUSDT"},
              {"market_id": "hyperliquid|xyz:GOLD"}]
    got = sr.pick_markets(listed)
    assert got["GOLD"]["market_id"] == "hyperliquid|xyz:GOLD"
    assert sr.pick_markets(MARKETS_LIST)["GOLD"]["market_id"] == "binance|XAUUSDT"


def test_fit_to_market_rounds_size_down_and_leverage_up():
    m = MARKETS_LIST[0]
    size, lev, why = sr.fit_to_market(0.32299, 2.59, 10, m, 80_000)
    assert (size, lev, why) == (0.322, 3, None)
    assert sr.fit_to_market(0.0004, 2, 10, m, 80_000)[2] == "TAILLE_SOUS_MINIMUM"
    assert sr.fit_to_market(0.5, 9.2, 9.5, m, 80_000)[2] == "LEVIER_ENTIER_HORS_FENETRE"


def test_account_state_uses_platform_daily_floor_and_history():
    rules = AccountRules(100_000, 0.03, 0.03, "static", 0.09)
    now = 1_800_000_000.0
    acct = {"balance": 101_000, "risk": {"equity": 100_500,
                                         "daily_loss_floor": 98_000}}
    closed = [
        {"opened_at": (now - 3600) * 1000, "closed_at": (now - 1800) * 1000,
         "realized_pnl": -50, "fees": 5, "funding": 0},
        {"opened_at": (now - 30 * 3600) * 1000, "closed_at": (now - 29 * 3600) * 1000,
         "realized_pnl": 300, "fees": 5, "funding": 0},
    ]
    st = sr.account_state(acct, rules, [], closed, now)
    assert st.equity == 100_500
    assert st.realized_pnl_today == pytest.approx(-500)   # 100 500 - 101 000
    assert st.trades_last_24h == 1
    assert st.minutes_since_last_loss == pytest.approx(30)


def test_arming_only_on_fresh_signals():
    armed, prev = {}, {"BTC": None}
    sr.update_arming(armed, prev, {"BTC": "buy"}, 0)
    assert armed["BTC"]["dir"] == "buy"
    sr.update_arming(armed, prev, {"BTC": "buy"}, 60)       # inchange
    assert armed["BTC"]["since"] == 0
    sr.update_arming(armed, prev, {"BTC": None}, 120)       # retombe
    assert "BTC" not in armed
    sr.update_arming(armed, prev, {"BTC": "sell"}, 180)
    sr.update_arming(armed, prev, {"BTC": "sell"}, 180 + sr.ENTRY_WINDOW_SECONDS + 1)
    assert "BTC" not in armed                                # fenetre passee


def test_only_engine_positions_are_ever_closed():
    managed = {"p1": {"sym": "BTC"}}
    positions = [{"id": "p1", "side": "long"}, {"id": "manual", "side": "long"}]
    assert sr.exits_to_make(managed, positions, {"BTC": "buy"}) == []
    assert sr.exits_to_make(managed, positions, {"BTC": None}) == ["p1"]
    assert sr.exits_to_make(managed, positions, {"BTC": "sell"}) == ["p1"]


def test_single_instance_lock(tmp_path):
    p = str(tmp_path / "sandbox.lock")
    first = sr.acquire_single_instance(p)
    assert first is not None
    assert sr.acquire_single_instance(p) is None
    first.close()


# --------------------------------------------------------------------------
# Garde-fou : plafond d'exposition 3x
# --------------------------------------------------------------------------

def test_exposure_cap_shrinks_size_never_grows_it():
    rules = AccountRules(100_000, 0.03, 0.03, "static", 0.09)
    st = AccountState(equity=100_000, balance=100_000)
    tight = OrderIntent("binance|BTCUSDT", "buy", 80_000.0, 79_960.0, 80_200.0)
    free = evaluate_order(tight, st, rules, PlatformLimits(), RiskPolicy())
    capped = evaluate_order(tight, st, rules, PlatformLimits(),
                            RiskPolicy(max_notional_multiple=3.0))
    assert free.computed["notional"] > 300_000
    assert capped.computed["notional"] == pytest.approx(300_000, rel=1e-6)
    assert capped.computed["capped_by_exposure"] is True
    assert capped.computed["loss_at_stop"] < free.computed["loss_at_stop"]


def test_exposure_cap_absent_by_default():
    assert RiskPolicy().max_notional_multiple is None


# --------------------------------------------------------------------------
# Boucle complete contre une fausse plateforme
# --------------------------------------------------------------------------

def _runner(tmp_path, exch, clock, strategies=None, table=None):
    api = ex.SandboxExecutor(KEY, session=exch, sleep=lambda _: None,
                             journal_path=str(tmp_path / "orders.log"),
                             fill_wait_seconds=0)
    markets = sr.pick_markets(MARKETS_LIST)
    strategies = strategies or {s: MomentumStrategy(FAST) for s in markets}
    paths = sr.Paths(**{f: str(tmp_path / getattr(sr.Paths(), f))
                        for f in sr.Paths.__dataclass_fields__})
    acct = exch.account()
    return sr.Runner(api, acct, markets, strategies, POLICY, cfg=FAST,
                     paths=paths, echo=False, clock=clock, table=table)


def _step(runner, exch, clock, btc=None, gold=None):
    if btc is not None:
        exch.prices["binance|BTCUSDT"] = btc
    if gold is not None:
        exch.prices["binance|XAUUSDT"] = gold
    clock.t += 60
    return runner.cycle(clock.t)


def _noise(i, base, amp):
    return base + (amp if i % 2 else -amp)


def _calm(runner, exch, clock, n=25):
    for i in range(n):
        _step(runner, exch, clock, btc=_noise(i, 80_000, 10),
              gold=_noise(i, 4_000, 0.5))


def test_full_cycle_enters_with_stop_then_exits_on_signal(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    r = _runner(tmp_path, exch, clock)
    _calm(r, exch, clock)
    assert not [c for c in exch.calls if c[0] == "POST"]

    # Tendance franche : le signal nait sous nos yeux.
    entries = []
    for k in range(1, 6):
        rep = _step(r, exch, clock, btc=80_000 + 60 * k, gold=4_000)
        entries += rep["entries"]
    assert entries and entries[0][0] == "BTC" and entries[0][1] == "filled"
    posts = [c for c in exch.calls if c[0] == "POST" and c[1] == "/orders"]
    assert len(posts) == 1
    body = posts[0][2]
    assert body["side"] == "buy" and body["stop_loss_price"] < body["expected_price"]
    assert body["take_profit_price"] > body["expected_price"]
    # Plafond 3x : notionnel <= 3 x equity.
    assert body["size"] * body["expected_price"] <= 3 * 100_000 + 1
    assert len(r.managed) == 1

    # Le marche se calme : le signal retombe sous exit_z, le moteur sort.
    exits = []
    for i in range(12):
        exits += _step(r, exch, clock, btc=_noise(i, 80_300, 10))["exits"]
        if exits:
            break
    assert exits, "le moteur aurait du fermer sa position"
    _step(r, exch, clock, btc=80_300)
    assert r.managed == {} and len(r.history) == 1
    assert r.history[0]["close_reason"] == "user"
    assert r.history[0]["net"] == pytest.approx(exch.balance - 100_000, abs=0.01)
    r.write_summary(exch.account(), clock.t)
    summary = open(r.paths.summary, encoding="utf-8").read()
    assert "trades du moteur fermes : 1" in summary
    assert "position ouverte : aucune" in summary


def test_signal_already_running_after_warmup_is_not_traded(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    strat = MomentumStrategy(FAST)
    # Serie deja en tendance avant le demarrage du moteur.
    for i in range(30):
        strat.observe(clock.t - (30 - i) * 60,
                      _noise(i, 80_000, 10) + (60 * (i - 24) if i > 24 else 0))
    r = _runner(tmp_path, exch, clock,
                {"BTC": strat, "GOLD": MomentumStrategy(FAST)})
    rep = _step(r, exch, clock, btc=80_000 + 60 * 6)
    assert rep["entries"] == []
    assert not [c for c in exch.calls if c[0] == "POST"]


def test_manual_position_is_never_touched_and_blocks_entries(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    manual = exch.add_manual_position("binance|XAUUSDT")
    r = _runner(tmp_path, exch, clock)
    _calm(r, exch, clock)
    for k in range(1, 6):
        _step(r, exch, clock, btc=80_000 + 60 * k)
    assert manual in exch.open                     # jamais fermee
    assert not [c for c in exch.calls if c[0] == "POST"]
    verdicts = [json.loads(l) for l in open(r.paths.trades, encoding="utf-8")
                if '"verdict"' in l]
    assert verdicts and "POSITION_LIMIT_REACHED" in verdicts[0]["rejects"]


def test_data_gap_does_not_close_a_position(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    r = _runner(tmp_path, exch, clock)
    _calm(r, exch, clock)
    for k in range(1, 6):
        _step(r, exch, clock, btc=80_000 + 60 * k)
    assert len(r.managed) == 1
    exch.fail_quotes.add("binance|BTCUSDT")
    for _ in range(5):                               # 5 min sans prix BTC
        rep = _step(r, exch, clock)
        assert rep["exits"] == []
    assert len(r.managed) == 1


def _open_one_btc_trade(tmp_path, policy):
    clock = Clock()
    exch = FakeExchange(clock)
    exch.close_policy = policy
    r = _runner(tmp_path, exch, clock)
    _calm(r, exch, clock)
    for k in range(1, 6):
        _step(r, exch, clock, btc=80_000 + 60 * k)
    assert len(r.managed) == 1
    return r, exch, clock


def _calm_until_exit_attempt(r, exch, clock, n=12):
    for i in range(n):
        _step(r, exch, clock, btc=_noise(i, 80_300, 10))
        if exch.close_bodies:
            return


def test_signal_exit_passes_where_v22_was_refused(tmp_path):
    """Sans expected_price, la v2.2 a vu 4 998 sorties refusees."""
    r, exch, clock = _open_one_btc_trade(tmp_path, "real")
    _calm_until_exit_attempt(r, exch, clock)
    assert exch.close_bodies, "le signal aurait du demander une sortie"
    body = exch.close_bodies[0]
    assert body["expected_price"] == exch.prices["binance|BTCUSDT"]
    assert exch.open == {}                       # sortie executee du 1er coup
    events = [json.loads(l)["event"] for l in open(r.paths.trades, encoding="utf-8")]
    assert "exit_signal" in events and "exit_error" not in events


def test_refused_close_falls_back_to_a_minimal_request(tmp_path):
    r, exch, clock = _open_one_btc_trade(tmp_path, "minimal_only")
    _calm_until_exit_attempt(r, exch, clock)
    assert set(exch.close_bodies[-1]) == {"type", "expected_price"}
    assert exch.open == {}


def test_refused_close_is_logged_once_and_alerted_once(tmp_path):
    r, exch, clock = _open_one_btc_trade(tmp_path, "always_reject")
    _calm_until_exit_attempt(r, exch, clock)
    for i in range(5):                             # 5 cycles de refus de plus
        _step(r, exch, clock, btc=_noise(i, 80_300, 10))
    assert len(exch.open) == 1                     # le stop reste pose
    lines = [json.loads(l) for l in open(r.paths.trades, encoding="utf-8")]
    errors = [l for l in lines if l["event"] == "exit_error"]
    assert len(errors) == 1                        # pas 4 998 lignes
    assert errors[0]["code"] == "invalid_position_close"
    assert errors[0]["details"][0]["field"] == "expected_price"
    alerts = open(r.paths.alert, encoding="utf-8").read()
    assert alerts.count("[SANDBOX] sortie refusee") == 1


def test_long_gap_rewarms_and_closes_what_it_cannot_see(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    r = _runner(tmp_path, exch, clock)
    _calm(r, exch, clock)
    for k in range(1, 6):
        _step(r, exch, clock, btc=80_000 + 60 * k)
    assert len(r.managed) == 1
    exch.fail_quotes.add("binance|BTCUSDT")
    for _ in range(35):                              # 35 min sans prix
        assert _step(r, exch, clock)["exits"] == []
    exch.fail_quotes.clear()
    rep = _step(r, exch, clock, btc=80_400)
    assert rep["exits"], "position aveugle : le moteur doit sortir"
    events = [json.loads(l)["event"] for l in open(r.paths.signals, encoding="utf-8")]
    assert "rewarm" in events
    # Pas de re-entree pendant la rechauffe.
    for i in range(10):
        assert _step(r, exch, clock, btc=80_400 + 60 * i)["entries"] == []


def test_check_mode_reports_and_sends_nothing(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    api = ex.SandboxExecutor(KEY, session=exch, sleep=lambda _: None)
    lines = []
    acct = exch.account()
    code = sr.check(api, acct, sr.pick_markets(MARKETS_LIST), POLICY,
                    out=lines.append)
    assert code == 0
    text = "\n".join(lines)
    assert "aucun ordre ne part" in text and KEY not in text
    assert "BTC" in text and "GOLD" in text and "OK : tout est pret" in text
    assert not [c for c in exch.calls if c[0] == "POST"]


def test_main_stops_without_a_test_key(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PERPS_TEST_KEY", raising=False)
    assert sr.main([]) == 2
    assert "PERPS_TEST_KEY" in capsys.readouterr().out


def test_main_refuses_a_live_key_placed_in_the_test_slot(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PERPS_TEST_KEY", "fp_live_abcdef0123456789")
    assert sr.main([]) == 2
    assert "fp_test_" in capsys.readouterr().out


def test_every_seen_price_is_archived_for_the_chart(tmp_path):
    from perps_engine import graphique
    clock = Clock()
    exch = FakeExchange(clock)
    r = _runner(tmp_path, exch, clock)
    _calm(r, exch, clock, n=5)
    exch.fail_quotes.add("binance|XAUUSDT")          # l'or muet un cycle
    _step(r, exch, clock, btc=80_123.0)
    prices = graphique.load_prices(r.paths.prices)
    assert len(prices["BTC"]) == 6 and len(prices["GOLD"]) == 5
    assert prices["BTC"][-1] == (round(clock.t, 3), 80_123.0)
    # Le meme point vu par l'etat sauvegarde n'est jamais archive deux fois.
    r.save_state()
    assert graphique.archive_prices([r.paths.state], r.paths.prices) == 0


def test_inactive_account_sends_nothing(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    exch.status = "failed"
    r = _runner(tmp_path, exch, clock)
    _calm(r, exch, clock)
    for k in range(1, 6):
        _step(r, exch, clock, btc=80_000 + 60 * k)
    assert not [c for c in exch.calls if c[0] == "POST"]


def test_restore_never_splices_another_venue(tmp_path):
    import time as _t
    from perps_engine import persistence
    obs = MomentumStrategy(FAST)
    obs.observe(_t.time() - 60, 4_000.0)
    obs_path = str(tmp_path / "state.json")
    persistence.save(obs_path, {"BTC": obs, "GOLD": obs})
    strategies = {"BTC": MomentumStrategy(FAST), "GOLD": MomentumStrategy(FAST)}
    ids = {"BTC": "binance|BTCUSDT", "GOLD": "binance|XAUUSDT"}
    rep = sr.restore_buffers(strategies, ids, str(tmp_path / "none.json"),
                             obs_path)
    assert [x["sym"] for x in rep["observation"]] == ["BTC"]
    assert rep["warmup"] == ["GOLD"]       # or Hyperliquid != or Binance


# --------------------------------------------------------------------------
# Table des associes (v2.5) : les sieges votent, les trades ne changent pas
# --------------------------------------------------------------------------

from datetime import datetime as _dt, timedelta as _td, timezone as _tz

from perps_engine import calendrier as cal
from perps_engine.table import MomentumSeat, Table


def _news_feed(at_ts, title="CPI m/m"):
    et = _tz(_td(hours=-5))
    return [{"title": title, "country": "USD", "impact": "High",
             "date": _dt.fromtimestamp(at_ts, et).isoformat(),
             "forecast": "", "previous": ""}]


def _table(tmp_path, feed, modes=None):
    c = cal.Calendar(str(tmp_path / "calendrier-ff.json"), fetch=lambda url: feed)
    return Table([MomentumSeat(), c], journal_path=str(tmp_path / "sandbox-table.log"),
                 modes=modes or {})


def _trend_run(tmp_path, table=None):
    clock = Clock()
    exch = FakeExchange(clock)
    r = _runner(tmp_path, exch, clock, table=table)
    _calm(r, exch, clock)
    for k in range(1, 6):
        _step(r, exch, clock, btc=80_000 + 60 * k, gold=4_000)
    posts = [{k: v for k, v in c[2].items() if k != "client_order_id"}
             for c in exch.calls if c[0] == "POST" and c[1] == "/orders"]
    return r, exch, clock, posts


def _rows(path):
    return [json.loads(l) for l in open(path, encoding="utf-8")]


def test_table_in_observation_changes_no_trade(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _, _, _, plain = _trend_run(tmp_path / "a")                  # comme la v2.4
    news = _news_feed(Clock().t + 30 * 60)                       # CPI pendant la tendance
    r, _, _, voted = _trend_run(tmp_path / "b", _table(tmp_path / "b", news))
    assert plain and voted == plain                              # meme ordre, au centime
    rows = _rows(r.paths.table)
    assert len(rows) == 1
    v = rows[0]["votes"]
    assert v["calendrier"]["vote"] == "contre" and v["calendrier"]["mode"] == "observe"
    assert "CPI m/m" in v["calendrier"]["raison"]
    assert v["momentum"]["vote"] == "pour" and rows[0]["resultat"] == "filled"
    assert any(n["event"] == "calendar_read" for n in _rows(r.paths.signals))


def test_calendar_veto_cancels_the_entry_when_switched_on(tmp_path):
    news = _news_feed(Clock().t + 30 * 60)
    table = _table(tmp_path, news, modes={"calendrier": "veto"})
    r, _, _, posts = _trend_run(tmp_path, table)
    assert posts == []
    vetoed = [e for e in _rows(r.paths.trades) if e["event"] == "entry_vetoed"]
    assert vetoed and vetoed[0]["by"] == ["calendrier"]
    assert _rows(r.paths.table)[0]["resultat"] == "veto:calendrier"


def test_summary_shows_next_announcement_and_votes(tmp_path):
    news = _news_feed(Clock().t + 3 * 3600, "Non-Farm Employment Change")
    r, exch, clock, posts = _trend_run(tmp_path, _table(tmp_path, news))
    assert posts
    r.write_summary(exch.account(), clock.t)
    s = open(r.paths.summary, encoding="utf-8").read()
    assert "prochaine annonce USD forte : Non-Farm Employment Change" in s
    assert "table : 1 vote(s)" in s and "pour 1, contre 0" in s


def test_without_a_table_nothing_is_voted(tmp_path):
    r, _, _, posts = _trend_run(tmp_path)
    assert posts and not os.path.exists(r.paths.table)


def test_broken_calendar_never_stops_the_engine(tmp_path):
    def down(url):
        raise RuntimeError("HTTP 503")
    c = cal.Calendar(str(tmp_path / "calendrier-ff.json"), fetch=down)
    table = Table([MomentumSeat(), c], journal_path=str(tmp_path / "sandbox-table.log"),
                  modes={"calendrier": "veto"})
    r, _, _, posts = _trend_run(tmp_path, table)
    assert posts                                   # abstention : pas de blocage
    assert _rows(r.paths.table)[0]["votes"]["calendrier"]["vote"] == "abstient"
    errors = [n for n in _rows(r.paths.signals) if n["event"] == "calendar_error"]
    assert len(errors) == 1                        # notee une fois, pas a chaque cycle


def test_check_lists_next_announcements_and_never_fails_on_them(tmp_path):
    import time as _t
    clock = Clock()
    exch = FakeExchange(clock)
    api = ex.SandboxExecutor(KEY, session=exch, sleep=lambda _: None)
    markets = sr.pick_markets(MARKETS_LIST)
    good = cal.Calendar(str(tmp_path / "c1.json"),
                        fetch=lambda url: _news_feed(_t.time() + 2 * 3600))
    lines = []
    assert sr.check(api, exch.account(), markets, POLICY, out=lines.append,
                    calendar=good) == 0
    text = "\n".join(lines)
    assert "Forex Factory lu" in text and "CPI m/m" in text and "dans 2 h" in text

    def down(url):
        raise RuntimeError("HTTP 503")
    lines = []
    bad = cal.Calendar(str(tmp_path / "c2.json"), fetch=down)
    assert sr.check(api, exch.account(), markets, POLICY, out=lines.append,
                    calendar=bad) == 0
    text = "\n".join(lines)
    assert "calendrier : pas lu" in text and "OK : tout est pret" in text
    assert not [c for c in exch.calls if c[0] == "POST"]


# --------------------------------------------------------------------------
# v2.6 : BTC en pause, veto du calendrier, frais par marche
# --------------------------------------------------------------------------

def test_v26_settings():
    assert sr.VERSION.startswith(("2.6", "2.7"))
    assert sr.TABLE_MODES == {"calendrier": "veto"}
    assert sr.PAUSED_MARKETS == ("BTC",)
    assert sr.FEE_BPS == {"BTC": 3.0, "GOLD": 0.5}


def _runner_v26(tmp_path, exch, clock, table=None, strategies=None):
    api = ex.SandboxExecutor(KEY, session=exch, sleep=lambda _: None,
                             journal_path=str(tmp_path / "orders.log"),
                             fill_wait_seconds=0)
    markets = sr.pick_markets(MARKETS_LIST)
    strategies = strategies or {s: MomentumStrategy(FAST) for s in markets}
    paths = sr.Paths(**{f: str(tmp_path / getattr(sr.Paths(), f))
                        for f in sr.Paths.__dataclass_fields__})
    return sr.Runner(api, exch.account(), markets, strategies, POLICY, cfg=FAST,
                     paths=paths, echo=False, clock=clock, table=table,
                     paused=sr.PAUSED_MARKETS, fees_by_sym=sr.FEE_BPS)


def test_paused_market_is_watched_but_never_traded(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    r = _runner_v26(tmp_path, exch, clock)
    _calm(r, exch, clock)
    entries = []
    for k in range(1, 6):
        entries += _step(r, exch, clock, btc=80_000 + 60 * k, gold=4_000)["entries"]
    assert not [c for c in exch.calls if c[0] == "POST"]
    assert entries and entries[0] == ("BTC", "rejected:MARCHE_EN_PAUSE")
    v = [e for e in _rows(r.paths.trades) if e["event"] == "verdict"]
    assert v and v[0]["approved"] is False and v[0]["rejects"] == ["MARCHE_EN_PAUSE"]
    assert v[0]["stop"] < v[0]["entry"] < v[0]["tp"] and v[0]["notional"] > 0
    assert v[0]["fee_bps"] == 3.0
    changes = [e for e in _rows(r.paths.signals) if e["event"] == "signal_change"]
    assert any(e["sym"] == "BTC" and e["to"] == "buy" for e in changes)
    assert r.managed == {}


def test_gold_is_still_traded_with_its_real_fees(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    r = _runner_v26(tmp_path, exch, clock)
    _calm(r, exch, clock)
    entries = []
    for k in range(1, 6):
        entries += _step(r, exch, clock, btc=80_000, gold=4_000 + 3 * k)["entries"]
    assert entries and entries[0] == ("GOLD", "filled")
    posts = [c for c in exch.calls if c[0] == "POST" and c[1] == "/orders"]
    assert len(posts) == 1
    assert [i["sym"] for i in r.managed.values()] == ["GOLD"]
    v = [e for e in _rows(r.paths.trades) if e["event"] == "verdict"]
    assert v[0]["approved"] is True and v[0]["fee_bps"] == 0.5


def test_lower_fees_mean_a_slightly_bigger_size_for_the_same_risk():
    from perps_engine.risk import size_from_risk
    a = size_from_risk(1000, 4000, 20, 3.0)
    b = size_from_risk(1000, 4000, 20, 0.5)
    assert b > a
    assert b * 20 + b * 4000 * 2 * 0.5 / 10_000 == pytest.approx(1000)


def test_calendar_veto_is_noted_as_a_refused_verdict(tmp_path):
    news = _news_feed(Clock().t + 30 * 60)
    table = _table(tmp_path, news, modes=dict(sr.TABLE_MODES))
    r, _, _, posts = _trend_run(tmp_path, table)
    assert posts == []
    v = [e for e in _rows(r.paths.trades) if e["event"] == "verdict"]
    assert v and v[0]["approved"] is False and v[0]["rejects"] == ["VETO_CALENDRIER"]
    assert v[0]["notional"] > 0
    assert [e["by"] for e in _rows(r.paths.trades)
            if e["event"] == "entry_vetoed"] == [["calendrier"]]
    assert _rows(r.paths.table)[0]["resultat"] == "veto:calendrier"


def test_paused_market_still_exits_a_position_already_open(tmp_path):
    r, exch, clock = _open_one_btc_trade(tmp_path, "real")
    r.save_book()
    r2 = sr.Runner(r.api, exch.account(), r.markets, r.strategies, POLICY,
                   cfg=FAST, paths=r.paths, echo=False, clock=clock,
                   paused=sr.PAUSED_MARKETS, fees_by_sym=sr.FEE_BPS)
    assert len(r2.managed) == 1
    exits = []
    for i in range(12):
        exits += _step(r2, exch, clock, btc=_noise(i, 80_300, 10))["exits"]
        if exits:
            break
    assert exits, "la position BTC deja ouverte doit sortir normalement"
    assert not [c for c in exch.calls if c[0] == "POST" and c[1] == "/orders"][1:]


def test_start_event_names_paused_markets_and_fees(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    r = _runner_v26(tmp_path, exch, clock)
    r.log_start({"own": [], "observation": [], "warmup": []})
    start = _rows(r.paths.trades)[-1]
    assert start["version"].startswith(("2.6", "2.7"))
    assert start["paused_markets"] == ["BTC"]
    assert start["fee_bps"] == {"BTC": 3.0, "GOLD": 0.5}


def test_hold_to_target_keeps_the_position_until_stop_or_target(tmp_path):
    clock = Clock()
    exch = FakeExchange(clock)
    api = ex.SandboxExecutor(KEY, session=exch, sleep=lambda _: None,
                             journal_path=str(tmp_path / "orders.log"),
                             fill_wait_seconds=0)
    markets = sr.pick_markets(MARKETS_LIST)
    paths = sr.Paths(**{f: str(tmp_path / getattr(sr.Paths(), f))
                        for f in sr.Paths.__dataclass_fields__})
    r = sr.Runner(api, exch.account(), markets,
                  {s: MomentumStrategy(FAST) for s in markets}, POLICY, cfg=FAST,
                  paths=paths, echo=False, clock=clock, hold=("BTC",))
    _calm(r, exch, clock)
    for k in range(1, 6):
        _step(r, exch, clock, btc=80_000 + 60 * k)
    assert len(r.managed) == 1
    for i in range(12):                              # le signal retombe
        assert _step(r, exch, clock, btc=_noise(i, 80_300, 10))["exits"] == []
    assert len(r.managed) == 1                       # toujours ouverte
    assert not [c for c in exch.calls if c[0] == "POST" and "close" in c[1]]
    r.log_start({"own": [], "observation": [], "warmup": []})
    assert _rows(r.paths.trades)[-1]["hold_to_target"] == ["BTC"]


def test_gold_is_held_to_stop_or_target_since_yc_said_go():
    assert sr.HOLD_TO_TARGET == ("GOLD",)
