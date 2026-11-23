"""Tests du telechargement : lecture de la fin du fichier et verrou."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import telecharger_1m as T  # noqa: E402


def test_derniere_ligne(tmp_path):
    p = tmp_path / "X_1m.csv"
    assert T._derniere_ligne(str(p)) is None
    p.write_text("open_time_ms,open,high\n")
    assert T._derniere_ligne(str(p)) is None
    lignes = ["open_time_ms,open,high"] + [f"{60000 * k},{k}.5,{k}.9" for k in range(2000)]
    p.write_text("\n".join(lignes) + "\n")
    assert T._derniere_ligne(str(p)) == [str(60000 * 1999), "1999.5", "1999.9"]


def test_verrou_un_seul_ecrivain(tmp_path):
    chemin = str(tmp_path / "X_1m.csv")
    with T.verrou(chemin):
        assert os.path.exists(chemin + ".verrou")
        try:
            with T.verrou(chemin, attente_s=0.2):
                raise AssertionError("deux ecrivains en meme temps")
        except TimeoutError:
            pass
    assert not os.path.exists(chemin + ".verrou")
    with T.verrou(chemin):
        pass


def test_verrou_perime_ignore(tmp_path):
    chemin = str(tmp_path / "X_1m.csv")
    open(chemin + ".verrou", "w").close()
    vieux = time.time() - 3600
    os.utime(chemin + ".verrou", (vieux, vieux))
    with T.verrou(chemin, attente_s=0.2):
        pass
    assert not os.path.exists(chemin + ".verrou")
