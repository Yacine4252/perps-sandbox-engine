"""Suivi de tendance sur d'autres marches du sandbox : ETH, SOL, Nasdaq 100 (23 nov 2026).

Demande de Yc (« Oui, teste-les »). Lecture de prix publics seulement : aucun ordre,
moteur, parametres et .env non touches. Memes 5 systemes, memes stops (2 et 3 ATR),
meme simulation que tendance_long.py, rien n'est regle.

Donnees :
  ETHUSDT spot Binance, 1 j et 4 h, depuis le 1er janv 2019 ;
  SOLUSDT spot Binance, 1 j et 4 h, depuis son lancement (aout 2020) ;
  Nasdaq 100 : contrat NQ=F (yfinance), journalier depuis 2019, et 1 h regroupe en
      4 h UTC sur les 730 derniers jours. Le sandbox le propose sous XYZ100 et QQQ.
Frais : la plateforme ne publie pas ceux de ces marches ; on prend ceux du BTC
(3 points de base par cote) et son portage (0,0276 % par jour), dans les deux sens.

En plus : part du temps ou la position du systeme est dans le meme sens que celle
du BTC (meme systeme, meme stop, journalier) : un marche toujours dans le meme
sens que le BTC n'ajoute pas grand-chose.

Usage : <python du venv> -I tendance_marches.py <dossier_donnees> <dossier_resultats>
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tendance_long as tl  # noqa: E402
import tendance_or as to  # noqa: E402

for _sym in ("ETH", "SOL", "NAS100"):          # frais et portage du BTC, faute de mieux
    tl.FRAIS[_sym] = tl.FRAIS["BTC"]
    tl.PORTAGE[_sym] = tl.PORTAGE["BTC"]

JOUR = 86_400_000


def yf_jour(dossier, ticker, nom):
    import yfinance as yf
    df = yf.download(ticker, start=tl.DEBUT, interval="1d", auto_adjust=False, progress=False)
    if hasattr(df.columns, "levels"):
        df.columns = [c[0] for c in df.columns]
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    lignes = [(int(ix.timestamp() * 1000), float(r.Open), float(r.High), float(r.Low), float(r.Close))
              for ix, r in df.iterrows()]
    return to.ecrire(os.path.join(dossier, f"{nom}_1d.csv"), lignes)


def yf_4h(dossier, ticker, nom):
    import yfinance as yf
    df = yf.download(ticker, period="730d", interval="1h", auto_adjust=False, progress=False)
    if hasattr(df.columns, "levels"):
        df.columns = [c[0] for c in df.columns]
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    paquets = {}
    for ix, r in df.iterrows():
        t = int(ix.timestamp() * 1000)
        cle = t - t % to.H4
        o, h, l, c = float(r.Open), float(r.High), float(r.Low), float(r.Close)
        if cle not in paquets:
            paquets[cle] = [cle, o, h, l, c]
        else:
            p = paquets[cle]
            p[2], p[3], p[4] = max(p[2], h), min(p[3], l), c
    maintenant = int(time.time() * 1000)
    lignes = [tuple(paquets[k]) for k in sorted(paquets) if k + to.H4 <= maintenant]
    return to.ecrire(os.path.join(dossier, f"{nom}_4h.csv"), lignes)


def sens_par_jour(trades):
    """{jour (ms, minuit UTC) : sens} pour chaque jour ou le systeme est en position."""
    out = {}
    for x in trades:
        j = x["t_entree"] - x["t_entree"] % JOUR
        while j < x["t_sortie"]:
            out[j] = x["sens"]
            j += JOUR
    return out


def main(dossier, sortie):
    os.makedirs(dossier, exist_ok=True)
    os.makedirs(sortie, exist_ok=True)
    debut = int(datetime.strptime(tl.DEBUT, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    nq4 = yf_4h(dossier, "NQ=F", "NQF")
    jeux = {
        ("ETH", "ETH 1 j"): (to.binance(dossier, tl.SPOT, "ETHUSDT", "1d", debut), 7, 1.0),
        ("ETH", "ETH 4 h"): (to.binance(dossier, tl.SPOT, "ETHUSDT", "4h", debut), 42, 1 / 6),
        ("SOL", "SOL 1 j"): (to.binance(dossier, tl.SPOT, "SOLUSDT", "1d", debut), 7, 1.0),
        ("SOL", "SOL 4 h"): (to.binance(dossier, tl.SPOT, "SOLUSDT", "4h", debut), 42, 1 / 6),
        ("NAS100", "Nasdaq NQ=F 1 j"): (yf_jour(dossier, "NQ=F", "NQF"), 5, 7 / 5),
        ("NAS100", "Nasdaq NQ=F 4 h"): (nq4, to.barres_par_semaine(nq4), 7 / 30),
    }
    btc_1j = to.lire(os.path.join(dossier, "BTCUSDT_1d.csv"))
    res, lignes = {}, []
    for (sym, nom), (b, bps, jpb) in jeux.items():
        ans = (b[-1][0] - b[0][0]) / (365.25 * JOUR)
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
                accord = None
                if nom.endswith("1 j"):
                    ref = sens_par_jour(tl.simuler(btc_1j, "BTC", systeme, k_stop, 7, 1.0))
                    moi = sens_par_jour(tr)
                    communs = [j for j in moi if j in ref]
                    if communs:
                        accord = 100 * sum(moi[j] == ref[j] for j in communs) / len(communs)
                res[f"{nom} {systeme} stop {k_stop:g} ATR"] = {"tout": s, "par_an": par_an, "ans": ans,
                                                               "meme_sens_que_btc_pct": accord}
                if not s["n"]:
                    lignes.append(f"{systeme:10} stop {k_stop:g} ATR : aucun trade")
                    continue
                lignes.append(
                    f"{systeme:10} stop {k_stop:g} ATR : {s['n']:4} trades ({s['n'] / ans:.0f}/an), "
                    f"esperance {s['esperance']:+.3f} R, PF {s['pf'] or 0:.2f}, total {s['total']:+.1f} R, "
                    f"DD max {s['dd']:.1f} R, pertes de suite {s['serie_pertes']}, "
                    f"plus bas {s['plus_bas_depuis_debut']:+.1f} R"
                    + (f", meme sens que le BTC {accord:.0f} % du temps" if accord is not None else ""))
                lignes.append("     par an : " + " | ".join(
                    f"{an} {st['esperance']:+.2f} R x{st['n']}" for an, st in par_an.items()))
    texte = "\n".join(lignes)
    with open(os.path.join(sortie, "tendance_marches.txt"), "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    with open(os.path.join(sortie, "tendance_marches.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=str)
    print(texte)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(*sys.argv[1:])
