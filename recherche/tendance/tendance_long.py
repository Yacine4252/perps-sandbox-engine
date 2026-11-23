"""Suivi de tendance a horizon long (demande de Claude app, go de Yc, 22 nov 2026).
Lecture de donnees publiques, aucun ordre, moteur non modifie.

Donnees : BTCUSDT Binance (spot, bougies 4 h et journalieres depuis 2019) ;
or : contrat GC=F journalier via yfinance (proxy de l'or du sandbox).

Deux systemes connus, sans rien optimiser :
  donchian20 : entree a la cloture au-dela du plus haut (plus bas) des 20
               bougies precedentes, sortie a la cassure inverse de 10 bougies ;
  donchian55 : pareil avec 55 / 20 (style Turtle) ;
  tsmom1/4/12 : momentum de serie temporelle, evalue chaque semaine : achat si
               le rendement sur 1, 4 ou 12 semaines est positif, vente sinon ;
               sortie quand le signe change.
Stop a 2 ATR et a 3 ATR (ATR 20, moyenne simple), touche dans la bougie
(rempli a l'ouverture si elle saute le stop). Entrees et sorties de signal a
l'ouverture de la bougie suivante : pas de regard vers l'avenir.
Frais par cote : BTC 3 points de base, or 0,5. Portage du sandbox, paye dans
les deux sens : BTC 0,0276 %, or 0,0153 % du notionnel par jour.
Risque de 0,5 % du compte par trade : 1 R = la distance au stop.

Compte a drawdown statique de 3 % : il meurt si son equite passe sous 97 % du
depart, soit -6 R cumules a 0,5 % de risque. Teste en partant au debut de
chaque annee, et depuis le debut des donnees.

Usage : <python du venv> -I tendance_long.py <dossier_donnees> <dossier_resultats>
"""
import csv
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

SPOT = "https://api.binance.com/api/v3/klines"
FRAIS = {"BTC": 3.0, "GOLD": 0.5}
PORTAGE = {"BTC": 0.000276, "GOLD": 0.000153}
DEBUT = "2019-01-01"
SYSTEMES = ("donchian20", "donchian55", "tsmom1", "tsmom4", "tsmom12")
STOPS = (2.0, 3.0)
JOUR = 86_400_000


def _get(url):
    for essai in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "perps-engine-recherche"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if essai == 4:
                raise
            time.sleep(3 * (essai + 1))


def binance(dossier, intervalle):
    chemin = os.path.join(dossier, f"BTCUSDT_{intervalle}.csv")
    debut = int(datetime.strptime(DEBUT, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    lignes, start, maintenant = [], debut, int(time.time() * 1000)
    while start < maintenant:
        paquet = _get(f"{SPOT}?symbol=BTCUSDT&interval={intervalle}&startTime={start}&limit=1000")
        fermees = [k for k in paquet if int(k[6]) < maintenant]
        if not fermees:
            break
        lignes += [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in fermees]
        start = int(fermees[-1][0]) + 1
        if len(paquet) < 1000:
            break
        time.sleep(0.3)
    with open(chemin, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c"])
        w.writerows(lignes)
    return lignes


def or_gcf(dossier):
    import yfinance as yf
    df = yf.download("GC=F", start=DEBUT, interval="1d", auto_adjust=False, progress=False)
    if hasattr(df.columns, "levels"):
        df.columns = [c[0] for c in df.columns]
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    lignes = [(int(ix.timestamp() * 1000), float(r.Open), float(r.High), float(r.Low), float(r.Close))
              for ix, r in df.iterrows()]
    with open(os.path.join(dossier, "GCF_1d.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c"])
        w.writerows(lignes)
    return lignes


def atr(b, n=20):
    out, tr = [None] * len(b), []
    for k in range(len(b)):
        _, _, h, l, c = b[k]
        pc = b[k - 1][4] if k else c
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        if k >= n:
            out[k] = sum(tr[k - n + 1:k + 1]) / n
    return out


def signaux(b, systeme, barres_par_semaine):
    """Pour chaque bougie, apres sa cloture : +1, -1, 0 (sortir) ou None (rien)."""
    n = len(b)
    out = [None] * n
    if systeme.startswith("donchian"):
        ent, sor = (20, 10) if systeme == "donchian20" else (55, 20)
        for k in range(ent, n):
            c = b[k][4]
            hh = max(x[2] for x in b[k - ent:k])
            ll = min(x[3] for x in b[k - ent:k])
            hs = max(x[2] for x in b[k - sor:k])
            ls = min(x[3] for x in b[k - sor:k])
            if c > hh:
                out[k] = 1
            elif c < ll:
                out[k] = -1
            else:
                out[k] = ("sortie", hs, ls)
        return out
    semaines = int(systeme[5:])
    recul = semaines * barres_par_semaine
    semaine_prec = None
    for k in range(recul, n):
        sem = datetime.fromtimestamp(b[k][0] / 1000, timezone.utc).isocalendar()[:2]
        fin_semaine = k + 1 == n or datetime.fromtimestamp(b[k + 1][0] / 1000, timezone.utc).isocalendar()[:2] != sem
        if fin_semaine and sem != semaine_prec:
            semaine_prec = sem
            out[k] = 1 if b[k][4] > b[k - recul][4] else -1
    return out


def simuler(b, sym, systeme, k_stop, barres_par_semaine, jours_par_barre):
    a = atr(b)
    sig = signaux(b, systeme, barres_par_semaine)
    frais = FRAIS[sym] / 1e4
    trades, pos, voulu = [], None, None
    for k in range(1, len(b)):
        t, o, h, l, c = b[k]
        # 1. ordre decide a la cloture precedente, execute a l'ouverture
        if voulu is not None:
            if pos and voulu != pos["d"]:
                trades.append(fermer(pos, o, t, "signal", frais, sym, jours_par_barre, k))
                pos = None
            if voulu in (1, -1) and pos is None and a[k - 1]:
                dist = k_stop * a[k - 1]
                pos = {"d": voulu, "entree": o, "stop": o - voulu * dist, "dist": dist, "t": t, "k": k}
            voulu = None
        # 2. stop dans la bougie
        if pos and ((pos["d"] == 1 and l <= pos["stop"]) or (pos["d"] == -1 and h >= pos["stop"])):
            prix = min(o, pos["stop"]) if pos["d"] == 1 else max(o, pos["stop"])
            trades.append(fermer(pos, prix, t, "stop", frais, sym, jours_par_barre, k))
            pos = None
        # 3. signal a la cloture
        s = sig[k]
        if isinstance(s, tuple):                      # donchian : cassure de sortie ?
            if pos and ((pos["d"] == 1 and c < s[2]) or (pos["d"] == -1 and c > s[1])):
                voulu = 0
        elif s in (1, -1):
            if pos is None or s != pos["d"]:
                voulu = s
    if pos:
        trades.append(fermer(pos, b[-1][4], b[-1][0], "fin", frais, sym, jours_par_barre, len(b)))
    return trades


def fermer(pos, prix, t, motif, frais, sym, jours_par_barre, k):
    brut = (prix - pos["entree"]) * pos["d"] / pos["dist"]
    cout = frais * (pos["entree"] + prix) / pos["dist"]
    jours = (k - pos["k"]) * jours_par_barre
    portage = PORTAGE[sym] * jours * pos["entree"] / pos["dist"]
    return {"t_entree": pos["t"], "t_sortie": t, "sens": pos["d"], "motif": motif,
            "r": brut - cout - portage, "brut": brut, "frais": cout, "portage": portage, "jours": jours}


def stats(lot):
    rs = [x["r"] for x in lot]
    if not rs:
        return {"n": 0}
    gains, pertes = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    cumul = pic = dd = 0.0
    serie = serie_max = 0
    plus_bas = 0.0
    for r in rs:
        cumul += r
        pic = max(pic, cumul)
        dd = max(dd, pic - cumul)
        plus_bas = min(plus_bas, cumul)
        serie = serie + 1 if r < 0 else 0
        serie_max = max(serie_max, serie)
    return {"n": len(rs), "esperance": sum(rs) / len(rs), "pf": gains / pertes if pertes else None,
            "total": sum(rs), "dd": dd, "serie_pertes": serie_max, "plus_bas_depuis_debut": plus_bas,
            "survit_3pct": plus_bas > -6.0, "gagnants_pct": 100 * sum(r > 0 for r in rs) / len(rs)}


def main(dossier, sortie):
    os.makedirs(dossier, exist_ok=True)
    os.makedirs(sortie, exist_ok=True)
    jeux = {("BTC", "1d"): (binance(dossier, "1d"), 7, 1.0),
            ("BTC", "4h"): (binance(dossier, "4h"), 42, 1 / 6),
            ("GOLD", "1d"): (or_gcf(dossier), 5, 7 / 5)}
    res, lignes = {}, []
    for (sym, tf), (b, bps, jpb) in jeux.items():
        lignes.append(f"=== {sym} {tf} : {len(b)} bougies, du "
                      f"{datetime.fromtimestamp(b[0][0] / 1000, timezone.utc):%Y-%m-%d} au "
                      f"{datetime.fromtimestamp(b[-1][0] / 1000, timezone.utc):%Y-%m-%d}")
        for systeme in SYSTEMES:
            for k_stop in STOPS:
                tr = simuler(b, sym, systeme, k_stop, bps, jpb)
                s = stats(tr)
                annees = defaultdict(list)
                for x in tr:
                    annees[datetime.fromtimestamp(x["t_sortie"] / 1000, timezone.utc).year].append(x)
                par_an = {an: stats(lot) for an, lot in sorted(annees.items())}
                res[f"{sym} {tf} {systeme} stop {k_stop:g} ATR"] = {"tout": s, "par_an": par_an}
                if not s["n"]:
                    continue
                morts = [an for an, st in par_an.items() if not st["survit_3pct"]]
                lignes.append(
                    f"{systeme:10} stop {k_stop:g} ATR : {s['n']:4} trades, esperance {s['esperance']:+.3f} R, "
                    f"PF {s['pf']:.2f}, total {s['total']:+.1f} R, DD max {s['dd']:.1f} R, "
                    f"pertes de suite {s['serie_pertes']}, depuis 2019 {'SURVIT' if s['survit_3pct'] else 'MEURT'} "
                    f"(plus bas {s['plus_bas_depuis_debut']:+.1f} R) ; annees ou un depart au 1er janvier meurt : "
                    f"{morts if morts else 'aucune'}")
                lignes.append("     par an : " + " | ".join(
                    f"{an} {st['esperance']:+.2f} R x{st['n']} PF {st['pf'] or 0:.1f} DD {st['dd']:.1f}"
                    for an, st in par_an.items()))
    texte = "\n".join(lignes)
    with open(os.path.join(sortie, "tendance_long.txt"), "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    with open(os.path.join(sortie, "tendance_long.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=str)
    print(texte)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(*sys.argv[1:])
