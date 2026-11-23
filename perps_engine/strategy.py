"""
strategy.py - momentum intraday normalise par la volatilite (v1.2).

Ce qu'elle propose : une DIRECTION et un STOP. Rien d'autre.
La taille et le levier sont derives par risk.py, jamais ici.

Base empirique
--------------
Shen, Urquhart & Wang (2022) : momentum intraday sur BTC, marche 24/7 sans
ouverture ni cloture. Normalisation par la volatilite conformement a la
litterature momentum crypto (etude multi-horizons EMA 2020-2025).

Ce qui n'a PAS ete fait, et qui doit etre dit
---------------------------------------------
Aucun backtest. Aucune performance passee mesuree. Les parametres (fenetre,
seuil z, multiple de stop) sont des points de depart raisonnables tires de la
litterature, pas des valeurs optimisees sur donnees historiques.

Tant que ce module n'a pas tourne en observation et produit un echantillon de
signaux verifiables, il n'a aucune performance demontree. DRY_RUN reste vrai.
"""

from __future__ import annotations

from typing import Optional

from . import signals
from .risk import OrderIntent


class MomentumStrategy:
    """Conserve la serie de prix entre les cycles et produit l'intention."""

    def __init__(self, cfg: Optional[signals.SignalConfig] = None) -> None:
        self.cfg = cfg or signals.SignalConfig()
        self.buffer = signals.PriceBuffer(max_minutes=1440)
        self.last_measure: dict = {}
        self.direction: Optional[str] = None   # etat porte pour l'hysteresis

    def observe(self, ts: float, price: Optional[float]) -> None:
        if price is not None:
            self.buffer.add(ts, price)

    def decide(self, market_id: str, now: float) -> Optional[OrderIntent]:
        m = signals.compute(self.buffer, self.cfg, now, self.direction)
        self.last_measure = m
        self.direction = m.get("direction")
        if not m["tradeable"]:
            return None
        return OrderIntent(market_id=market_id,
                           side=m["direction"],
                           entry_price=m["entry_price"],
                           stop_price=m["stop_price"],
                           take_profit_price=m["take_profit_price"])


# Compatibilite avec l'ancienne signature : sans etat, aucun signal possible.
def decide(market_id: str, quote: dict, context: dict) -> Optional[OrderIntent]:
    return None
