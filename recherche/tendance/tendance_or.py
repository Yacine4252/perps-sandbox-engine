"""Suivi de tendance sur l'or en 4 h et en journalier, BTC en rappel (22-23 nov 2026).

Demande de Claude app, accord de Yc (yfinance deja installe le 21 nov). Lecture de
prix publics seulement : aucun ordre, moteur, parametres et .env non touches.
Rien n'est regle sur les trades du sandbox : memes 5 systemes et memes stops
(2 et 3 ATR) que tendance_long.py, dont on reprend la simulation telle quelle.

Donnees de l'or :
  PAXGUSDT spot Binance (jeton adosse a l'or, cote 24 h/24 comme le sandbox),
      1 j et 4 h, depuis son lancement le 28 aout 2020 ;
  XAUUSDT perpetuel Binance (l'or du sandbox), 4 h, depuis le 11 dec 2025 :
      historique court, a lire comme un controle ;
  GC=F (contrat a terme, yfinance) : 1 h regroupe en 4 h UTC, sur les 730 derniers
      jours (limite de yfinance), et le journalier deja telecharge le 21 nov.
BTC : fichiers deja telecharges par tendance_long.py (jusqu'au 21 nov).

Usage : <python du venv> -I tendance_or.py <dossier_donnees> <dossier_resultats>
"""
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tendance_long as tl  # noqa: E402

FUTURES = "https://fapi.binance.com/fapi/v1/klines"
H4 = 4 * 3_600_000


def lire(chemin):
    with open(chemin, newline="") as f:
        r = csv.reader(f)
        next(r)
        return [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in r]


def ecrire(chemin, lignes):
    with open(chemin, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c"])
        w.writerows(lignes)
    return lignes


def binance(dossier, base, symbole, intervalle, debut_ms=0):
    lignes, start, maintenant = [], debut_ms, int(time.time() * 1000)
    while start < maintenant:
        paquet = tl._get(f"{base}?symbol={symbole}&interval={intervalle}&startTime={start}&limit=1000")
        fermees = [k for k in paquet if int(k[6]) < maintenant]
        if not fermees:
            break
        lignes += [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in fermees]
        start = int(fermees[-1][0]) + 1
        if len(paquet) < 1000:
            break
        time.sleep(0.3)
    return ecrire(os.path.join(dossier, f"{symbole}_{intervalle}.csv"), lignes)


def gcf_4h(dossier):
    """GC=F 1 h sur 730 jours, regroupe en bougies 4 h alignees sur 00/04/08/... UTC."""
    import yfinance as yf
    df = yf.download("GC=F", period="730d", interval="1h", auto_adjust=False, progress=False)
    if hasattr(df.columns, "levels"):
        df.columns = [c[0] for c in df.columns]
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    paquets = {}
    for ix, r in df.iterrows():
        t = int(ix.timestamp() * 1000)
        cle = t - t % H4
        o, h, l, c = float(r.Open), float(r.High), float(r.Low), float(r.Close)
        if cle not in paquets:
            paquets[cle] = [cle, o, h, l, c]
        else:
            p = paquets[cle]
            p[2], p[3], p[4] = max(p[2], h), min(p[3], l), c
    lignes = [tuple(paquets[k]) for k in sorted(paquets)]
    maintenant = int(time.time() * 1000)
    lignes = [x for x in lignes if x[0] + H4 <= maintenant]          # bougies terminees seulement
    return ecrire(os.path.join(dossier, "GCF_4h.csv"), lignes)


def barres_par_semaine(b):
    """Nombre median de bougies par semaine ISO (30 environ pour GC=F en 4 h)."""
    n = defaultdict(int)
    for x in b:
        n[datetime.fromtimestamp(x[0] / 1000, timezone.utc).isocalendar()[:2]] += 1
    v = sorted(n.values())[1:-1] or sorted(n.values())
    return v[len(v) // 2]


def main(dossier, sortie):
    os.makedirs(dossier, exist_ok=True)
    os.makedirs(sortie, exist_ok=True)
    g4 = gcf_4h(dossier)
    bps_g4 = barres_par_semaine(g4)
    jeux = {
        ("GOLD", "PAXG 1 j"): (binance(dossier, tl.SPOT, "PAXGUSDT", "1d"), 7, 1.0),
        ("GOLD", "PAXG 4 h"): (binance(dossier, tl.SPOT, "PAXGUSDT", "4h"), 42, 1 / 6),
        ("GOLD", "XAUUSDT perp 4 h"): (binance(dossier, FUTURES, "XAUUSDT", "4h"), 42, 1 / 6),
        ("GOLD", "GC=F 4 h"): (g4, bps_g4, 7 / (5 * 6)),
        ("GOLD", "GC=F 1 j"): (lire(os.path.join(dossier, "GCF_1d.csv")), 5, 7 / 5),
        ("BTC", "BTC 1 j"): (lire(os.path.join(dossier, "BTCUSDT_1d.csv")), 7, 1.0),
        ("BTC", "BTC 4 h"): (lire(os.path.join(dossier, "BTCUSDT_4h.csv")), 42, 1 / 6),
    }
    res, lignes = {}, []
    for (sym, nom), (b, bps, jpb) in jeux.items():
        ans = (b[-1][0] - b[0][0]) / (365.25 * 86_400_000)
        lignes.append(f"=== {nom} : {len(b)} bougies ({bps} par semaine), du "
                      f"{datetime.fromtimestamp(b[0][0] / 1000, timezone.utc):%Y-%m-%d} au "
                      f"{datetime.fromtimestamp(b[-1][0] / 1000, timezone.utc):%Y-%m-%d} ({ans:.1f} ans)")
        for systeme in tl.SYSTEMES:
            for k_stop in tl.STOPS:
                tr = tl.simuler(b, sym, systeme, k_stop, bps, jpb)
                s = tl.stats(tr)
                annees = defaultdict(list)
                for x in tr:
                    annees[datetime.fromtimestamp(x["t_sortie"] / 1000, timezone.utc).year].append(x)
                par_an = {an: tl.stats(lot) for an, lot in sorted(annees.items())}
                res[f"{nom} {systeme} stop {k_stop:g} ATR"] = {"tout": s, "par_an": par_an, "ans": ans}
                if not s["n"]:
                    lignes.append(f"{systeme:10} stop {k_stop:g} ATR : aucun trade")
                    continue
                lignes.append(
                    f"{systeme:10} stop {k_stop:g} ATR : {s['n']:4} trades ({s['n'] / ans:.0f}/an), "
                    f"esperance {s['esperance']:+.3f} R, PF {s['pf'] or 0:.2f}, total {s['total']:+.1f} R, "
                    f"DD max {s['dd']:.1f} R, pertes de suite {s['serie_pertes']}, "
                    f"plus bas {s['plus_bas_depuis_debut']:+.1f} R, compte a 3 % "
                    f"{'SURVIT' if s['survit_3pct'] else 'MEURT'}")
                lignes.append("     par an : " + " | ".join(
                    f"{an} {st['esperance']:+.2f} R x{st['n']}" for an, st in par_an.items()))
    texte = "\n".join(lignes)
    with open(os.path.join(sortie, "tendance_or.txt"), "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    with open(os.path.join(sortie, "tendance_or.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=str)
    print(texte)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(*sys.argv[1:])
