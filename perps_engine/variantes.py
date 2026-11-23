"""
variantes.py - une strategie par compte sandbox (v2.7, 21 nov 2026).

Le meme moteur tourne une fois par compte, chacun dans son dossier
(C:\\dev\\perps-engine\\comptes\\S2 ... S10), avec une fiche strategie.json.
Sans fiche, le moteur est la v2.6 de Sandbox 1, a l'identique.

Ce que la fiche peut changer (tout le reste est la v2.6) :
  compte, marches, risque par trade, objectif en R, garder jusqu'au stop ou a
  l'objectif, siege tendance 4 h (veto), entree confirmee apres N minutes,
  sortie au temps sans +1 R, sortie si le prix revient a +x R apres +1 R,
  stop suiveur a x ATR 1 h apres +1 R, moitie fermee a +1 R.

Les gestions apres l'entree sont faites par le moteur, au prix de chaque cycle
de 60 s, par une fermeture au marche (totale ou de la moitie). Le stop et
l'objectif poses sur la plateforme a l'entree restent en place et protegent la
position si le moteur s'arrete.

Decide par Yc (go du 21 nov 2026) sur la proposition de Muse. Les resultats
historiques de chaque idee sont dans recherche/sieges/RESULTATS.md, partie 7.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, fields
from typing import Callable, Optional

import requests

KLINES = "https://fapi.binance.com/fapi/v1/klines"
BINANCE = {"GOLD": "XAUUSDT", "BTC": "BTCUSDT"}
USER_AGENT = "perps-engine/2.7 (variantes)"


@dataclass(frozen=True)
class Profil:
    nom: str
    compte: str
    marches: tuple = ("GOLD",)
    risque_pct: float = 0.01
    objectif_r: float = 2.0
    rr_min: float = 1.5
    tenir: bool = True
    tendance4h: bool = False
    confirmation_min: int = 0
    confirmation_z: float = 1.5
    sortie_temps_h: float = 0.0
    verrou_r: Optional[float] = None
    suiveur_atr: Optional[float] = None
    partiel: bool = False

    def resume(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def charger_profil(chemin: str) -> Optional[Profil]:
    """La fiche du dossier, ou None s'il n'y en a pas (Sandbox 1 : v2.6)."""
    if not os.path.exists(chemin):
        return None
    with open(chemin, encoding="utf-8-sig") as f:
        d = json.load(f)
    connus = {f.name for f in fields(Profil)}
    inconnus = sorted(set(d) - connus)
    if inconnus:
        raise ValueError(f"{chemin} : champ(s) inconnu(s) {inconnus}")
    if "marches" in d:
        d["marches"] = tuple(d["marches"])
    p = Profil(**d)
    if not str(p.compte).startswith("FP-"):
        raise ValueError(f"{chemin} : compte invalide {p.compte!r}")
    if not 0 < p.risque_pct <= 0.01:
        raise ValueError(f"{chemin} : risque par trade hors de ]0 ; 1 %]")
    if p.objectif_r < p.rr_min:
        raise ValueError(f"{chemin} : objectif {p.objectif_r} R sous le minimum {p.rr_min} R")
    if not set(p.marches) <= set(BINANCE):
        raise ValueError(f"{chemin} : marche inconnu dans {p.marches}")
    return p


def _get_json(url: str):
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    return r.json()


def bougies_terminees(sym: str, intervalle: str, n: int, now: float,
                      fetch: Callable = _get_json) -> list:
    """Bougies Binance Futures terminees avant `now` : [(o, h, l, c), ...]."""
    data = fetch(f"{KLINES}?symbol={BINANCE[sym]}&interval={intervalle}&limit={n}")
    return [(float(k[1]), float(k[2]), float(k[3]), float(k[4]))
            for k in data if int(k[6]) < now * 1000]


class TendanceSeat:
    """Siege tendance 4 h (idee de Muse) : POUR un achat au-dessus de la moyenne
    mobile 50 des clotures 4 h terminees, POUR une vente en dessous, CONTRE
    sinon. S'abstient sans donnees : il ne bloque jamais sur une panne."""
    name = "tendance4h"
    proposer = False

    def __init__(self, fetch: Callable = _get_json, refresh_s: int = 3600) -> None:
        self.fetch = fetch
        self.refresh_s = refresh_s
        self.mm: dict = {}
        self.lu: dict = {}
        self.erreur: Optional[str] = None

    def moyenne(self, sym: str, now: float) -> Optional[float]:
        if sym not in self.lu or now - self.lu[sym] >= self.refresh_s:
            try:
                clot = [b[3] for b in bougies_terminees(sym, "4h", 60, now, self.fetch)]
                self.mm[sym] = sum(clot[-50:]) / 50 if len(clot) >= 50 else None
                self.erreur = None
            except Exception as e:                       # la panne ne bloque rien
                self.erreur = f"{type(e).__name__}: {e}"[:160]
            self.lu[sym] = now
        return self.mm.get(sym)

    def vote(self, ctx: dict) -> dict:
        sym, side, prix = ctx.get("sym"), ctx.get("side"), ctx.get("price")
        mm = self.moyenne(sym, ctx.get("now") or time.time())
        if mm is None or not prix:
            return {"vote": "abstient",
                    "raison": f"moyenne 4 h indisponible ({self.erreur or 'pas assez de bougies'})"}
        dessus = prix > mm
        if (side == "buy" and dessus) or (side == "sell" and not dessus):
            return {"vote": "pour", "raison": f"dans le sens de la tendance 4 h (MM50 {mm:.2f})"}
        return {"vote": "contre", "raison": f"contre la tendance 4 h (prix {prix:.2f}, MM50 {mm:.2f})"}


class Atr1h:
    """ATR 1 h : moyenne des 14 dernieres etendues vraies, bougies terminees."""

    def __init__(self, fetch: Callable = _get_json, refresh_s: int = 600, n: int = 14) -> None:
        self.fetch = fetch
        self.refresh_s = refresh_s
        self.n = n
        self.val: dict = {}
        self.lu: dict = {}

    def valeur(self, sym: str, now: float) -> Optional[float]:
        if sym not in self.lu or now - self.lu[sym] >= self.refresh_s:
            try:
                b = bougies_terminees(sym, "1h", self.n + 2, now, self.fetch)
                tr = [max(h - l, abs(h - b[k - 1][3]), abs(l - b[k - 1][3]))
                      for k, (o, h, l, c) in enumerate(b) if k > 0]
                self.val[sym] = sum(tr[-self.n:]) / self.n if len(tr) >= self.n else None
            except Exception:
                pass                                     # garde la derniere valeur connue
            self.lu[sym] = now
        return self.val.get(sym)


def gerer(profil: Profil, info: dict, prix: Optional[float], now: float,
          atr: Optional[float] = None) -> Optional[tuple]:
    """Gestion d'une position du moteur au prix du cycle. Met a jour `info`
    (meilleur gain atteint, stop suiveur) et rend None, ("partiel", raison)
    ou ("fermer", raison)."""
    if prix is None or not info.get("entry") or not info.get("stop"):
        return None
    d = 1 if info.get("side") == "buy" else -1
    risque = abs(info["entry"] - info["stop"])
    if risque <= 0:
        return None
    gain = (prix - info["entry"]) * d / risque
    info["mfav_r"] = max(info.get("mfav_r", 0.0), gain)
    if profil.partiel and not info.get("partiel_fait") and gain >= 1:
        return ("partiel", "+1 R atteint : moitie fermee")
    if profil.verrou_r is not None and info["mfav_r"] >= 1 and gain <= profil.verrou_r:
        return ("fermer", f"retour a +{profil.verrou_r:g} R apres +1 R")
    if profil.suiveur_atr and info["mfav_r"] >= 1:
        ext = info.get("extreme")
        ext = prix if ext is None else (max(ext, prix) if d == 1 else min(ext, prix))
        info["extreme"] = ext
        if atr:
            niveau = ext - d * profil.suiveur_atr * atr
            ancien = info.get("suiveur", info["stop"])
            info["suiveur"] = max(ancien, niveau) if d == 1 else min(ancien, niveau)
        if info.get("suiveur") is not None and (prix - info["suiveur"]) * d <= 0:
            return ("fermer", f"stop suiveur {profil.suiveur_atr:g} ATR 1 h touche")
    if (profil.sortie_temps_h and info["mfav_r"] < 1
            and now - float(info.get("opened_at") or now) >= profil.sortie_temps_h * 3600):
        return ("fermer", f"{profil.sortie_temps_h:g} h sans +1 R")
    return None
