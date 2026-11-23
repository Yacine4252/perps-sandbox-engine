"""
Tests de la reprise apres redemarrage et des alertes autonomes.

Les deux modules repondent a des incidents reels : un reboot Windows le
15 septembre qui a coute 3 h de chauffe et l'interruption de l'echantillon,
et deux semaines de journaux que personne n'a ouverts faute de remontee.
"""

import json
import os
import time

import pytest

from perps_engine import alerte, persistence
from perps_engine.strategy import MomentumStrategy


@pytest.fixture
def tmp_state(tmp_path):
    return str(tmp_path / "state.json")


def make_strategies(n_points=300, base=77_000.0):
    strats = {"BTC": MomentumStrategy(), "GOLD": MomentumStrategy()}
    now = time.time()
    for i in range(n_points):
        t = now - (n_points - i) * 60
        strats["BTC"].observe(t, base + i)
        strats["GOLD"].observe(t, 4350.0 + i * 0.1)
    strats["BTC"].direction = "buy"
    return strats


# --- sauvegarde / reprise -------------------------------------------------

def test_state_survives_a_restart(tmp_state):
    """Le coeur du correctif : la serie ne doit pas mourir avec le processus."""
    before = make_strategies()
    assert persistence.save(tmp_state, before)

    after = {"BTC": MomentumStrategy(), "GOLD": MomentumStrategy()}
    state = persistence.load(tmp_state)
    rec = persistence.restore(after, state)

    assert state["ok"]
    assert len(after["BTC"].buffer) == len(before["BTC"].buffer)
    assert after["BTC"].direction == "buy"
    assert {r["sym"] for r in rec["restored"]} == {"BTC", "GOLD"}


def test_restored_buffer_is_immediately_usable(tmp_state):
    """Reprendre l'etat doit eviter la chauffe, sinon ca ne sert a rien."""
    before = make_strategies(n_points=300)
    persistence.save(tmp_state, before)
    after = {"BTC": MomentumStrategy(), "GOLD": MomentumStrategy()}
    persistence.restore(after, persistence.load(tmp_state))

    span = after["BTC"].buffer.span_minutes()
    assert span >= after["BTC"].cfg.warmup_minutes


def test_stale_state_is_refused_not_silently_used(tmp_state):
    """
    Un etat vieux de plusieurs heures ferait calculer le momentum par-dessus
    un trou. Mieux vaut rechauffer que mentir.
    """
    persistence.save(tmp_state, make_strategies())
    payload = json.loads(open(tmp_state).read())
    payload["saved_at"] = time.time() - 3 * 3600
    open(tmp_state, "w").write(json.dumps(payload))

    state = persistence.load(tmp_state)
    assert not state["ok"]
    assert "rechauffage" in state["reason"]
    assert state["age_minutes"] > persistence.MAX_GAP_MINUTES


def test_missing_file_degrades_cleanly(tmp_path):
    state = persistence.load(str(tmp_path / "absent.json"))
    assert not state["ok"] and state["markets"] == {}


def test_corrupt_file_degrades_cleanly(tmp_state):
    """Un fichier tronque par une coupure ne doit pas planter le moteur."""
    open(tmp_state, "w").write('{"saved_at": 123, "markets": {"BTC"')
    state = persistence.load(tmp_state)
    assert not state["ok"] and "corrompu" in state["reason"]


def test_save_is_atomic_no_partial_file(tmp_state):
    """Apres une sauvegarde, le fichier est soit absent soit complet."""
    persistence.save(tmp_state, make_strategies())
    payload = json.loads(open(tmp_state).read())   # doit parser du premier coup
    assert "markets" in payload and "saved_at" in payload
    leftovers = [f for f in os.listdir(os.path.dirname(tmp_state))
                 if f.startswith(".state-")]
    assert not leftovers, f"fichiers temporaires laisses : {leftovers}"


def test_new_market_in_basket_is_skipped_not_crashed(tmp_state):
    """Ajouter un marche au panier ne doit pas casser la reprise."""
    persistence.save(tmp_state, make_strategies())
    after = {"BTC": MomentumStrategy(), "GOLD": MomentumStrategy(),
             "NOUVEAU": MomentumStrategy()}
    rec = persistence.restore(after, persistence.load(tmp_state))
    assert rec["skipped"] == ["NOUVEAU"]
    assert len(after["BTC"].buffer) > 0


# --- alertes --------------------------------------------------------------

def test_dead_market_raises_after_streak_not_on_first_miss():
    """Une cotation ratee arrive ; dix d'affilee, c'est une panne."""
    book = alerte.AlertBook()
    for _ in range(alerte.DEAD_MARKET_CYCLES - 1):
        assert book.note_market("EUR", False) is None
    assert "ne repond plus" in book.note_market("EUR", False)


def test_recovered_market_resets_the_streak():
    book = alerte.AlertBook()
    for _ in range(5):
        book.note_market("EUR", False)
    book.note_market("EUR", True)
    assert book.note_market("EUR", False) is None


def test_long_signal_is_surfaced():
    """Un signal qui tient 4 h est ce qui porte l'echantillon : il se signale."""
    book = alerte.AlertBook()
    t0 = time.time()
    assert book.note_signal("BTC", "buy", t0) is None
    msg = book.note_signal("BTC", "buy", t0 + alerte.LONG_SIGNAL_MINUTES * 60)
    assert msg and "tient un signal" in msg


def test_signal_timer_resets_when_signal_closes():
    book = alerte.AlertBook()
    t0 = time.time()
    book.note_signal("BTC", "buy", t0)
    book.note_signal("BTC", None, t0 + 60)
    assert book.note_signal("BTC", "buy",
                            t0 + alerte.LONG_SIGNAL_MINUTES * 60) is None


def test_alerts_do_not_repeat_every_cycle():
    """Une alerte repetee a chaque minute ne vaut pas mieux que pas d'alerte."""
    book = alerte.AlertBook()
    t0 = time.time()
    book.note_signal("BTC", "buy", t0)
    late = t0 + alerte.LONG_SIGNAL_MINUTES * 60
    assert book.note_signal("BTC", "buy", late)
    assert book.note_signal("BTC", "buy", late + 60) is None


def test_floor_breach_is_reported():
    book = alerte.AlertBook()
    msg = book.note_account(room_pct=-0.46, drawdown_pct=115.3)
    assert msg and "SOUS LE PLANCHER" in msg


def test_error_burst_raises_but_single_error_does_not():
    book = alerte.AlertBook()
    for _ in range(alerte.ERROR_BURST - 1):
        assert book.note_error("timeout") is None
    assert "erreurs en une heure" in book.note_error("timeout")


def test_alert_file_accumulates_lines(tmp_path):
    path = str(tmp_path / "ALERTE.txt")
    alerte.raise_alert("premiere", path)
    alerte.raise_alert("seconde", path)
    lines = open(path, encoding="utf-8").read().strip().split("\n")
    assert len(lines) == 2 and "seconde" in lines[1]


def test_summary_is_short_enough_to_paste(tmp_path):
    """Le resume doit tenir en quelques lignes, sinon personne ne le lit."""
    path = str(tmp_path / "resume.txt")
    book = alerte.AlertBook()
    book.transitions = 11
    measures = {"BTC": {"direction": "buy", "z": 2.1},
                "GOLD": {"direction": None, "reason": "|z| 0.4 < entree 2.0"}}
    alerte.write_summary(book, measures, {"equity": 9654.0, "room_usd": -45.9,
                                          "funding_per_day": 387.1}, 59.5, path)
    lines = open(path, encoding="utf-8").read().strip().split("\n")
    assert len(lines) <= 10
    assert any("BTC" in l and "buy" in l for l in lines)
