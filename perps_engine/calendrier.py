"""
calendrier.py - siege 2 de la table des associes : les annonces (v2.5).

Source
------
Le flux hebdomadaire de Forex Factory publie par faireconomy : le meme que
FundedNext utilise pour ses regles de news. Aucune cle, aucun compte.
Lu au plus toutes les 3 h et garde sur disque (calendrier-ff.json) : un
redemarrage ne le relit pas, une coupure Internet ne l'efface pas.

Le vote
-------
  CONTRE    l'entree tombe entre 30 min AVANT et 60 min APRES une annonce
            USD a fort impact (CPI, Fed, NFP, PCE, PIB...)
  POUR      rien de fort dans cette fenetre
  ABSTIENT  calendrier pas encore lu, ou semaine suivante pas encore publiee

Les annonces USD moyennes et fortes a moins de 2 h sont notees avec le vote,
pour le bilan. En v2.5 ce siege est en OBSERVATION : son vote est ecrit dans
sandbox-table.log, il ne bloque aucun trade.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

import requests

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SOURCE = "Forex Factory"
USER_AGENT = "perps-engine/2.5 (calendrier)"
REFRESH_SECONDS = 3 * 3600      # le flux change peu : on reste tres poli
RETRY_SECONDS = 30 * 60         # apres un echec
ERROR_LOG_SECONDS = 6 * 3600    # une panne qui dure n'est notee que toutes les 6 h
BEFORE_MIN = 30                 # pas d'entree dans les 30 min avant l'annonce
AFTER_MIN = 60                  # ni dans les 60 min apres
NOTE_MIN = 120                  # annonces notees avec le vote, pour le bilan
COUNTRIES = ("USD",)            # le BTC et l'or bougent sur le dollar
KEEP_DAYS = 10

_DAYS = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")
_MONTHS = ("janv", "fevr", "mars", "avr", "mai", "juin", "juil", "aout",
           "sept", "oct", "nov", "dec")


@dataclass(frozen=True)
class Event:
    ts: float
    title: str
    country: str
    impact: str          # high / medium / low / holiday ... (minuscules)

    def as_dict(self) -> dict:
        return {"ts": self.ts, "title": self.title, "country": self.country,
                "impact": self.impact}


# --------------------------------------------------------------------------
# Lecture du flux (outils purs)
# --------------------------------------------------------------------------

def week_end(dt: datetime) -> float:
    """Dimanche 00:00 qui suit, dans le fuseau de l'annonce : la semaine
    Forex Factory va du dimanche au samedi."""
    days = (6 - dt.weekday()) % 7 or 7
    end = (dt + timedelta(days=days)).replace(hour=0, minute=0, second=0,
                                              microsecond=0)
    return end.timestamp()


def parse_feed(payload) -> tuple:
    """
    Rend (annonces triees, fin de la semaine couverte).
    L'heure de chaque annonce porte son propre fuseau (ex. -05:00) : aucun
    calcul de fuseau ici, donc aucune erreur de fuseau.
    """
    if not isinstance(payload, list):
        raise ValueError("format inattendu (pas une liste d'annonces)")
    if not payload:
        raise ValueError("flux vide")
    events, last = [], None
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            dt = datetime.fromisoformat(
                str(item.get("date")).strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            continue                       # heure sans fuseau : on l'ignore
        events.append(Event(dt.timestamp(),
                            str(item.get("title") or "?").strip(),
                            str(item.get("country") or "").strip().upper(),
                            str(item.get("impact") or "").strip().lower()))
        if last is None or dt.timestamp() > last.timestamp():
            last = dt
    if not events:
        raise ValueError("aucune annonce lisible (le format a change ?)")
    events.sort(key=lambda e: e.ts)
    return events, week_end(last)


def http_fetch(url: str = FEED_URL, timeout: float = 15.0):
    r = requests.get(url, timeout=timeout,
                     headers={"User-Agent": USER_AGENT,
                              "Accept": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError("reponse illisible (limite de requetes ?)") from None


def local_label(ts: float) -> str:
    """Heure du PC, en francais, sans accent (console Windows)."""
    dt = datetime.fromtimestamp(ts)
    return f"{_DAYS[dt.weekday()]} {dt.day} {_MONTHS[dt.month - 1]} {dt:%H:%M}"


def when(ts: float, now: float) -> str:
    m = round((ts - now) / 60)
    if m == 0:
        return "maintenant"
    return f"dans {m} min" if m > 0 else f"il y a {-m} min"


def in_words(seconds: float) -> str:
    if seconds < 90 * 60:
        return f"dans {max(0, round(seconds / 60))} min"
    if seconds < 48 * 3600:
        return f"dans {seconds / 3600:.0f} h"
    return f"dans {seconds / 86400:.0f} j"


# --------------------------------------------------------------------------
# Le siege
# --------------------------------------------------------------------------

class Calendar:
    name = "calendrier"

    def __init__(self, cache_path: str = "calendrier-ff.json",
                 fetch: Optional[Callable[[str], object]] = None) -> None:
        self.cache_path = cache_path
        self.fetch = fetch or http_fetch
        self.events: list = []
        self.coverage_end: Optional[float] = None
        self.fetched_at: Optional[float] = None
        self.next_try = 0.0
        self.last_error: Optional[str] = None
        self._error_logged_at: Optional[float] = None
        self._load()

    # -- disque --------------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            events = [Event(float(e["ts"]), str(e["title"]), str(e["country"]),
                            str(e["impact"])) for e in data["events"]]
            end = float(data["coverage_end"])
            fetched = float(data["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        self.events = sorted(events, key=lambda e: e.ts)
        self.coverage_end, self.fetched_at = end, fetched
        self.next_try = fetched + REFRESH_SECONDS

    def _save(self) -> None:
        payload = {"source": FEED_URL, "fetched_at": self.fetched_at,
                   "coverage_end": self.coverage_end,
                   "events": [e.as_dict() for e in self.events]}
        try:
            d = os.path.dirname(os.path.abspath(self.cache_path)) or "."
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".cal-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                os.replace(tmp, self.cache_path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        except (OSError, ValueError, TypeError):
            pass                     # la copie disque est un confort, pas un besoin

    # -- relecture -----------------------------------------------------------
    def refresh(self, now: float, force: bool = False) -> Optional[dict]:
        """Relit le flux s'il est temps. Rend une note pour le journal, ou None."""
        if not force and now < self.next_try:
            return None
        try:
            fresh, end = parse_feed(self.fetch(FEED_URL))
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            self.next_try = now + RETRY_SECONDS
            if (self._error_logged_at is None
                    or now - self._error_logged_at >= ERROR_LOG_SECONDS):
                self._error_logged_at = now
                return {"event": "calendar_error", "detail": self.last_error,
                        "cached_events": len(self.events)}
            return None
        # Une annonce deplacee ne doit pas rester a son ancienne heure : sur
        # la periode couverte par le flux neuf, seul le flux neuf compte.
        lo, hi = fresh[0].ts, fresh[-1].ts
        kept = [e for e in self.events
                if (e.ts < lo or e.ts > hi) and e.ts >= now - KEEP_DAYS * 86400]
        self.events = sorted(kept + fresh, key=lambda e: e.ts)
        self.coverage_end = max(end, self.coverage_end or 0.0)
        self.fetched_at = now
        self.next_try = now + REFRESH_SECONDS
        self.last_error = None
        self._error_logged_at = None
        self._save()
        nxt = self.next_strong(now, 1)
        return {"event": "calendar_read", "events": len(fresh),
                "strong_usd": sum(1 for e in fresh if self._strong(e)),
                "next_strong": (f"{nxt[0].title} {local_label(nxt[0].ts)}"
                                if nxt else None)}

    # -- lecture -------------------------------------------------------------
    @staticmethod
    def _strong(e: Event) -> bool:
        return e.country in COUNTRIES and e.impact == "high"

    def next_strong(self, now: float, n: int = 1) -> list:
        return [e for e in self.events if self._strong(e) and e.ts >= now][:n]

    def vote(self, ctx: dict) -> dict:
        now = float(ctx["now"])
        if not self.events:
            why = "calendrier pas encore lu"
            if self.last_error:
                why += f" ({self.last_error})"
            return {"vote": "abstient", "raison": why}
        if self.coverage_end is None or now + BEFORE_MIN * 60 > self.coverage_end:
            return {"vote": "abstient", "raison": "semaine suivante pas encore lue"}
        near = [{"titre": e.title, "impact": e.impact,
                 "minutes": round((e.ts - now) / 60)}
                for e in self.events
                if e.country in COUNTRIES and e.impact in ("high", "medium")
                and abs(e.ts - now) <= NOTE_MIN * 60]
        hits = [e for e in self.events if self._strong(e)
                and e.ts - BEFORE_MIN * 60 <= now <= e.ts + AFTER_MIN * 60]
        if hits:
            e = min(hits, key=lambda x: abs(x.ts - now))
            return {"vote": "contre",
                    "raison": f"{e.title} (USD, fort) {when(e.ts, now)}",
                    "annonces": near}
        return {"vote": "pour",
                "raison": (f"aucune annonce USD forte de {AFTER_MIN} min avant "
                           f"a {BEFORE_MIN} min apres l'entree"),
                "annonces": near}

    # -- affichage -----------------------------------------------------------
    def summary_line(self, now: float) -> str:
        if not self.events:
            err = f" ({self.last_error})" if self.last_error else ""
            return f"calendrier : pas encore lu{err}"
        if self.coverage_end is None or now >= self.coverage_end:
            return "calendrier : semaine suivante pas encore lue"
        nxt = self.next_strong(now, 1)
        if not nxt or nxt[0].ts > self.coverage_end:
            return "calendrier : plus d'annonce USD forte d'ici dimanche"
        e = nxt[0]
        return (f"calendrier : prochaine annonce USD forte : {e.title}, "
                f"{local_label(e.ts)} ({in_words(e.ts - now)})")

    def check_lines(self, now: float, mode: str = "observe") -> list:
        if not self.events:
            return [f"calendrier : pas lu ({self.last_error or 'aucune copie'}) - "
                    "le moteur tourne quand meme, ce siege s'abstiendra"]
        week = [e for e in self.events if self._strong(e)
                and now <= e.ts <= (self.coverage_end or now)]
        role = (f"veto : pas d'entree de {BEFORE_MIN} min avant a {AFTER_MIN} min apres"
                if mode == "veto" else "vote en observation, ne bloque rien")
        head = (f"calendrier : {SOURCE} lu, {len(week)} annonce(s) USD forte(s) "
                f"d'ici dimanche ({role})")
        if self.last_error:
            head += f" - relecture ratee ({self.last_error}), copie gardee utilisee"
        lines = [head]
        for e in self.next_strong(now, 3):
            lines.append(f"             {local_label(e.ts)}  {e.title}  "
                         f"({in_words(e.ts - now)})")
        return lines
