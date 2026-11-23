"""Lance le moteur de tendance lente pour un compte (22 nov 2026).

Usage : <python du venv> -I lancer_tendance.py <dossier du compte> [--check]

Le dossier contient la fiche tendance.json et recoit les journaux de ce
compte. La cle reste dans le .env du dossier du moteur : elle n'est jamais
copiee. --check montre ce que le moteur ferait, sans envoyer d'ordre.
"""
import os
import sys

RACINE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    dossier = os.path.abspath(sys.argv[1])
    if not os.path.exists(os.path.join(dossier, "tendance.json")):
        print(f"ARRET : pas de tendance.json dans {dossier}")
        return 2
    sys.path.insert(0, RACINE)
    os.chdir(dossier)
    from perps_engine import tendance_lente
    return tendance_lente.main(sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
