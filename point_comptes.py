"""Point des 10 comptes sandbox (v2.7) : une ligne par compte. Lecture seule.

Usage : <python du venv> -I point_comptes.py
Lit les journaux de Sandbox 1 (ce dossier) et de comptes/S2 ... comptes/S10, et
ecrit le resultat dans comptes/point_comptes.txt.
"""
import json
import os
from datetime import datetime, timezone

RACINE = os.path.dirname(os.path.abspath(__file__))
COMPTES = [("S1", RACINE, "v2.6 : or tenu 2 R, risque 0,3 % (1 % jusqu'au 22 nov)")] + [
    (f"S{n}", os.path.join(RACINE, "comptes", f"S{n}"), None) for n in range(2, 11)]


def evenements(chemin):
    if not os.path.exists(chemin):
        return
    with open(chemin, encoding="utf-8", errors="replace") as f:
        for ligne in f:
            try:
                yield json.loads(ligne)
            except ValueError:
                continue


def point(nom, dossier, defaut):
    trades = os.path.join(dossier, "sandbox-trades.log")
    starts = [e for e in evenements(trades) if e.get("event") == "start"]
    if not starts:
        return {"nom": nom, "strategie": defaut or "-", "etat": "pas demarre"}
    debut = starts[-1]["ts"] if nom != "S1" else next(
        (e["ts"] for e in starts if str(e.get("version", "")).startswith("2.6")), starts[-1]["ts"])
    profil = starts[-1].get("profil") or {}
    fermes = [e for e in evenements(trades) if e.get("event") == "position_closed" and e["ts"] >= debut]
    gestion = [e for e in evenements(trades) if e.get("event") == "exit_managed" and e["ts"] >= debut]
    erreurs = [e for e in evenements(trades) if e.get("event") == "error" and e["ts"] >= debut]
    snap = None
    for e in evenements(os.path.join(dossier, "sandbox-signals.log")):
        if e.get("event") == "snapshot":
            snap = e
    age = None
    if snap:
        t = datetime.fromisoformat(snap["ts"])
        age = (datetime.now(timezone.utc) - t).total_seconds() / 60
    return {"nom": nom, "strategie": profil.get("nom") or defaut or "-",
            "compte": starts[-1].get("account"), "depuis": debut[:16].replace("T", " "),
            "trades": len(fermes), "gagnants": sum(1 for e in fermes if e["net"] > 0),
            "net": sum(e["net"] for e in fermes), "frais": sum(e["fees"] for e in fermes),
            "gestion": len(gestion), "erreurs": len(erreurs),
            "equite": snap["equity"] if snap else None,
            "ouvertes": len(snap.get("managed") or []) if snap else None,
            "etat": "tourne" if age is not None and age < 20 else
                    (f"silence depuis {age:.0f} min" if age is not None else "pas de point")}


def main():
    lignes = [f"Point des comptes sandbox, {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", ""]
    for nom, dossier, defaut in COMPTES:
        p = point(nom, dossier, defaut)
        if "compte" not in p:
            lignes.append(f"{nom:4} {p['etat']:12} {p['strategie']}")
            continue
        eq = f"{p['equite']:,.0f} $".replace(",", " ") if p["equite"] else "-"
        lignes.append(f"{nom:4} {p['compte']}  {p['etat']:12} equite {eq:>11}  depuis {p['depuis']} UTC : "
                      f"{p['trades']} trades ({p['gagnants']} gagnants), net {p['net']:+.0f} $, "
                      f"frais {p['frais']:.0f} $, ouvertes {p['ouvertes']}, erreurs {p['erreurs']}")
        lignes.append(f"     {p['strategie']}")
    texte = "\n".join(lignes)
    os.makedirs(os.path.join(RACINE, "comptes"), exist_ok=True)
    with open(os.path.join(RACINE, "comptes", "point_comptes.txt"), "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    print(texte)


if __name__ == "__main__":
    main()
