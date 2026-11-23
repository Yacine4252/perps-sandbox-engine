"""Verifie la combinaison figee le 6 nov 2026 sur des donnees qu'elle n'a jamais vues.
Aucun ordre, moteur non modifie.

La combinaison (candidat_fige.json) : or (XAUUSDT), momentum du moteur tenu
jusqu'au stop ou a l'objectif, trade pris seulement si les sieges structure H1
ET funding votent pour. Seuls comptent les trades ouverts APRES le gel
(6 nov 2026 18:28 UTC).

Usage, depuis ce dossier :
  1. python -I telecharger_1m.py donnees 2025-11-01 BTCUSDT XAUUSDT
  2. <python du venv> -I signaux_moteur.py donnees cache BTCUSDT XAUUSDT
  3. <python du venv> -I verifier_candidat.py donnees cache

Le script verifie d'abord que le code n'a pas change depuis le gel (empreintes).
"""
import hashlib
import json
import os
import sys

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

import analyse  # noqa: E402
import combiner as C  # noqa: E402
import comparer_sorties as CS  # noqa: E402
import rejeu  # noqa: E402
from donnees import date_utc  # noqa: E402
from sieges import Contexte  # noqa: E402

FICHIER = os.path.join(ICI, "candidat_fige.json")
CODE = ["sieges.py", "rejeu.py", "donnees.py", "combiner.py", "signaux_moteur.py"]


def empreintes():
    return {f: hashlib.sha256(open(os.path.join(ICI, f), "rb").read()).hexdigest() for f in CODE}


def main(dossier, cache):
    fige = json.load(open(FICHIER, encoding="utf-8"))
    changes = [f for f, h in empreintes().items() if fige["empreintes"].get(f) != h]
    if changes:
        print("ATTENTION : code modifie depuis le gel :", ", ".join(changes))
    gel = fige["gel_ms"]
    sym = fige["marche"]
    cfg = (fige["regle"], fige["equipe"])
    m, b, fund = analyse.charger_marche(dossier, cache, sym)
    ctx = Contexte(b, fund)

    def veto(s, i, d, entree, objectif):
        p, c = C.masques(ctx.voter(i, d, entree, objectif)[0])
        return not C.garde(cfg[0], cfg[1], p, c)

    print(f"Combinaison figee : {fige['description']}")
    print(f"Donnees jusqu'au {date_utc(m.t[-1] + rejeu.MIN_MS)} UTC ; gel le {date_utc(gel)} UTC")
    # chaque signal comme un trade independant (l'echantillon de la recherche)
    tous = rejeu.tous_les_signaux(m, gel, False)
    for x in tous:
        x["pour"], x["contre"] = C.masques(ctx.voter(x["i_entree"], x["sens"], x["entree"], x["objectif"])[0])
    CS.ajouter_portage(tous)
    gardes = [x for x in C.filtrer(tous, cfg) if x["sortie"] != "fin"]
    # avec le garde-fou du moteur, sur ce marche seul
    fid, _, _ = rejeu.fidele([m], gel, False, veto)
    CS.ajouter_portage(fid)
    fid = [x for x in fid if x["sortie"] != "fin"]
    jours = max((m.t[-1] + rejeu.MIN_MS - gel) / 864e5, 1e-9)
    for nom, lot in (("tous les signaux gardes", gardes), ("avec le garde-fou du moteur", fid)):
        if not lot:
            print(f"   {nom} : aucun trade ferme depuis le gel")
            continue
        s = CS.resume(lot, jours)
        p = C.p_unilaterale(lot)
        print(f"   {nom} : {s['n']} trades, net {s['esp_net']:+.3f} R par trade, portage compris "
              f"{s['esp_net_portage']:+.3f} R, total {s['total_r']:+.1f} R, p {p if p is None else round(p, 3)}")
    print("Rappel : sur les 3 mois de confirmation, -0,07 R (tous les signaux) et +0,05 R (garde-fou).")
    print("Il faut au moins une centaine de trades pour conclure ; en dessous, c'est du bruit.")


def figer():
    """Ecrit candidat_fige.json (a lancer une seule fois, le 6 nov 2026)."""
    fige = {"description": "or, momentum tenu jusqu'au stop ou a l'objectif, "
                           "unanime : structure H1 + funding",
            "marche": "GOLD", "base": "momentum stop/objectif", "regle": "unanime",
            "equipe": (1 << C.SIEGES7.index("structure_h1")) | (1 << C.SIEGES7.index("of_funding")),
            "gel_ms": 1793989680000, "gel_utc": "2026-11-06 18:28",
            "resultats_au_gel": {"8 mois": "+0,28 R, 342 trades, t 3,5",
                                 "3 mois": "-0,07 R, 143 trades ; garde-fou +0,05 R, 60 trades",
                                 "p_recherche": 0.21},
            "empreintes": empreintes()}
    with open(FICHIER, "w", encoding="utf-8") as f:
        json.dump(fige, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    if sys.argv[1:] == ["--figer"]:
        figer()
    elif len(sys.argv) == 3:
        main(*sys.argv[1:])
    else:
        print(__doc__)
        sys.exit(1)
