r"""Rapport d'edge du perps-engine - reconstruit les trades papier depuis signals.log.

Lecture seule : ne touche ni au moteur ni aux logs.
Les trades sont reconstruits a partir des changements de signal :
  entree au prix du signal (buy/sell), sortie au prix du signal_off (ou au flip).
  R = pnl / distance au stop. Approximation : pas de modelisation des stops
  touches en cours de trade (sortie au prix de cloture du signal).
Usage: C:\dev\perps-engine\python312\python.exe edge_report.py
"""
import json
import math
from collections import defaultdict
from datetime import datetime

SIGNALS = r"C:\dev\perps-engine\signals.log"
V16_START = "2026-09-21T23:35:00+00:00"  # installation v16 validee de bout en bout


def close_trade(o, exit_px, exit_ts, reason):
    pnl = (exit_px - o["entry_px"]) * o["dir"]
    risk = abs(o["entry_px"] - o["stop_px"]) if o["stop_px"] else None
    r = pnl / risk if risk else None
    hold = (datetime.fromisoformat(exit_ts) - datetime.fromisoformat(o["entry_ts"])).total_seconds() / 60.0
    return {
        "sym": o["sym"],
        "dir": "long" if o["dir"] > 0 else "short",
        "entry_ts": o["entry_ts"],
        "exit_ts": exit_ts,
        "r": r,
        "hold_min": hold,
        "reason": reason,
        "regime": "v16" if o["entry_ts"] >= V16_START else "pre-v16",
    }


trades, open_pos = [], {}
n_flip = 0
for line in open(SIGNALS, encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line:
        continue
    d = json.loads(line)
    if d.get("event") != "signal_change":
        continue
    sym, to = d["sym"], d["to"]
    ts, px = d["ts"], d["price"]
    if to in ("buy", "sell"):
        if sym in open_pos:  # retournement direct buy<->sell
            trades.append(close_trade(open_pos.pop(sym), px, ts, "flip"))
            n_flip += 1
        open_pos[sym] = {
            "sym": sym,
            "dir": 1 if to == "buy" else -1,
            "entry_ts": ts,
            "entry_px": px,
            "stop_px": d.get("stop_price"),
            "z": d.get("z"),
        }
    elif to is None and sym in open_pos:
        trades.append(close_trade(open_pos.pop(sym), px, ts, "signal_off"))


def stats(ts):
    rs = [t["r"] for t in ts if t["r"] is not None]
    n = len(rs)
    if n < 2:
        return {"n": n}
    mean = sum(rs) / n
    var = sum((x - mean) ** 2 for x in rs) / (n - 1)
    sd = math.sqrt(var)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    wins = sum(1 for x in rs if x > 0)
    holds = sorted(t["hold_min"] for t in ts if t["r"] is not None)
    med_hold = holds[len(holds) // 2]
    return {
        "n": n,
        "winrate": wins / n,
        "mean_r": mean,
        "sd_r": sd,
        "t_stat": t,
        "total_r": sum(rs),
        "best": max(rs),
        "worst": min(rs),
        "med_hold_min": med_hold,
    }


print("trades reconstruits : %d | signaux encore ouverts : %d | flips : %d"
      % (len(trades), len(open_pos), n_flip))
for regime in ("v16", "pre-v16"):
    s = stats([t for t in trades if t["regime"] == regime])
    print("--- regime %s ---" % regime)
    if s["n"] < 2:
        print("  pas assez de trades (n=%d)" % s["n"])
        continue
    print("  n=%d  winrate=%.1f%%  R moyen=%+.2f  R total=%+.1f" % (
        s["n"], 100 * s["winrate"], s["mean_r"], s["total_r"]))
    print("  t-stat=%.2f  (seuil vise : 2.3 | cible : ~70 trades)" % s["t_stat"])
    print("  meilleur=%+.2fR  pire=%+.2fR  duree mediane=%.0f min" % (
        s["best"], s["worst"], s["med_hold_min"]))

per_sym = defaultdict(list)
for t in trades:
    if t["regime"] == "v16" and t["r"] is not None:
        per_sym[t["sym"]].append(t["r"])
print("--- par symbole (v16) ---")
for sym, rs in sorted(per_sym.items(), key=lambda kv: -len(kv[1])):
    print("  %s : n=%d  R total=%+.1f" % (sym, len(rs), sum(rs)))
