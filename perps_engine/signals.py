"""
signals.py - construction du signal, normalise par la volatilite.

Fondement
---------
Shen, Urquhart & Wang (Financial Review, 2022), "Bitcoin intraday time series
momentum" : BTC n'a ni ouverture ni cloture, donc les auteurs decoupent la
journee par le volume et montrent que le rendement du premier segment predit
celui du dernier. La prevision est la plus forte sur les segments a fort volume
ou forte volatilite.

Toutes les etudes serieuses sur le momentum crypto normalisent le signal par la
volatilite (cf. l'etude multi-horizons EMA 2020-2025 sur huit actifs). Un seuil
brut calibre en semaine calme se declenche a tort en semaine agitee. On travaille
donc en unites d'ecart-type, jamais en pourcentage absolu.

Ce module ne decide rien : il mesure. La decision est dans strategy.py, le
dimensionnement dans risk.py.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple


@dataclass(frozen=True)
class SignalConfig:
    lookback_minutes: int = 60        # fenetre du momentum
    vol_window_minutes: int = 240     # fenetre d'estimation de la volatilite
    warmup_minutes: int = 180         # aucun signal avant d'avoir vu ca
    stop_vol_multiple: float = 2.0    # distance du stop en ecarts-type
    tp_r_multiple: float = 2.0        # objectif en multiples du risque
    max_staleness_seconds: int = 180  # au-dela, le prix n'est plus fiable

    # --- hysteresis (double seuil) ----------------------------------------
    # Observe le 17 sept : GOLD entre a z=-1.609 et sort a z=-1.441, soit
    # 0.17 ecart-type plus loin. Duree de vie du signal : 63 secondes. Un
    # momentum calcule sur 60 minutes qui vit une minute ne mesure pas le
    # marche, il mesure un seuil qui vibre.
    #
    # Deux seuils separes suppriment ce clignotement : il faut franchir
    # entry_z pour ouvrir, mais repasser SOUS exit_z pour fermer. Entre les
    # deux, l'etat precedent est conserve. Meme principe qu'un trigger de
    # Schmitt en electronique, ou qu'une bande morte sur un thermostat.
    entry_z: float = 2.0              # franchir ceci pour ouvrir un signal
    exit_z: float = 1.0               # repasser SOUS ceci pour le fermer

    # --- plancher de cout -------------------------------------------------
    # Mesure le 17 sept, distance de stop contre cout aller-retour (0.06 %) :
    #   EUR   0.059 %  -> le cout represente 102 % du risque : intradable
    #   JPY   0.103 %  -> 58 %
    #   GOLD  0.385 %  -> 16 %
    # Un stop plus petit que la commission perd avant que le marche bouge.
    # Le moteur refuse donc tout signal dont la distance de stop ne couvre
    # pas min_stop_cost_multiple fois le cout aller-retour. C'est la donnee
    # qui ecarte un marche, pas un choix de panier a priori.
    round_trip_cost_pct: float = 0.06     # 3 bps par cote, en pourcentage
    min_stop_cost_multiple: float = 5.0   # stop >= 5x le cout aller-retour


class PriceBuffer:
    """Serie de prix horodatee, bornee par une fenetre temporelle."""

    def __init__(self, max_minutes: int = 1440) -> None:
        self._pts: Deque[Tuple[float, float]] = deque()
        self._max_seconds = max_minutes * 60

    def add(self, ts: float, price: float) -> None:
        if price is None or price <= 0:
            return
        self._pts.append((ts, float(price)))
        cutoff = ts - self._max_seconds
        while self._pts and self._pts[0][0] < cutoff:
            self._pts.popleft()

    def __len__(self) -> int:
        return len(self._pts)

    @property
    def last(self) -> Optional[Tuple[float, float]]:
        return self._pts[-1] if self._pts else None

    def span_minutes(self) -> float:
        if len(self._pts) < 2:
            return 0.0
        return (self._pts[-1][0] - self._pts[0][0]) / 60

    def price_at_or_before(self, ts: float) -> Optional[float]:
        """Dernier prix connu a cet instant ou avant. None si hors historique."""
        found = None
        for t, p in self._pts:
            if t <= ts:
                found = p
            else:
                break
        return found

    def returns_series(self) -> list:
        out = []
        pts = list(self._pts)
        for i in range(1, len(pts)):
            p0, p1 = pts[i - 1][1], pts[i][1]
            if p0 > 0:
                out.append(math.log(p1 / p0))
        return out

    def window_returns(self, minutes: int) -> list:
        if not self._pts:
            return []
        cutoff = self._pts[-1][0] - minutes * 60
        pts = [(t, p) for t, p in self._pts if t >= cutoff]
        out = []
        for i in range(1, len(pts)):
            p0, p1 = pts[i - 1][1], pts[i][1]
            if p0 > 0:
                out.append(math.log(p1 / p0))
        return out


def stdev(xs: list) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def compute(buffer: PriceBuffer, cfg: SignalConfig, now: float,
            prev_direction: Optional[str] = None) -> dict:
    """
    Rend la mesure complete. `tradeable` dit si les donnees suffisent ;
    `direction` vaut "buy", "sell" ou None.

    `prev_direction` porte l'etat du cycle precedent, necessaire a
    l'hysteresis : sans lui on ne peut pas distinguer "ouvrir" de "maintenir",
    et le signal se remet a clignoter.

    Aucune extrapolation : donnees insuffisantes -> pas de signal, jamais un
    signal degrade. Un moteur qui invente un chiffre manquant est pire qu'un
    moteur muet.
    """
    out = {"tradeable": False, "direction": None, "reason": None,
           "samples": len(buffer), "span_minutes": round(buffer.span_minutes(), 1)}

    last = buffer.last
    if last is None:
        out["reason"] = "aucun prix"
        return out

    ts, price = last
    out["price"] = price

    if now - ts > cfg.max_staleness_seconds:
        out["reason"] = "prix perime"
        return out

    if buffer.span_minutes() < cfg.warmup_minutes:
        out["reason"] = f"chauffe {buffer.span_minutes():.0f}/{cfg.warmup_minutes} min"
        return out

    # --- momentum sur la fenetre de lookback ------------------------------
    past = buffer.price_at_or_before(ts - cfg.lookback_minutes * 60)
    if past is None or past <= 0:
        out["reason"] = "historique insuffisant"
        return out
    mom = math.log(price / past)
    out["momentum_log_return"] = round(mom, 6)
    out["momentum_pct"] = round((math.exp(mom) - 1) * 100, 3)

    # --- volatilite par minute, puis mise a l'echelle du lookback ---------
    rets = buffer.window_returns(cfg.vol_window_minutes)
    sd_min = stdev(rets)
    if sd_min <= 0:
        out["reason"] = "volatilite nulle"
        return out
    sd_lookback = sd_min * math.sqrt(cfg.lookback_minutes)
    out["vol_per_min_pct"] = round(sd_min * 100, 4)
    out["vol_lookback_pct"] = round(sd_lookback * 100, 3)

    z = mom / sd_lookback
    out["z"] = round(z, 3)

    # --- distance de stop, exprimee en volatilite -------------------------
    stop_distance = price * sd_lookback * cfg.stop_vol_multiple
    out["stop_distance"] = round(stop_distance, 2)
    out["stop_distance_pct"] = round(stop_distance / price * 100, 3)

    # --- plancher de cout : ecarte les marches trop calmes pour leurs frais
    min_stop_pct = cfg.round_trip_cost_pct * cfg.min_stop_cost_multiple
    stop_pct = stop_distance / price * 100
    out["cost_ratio"] = round(cfg.round_trip_cost_pct / stop_pct, 3) if stop_pct else None
    if stop_pct < min_stop_pct:
        out["reason"] = (f"stop {stop_pct:.3f} % < plancher {min_stop_pct:.2f} % "
                         f"(cout {cfg.round_trip_cost_pct} %)")
        out["blocked_by_cost"] = True
        return out

    # --- hysteresis : ouvrir sur entry_z, ne fermer que sous exit_z -------
    want = "buy" if z > 0 else "sell"
    if prev_direction in ("buy", "sell"):
        # Un signal ouvert se maintient tant que |z| reste au-dessus de
        # exit_z ET que le sens n'a pas change. Un renversement franc exige
        # de repasser la barre haute dans l'autre sens.
        if want == prev_direction and abs(z) >= cfg.exit_z:
            direction = prev_direction
        elif want != prev_direction and abs(z) >= cfg.entry_z:
            direction = want
        else:
            out["reason"] = (f"|z| {abs(z):.2f} < sortie {cfg.exit_z}"
                             if want == prev_direction else
                             f"renversement demande |z| >= {cfg.entry_z}")
            return out
    else:
        if abs(z) < cfg.entry_z:
            out["reason"] = f"|z| {abs(z):.2f} < entree {cfg.entry_z}"
            return out
        direction = want

    sign = 1 if direction == "buy" else -1
    out.update(tradeable=True, direction=direction,
               entry_price=price,
               stop_price=round(price - sign * stop_distance, 2),
               take_profit_price=round(
                   price + sign * stop_distance * cfg.tp_r_multiple, 2),
               reason="signal actif")
    return out


def carry_hurdle(stop_distance: float, price: float, size_notional_hint: float,
                 funding_per_day: float, expected_hold_hours: float) -> dict:
    """
    Le portage doit rester petit devant le mouvement vise.

    Mesure observee sur le compte : ~1.39 $/h sur 99 700 $ de notionnel, soit
    ~0.033 %/jour. Modeste en relatif, enorme face a un budget de drawdown de
    300 $. Une position gagnante peut masquer une hemorragie continue : les deux
    flux se mesurent separement.
    """
    if size_notional_hint <= 0 or price <= 0:
        return {"carry_ok": True, "carry_pct_of_target": 0.0}
    daily_pct = funding_per_day / size_notional_hint if size_notional_hint else 0.0
    carry_cost_pct = daily_pct * (expected_hold_hours / 24)
    target_pct = stop_distance / price
    ratio = carry_cost_pct / target_pct if target_pct > 0 else 1.0
    return {"carry_ok": ratio < 0.25,
            "carry_pct_of_target": round(ratio * 100, 2)}
