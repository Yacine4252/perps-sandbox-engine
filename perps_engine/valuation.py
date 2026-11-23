"""
valuation.py - calcul de l'equity REELLE.

Pourquoi ce module existe
-------------------------
Le champ `equity` renvoye par l'API ne contient pas le PnL latent. Sur 6h15
d'observation avec une position de 99 700 $ de notionnel ouverte, la valeur
n'a bouge que par paliers de ~1.25 $ (le financement), jamais avec le prix.

Un moteur qui se fie a ce champ est aveugle au risque reel : le plancher de
drawdown se compare a une valeur qui ne bouge pas, donc la coupure ne se
declenche jamais a temps. Sur un compte reel, c'est la difference entre un
garde-fou et une decoration.

L'equity qui compte :

    equity = solde + SOMME( (marque - entree) * taille * sens )

Les noms de champs de l'API des positions ne sont pas garantis : ce module
les extrait de facon tolerante et signale ce qu'il n'a pas su lire, plutot
que de supposer un format et de se tromper en silence.
"""

from __future__ import annotations

from typing import Callable, Optional

_SIZE_KEYS = ("size", "quantity", "qty", "base_size", "amount", "contracts")
_ENTRY_KEYS = ("entry_price", "average_entry_price", "avg_entry_price",
               "entry", "average_price", "avg_price", "open_price")
_SIDE_KEYS = ("side", "direction", "position_side")
_MARKET_KEYS = ("market_id", "market", "symbol", "instrument", "coin")
_MARK_KEYS = ("mark_price", "mark", "last_price", "index_price")
_UPNL_KEYS = ("unrealized_pnl", "unrealised_pnl", "unrealized_profit", "upnl")


def _first(d: dict, keys) -> Optional[object]:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _as_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalise_position(raw: dict) -> dict:
    """Extrait les champs utiles d'une position, quel que soit le nommage."""
    size = _as_float(_first(raw, _SIZE_KEYS))
    entry = _as_float(_first(raw, _ENTRY_KEYS))
    side = _first(raw, _SIDE_KEYS)
    market = _first(raw, _MARKET_KEYS)
    mark = _as_float(_first(raw, _MARK_KEYS))
    upnl = _as_float(_first(raw, _UPNL_KEYS))

    sign = 1.0
    if isinstance(side, str) and side.lower() in ("sell", "short", "-1"):
        sign = -1.0
    elif size is not None and size < 0:
        sign = -1.0

    return {"market_id": market, "side": side, "sign": sign,
            "size": abs(size) if size is not None else None,
            "entry_price": entry, "mark_price": mark,
            "api_unrealized_pnl": upnl,
            "unreadable": [k for k, v in
                           (("size", size), ("entry_price", entry), ("market_id", market))
                           if v is None]}


def position_pnl(pos: dict, mark: Optional[float]) -> Optional[float]:
    """PnL latent d'une position. None si les donnees ne permettent pas le calcul."""
    if mark is None:
        mark = pos.get("mark_price")
    if mark is None or pos["size"] is None or pos["entry_price"] is None:
        # Dernier recours : la valeur de l'API, si elle existe.
        return pos.get("api_unrealized_pnl")
    return (mark - pos["entry_price"]) * pos["size"] * pos["sign"]


def compute_equity(balance: float, raw_positions: list,
                   quote_fn: Callable[[str], Optional[float]]) -> dict:
    """
    Rend l'equity reelle et le detail par position.

    quote_fn(market_id) -> prix milieu, ou None si indisponible.
    Toute position dont le PnL n'a pas pu etre calcule est signalee dans
    `incomplete` : on ne prend jamais un PnL manquant pour un PnL nul.
    """
    total_pnl = 0.0
    details, incomplete = [], []

    for raw in raw_positions or []:
        pos = normalise_position(raw)
        mark = None
        if pos["market_id"]:
            try:
                mark = quote_fn(pos["market_id"])
            except Exception:
                mark = None
        pnl = position_pnl(pos, mark)

        if pnl is None:
            incomplete.append({"market_id": pos["market_id"],
                               "unreadable": pos["unreadable"],
                               "raw_keys": sorted(raw.keys())})
        else:
            total_pnl += pnl

        details.append({"market_id": pos["market_id"], "side": pos["side"],
                        "size": pos["size"], "entry": pos["entry_price"],
                        "mark": mark, "pnl": round(pnl, 2) if pnl is not None else None})

    return {"equity": round(balance + total_pnl, 4),
            "balance": round(balance, 4),
            "unrealized_pnl": round(total_pnl, 2),
            "positions": details,
            "incomplete": incomplete,
            "reliable": not incomplete}


class FundingTracker:
    """
    Mesure le cout de portage a partir des variations de solde hors trades.

    Le journal a montre ~1.39 $/h sur une position de 99 700 $ de notionnel,
    soit ~33 $/jour. Sur un compte a 300 $ de budget de drawdown, une position
    tenue une semaine consomme tout le compte sans qu'aucun stop ne soit touche.
    Le portage est du drawdown : il se mesure, il ne s'ignore pas.
    """

    def __init__(self) -> None:
        self.first_balance: Optional[float] = None
        self.first_ts: Optional[float] = None
        self.last_balance: Optional[float] = None
        self.cumulative: float = 0.0

    def update(self, balance: float, ts: float, trades_occurred: bool = False) -> dict:
        if self.first_balance is None:
            self.first_balance, self.first_ts = balance, ts
        elif self.last_balance is not None and not trades_occurred:
            delta = balance - self.last_balance
            if delta < 0:
                self.cumulative += -delta
        self.last_balance = balance

        hours = max((ts - (self.first_ts or ts)) / 3600, 1e-9)
        per_hour = self.cumulative / hours
        return {"funding_cumulative": round(self.cumulative, 4),
                "funding_per_hour": round(per_hour, 4),
                "funding_per_day": round(per_hour * 24, 2),
                "hours_observed": round(hours, 2)}


def floor_distance(equity: float, floor: float, starting_balance: float) -> dict:
    room = equity - floor
    return {"room_usd": round(room, 2),
            "room_pct_of_account": round(room / starting_balance * 100, 3),
            "pct_of_drawdown_used": round(
                (starting_balance - equity) / (starting_balance - floor) * 100, 1)
            if starting_balance > floor else 0.0}


def days_to_floor(room_usd: float, funding_per_day: float) -> Optional[float]:
    """Compte a rebours du portage seul, marche immobile. None si non applicable."""
    if funding_per_day <= 0:
        return None
    return round(room_usd / funding_per_day, 1)
