"""Compare le signal rejoue sur les bougies Binance avec les signaux que le moteur
sandbox a reellement emis (sandbox-signals.log). Lecture seule.

Usage :  python -I verifier_live.py <dossier_moteur> <dossier_cache>
"""
import csv
import json
import os
import sys
from bisect import bisect_left
from datetime import datetime

MIN_MS = 60_000
FICHIERS = {"BTC": "BTCUSDT_signaux.csv", "GOLD": "XAUUSDT_signaux.csv"}
TOLERANCE_MIN = 5


def _ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def charger_cache(chemin, depuis_ms):
    t, d, z = [], [], []
    with open(chemin, newline="") as f:
        r = csv.reader(f)
        next(r)
        for x in r:
            ti = int(x[0]) + MIN_MS                  # instant du cycle = cloture
            if ti < depuis_ms:
                continue
            t.append(ti)
            d.append(int(x[1]))
            z.append(float(x[2]) if x[2] else None)
    return t, d, z


def main(dossier_moteur, cache):
    live = {"BTC": [], "GOLD": []}
    with open(os.path.join(dossier_moteur, "sandbox-signals.log"), encoding="utf-8") as f:
        for ligne in f:
            e = json.loads(ligne)
            if e.get("event") == "signal_change" and e.get("sym") in live:
                live[e["sym"]].append(e)
    # periodes de chauffe : demarrages sans serie restauree, et "rewarm"
    chauffes = {"BTC": [], "GOLD": []}
    with open(os.path.join(dossier_moteur, "sandbox-trades.log"), encoding="utf-8") as f:
        for ligne in f:
            e = json.loads(ligne)
            if e.get("event") == "start":
                for s in (e.get("restored") or {}).get("warmup", []):
                    if s in chauffes:
                        chauffes[s].append(_ms(e["ts"]))
    with open(os.path.join(dossier_moteur, "sandbox-signals.log"), encoding="utf-8") as f:
        for ligne in f:
            e = json.loads(ligne)
            if e.get("event") == "rewarm" and e.get("sym") in chauffes:
                chauffes[e["sym"]].append(_ms(e["ts"]))
    # le moteur tournait-il ? une ligne de prix par cycle dans prix-sandbox.csv
    vu_live = {"BTC": [], "GOLD": []}
    with open(os.path.join(dossier_moteur, "prix-sandbox.csv"), newline="") as f:
        r = csv.reader(f)
        next(r)
        for ts, sym, _ in r:
            if sym in vu_live:
                vu_live[sym].append(int(float(ts) * 1000))
    for s in vu_live:
        vu_live[s].sort()

    def tournait(sym, t):
        xs = vu_live[sym]
        k = bisect_left(xs, t - 3 * MIN_MS)
        if k >= len(xs) or xs[k] > t + 3 * MIN_MS:
            return False
        return not any(0 <= t - c < 4 * 3_600_000 for c in chauffes[sym])

    for sym, evts in live.items():
        nes = [e for e in evts if e["to"] in ("buy", "sell") and not e.get("note")]
        if not nes:
            continue
        depuis = _ms(evts[0]["ts"]) - 3_600_000
        t, d, z = charger_cache(os.path.join(cache, FICHIERS[sym]), depuis)
        nes_rejeu = [(t[k], d[k]) for k in range(1, len(t)) if d[k] != d[k - 1] and d[k] != 0]
        trouve, ecarts_z = 0, []
        for e in nes:
            te = _ms(e["ts"])
            sens = 1 if e["to"] == "buy" else -1
            if any(abs(tr - te) <= TOLERANCE_MIN * MIN_MS and s == sens for tr, s in nes_rejeu):
                trouve += 1
            k = min(range(len(t)), key=lambda j: abs(t[j] - te)) if t else None
            if k is not None and z[k] is not None and e.get("z") is not None:
                ecarts_z.append(abs(z[k] - e["z"]))
        comparables = [(tr, s) for tr, s in nes_rejeu if tr <= t[-1] and tournait(sym, tr)
                       and tr >= _ms(evts[0]["ts"])]
        confirmes = sum(1 for tr, s in comparables
                        if any(abs(_ms(e["ts"]) - tr) <= TOLERANCE_MIN * MIN_MS
                               and (1 if e["to"] == "buy" else -1) == s for e in nes))
        ecarts_z.sort()
        med = ecarts_z[len(ecarts_z) // 2] if ecarts_z else None
        print(f"{sym}: {len(nes)} signaux nes en direct, {trouve} retrouves dans le rejeu "
              f"(a {TOLERANCE_MIN} min pres, meme sens) ; {len(comparables)} signaux du rejeu "
              f"pendant que le moteur tournait (hors chauffe), {confirmes} vus aussi en direct ; "
              f"ecart median de z au meme instant : {med:.2f}" if med is not None else "")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
