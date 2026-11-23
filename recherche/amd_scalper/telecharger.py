"""Telecharge les bougies 5 min Binance USDT-M (donnees publiques, aucune cle).

Usage :  python -I telecharger.py <dossier_donnees> SYMBOLE [SYMBOLE ...]

Ecrit <SYMBOLE>_5m.csv (open_time_ms, open, high, low, close, volume).
Si le fichier existe deja, seules les bougies manquantes sont ajoutees.
Seules les bougies FERMEES sont gardees.
"""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

BASE = "https://fapi.binance.com/fapi/v1/klines"
PAS_MS = 5 * 60 * 1000
# Premiere bougie XAUUSDT disponible : 2025-12-11. On prend la meme periode pour le BTC.
DEBUT_MS = int(datetime(2025, 12, 11, tzinfo=timezone.utc).timestamp() * 1000)


def _fmt(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _paquet(sym, start_ms):
    url = f"{BASE}?symbol={sym}&interval=5m&startTime={start_ms}&limit=1500"
    req = urllib.request.Request(url, headers={"User-Agent": "perps-engine-recherche"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def telecharger(dossier, sym):
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, f"{sym}_5m.csv")
    lignes = []
    if os.path.exists(chemin):
        with open(chemin, newline="") as f:
            lignes = list(csv.reader(f))[1:]
    debut = int(lignes[-1][0]) + PAS_MS if lignes else DEBUT_MS
    maintenant = int(time.time() * 1000)
    nouvelles = 0
    while debut < maintenant - PAS_MS:
        paquet = _paquet(sym, debut)
        if not paquet:
            break
        for k in paquet:
            if int(k[6]) < maintenant:          # close_time passe : bougie fermee
                lignes.append([k[0], k[1], k[2], k[3], k[4], k[5]])
                nouvelles += 1
        debut = int(paquet[-1][0]) + PAS_MS
        if len(paquet) < 1500:
            break
        time.sleep(0.25)
    with open(chemin, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time_ms", "open", "high", "low", "close", "volume"])
        w.writerows(lignes)
    print(f"{sym} : {len(lignes)} bougies ({nouvelles} nouvelles), "
          f"de {_fmt(lignes[0][0])} a {_fmt(lignes[-1][0])}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    for s in sys.argv[2:]:
        telecharger(sys.argv[1], s)
