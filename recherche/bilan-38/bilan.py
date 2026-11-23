"""Bilan des trades v2.4 (5 nov 2026 20:19 UTC -> 21 nov 2026 01:07 UTC, demarrage
de la v2.6), avec a part les 17 trades d'avant la v2.4. Lecture seule de
sandbox-trades.log. Demande de Claude app (go de Yc), 22 nov 2026.

Usage, depuis ce dossier : <python du venv> -I bilan.py
Ecrit trades.csv (un trade par ligne) et resume.txt (les totaux).
"""
import csv
import json
import os
from collections import Counter

ICI = os.path.dirname(os.path.abspath(__file__))
JOURNAL = os.path.join(ICI, "..", "..", "sandbox-trades.log")
V24, V26 = "2026-11-05T20:19", "2026-11-21T01:07"


def evenements():
    with open(JOURNAL, encoding="utf-8", errors="replace") as f:
        for ligne in f:
            try:
                yield json.loads(ligne)
            except ValueError:
                continue


def trades():
    ouverts, signal, out = {}, {}, []
    for e in evenements():
        ev = e.get("event")
        if ev == "entry_order" and e.get("status") == "filled":
            ouverts[e["sym"]] = e
        elif ev == "exit_signal":
            signal[e["position_id"]] = e.get("reason")
        elif ev == "position_closed":
            ent = ouverts.pop(e["sym"], None)
            risque = abs(ent["fill_price"] - ent["stop"]) * ent["size"] if ent else None
            raison = ("signal" if e["position_id"] in signal else
                      {"stop_loss": "stop", "take_profit": "objectif"}.get(e.get("close_reason"), "autre"))
            out.append({"date_utc": e["ts"][:16].replace("T", " "), "marche": e["sym"],
                        "sens": "achat" if e["side"] in ("buy", "long") else "vente",
                        "entree": e["entry"], "sortie": e["exit"], "raison": raison,
                        "duree_min": round(e["hold_min"], 1), "brut": round(e["gross"], 2),
                        "frais": round(e["fees"], 2), "portage": round(e.get("funding") or 0, 2),
                        "net": round(e["net"], 2),
                        "r": round(e["net"] / risque, 3) if risque else None,
                        "periode": "avant v2.4" if e["ts"] < V24 else ("v2.4-v2.5" if e["ts"] < V26 else "v2.6")})
    return out


def totaux(lot):
    if not lot:
        return None
    net = [x["net"] for x in lot]
    gains, pertes = [x for x in net if x > 0], [x for x in net if x < 0]
    rg = [x["r"] for x in lot if x["r"] is not None and x["net"] > 0]
    rp = [x["r"] for x in lot if x["r"] is not None and x["net"] < 0]
    cumul = pic = dd = 0.0
    for v in net:
        cumul += v
        pic = max(pic, cumul)
        dd = max(dd, pic - cumul)
    brut, frais = sum(x["brut"] for x in lot), sum(x["frais"] for x in lot)
    return {"n": len(lot), "net": sum(net), "brut": brut, "frais": frais,
            "frais_pct_brut_abs": 100 * frais / abs(brut) if brut else None,
            "reussite_pct": 100 * len(gains) / len(lot),
            "gain_moyen_r": sum(rg) / len(rg) if rg else None,
            "perte_moyenne_r": sum(rp) / len(rp) if rp else None,
            "pf": sum(gains) / -sum(pertes) if pertes else None, "dd_max": dd,
            "raisons": dict(Counter(x["raison"] for x in lot))}


def fmt(nom, t):
    if not t:
        return f"{nom} : aucun trade"
    f = lambda v, s="": "-" if v is None else f"{v:{s}}"  # noqa: E731
    return (f"{nom} : {t['n']} trades | net {t['net']:+,.0f} $ | brut {t['brut']:+,.0f} $ | frais {t['frais']:,.0f} $ "
            f"({f(t['frais_pct_brut_abs'], '.0f')} % du brut en valeur absolue) | reussite {t['reussite_pct']:.0f} % | "
            f"gain moyen {f(t['gain_moyen_r'], '+.2f')} R, perte moyenne {f(t['perte_moyenne_r'], '+.2f')} R | "
            f"PF {f(t['pf'], '.2f')} | drawdown max {t['dd_max']:,.0f} $ | sorties {t['raisons']}").replace(",", " ")


def main():
    tout = trades()
    with open(os.path.join(ICI, "trades.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(tout[0]))
        w.writeheader()
        w.writerows(tout)
    lignes = []
    for per in ("v2.4-v2.5", "avant v2.4", "v2.6"):
        lot = [x for x in tout if x["periode"] == per]
        lignes.append(f"== {per}")
        for sym in ("GOLD", "BTC"):
            lignes.append(fmt(f"  {sym}", totaux([x for x in lot if x["marche"] == sym])))
        lignes.append(fmt("  Total", totaux(lot)))
    texte = "\n".join(lignes)
    with open(os.path.join(ICI, "resume.txt"), "w", encoding="utf-8") as fh:
        fh.write(texte + "\n")
    print(texte)


if __name__ == "__main__":
    main()
