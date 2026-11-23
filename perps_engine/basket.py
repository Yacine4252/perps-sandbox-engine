"""
basket.py - le panier de marches observes (v2, resserre apres analyse).

Pourquoi resserre
------------------
La v1 (12 marches) melangeait :
  - 6 cryptos tres correlees (BTC/ETH/SOL/XRP/BNB/DOGE) : un seul facteur de
    risque dominant, replique 6 fois, paye 6 fois en frais
  - 2 metaux correles entre eux (GOLD/SILVER, ~0.7-0.8 historiquement)
  - 2 indices synthetiques dont l'analyse de la semaine du 13-15 sept a
    montre un comportement suspect : SP500 et JP225 ont produit 49 et 34
    retournements de signal sur 41h, contre 19 pour BTC. Explication la
    plus probable : produits synthetiques qui suivent un marche cash aux
    heures limitees. Hors cotation, la liquidite s'effondre, la volatilite
    realisee tombe pres de zero, et un mouvement derisoire produit un
    z-score enorme par division par un denominateur minuscule. Le signal
    ne mesure plus une tendance, il mesure que le marche sous-jacent dort.

Le probleme de fond n'etait pas la TAILLE du panier, c'etait sa
COMPOSITION : 12 marches pour ~2-3 paris reellement independants. Cf.
Grinold & Kahn (loi fondamentale de la gestion active) : le ratio
d'information croit avec la racine du nombre de paris INDEPENDANTS, pas
avec le nombre de tickers surveilles. Ajouter des variantes correlees du
meme pari ne cree aucune information, seulement des frais.

Panier v2 : quatre classes a facteurs de risque distincts.
  BTC     - sentiment crypto / flux natifs
  GOLD    - taux reels / valeur refuge
  EURUSD  - differentiel de taux, zone euro vs dollar
  USDJPY  - devise de financement, comportement risk-off distinct de l'EUR

Aucun synthetique a risque d'illiquidite hors-heures, aucune redondance
avec un membre deja present. Sur les donnees du 13-15 sept, ce
sous-ensemble aurait a lui seul produit 124 des 328 signaux observes :
assez pour trancher, sans les doublons crypto ni les artefacts d'indices.

Si un edge reel se confirme sur ces quatre marches, l'elargissement redevient
justifie - mais seulement en ajoutant des classes d'actifs distinctes, jamais
des variantes correlees d'une classe deja presente.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class Market:
    market_id: str
    symbol: str
    klass: str          # crypto | metal | index | fx
    max_leverage: float


# EUR et JPY restent listes mais INACTIFS : mesure du 17 sept, leur stop
# (0.059 % et 0.103 %) ne couvre pas le cout aller-retour de 0.06 %. Le
# plancher de cout de signals.py les ecarterait de toute facon a chaque
# cycle ; les garder actifs ne ferait que remplir le journal de refus.
# Ils reviendront si le venue offre des frais plus bas ou si leur
# volatilite remonte durablement.
DEFAULT_BASKET: List[Market] = [
    Market("binance|BTCUSDT", "BTC", "crypto", 10),
    Market("hyperliquid|xyz:GOLD", "GOLD", "metal", 10),
]

INACTIVE_BASKET: List[Market] = [
    Market("lighter|EURUSD", "EUR", "fx", 50),
    Market("lighter|USDJPY", "JPY", "fx", 50),
]


def cross_sectional_rank(measures: dict) -> dict:
    """
    Momentum TRANSVERSAL : classe les marches par force relative.

    `measures` : {symbol: mesure de signals.compute()}.
    Seuls les marches dont le z est calculable entrent au classement - un
    marche encore en chauffe n'est pas classe dernier, il est absent.
    """
    scored = [(sym, m["z"]) for sym, m in measures.items()
              if m.get("z") is not None]
    if len(scored) < 2:
        return {"ranked": [], "top": None, "bottom": None, "breadth": None}

    scored.sort(key=lambda x: x[1], reverse=True)
    positives = sum(1 for _, z in scored if z > 0)
    return {"ranked": [{"sym": s, "z": round(z, 2)} for s, z in scored],
            "top": scored[0][0],
            "bottom": scored[-1][0],
            "breadth": round(positives / len(scored), 2),
            "dispersion": round(scored[0][1] - scored[-1][1], 2)}


def expected_false_positive_rate(n_markets: int, entry_z: float) -> float:
    """
    Combien de signaux attendre du pur hasard, pour garder la tete froide
    a la lecture du journal.

    Approximation normale bilaterale : P(|Z| > k) ~ 2 * (1 - Phi(k)).
    A 1.5 ecart-type, ~13.4 % par marche.
    """
    import math
    p = 2 * (1 - 0.5 * (1 + math.erf(entry_z / math.sqrt(2))))
    return round(p * n_markets, 2)
