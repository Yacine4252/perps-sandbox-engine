"""Backtest d'AMD_Scalper_v1 sur bougies Binance 5 min. Aucun ordre, donnees publiques.

Usage :  python -I backtest.py <dossier_donnees> <dossier_resultats>

La config de reference du 19 aout (MNQ1! M5) est lue de deux facons, car le brief
et le script ne disent pas la meme chose :
  A = champs du script pris tels quels : compression 5 min (2 bougies en M5), ATR 14 min (3 bougies)
  B = brief lu en bougies : compression 5 bougies, ATR 14 bougies
Communs : SL max 0,5 ATR, time-stop 120 min, FVG valable 30 min, R:R 2 a 4,
contact a la meche, entree au bord proximal, stop le plus serre + 4 ticks (0,05 ATR).
Chaque lecture tourne en mode fidele (le script tel quel) et realiste (voir amd_v1.py).
"""
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)
import amd_v1  # noqa: E402

FRAIS_BPS = {"BTCUSDT": 3.0, "XAUUSDT": 0.5}    # factures par la plateforme sandbox, par cote
LECTURES = {"A": {"comp_min": 5, "atr_min": 14}, "B": {"comp_min": 25, "atr_min": 70}}
MODES = {"fidele": False, "realiste": True}
REFERENCE = "Reference MNQ1! M5 (TradingView, 36 jours) : 124 trades, 3,4/jour, 1,02 R, PF 3,09, DD 6,25 R, reussite 52,85 %"


def charger(chemin):
    t, o, h, l, c = [], [], [], [], []
    with open(chemin, newline="") as f:
        lecteur = csv.reader(f)
        next(lecteur)
        for x in lecteur:
            t.append(int(x[0]))
            o.append(float(x[1]))
            h.append(float(x[2]))
            l.append(float(x[3]))
            c.append(float(x[4]))
    return t, o, h, l, c


def _date(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def ecrire_trades(chemin, trades):
    with open(chemin, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fvg_utc", "ouverture_utc", "cloture_utc", "sens", "entree", "stop", "objectif",
                    "rr", "risque_bps", "sortie", "prix_sortie", "r_brut", "r_net", "session"])
        for x in trades:
            w.writerow([_date(x["t_fvg"]), _date(x["t_ouverture"]), _date(x["t_cloture"]),
                        "long" if x["sens"] == 1 else "short", x["entree"], round(x["stop"], 6),
                        x["objectif"], round(x["rr"], 3), round(x["risque_bps"], 2), x["sortie"],
                        x["prix_sortie"], round(x["r_brut"], 4), round(x["r_net"], 4), x["session"]])


def par_mois(trades):
    m = defaultdict(list)
    for x in trades:
        m[_date(x["t_cloture"])[:7]].append(x["r_net"])
    return {k: (len(v), sum(v) / len(v), sum(v)) for k, v in sorted(m.items())}


def main(dossier_donnees, dossier_resultats):
    os.makedirs(dossier_resultats, exist_ok=True)
    resume = {}
    print(REFERENCE)
    for sym, frais in FRAIS_BPS.items():
        t, o, h, l, c = charger(os.path.join(dossier_donnees, f"{sym}_5m.csv"))
        cols = {}
        for lec, champs in LECTURES.items():
            for mode, realiste in MODES.items():
                nom = f"{lec} {mode}"
                R = amd_v1.Reglages(nom=nom, frais_bps=frais, realiste=realiste, **champs)
                res = amd_v1.simuler(t, o, h, l, c, R)
                s = res.stats
                tr = res.trades
                s["ouverts_a_la_naissance_pct"] = 100.0 * sum(x["t_ouverture"] == x["t_fvg"] for x in tr) / len(tr) if tr else 0.0
                s["stops_bougie_entree_pct"] = 100.0 * sum(x["sortie"] == "sl" and x["t_cloture"] == x["t_ouverture"] for x in tr) / len(tr) if tr else 0.0
                s["par_mois"] = par_mois(tr)
                cols[nom] = s
                ecrire_trades(os.path.join(dossier_resultats, f"{sym}_{lec}_{mode}_trades.csv"), tr)
        resume[sym] = cols
        _imprimer(sym, frais, t, cols)
        resume[sym + "_decomposition"] = decomposition(sym, frais, t, o, h, l, c)
    with open(os.path.join(dossier_resultats, "resume.json"), "w", encoding="utf-8") as f:
        json.dump(resume, f, ensure_ascii=False, indent=1)


ETAPES = [
    ("1. script tel quel", {}),
    ("2. + pas de remplissage a la naissance", {"sans_naissance": True}),
    ("3. + bougie d'entree honnete", {"sans_naissance": True, "remplissage_honnete": True}),
    ("4. + time-stop au prix (= realiste)", {"realiste": True}),
]


def decomposition(sym, frais, t, o, h, l, c):
    """Lecture A : ajoute une a une les corrections de realisme pour voir d'ou vient l'ecart."""
    print(f"Decomposition {sym} (lecture A) :")
    out = {}
    for titre, kw in ETAPES:
        s = amd_v1.simuler(t, o, h, l, c, amd_v1.Reglages(frais_bps=frais, **LECTURES["A"], **kw)).stats
        out[titre] = {k: s[k] for k in ("n_trades", "esperance_avant_frais", "esperance", "reussite")}
        print(f"   {titre:42} {s['n_trades']:5} trades   avant frais {s['esperance_avant_frais']:+.2f} R"
              f"   net {s['esperance']:+.2f} R   reussite {s['reussite']:.1f} %")
    return out


def _imprimer(sym, frais, t, cols):
    noms = list(cols)
    print()
    print(f"=== {sym}  (frais {frais} bps par cote)  du {_date(t[0])[:10]} au {_date(t[-1])[:10]}")
    print(f"{'':28}" + "".join(f"{n:>13}" for n in noms))

    def ligne(titre, fn, fmt="{:.2f}"):
        print(f"{titre:28}" + "".join(f"{fmt.format(fn(cols[n])):>13}" for n in noms))

    ligne("Barres compression / ATR", lambda s: f"{s['barres']['compression']} / {s['barres']['atr']}", "{}")
    ligne("Trades", lambda s: s["n_trades"], "{}")
    ligne("Trades par jour", lambda s: s["trades_par_jour"])
    ligne("Esperance nette (R)", lambda s: s["esperance"])
    ligne("  avant frais (R)", lambda s: s["esperance_avant_frais"])
    ligne("  frais moyens (R)", lambda s: s["frais_moyens_r"])
    ligne("EDGE vs controle (R)", lambda s: s["edge"])
    ligne("Reussite (%)", lambda s: s["reussite"], "{:.1f}")
    ligne("Profit factor", lambda s: s["pf"])
    ligne("Total (R)", lambda s: s["total_r"], "{:.1f}")
    ligne("R par jour", lambda s: s["r_par_jour"])
    ligne("Drawdown max (R)", lambda s: s["drawdown_max_r"], "{:.1f}")
    ligne("Pertes consecutives max", lambda s: s["pertes_consecutives_max"], "{}")
    ligne("Time-stops (%)", lambda s: s["time_stops_pct"], "{:.1f}")
    ligne("Stop median (bps)", lambda s: s["risque_median_bps"], "{:.1f}")
    ligne("Ouverts a la naissance (%)", lambda s: s["ouverts_a_la_naissance_pct"], "{:.0f}")
    ligne("Stoppes bougie d'entree (%)", lambda s: s["stops_bougie_entree_pct"], "{:.0f}")
    ligne("BRUT SL/TP seuls (R)", lambda s: s["brut_sl_tp"]["esperance"])
    ligne("Long : esperance", lambda s: s["long"]["esperance"])
    ligne("Short : esperance", lambda s: s["short"]["esperance"])
    for se in ("Asie", "Londres", "New York", "Hors session"):
        ligne(f"{se} : n / esp.", lambda s, se=se: f"{s['sessions'][se]['n']} / {s['sessions'][se]['esperance']:+.2f}", "{}")
    e = cols[noms[0]]["entonnoir"]
    print("Entonnoir (" + noms[0] + ") : " + ", ".join(f"{k} {v}" for k, v in e.items()))
    for n in noms:
        if n.endswith("realiste"):
            mois = cols[n]["par_mois"]
            print(f"Par mois ({n}) : " + "  ".join(f"{k[2:]} {v[1]:+.2f}R/{v[0]}" for k, v in mois.items()))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
