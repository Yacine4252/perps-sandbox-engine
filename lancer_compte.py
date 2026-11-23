"""Lance le moteur sandbox pour un compte (v2.7).

Usage : <python du venv> -I lancer_compte.py <dossier du compte> [--check]

Le dossier contient la fiche strategie.json et recoit les journaux de ce
compte. La cle reste dans le .env du dossier du moteur : elle n'est jamais
copiee. Les taches Windows PerpsS2 ... PerpsS10 appellent ce script.
"""
import os
import sys

RACINE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    dossier = os.path.abspath(sys.argv[1])
    if not os.path.exists(os.path.join(dossier, "strategie.json")):
        print(f"ARRET : pas de strategie.json dans {dossier}")
        return 2
    sys.path.insert(0, RACINE)
    os.chdir(dossier)
    from perps_engine import sandbox_run
    return sandbox_run.main(sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
