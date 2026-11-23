"""Telecharge les bougies 1 min Binance USDT-M et l'historique du funding.
Donnees publiques, aucune cle, aucun ordre.

Usage :  python -I telecharger_1m.py <dossier_donnees> <debut AAAA-MM-JJ> SYMBOLE [SYMBOLE ...]

Ecrit dans <dossier_donnees> :
  <SYMBOLE>_1m.csv       open_time_ms, open, high, low, close, volume, taker_buy, trades
  <SYMBOLE>_funding.csv  funding_time_ms, funding_rate
Si un fichier existe deja, seules les donnees manquantes sont ajoutees.
Seules les bougies FERMEES sont gardees. Un symbole liste apres <debut>
commence simplement a sa premiere bougie.
"""
import contextlib
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

KLINES = "https://fapi.binance.com/fapi/v1/klines"
FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"
PAS_MS = 60 * 1000
UA = {"User-Agent": "perps-engine-recherche"}


def _fmt(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _get(url):
    for essai in range(5):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # reseau ou 429 : on attend et on recommence
            if essai == 4:
                raise
            print(f"   reessai apres {type(e).__name__}", flush=True)
            time.sleep(5 * (essai + 1))


def _derniere_ligne(chemin):
    """Derniere ligne de donnees, ou None si le fichier n'a que l'en-tete.
    Ne lit que la fin du fichier, qui fait plusieurs dizaines de Mo."""
    if not os.path.exists(chemin):
        return None
    with open(chemin, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4096))
        lignes = [x for x in f.read().decode("utf-8", "replace").splitlines() if x.strip()]
    if not lignes:
        return None
    der = next(csv.reader([lignes[-1]]))
    return der if der and der[0].isdigit() else None


@contextlib.contextmanager
def verrou(chemin, attente_s=300, perime_s=900):
    """Un seul ecrivain a la fois par fichier. Le suivi des trades tourne tout
    seul toutes les 15 min et peut croiser un lancement a la main : sans verrou,
    les deux ajouteraient les memes bougies en double. Le second attend que le
    premier ait fini. Un verrou plus vieux que perime_s vient d'un lancement
    interrompu : il est ignore."""
    v = chemin + ".verrou"
    debut = time.time()
    while True:
        try:
            os.close(os.open(v, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError):
            try:
                if time.time() - os.path.getmtime(v) > perime_s:
                    os.remove(v)
                    continue
            except OSError:
                continue  # le verrou vient d'etre libere
            if time.time() - debut > attente_s:
                raise TimeoutError(f"{v} : un autre telechargement est en cours")
            time.sleep(1)
    try:
        yield
    finally:
        try:
            os.remove(v)
        except OSError:
            pass


def bougies(dossier, sym, debut_ms):
    chemin = os.path.join(dossier, f"{sym}_1m.csv")
    with verrou(chemin):
        der = _derniere_ligne(chemin)
        start = int(der[0]) + PAS_MS if der else debut_ms
        neuf = der is None
        maintenant = int(time.time() * 1000)
        n = 0
        with open(chemin, "a", newline="") as f:
            w = csv.writer(f)
            if neuf:
                w.writerow(["open_time_ms", "open", "high", "low", "close", "volume", "taker_buy", "trades"])
            while start < maintenant:
                paquet = _get(f"{KLINES}?symbol={sym}&interval=1m&startTime={start}&limit=1500")
                fermees = [k for k in paquet if int(k[6]) < maintenant]
                if not fermees:
                    break
                for k in fermees:
                    w.writerow([k[0], k[1], k[2], k[3], k[4], k[5], k[9], k[8]])
                n += len(fermees)
                start = int(fermees[-1][0]) + PAS_MS
                if len(paquet) < 1500:
                    break
                time.sleep(0.3)
    print(f"{sym} 1 min : +{n} bougies, jusqu'a {_fmt(start - PAS_MS)}", flush=True)


def funding(dossier, sym, debut_ms):
    chemin = os.path.join(dossier, f"{sym}_funding.csv")
    with verrou(chemin):
        der = _derniere_ligne(chemin)
        start = int(der[0]) + 1 if der else debut_ms
        neuf = der is None
        n = 0
        with open(chemin, "a", newline="") as f:
            w = csv.writer(f)
            if neuf:
                w.writerow(["funding_time_ms", "funding_rate"])
            while True:
                paquet = _get(f"{FUNDING}?symbol={sym}&startTime={start}&limit=1000")
                if not paquet:
                    break
                for x in paquet:
                    w.writerow([x["fundingTime"], x["fundingRate"]])
                n += len(paquet)
                start = int(paquet[-1]["fundingTime"]) + 1
                if len(paquet) < 1000:
                    break
                time.sleep(0.5)
    print(f"{sym} funding : +{n} valeurs", flush=True)


def main(dossier, debut, symboles):
    os.makedirs(dossier, exist_ok=True)
    debut_ms = int(datetime.strptime(debut, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    for sym in symboles:
        funding(dossier, sym, debut_ms)
        bougies(dossier, sym, debut_ms)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
