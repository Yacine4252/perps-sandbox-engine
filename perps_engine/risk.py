"""
risk.py - la couche de garde-fous.

Regle unique du projet : aucun ordre n'existe sans etre passe par evaluate_order().
Module PUR : pas de reseau, pas d'horloge, pas d'etat global.
Memes entrees -> meme sortie. Testable hors ligne, integralement.

Le module ne connait aucun compte en particulier. Il recoit des regles et un etat,
il rend un verdict. Changer de compte, de taille ou de firme ne touche pas ce fichier.

PRINCIPE DIRECTEUR
------------------
La strategie propose une DIRECTION et un STOP. Elle ne propose ni taille ni levier.
Ces deux grandeurs sont derivees ici, dans cet ordre :

    1. taille   = budget de risque / distance au stop  (frais inclus)
    2. notionnel = taille x prix
    3. levier   = le PLUS BAS qui tienne dans le plafond de marge
                  -> maximise la distance a la liquidation

Le levier n'est pas un curseur d'agressivite. C'est la consequence arithmetique
d'une taille et d'un plafond de marge. Un levier eleve avec un stop serre immobilise
moins de capital et n'ajoute aucun risque tant que la liquidation reste au-dela du
stop. Un levier bas avec un stop large est le vrai danger : c'est ce qui laisse la
liquidation arriver avant le stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Reject(str, Enum):
    NO_STOP = "NO_STOP"
    STOP_WRONG_SIDE = "STOP_WRONG_SIDE"
    SIZE_PROVIDED_BY_CALLER = "SIZE_PROVIDED_BY_CALLER"
    LEVERAGE_PROVIDED_BY_CALLER = "LEVERAGE_PROVIDED_BY_CALLER"
    NO_VALID_LEVERAGE = "NO_VALID_LEVERAGE"
    STOP_BEYOND_LIQUIDATION = "STOP_BEYOND_LIQUIDATION"
    FEES_EXCEED_RISK = "FEES_EXCEED_RISK"
    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
    DAILY_CUTOFF_REACHED = "DAILY_CUTOFF_REACHED"
    TRADE_LIMIT_24H = "TRADE_LIMIT_24H"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    ROOM_EXHAUSTED = "ROOM_EXHAUSTED"
    POSITION_LIMIT_REACHED = "POSITION_LIMIT_REACHED"
    STALE_DATA = "STALE_DATA"
    TARGET_REACHED = "TARGET_REACHED"
    RR_TOO_LOW = "RR_TOO_LOW"


# --------------------------------------------------------------------------
# Entrees
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountRules:
    """Regles de la firme. Remplies depuis GET /accounts/{id}/trading-policy."""
    starting_balance: float
    daily_loss_pct: float
    drawdown_pct: float
    drawdown_type: str            # "static" | "trailing"
    profit_target_pct: float
    consistency_pct: float = 0.0

    @classmethod
    def from_policy(cls, policy: dict, starting_balance: float) -> "AccountRules":
        rules = {r["id"]: r.get("parameters", {}) for r in policy.get("account_rules", [])}
        dd = rules.get("max-drawdown", {})
        return cls(
            starting_balance=starting_balance,
            daily_loss_pct=rules.get("max-daily-loss", {}).get("pct", 0) / 100,
            drawdown_pct=dd.get("pct", 0) / 100,
            drawdown_type=dd.get("type", "static"),
            profit_target_pct=rules.get("profit-target", {}).get("pct", 0) / 100,
            consistency_pct=rules.get("consistency", {}).get("pct", 0) / 100,
        )


@dataclass(frozen=True)
class PlatformLimits:
    """Limites techniques. Remplies depuis le bloc `limits` de l'API."""
    min_order_notional_usd: float = 10.0
    max_open_positions: int = 10
    maintenance_margin_ratio_bps: int = 1500   # % de la MARGE INITIALE
    max_book_staleness_ms: int = 2500
    max_platform_leverage: float = 50.0
    taker_fee_bps: float = 3.0                 # par cote

    @classmethod
    def from_policy(cls, policy: dict, taker_fee_bps: float = 3.0,
                    max_platform_leverage: float = 50.0) -> "PlatformLimits":
        lim = policy.get("limits", {})
        return cls(
            min_order_notional_usd=lim.get("min_order_notional_usd", 10.0),
            max_open_positions=lim.get("max_open_positions", 10),
            maintenance_margin_ratio_bps=lim.get("maintenance_margin_ratio_bps", 1500),
            max_book_staleness_ms=lim.get("max_book_staleness_ms", 2500),
            max_platform_leverage=max_platform_leverage,
            taker_fee_bps=taker_fee_bps,
        )


@dataclass(frozen=True)
class RiskPolicy:
    """MA discipline. Toujours plus stricte que la firme, jamais l'inverse."""
    risk_pct_of_balance: float = 0.01      # plafond dur : 1% du capital initial
    risk_pct_of_room: float = 0.25         # part de la marge RESTANTE engageable
    daily_cutoff_pct: float = 0.70
    max_trades_24h: int = 3
    cooldown_after_loss_min: int = 45
    liq_buffer_pct: float = 0.20           # coussin stop <-> liquidation
    max_concurrent_positions: int = 1
    max_margin_utilisation: float = 0.50   # marge immobilisee / equity
    max_leverage_ceiling: float = 20.0     # plafond de confort
    min_rr: float = 1.5
    # Plafond d'exposition : notionnel <= N x equity. None = aucun plafond.
    # Ajoute le 23 oct apres deux jours sur le 100K : NDX a ~3x = +2 436 $
    # net, or a ~10x = -15 $ net pour un risque enorme. La taille est
    # REDUITE pour tenir sous le plafond, jamais augmentee.
    max_notional_multiple: Optional[float] = None


@dataclass(frozen=True)
class AccountState:
    equity: float
    balance: float
    realized_pnl_today: float = 0.0
    open_positions: int = 0
    trades_last_24h: int = 0
    minutes_since_last_loss: Optional[float] = None
    quote_age_ms: int = 0
    high_water_balance: Optional[float] = None


@dataclass(frozen=True)
class OrderIntent:
    """Ce que la strategie a le droit de proposer. Ni taille ni levier."""
    market_id: str
    side: str                              # "buy" | "sell"
    entry_price: float
    stop_price: Optional[float]
    take_profit_price: Optional[float] = None
    margin_mode: str = "isolated"
    size: Optional[float] = None           # doit rester None
    leverage: Optional[float] = None       # doit rester None


@dataclass
class RiskVerdict:
    approved: bool
    computed_size: Optional[float]
    computed_leverage: Optional[float] = None
    reject_codes: list = field(default_factory=list)
    computed: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.approved


# --------------------------------------------------------------------------
# Primitives - verifiees contre les donnees reelles de la plateforme
# --------------------------------------------------------------------------

def liquidation_price(entry: float, leverage: float, side: str,
                      maintenance_margin_ratio_bps: int) -> float:
    """
    Prix de liquidation en marge isolee.

    maintenance_margin_ratio_bps exprime la marge de maintenance en % de la
    MARGE INITIALE, pas du notionnel.

    Verifie contre un ordre reel : entree 77165.569, 10x, mmr 1500bps
      -> 77165.569 * (1 - 0.10*0.85) = 70606.50
      La plateforme affichait 70606.496.
    """
    mmr = maintenance_margin_ratio_bps / 10_000
    move = (1 / leverage) * (1 - mmr)
    return entry * (1 - move) if side == "buy" else entry * (1 + move)


def max_leverage_for_stop(entry: float, stop_distance: float,
                          maintenance_margin_ratio_bps: int,
                          liq_buffer_pct: float) -> float:
    """
    Levier le PLUS ELEVE qui garde la liquidation au-dela du stop, coussin inclus.
    Au-dela, la position est liquidee avant que le stop ne protege quoi que ce soit.
    """
    if stop_distance <= 0:
        return 0.0
    mmr = maintenance_margin_ratio_bps / 10_000
    return entry * (1 - mmr) * (1 - liq_buffer_pct) / stop_distance


def equity_floor(rules: AccountRules, state: AccountState) -> float:
    if rules.drawdown_type == "trailing":
        hwm = max(state.high_water_balance or rules.starting_balance, rules.starting_balance)
        return hwm * (1 - rules.drawdown_pct)
    return rules.starting_balance * (1 - rules.drawdown_pct)


def size_from_risk(risk_budget: float, entry: float, stop_distance: float,
                   fee_bps_per_side: float) -> float:
    """
    Taille derivee du stop, frais aller-retour integres a l'equation :

        size*stop_distance + size*entry*2*fee_rate = risk_budget

    Les frais ne sont pas une estimation ajoutee apres coup. Sur un compte a
    drawdown serre, le cout de transaction EST du drawdown.
    """
    fee_rate = fee_bps_per_side / 10_000
    denom = stop_distance + 2 * entry * fee_rate
    return risk_budget / denom if denom > 0 else 0.0


# --------------------------------------------------------------------------
# Fonction publique unique
# --------------------------------------------------------------------------

def evaluate_order(intent: OrderIntent, state: AccountState, rules: AccountRules,
                   limits: PlatformLimits, policy: RiskPolicy) -> RiskVerdict:
    rejects: list = []
    c: dict = {}

    # --- Budget ------------------------------------------------------------
    floor = equity_floor(rules, state)
    room = state.equity - floor
    daily_budget = rules.starting_balance * rules.daily_loss_pct
    effective_daily = min(daily_budget, max(room, 0.0))
    loss_today = max(-state.realized_pnl_today, 0.0)
    target_equity = rules.starting_balance * (1 + rules.profit_target_pct)

    risk_budget = min(rules.starting_balance * policy.risk_pct_of_balance,
                      max(room, 0.0) * policy.risk_pct_of_room,
                      max(effective_daily - loss_today, 0.0))

    c.update(floor=round(floor, 2), room=round(room, 2),
             daily_budget=round(daily_budget, 2),
             effective_daily=round(effective_daily, 2),
             loss_today=round(loss_today, 2),
             risk_budget=round(risk_budget, 2))

    # --- Blocages d'etat ---------------------------------------------------
    if room <= 0:
        rejects.append(Reject.ROOM_EXHAUSTED)
    if state.equity >= target_equity:
        rejects.append(Reject.TARGET_REACHED)
    if effective_daily > 0 and loss_today >= effective_daily * policy.daily_cutoff_pct:
        rejects.append(Reject.DAILY_CUTOFF_REACHED)
    if state.trades_last_24h >= policy.max_trades_24h:
        rejects.append(Reject.TRADE_LIMIT_24H)
    if (state.minutes_since_last_loss is not None
            and state.minutes_since_last_loss < policy.cooldown_after_loss_min):
        rejects.append(Reject.COOLDOWN_ACTIVE)
    if state.open_positions >= min(policy.max_concurrent_positions, limits.max_open_positions):
        rejects.append(Reject.POSITION_LIMIT_REACHED)
    if state.quote_age_ms > limits.max_book_staleness_ms:
        rejects.append(Reject.STALE_DATA)

    # --- L'appelant ne decide ni la taille ni le levier --------------------
    if intent.size is not None:
        rejects.append(Reject.SIZE_PROVIDED_BY_CALLER)
    if intent.leverage is not None:
        rejects.append(Reject.LEVERAGE_PROVIDED_BY_CALLER)

    # --- Stop --------------------------------------------------------------
    if intent.stop_price is None or intent.stop_price == intent.entry_price:
        rejects.append(Reject.NO_STOP)
        return RiskVerdict(False, None, None, _uniq(rejects), c)

    long = intent.side == "buy"
    if (long and intent.stop_price >= intent.entry_price) or \
       (not long and intent.stop_price <= intent.entry_price):
        rejects.append(Reject.STOP_WRONG_SIDE)

    stop_distance = abs(intent.entry_price - intent.stop_price)
    c["stop_distance"] = round(stop_distance, 4)
    c["stop_pct"] = round(stop_distance / intent.entry_price * 100, 3)

    # --- Ratio risque/rendement -------------------------------------------
    if intent.take_profit_price is not None:
        rr = abs(intent.take_profit_price - intent.entry_price) / stop_distance
        c["rr"] = round(rr, 2)
        if rr < policy.min_rr:
            rejects.append(Reject.RR_TOO_LOW)

    # --- Taille ------------------------------------------------------------
    size = size_from_risk(risk_budget, intent.entry_price, stop_distance, limits.taker_fee_bps)
    if (policy.max_notional_multiple is not None and state.equity > 0
            and intent.entry_price > 0):
        cap = policy.max_notional_multiple * state.equity / intent.entry_price
        if size > cap:
            size = cap
            c["capped_by_exposure"] = True
    notional = size * intent.entry_price
    fees = notional * 2 * limits.taker_fee_bps / 10_000

    c.update(computed_size=round(size, 6), notional=round(notional, 2),
             fees_round_trip=round(fees, 2),
             loss_at_stop=round(size * stop_distance + fees, 2))

    if fees >= risk_budget:
        rejects.append(Reject.FEES_EXCEED_RISK)
    if notional < limits.min_order_notional_usd:
        rejects.append(Reject.BELOW_MIN_NOTIONAL)

    # --- Fenetre de levier admissible --------------------------------------
    # Plancher : immobiliser au plus max_margin_utilisation de l'equity.
    margin_cap = state.equity * policy.max_margin_utilisation
    lev_min = notional / margin_cap if margin_cap > 0 else float("inf")
    # Plafond : garder la liquidation au-dela du stop.
    lev_max_liq = max_leverage_for_stop(intent.entry_price, stop_distance,
                                        limits.maintenance_margin_ratio_bps,
                                        policy.liq_buffer_pct)
    lev_hi = min(lev_max_liq, policy.max_leverage_ceiling, limits.max_platform_leverage)
    lev_lo = max(lev_min, 1.0)

    c.update(lev_min_for_margin=round(lev_min, 2),
             lev_max_for_liquidation=round(lev_max_liq, 2),
             lev_window=[round(lev_lo, 2), round(lev_hi, 2)])

    if lev_lo > lev_hi:
        # Deux causes possibles, toutes deux structurelles :
        #  - le stop est trop large pour le capital qu'on accepte d'immobiliser
        #  - la taille exigee depasse ce que le compte peut porter proprement
        rejects.append(Reject.NO_VALID_LEVERAGE)
        if lev_max_liq < lev_min:
            rejects.append(Reject.STOP_BEYOND_LIQUIDATION)
        return RiskVerdict(False, None, None, _uniq(rejects), c)

    # Le levier le plus bas de la fenetre = liquidation la plus eloignee.
    leverage = round(lev_lo, 2)
    liq = liquidation_price(intent.entry_price, leverage, intent.side,
                            limits.maintenance_margin_ratio_bps)
    c.update(chosen_leverage=leverage,
             margin_required=round(notional / leverage, 2),
             liquidation_price=round(liq, 2),
             stop_to_liq_buffer_pct=round(
                 (abs(intent.entry_price - liq) - stop_distance)
                 / abs(intent.entry_price - liq) * 100, 1))

    rejects = _uniq(rejects)
    approved = not rejects and size > 0
    return RiskVerdict(approved,
                       round(size, 6) if approved else None,
                       leverage if approved else None,
                       rejects, c)


def _uniq(codes: list) -> list:
    seen, out = set(), []
    for x in codes:
        v = x.value if isinstance(x, Reject) else x
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out
