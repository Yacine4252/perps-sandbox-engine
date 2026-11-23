"""
alerte.py - le moteur signale ce qui compte, sans qu'on le lui demande.

Pourquoi ce module existe
-------------------------
Pendant deux semaines, les donnees se sont accumulees dans les journaux et
personne ne les a lues. Pas par negligence : rien ne les remontait. Il
fallait ouvrir un fichier de 500 Ko et chercher a l'oeil.

Un journal que personne n'ouvre ne vaut rien. Ce module inverse la charge :
le moteur ecrit ALERTE.txt quand quelque chose merite une lecture, et
resume.txt toutes les 6 h. Le reste du temps, il se tait.

Regle de conception : une alerte qui se declenche tout le temps ne vaut pas
mieux que pas d'alerte du tout. Chaque seuil ici est choisi pour qu'une
semaine normale produise zero ou une alerte.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional
from datetime import datetime, timezone

ALERT_FILE = "ALERTE.txt"
SUMMARY_FILE = "resume.txt"
SUMMARY_EVERY_HOURS = 6

# --- seuils ---------------------------------------------------------------
LONG_SIGNAL_MINUTES = 240      # un signal qui tient 4 h merite un regard
ROOM_WARNING_PCT = 25.0        # marge restante sous 25 % du budget
DEAD_MARKET_CYCLES = 10        # un marche muet 10 cycles d'affilee
ERROR_BURST = 5                # 5 erreurs en une heure


class AlertBook:
    """
    Tient le compte de ce qui merite d'etre signale, et evite de repeter
    la meme alerte a chaque cycle.
    """

    def __init__(self) -> None:
        self.raised: dict = {}           # cle -> timestamp de la derniere levee
        self.dead_streak: dict = {}      # sym -> cycles muets consecutifs
        self.signal_since: dict = {}     # sym -> ts du debut du signal courant
        self.errors: list = []
        self.last_summary = 0.0
        self.transitions = 0

    # -- anti-repetition ---------------------------------------------------
    def _fresh(self, key: str, cooldown_hours: float = 12) -> bool:
        last = self.raised.get(key, 0)
        if time.time() - last < cooldown_hours * 3600:
            return False
        self.raised[key] = time.time()
        return True

    # -- collecte ----------------------------------------------------------
    def note_market(self, sym: str, price_ok: bool) -> Optional[str]:
        if price_ok:
            self.dead_streak[sym] = 0
            return None
        self.dead_streak[sym] = self.dead_streak.get(sym, 0) + 1
        if (self.dead_streak[sym] >= DEAD_MARKET_CYCLES
                and self._fresh(f"mort:{sym}")):
            return f"{sym} ne repond plus depuis {self.dead_streak[sym]} cycles"
        return None

    def note_signal(self, sym: str, direction, now: float) -> Optional[str]:
        if direction is None:
            self.signal_since.pop(sym, None)
            return None
        start = self.signal_since.setdefault(sym, now)
        held = (now - start) / 60
        if held >= LONG_SIGNAL_MINUTES and self._fresh(f"long:{sym}", 6):
            return (f"{sym} tient un signal {direction} depuis "
                    f"{held/60:.1f} h - le genre de mouvement qui porte "
                    f"l'echantillon")
        return None

    def note_transition(self) -> None:
        self.transitions += 1

    def note_account(self, room_pct, drawdown_pct) -> Optional[str]:
        if room_pct is None:
            return None
        if room_pct <= 0 and self._fresh("plancher", 24):
            return "COMPTE SOUS LE PLANCHER - plus de marge de drawdown"
        if drawdown_pct is not None and drawdown_pct >= (100 - ROOM_WARNING_PCT):
            if self._fresh("marge", 12):
                return (f"marge restante faible : {drawdown_pct:.0f} % du "
                        f"budget de drawdown consomme")
        return None

    def note_error(self, detail: str) -> Optional[str]:
        now = time.time()
        self.errors = [e for e in self.errors if now - e < 3600]
        self.errors.append(now)
        if len(self.errors) >= ERROR_BURST and self._fresh("erreurs", 6):
            return f"{len(self.errors)} erreurs en une heure - dernier : {detail}"
        return None


def raise_alert(message: str, path: str = ALERT_FILE) -> None:
    """Ajoute une ligne horodatee. Le fichier s'accumule, il ne s'ecrase pas."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def write_summary(book: AlertBook, measures: dict, account: dict,
                  hours_running: float, path: str = SUMMARY_FILE) -> None:
    """
    Ecrit un resume court, concu pour etre colle tel quel dans une
    conversation. Pas un dump : les quelques chiffres qui decident.
    """
    L = [f"RESUME  {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
         f"  tourne depuis {hours_running:.1f} h",
         f"  transitions depuis le demarrage : {book.transitions}"]

    for sym, m in sorted(measures.items()):
        if m.get("blocked_by_cost"):
            L.append(f"  {sym:6} ecarte : frais > risque")
        elif m.get("direction"):
            held = (time.time() - book.signal_since.get(sym, time.time())) / 60
            L.append(f"  {sym:6} {m['direction']:4} z={m.get('z')}  "
                     f"depuis {held:.0f} min")
        else:
            L.append(f"  {sym:6} neutre   {m.get('reason', '')}")

    if account:
        L.append(f"  compte : equity {account.get('equity')} "
                 f"marge {account.get('room_usd')} "
                 f"portage {account.get('funding_per_day')} $/j")

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
    except OSError:
        pass
    book.last_summary = time.time()


def due_for_summary(book: AlertBook) -> bool:
    return time.time() - book.last_summary >= SUMMARY_EVERY_HOURS * 3600
