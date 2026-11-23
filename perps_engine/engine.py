"""
engine.py - observation multi-marches (v1.3).

Historique des corrections
--------------------------
v1.0  boucle d'observation, un marche, aucune execution
v1.1  l'equity de l'API ignore le PnL latent -> calcul maison + suivi du portage
v1.2  signal momentum normalise par la volatilite
v1.3  panier de 12 marches + classement transversal

Toujours aucune execution. client.py ne contient aucun verbe d'ecriture et le
prevol le verifie a chaque demarrage.

Deux journaux separes, parce qu'ils repondent a deux questions differentes :
  decisions.log  - etat du compte, verdicts du gardien
  signals.log    - mesures de marche, transitions de signal, classement
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

from . import alerte, basket, config, persistence, valuation
from .client import ReadOnlyClient
from .risk import AccountRules, AccountState, PlatformLimits, evaluate_order
from .strategy import MomentumStrategy

ACCOUNT_LOG = "decisions.log"
SIGNAL_LOG = "signals.log"
STATE_FILE = "state.json"
SAVE_EVERY_SECONDS = 300      # sauvegarde de l'etat toutes les 5 min

HEARTBEAT_SECONDS = 900     # releve complet toutes les 15 min
EQUITY_MOVE_USD = 5.0


def _write(path: str, entry: dict, echo: bool = True) -> None:
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if echo:
        print(json.dumps(entry, ensure_ascii=False))


def _preflight() -> None:
    from . import client
    src = open(client.__file__, encoding="utf-8").read()
    for verb in ("session.post", ".post(", ".delete(", ".put("):
        if verb in src:
            sys.exit(f"ARRET : verbe d'ecriture detecte dans client.py ({verb})")
    if not config.DRY_RUN:
        sys.exit("ARRET : DRY_RUN est faux. Cette version n'execute pas.")


def _mid(client, market_id: str):
    """Prix milieu, ou None. Un marche muet ne doit pas arreter les onze autres."""
    try:
        q = client.quote(market_id)
        mid = q.get("mid")
        if mid is None and q.get("bid") and q.get("ask"):
            mid = (float(q["bid"]) + float(q["ask"])) / 2
        return float(mid) if mid is not None else None
    except Exception:
        return None


def poll_markets(client, strategies: dict, now: float) -> dict:
    """Interroge chaque marche, alimente sa serie, rend les mesures."""
    measures, failures = {}, []
    for mkt in config.BASKET:
        strat = strategies[mkt.symbol]
        price = _mid(client, mkt.market_id)
        if price is None:
            failures.append(mkt.symbol)
        strat.observe(now, price)
        strat.decide(mkt.market_id, now)
        measures[mkt.symbol] = dict(strat.last_measure)
        measures[mkt.symbol]["klass"] = mkt.klass
    return {"measures": measures, "failures": failures}


def account_snapshot(profile, client, funding: valuation.FundingTracker) -> dict:
    acct = client.get_account(profile.account_id)
    policy_raw = client.trading_policy(profile.account_id)
    rules = AccountRules.from_policy(policy_raw, profile.starting_balance)

    positions = client.open_positions(profile.account_id) or []
    balance = float(acct.get("balance", 0))
    api_equity = float(acct.get("equity", balance))
    val = valuation.compute_equity(balance, positions, lambda m: _mid(client, m))
    equity = val["equity"]

    floor = rules.starting_balance * (1 - rules.drawdown_pct)
    if rules.drawdown_type == "trailing":
        hwm = float(acct.get("high_water_balance", 0)) or rules.starting_balance
        floor = max(hwm, rules.starting_balance) * (1 - rules.drawdown_pct)

    dist = valuation.floor_distance(equity, floor, rules.starting_balance)
    fund = funding.update(balance, time.time())

    return {"event": "account", "equity": equity,
            "api_equity": round(api_equity, 4),
            "equity_gap": round(equity - api_equity, 2),
            "unrealized_pnl": val["unrealized_pnl"],
            "floor": round(floor, 2), "open_positions": len(positions),
            **dist, **fund,
            "days_to_floor_from_funding": valuation.days_to_floor(
                dist["room_usd"], fund["funding_per_day"]),
            "breached": dist["room_usd"] <= 0,
            "valuation_reliable": val["reliable"],
            "_rules": rules, "_policy": policy_raw, "_state": AccountState(
                equity=equity, balance=balance, open_positions=len(positions))}


def main() -> None:
    _preflight()
    profile = config.ACCOUNTS[config.ACTIVE_ACCOUNT]
    client = ReadOnlyClient(profile.api_base, profile.api_key)
    cfg = profile.signal_config

    strategies = {m.symbol: MomentumStrategy(cfg) for m in config.BASKET}
    funding = valuation.FundingTracker()
    book = alerte.AlertBook()
    started_at = time.time()

    # Reprise apres redemarrage : evite de reperdre 3 h de chauffe.
    state = persistence.load(STATE_FILE)
    rec = persistence.restore(strategies, state)
    _write(SIGNAL_LOG, {"event": "resume", "ok": state["ok"],
                        "detail": state["reason"],
                        "restored": rec["restored"], "skipped": rec["skipped"]})

    _write(SIGNAL_LOG, {"event": "start", "version": "1.6",
                        "markets": [m.symbol for m in config.BASKET],
                        "entry_z": cfg.entry_z,
                        "warmup_minutes": cfg.warmup_minutes,
                        "expected_noise_signals_per_cycle":
                            basket.expected_false_positive_rate(
                                len(config.BASKET), cfg.entry_z)})

    prev_dirs: dict = {}
    last_heartbeat = last_save = 0.0
    breach_announced = False

    while True:
        if os.path.exists(config.HALT_FILE):
            persistence.save(STATE_FILE, strategies)
            _write(SIGNAL_LOG, {"event": "halt", "state_saved": True})
            return
        try:
            now = time.time()
            polled = poll_markets(client, strategies, now)
            measures = polled["measures"]

            for sym, m in measures.items():
                for msg in (book.note_market(sym, m.get("price") is not None),
                            book.note_signal(sym, m.get("direction"), now)):
                    if msg:
                        alerte.raise_alert(msg)
                        _write(SIGNAL_LOG, {"event": "alerte", "detail": msg})

            if now - last_save >= SAVE_EVERY_SECONDS:
                persistence.save(STATE_FILE, strategies)
                last_save = now

            # --- transitions de signal : ecrites immediatement -------------
            for sym, m in measures.items():
                d = m.get("direction")
                if d != prev_dirs.get(sym):
                    if d is not None or prev_dirs.get(sym) is not None:
                        book.note_transition()
                        _write(SIGNAL_LOG, {
                            "event": "signal_change", "sym": sym,
                            "klass": m.get("klass"), "from": prev_dirs.get(sym),
                            "to": d, "z": m.get("z"), "price": m.get("price"),
                            "stop_price": m.get("stop_price"),
                            "take_profit_price": m.get("take_profit_price"),
                            "stop_distance_pct": m.get("stop_distance_pct"),
                            "vol_lookback_pct": m.get("vol_lookback_pct")})
                    prev_dirs[sym] = d

            # --- releve periodique ----------------------------------------
            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                last_heartbeat = now
                rank = basket.cross_sectional_rank(measures)
                _write(SIGNAL_LOG, {
                    "event": "snapshot",
                    "active": [{"sym": s, "dir": m["direction"], "z": m["z"]}
                               for s, m in measures.items() if m["tradeable"]],
                    "warming": [s for s, m in measures.items()
                                if m.get("reason", "").startswith("chauffe")],
                    "failures": polled["failures"],
                    "cross_sectional": rank})

                acct = account_snapshot(profile, client, funding)
                rules, state = acct.pop("_rules"), acct.pop("_state")
                policy_raw = acct.pop("_policy")
                _write(ACCOUNT_LOG, acct, echo=False)

                msg = book.note_account(acct.get("room_pct_of_account"),
                                        acct.get("pct_of_drawdown_used"))
                if msg:
                    alerte.raise_alert(msg)
                    _write(SIGNAL_LOG, {"event": "alerte", "detail": msg})

                if alerte.due_for_summary(book):
                    alerte.write_summary(book, measures, acct,
                                         (time.time() - started_at) / 3600)

                if acct["breached"] and not breach_announced:
                    breach_announced = True
                    _write(SIGNAL_LOG, {
                        "event": "mode", "detail": "compte sous le plancher - "
                        "mode signal seul, les verdicts reprennent des qu'un "
                        "compte valide est configure"})

                # verdicts seulement si le compte a encore de la marge
                if not acct["breached"]:
                    limits = PlatformLimits.from_policy(
                        policy_raw, taker_fee_bps=profile.taker_fee_bps)
                    for sym, m in measures.items():
                        if not m["tradeable"]:
                            continue
                        strat = strategies[sym]
                        intent = strat.decide(
                            next(x.market_id for x in config.BASKET
                                 if x.symbol == sym), now)
                        if intent is None:
                            continue
                        v = evaluate_order(intent, state, rules, limits,
                                           profile.risk_policy)
                        _write(ACCOUNT_LOG, {
                            "event": "verdict", "sym": sym,
                            "side": intent.side, "z": m["z"],
                            "approved": v.approved, "size": v.computed_size,
                            "leverage": v.computed_leverage,
                            "rejects": v.reject_codes}, echo=False)

        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            _write(SIGNAL_LOG, {"event": "error", "detail": detail})
            msg = book.note_error(detail)
            if msg:
                alerte.raise_alert(msg)
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()
