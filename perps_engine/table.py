"""
table.py - la table des associes (v2.5).

Le momentum propose une entree. Avant que l'ordre parte, chaque siege vote :
POUR, CONTRE ou ABSTIENT. Le vote et la suite (entree prise, refusee par le
garde-fou...) vont dans sandbox-table.log.

Deux modes par siege
--------------------
  observe  le vote est ecrit, il ne change rien      (v2.5 : tous les sieges)
  veto     un vote CONTRE annule l'entree
Un siege n'obtient le veto qu'au bilan, s'il fait mieux que le hasard.
Le president (risk.py : taille, plafond 3x, limites du compte) garde le
dernier mot comme avant : c'est lui qui accepte ou refuse l'ordre.

Bilan des votes (dans C:\\dev\\perps-engine) :
    python -m perps_engine.table
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from .graphique import load_trades

VOTES = ("pour", "contre", "abstient")
MODES = ("propose", "observe", "veto")
TABLE_LOG = "sandbox-table.log"
TRADES_LOG = "sandbox-trades.log"
LINK_TOLERANCE_SECONDS = 600
MIN_TRADES_TO_JUDGE = 10
ENTERED = ("filled", "pending", "uncertain")


class MomentumSeat:
    """Siege 1 : c'est lui qui propose. Son vote rappelle la force du signal."""
    name = "momentum"
    proposer = True

    def vote(self, ctx: dict) -> dict:
        z = ctx.get("z")
        force = f" z={z:+.2f}" if isinstance(z, (int, float)) else ""
        return {"vote": "pour", "raison": f"signal {ctx.get('side')}{force}"}


class Table:
    def __init__(self, seats: list, journal_path: str = TABLE_LOG,
                 modes: Optional[dict] = None) -> None:
        self.seats = list(seats)
        modes = modes or {}
        self.modes = {s.name: ("propose" if getattr(s, "proposer", False)
                               else modes.get(s.name, "observe"))
                      for s in self.seats}
        bad = {n: m for n, m in self.modes.items() if m not in MODES}
        if bad:
            raise ValueError(f"mode inconnu : {bad}")
        self.journal_path = journal_path
        self.counts: dict = {}
        self.deliberations = 0

    # -- chaque cycle --------------------------------------------------------
    def refresh(self, now: float) -> list:
        """Laisse chaque siege relire sa source. Rend les notes du journal."""
        notes = []
        for s in self.seats:
            fn = getattr(s, "refresh", None)
            if fn is None:
                continue
            try:
                note = fn(now)
            except Exception as e:
                note = {"event": "seat_error",
                        "detail": f"{type(e).__name__}: {e}"[:200]}
            if note:
                notes.append(dict(note, seat=s.name))
        return notes

    # -- a chaque entree proposee --------------------------------------------
    def deliberate(self, ctx: dict) -> dict:
        votes = {}
        for s in self.seats:
            try:
                v = dict(s.vote(ctx) or {})
            except Exception as e:
                # Un siege en panne s'abstient : il ne bloque rien, ne casse rien.
                v = {"vote": "abstient",
                     "raison": f"erreur {type(e).__name__}: {e}"[:160]}
            if v.get("vote") not in VOTES:
                v["vote"] = "abstient"
            v["mode"] = self.modes[s.name]
            votes[s.name] = v
        blockers = [n for n, v in votes.items()
                    if v["mode"] == "veto" and v["vote"] == "contre"]
        return {"sym": ctx.get("sym"), "side": ctx.get("side"),
                "z": ctx.get("z"), "price": ctx.get("price"),
                "votes": votes, "bloque_par": blockers}

    def record(self, deliberation: dict, result: str) -> None:
        self.deliberations += 1
        for name, v in (deliberation.get("votes") or {}).items():
            if self.modes.get(name) == "propose":
                continue
            c = self.counts.setdefault(name, {k: 0 for k in VOTES})
            k = v.get("vote") if v.get("vote") in VOTES else "abstient"
            c[k] += 1
        entry = {"event": "deliberation", **deliberation, "resultat": result,
                 "ts": datetime.now(timezone.utc).isoformat()}
        try:
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    # -- resume horaire ------------------------------------------------------
    def summary_lines(self, now: float) -> list:
        lines = []
        for s in self.seats:
            fn = getattr(s, "summary_line", None)
            if fn is None:
                continue
            try:
                lines.append(fn(now))
            except Exception:
                pass
        if not self.deliberations:
            lines.append("table : aucun vote depuis le lancement (pas de signal neuf)")
            return lines
        parts = [f"{n} ({_mode_fr(self.modes.get(n))}) pour {c['pour']}, "
                 f"contre {c['contre']}, abstient {c['abstient']}"
                 for n, c in self.counts.items()]
        lines.append(f"table : {self.deliberations} vote(s) depuis le lancement - "
                     + " ; ".join(parts))
        return lines


def _mode_fr(mode: Optional[str]) -> str:
    return "veto" if mode == "veto" else "observation"


# --------------------------------------------------------------------------
# Bilan : les votes face aux resultats des trades
# --------------------------------------------------------------------------

def _epoch(ts) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def load_deliberations(path: str = TABLE_LOG) -> list:
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("event") != "deliberation":
                continue
            t = _epoch(e.get("ts"))
            if t is None:
                continue
            e["t"] = t
            out.append(e)
    return out


def link(trades: list, deliberations: list,
         tolerance: float = LINK_TOLERANCE_SECONDS) -> list:
    """Relie chaque trade ferme au vote qui l'a ouvert : meme marche, meme
    sens, a 10 min pres. Rend [(trade, deliberation), ...]."""
    used, pairs = set(), []
    for t in trades:
        if t.get("open") or t.get("entry_ts") is None:
            continue
        best = None
        for i, d in enumerate(deliberations):
            if i in used or d.get("sym") != t.get("sym") \
                    or d.get("side") != t.get("side"):
                continue
            if str(d.get("resultat")) not in ENTERED:
                continue
            gap = abs(d["t"] - t["entry_ts"])
            if gap <= tolerance and (best is None or gap < best[1]):
                best = (i, gap)
        if best is not None:
            used.add(best[0])
            pairs.append((t, deliberations[best[0]]))
    return pairs


def verdict(groups: dict) -> str:
    against, favor = groups.get("contre", []), groups.get("pour", [])
    for label, nets in (("contre", against), ("pour", favor)):
        if len(nets) < MIN_TRADES_TO_JUDGE:
            return (f"trop tot pour juger : {len(nets)} trade(s) '{label}' "
                    f"sur {MIN_TRADES_TO_JUDGE} necessaires")
    mc, mp = sum(against) / len(against), sum(favor) / len(favor)
    if mc < mp:
        return (f"les trades 'contre' font {mp - mc:,.2f} $ de moins en moyenne : "
                "candidat au veto, a confirmer au bilan")
    return (f"les trades 'contre' ne font pas pire ({mc:+,.2f} $ contre "
            f"{mp:+,.2f} $) : pas de veto")


def report(trades_path: str = TRADES_LOG, table_path: str = TABLE_LOG) -> list:
    trades, _ = load_trades(trades_path)
    delibs = load_deliberations(table_path)
    L = ["TABLE DES ASSOCIES - bilan des votes"]
    if not delibs:
        L.append("  aucun vote enregistre pour l'instant (il faut un signal neuf).")
        return L

    entered = sum(1 for d in delibs if str(d.get("resultat")) in ENTERED)
    vetoed = sum(1 for d in delibs if str(d.get("resultat")).startswith("veto"))
    refused = len(delibs) - entered - vetoed
    pairs = link(trades, delibs)
    first = min(d["t"] for d in delibs)
    before = sum(1 for t in trades if not t.get("open")
                 and (t.get("entry_ts") or 0) < first - LINK_TOLERANCE_SECONDS)
    head = (f"  votes : {len(delibs)}   entrees prises : {entered}   "
            f"refusees par le garde-fou : {refused}")
    if vetoed:
        head += f"   annulees par veto : {vetoed}"
    L.append(head)
    L.append(f"  trades fermes relies a un vote : {len(pairs)}   "
             f"(fermes avant la table : {before})")

    seats, modes = [], {}
    for d in delibs:
        for name, v in (d.get("votes") or {}).items():
            if v.get("mode") == "propose":
                continue
            if name not in seats:
                seats.append(name)
            modes[name] = v.get("mode")

    for name in seats:
        counts = {k: 0 for k in VOTES}
        for d in delibs:
            v = ((d.get("votes") or {}).get(name) or {}).get("vote")
            counts[v if v in VOTES else "abstient"] += 1
        groups = {k: [] for k in VOTES}
        for t, d in pairs:
            v = ((d.get("votes") or {}).get(name) or {}).get("vote")
            groups[v if v in VOTES else "abstient"].append(float(t.get("net") or 0))
        L.append("")
        L.append(f"  {name} ({_mode_fr(modes.get(name))}) - votes : pour "
                 f"{counts['pour']}, contre {counts['contre']}, "
                 f"abstient {counts['abstient']}")
        for k in VOTES:
            nets = groups[k]
            if nets:
                L.append(f"    trades '{k}'{' ' * (8 - len(k))} : {len(nets):3}   "
                         f"net {sum(nets):+11,.2f} $   "
                         f"moyenne {sum(nets) / len(nets):+10,.2f} $")
            else:
                L.append(f"    trades '{k}'{' ' * (8 - len(k))} :   0")
        L.append("    -> " + verdict(groups))
    return L


def main(argv: Optional[list] = None) -> int:
    for line in report():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
