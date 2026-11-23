"""Fait tourner le code du moteur sur les bougies 1 min, cycle par cycle, comme
sandbox_run.py le fait toutes les 60 s. Aucune ligne du moteur n'est copiee ni
modifiee : MomentumStrategy, PriceBuffer et SignalConfig sont importes tels quels.

Le prix vu a chaque cycle est la cloture de la bougie 1 min (le moteur lit le
prix milieu de la plateforme environ toutes les 60 s).

Usage (python du venv du moteur, qui a requests) :
  .venv\\Scripts\\python -I signaux_moteur.py <dossier_donnees> <dossier_cache> SYMBOLE...
Ecrit <SYMBOLE>_signaux.csv : open_time_ms, dir (+1 achat, -1 vente, 0), z,
stop_distance, vu (le cycle precedent avait deja un z), raison
"""
import csv
import os
import sys
import time

ICI = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.dirname(os.path.dirname(ICI))
sys.path.insert(0, RACINE)

from perps_engine import sandbox_run as SR            # noqa: E402
from perps_engine.signals import PriceBuffer, SignalConfig  # noqa: E402
from perps_engine.strategy import MomentumStrategy     # noqa: E402

CODES = {"buy": 1, "sell": -1}


def code_raison(r):
    if not r:
        return ""
    if r == "signal actif":
        return "actif"
    for debut, code in (("chauffe", "chauffe"), ("stop ", "cout"), ("|z|", "z"),
                        ("renversement", "z"), ("volatilite nulle", "vol0"),
                        ("historique", "hist")):
        if r.startswith(debut):
            return code
    return r[:20]


def rejouer(chemin_1m, chemin_sortie, sym):
    cfg = SignalConfig()
    strat = MomentumStrategy(cfg)
    z_avant = False          # le cycle precedent avait-il un z ? (seen_before)
    n = 0
    t0 = time.time()
    with open(chemin_1m, newline="") as f, open(chemin_sortie, "w", newline="") as g:
        r = csv.reader(f)
        next(r)
        w = csv.writer(g)
        w.writerow(["open_time_ms", "dir", "z", "stop_distance", "vu", "raison"])
        for x in r:
            t_open = int(x[0])
            prix = float(x[4])
            now = (t_open + 60_000) / 1000.0
            # --- copie de l'ordre des operations de Runner.cycle() ---------
            last = strat.buffer.last
            if last is not None and now - last[0] > SR.GAP_RESET_SECONDS:
                strat.buffer = PriceBuffer(max_minutes=1440)
                strat.direction = None
                z_avant = False                 # self.measures.pop(sym)
            strat.observe(now, prix)
            avant = strat.direction
            vu = z_avant
            strat.decide(sym, now)
            m = strat.last_measure
            if m.get("reason") in SR.DATA_GAP_REASONS:
                strat.direction = avant
                d = avant
            else:
                d = m.get("direction")
            z_avant = m.get("z") is not None
            w.writerow([t_open, CODES.get(d, 0), m.get("z", ""), m.get("stop_distance", ""),
                        int(vu), code_raison(m.get("reason"))])
            n += 1
    print(f"{sym} : {n} cycles rejoues en {time.time() - t0:.0f} s", flush=True)


def main(dossier_donnees, dossier_cache, symboles):
    os.makedirs(dossier_cache, exist_ok=True)
    assert SR.POLL_SECONDS == 60 and SignalConfig().lookback_minutes == 60
    for sym in symboles:
        rejouer(os.path.join(dossier_donnees, f"{sym}_1m.csv"),
                os.path.join(dossier_cache, f"{sym}_signaux.csv"), sym)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
