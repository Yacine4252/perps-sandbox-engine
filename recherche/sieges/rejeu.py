"""Rejeu du momentum v2.4 sur bougies 1 min, en R. Aucun ordre.

Deux rejeux :
  fidele : les deux marches ensemble, avec le garde-fou du moteur (une position
           a la fois, 3 trades par 24 h, pause de 45 min apres une perte) :
           les trades que le moteur aurait pris.
  tous   : chaque signal neuf devient un trade, marche par marche, sans
           garde-fou : l'echantillon complet pour juger les sieges.

Le signal vient du code du moteur (signaux_moteur.py). Les entrees (update_arming),
le garde-fou (evaluate_order, POLICY) et les arrondis (round_price) sont ceux de
sandbox_run.py et risk.py, appeles directement.

Sorties, comme sur la plateforme et dans sandbox_run.py :
  - stop et objectif (2 R) poses dans l'ordre d'entree ; si une bougie 1 min
    touche les deux, on compte le stop (on ne sait pas lequel est venu d'abord) ;
  - sinon sortie au prix de cloture du cycle ou le signal retombe ou s'inverse.
R = distance d'entree au stop. Frais en R = frais x (entree + sortie) / R.
"""
import os
import sys
from collections import Counter, deque
from dataclasses import dataclass

ICI = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.dirname(os.path.dirname(ICI))
sys.path.insert(0, RACINE)

from perps_engine import sandbox_run as SR                       # noqa: E402
from perps_engine.risk import (AccountRules, AccountState,       # noqa: E402
                               OrderIntent, PlatformLimits, evaluate_order)
from perps_engine.signals import SignalConfig                    # noqa: E402

MIN_MS = 60_000
COTE = {1: "buy", -1: "sell"}
TP_R = SignalConfig().tp_r_multiple

# Compte fictif sain pour le garde-fou : 100 000 $, plancher -6 % statique, perte
# du jour -3 %, comme le compte sandbox. L'equite reste fixe : on mesure la
# strategie, pas l'usure du compte (le plancher du compte n'est pas simule).
REGLES = AccountRules(starting_balance=100_000, daily_loss_pct=0.03, drawdown_pct=0.06,
                      drawdown_type="static", profit_target_pct=0.08)
LIMITES = PlatformLimits(taker_fee_bps=SR.TAKER_FEE_BPS, max_platform_leverage=50.0)


@dataclass
class Marche:
    sym: str
    t: list        # ouverture des bougies 1 min (ms)
    o: list
    h: list
    l: list
    c: list
    dir: list      # signal du moteur apres le cycle de cette bougie : +1, -1, 0
    dist: list     # distance du stop calculee par le moteur (None sans z)
    z: list
    vu: list       # le cycle precedent avait deja un z (sinon : ne a la sortie de chauffe)
    frais_bps: float


@dataclass
class Position:
    sym: str
    dir: int
    entree: float
    stop: float
    objectif: float
    risque: float
    i: int         # indice de la bougie d'entree dans son marche
    t: int         # instant d'entree (cloture de la bougie), ms
    z: float


def ouvrir(m, i, d):
    entree = m.c[i]
    dist = m.dist[i]
    stop = SR.round_price(entree - d * dist)
    obj = SR.round_price(entree + d * dist * TP_R)
    return Position(m.sym, d, entree, stop, obj, abs(entree - stop), i, m.t[i] + MIN_MS, m.z[i])


def sortie_plateforme(pos, o, h, l):
    """(motif, prix, ambigu) si le stop ou l'objectif est touche pendant la bougie."""
    if pos.dir == 1:
        stop, obj = l <= pos.stop, h >= pos.objectif
        if stop:
            return "sl", min(o, pos.stop), obj
        if obj:
            return "tp", pos.objectif, False
    else:
        stop, obj = h >= pos.stop, l <= pos.objectif
        if stop:
            return "sl", max(o, pos.stop), obj
        if obj:
            return "tp", pos.objectif, False
    return None


def cloturer(pos, prix, t_ms, motif, frais_bps, ambigu=False):
    brut = (prix - pos.entree) * pos.dir / pos.risque
    frais = frais_bps / 1e4 * (pos.entree + prix) / pos.risque
    return {"sym": pos.sym, "sens": pos.dir, "t_entree": pos.t, "i_entree": pos.i,
            "entree": pos.entree, "stop": pos.stop, "objectif": pos.objectif,
            "risque": pos.risque, "risque_bps": pos.risque / pos.entree * 1e4, "z": pos.z,
            "t_sortie": t_ms, "prix_sortie": prix, "sortie": motif, "ambigu": ambigu,
            "r_brut": brut, "frais_r": frais, "r_net": brut - frais,
            "duree_min": (t_ms - pos.t) / MIN_MS}


def tous_les_signaux(m, debut_ms, sortie_signal=True):
    """Chaque signal neuf (vu naitre) a partir de debut_ms devient un trade.
    sortie_signal=False : stop ou objectif seulement (le comportement d'avant la
    v2.4) ; les trades d'un meme marche peuvent alors se chevaucher."""
    trades = []
    ouvertes = []
    prec = 0
    for i in range(len(m.t)):
        T = m.t[i] + MIN_MS
        if ouvertes:
            restent = []
            for pos in ouvertes:
                x = sortie_plateforme(pos, m.o[i], m.h[i], m.l[i]) if pos.i < i else None
                if x:
                    trades.append(cloturer(pos, x[1], T, x[0], m.frais_bps, x[2]))
                else:
                    restent.append(pos)
            ouvertes = restent
        d = m.dir[i]
        neuf = d != prec and d != 0 and m.vu[i]
        prec = d
        if sortie_signal and ouvertes:
            restent = []
            for pos in ouvertes:
                if d != pos.dir:
                    trades.append(cloturer(pos, m.c[i], T, "signal", m.frais_bps))
                else:
                    restent.append(pos)
            ouvertes = restent
        if neuf and T >= debut_ms and m.dist[i] and not (sortie_signal and ouvertes):
            ouvertes.append(ouvrir(m, i, d))
    for pos in ouvertes:
        trades.append(cloturer(pos, m.c[-1], m.t[-1] + MIN_MS, "fin", m.frais_bps))
    trades.sort(key=lambda x: x["t_entree"])
    return trades


def fidele(marches, debut_ms, sortie_signal=True, filtre=None):
    """Les marches ensemble, dans l'ordre du moteur (BTC puis GOLD), avec son
    garde-fou. Rend (trades, refus par motif, tentatives).
    sortie_signal=False : le comportement d'avant la v2.4, quand chaque sortie sur
    signal echouait (HTTP 400) : la position reste ouverte jusqu'au stop ou a
    l'objectif, et bloque les signaux suivants (une position a la fois).
    filtre(sym, i, sens, entree, objectif) -> True : les sieges mettent leur veto,
    avant le garde-fou, comme la table de la v2.5 en mode veto."""
    par_sym = {m.sym: m for m in marches}
    ptr = {}
    prev_dirs, armed = {}, {}
    for m in marches:
        p = 0
        while p < len(m.t) and m.t[p] < debut_ms:
            p += 1
        ptr[m.sym] = p
        prev_dirs[m.sym] = COTE.get(m.dir[p - 1]) if p > 0 else None
    temps = sorted({u for m in marches for u in m.t[ptr[m.sym]:]})

    trades, refus, ouvertures = [], Counter(), deque()
    tentatives = 0
    pos, derniere_perte = None, None

    def fermer(prix, T, motif, ambigu=False):
        nonlocal pos, derniere_perte
        tr = cloturer(pos, prix, T, motif, par_sym[pos.sym].frais_bps, ambigu)
        trades.append(tr)
        if tr["r_net"] < 0:
            derniere_perte = T
        pos = None

    for u in temps:
        present = {}
        for m in marches:
            p = ptr[m.sym]
            if p < len(m.t) and m.t[p] == u:
                present[m.sym] = p
                ptr[m.sym] = p + 1
        T = u + MIN_MS
        now = T / 1000.0

        # 1. stop / objectif executes par la plateforme pendant la minute
        if pos is not None and pos.sym in present and present[pos.sym] > pos.i:
            m = par_sym[pos.sym]
            i = present[pos.sym]
            x = sortie_plateforme(pos, m.o[i], m.h[i], m.l[i])
            if x:
                fermer(x[1], T, x[0], x[2])

        # 2. signaux et armement (code du moteur)
        dirs = {}
        for m in marches:
            dirs[m.sym] = (COTE.get(m.dir[present[m.sym]]) if m.sym in present
                           else prev_dirs.get(m.sym))
        for sym, frm, to in SR.update_arming(armed, prev_dirs, dirs, now):
            if to in ("buy", "sell") and not par_sym[sym].vu[present[sym]]:
                armed.pop(sym, None)            # ne a la sortie de chauffe

        # 3. sortie quand le signal retombe ou s'inverse
        if (sortie_signal and pos is not None and pos.sym in present
                and dirs[pos.sym] != COTE[pos.dir]):
            m = par_sym[pos.sym]
            fermer(m.c[present[pos.sym]], T, "signal")

        # 4. entrees : une tentative par signal, garde-fou du moteur
        for sym in list(armed):
            arm = armed.get(sym)
            if not arm or dirs.get(sym) != arm["dir"] or sym not in present:
                continue
            m = par_sym[sym]
            d = 1 if arm["dir"] == "buy" else -1
            neuve = ouvrir(m, present[sym], d)
            if filtre is not None and filtre(sym, present[sym], d, neuve.entree, neuve.objectif):
                armed.pop(sym, None)
                refus["veto des sieges"] += 1
                continue
            while ouvertures and ouvertures[0] < T - 86_400_000:
                ouvertures.popleft()
            etat = AccountState(
                equity=100_000, balance=100_000, open_positions=1 if pos else 0,
                trades_last_24h=len(ouvertures),
                minutes_since_last_loss=(T - derniere_perte) / MIN_MS if derniere_perte else None)
            intent = OrderIntent(market_id=sym, side=arm["dir"], entry_price=neuve.entree,
                                 stop_price=neuve.stop, take_profit_price=neuve.objectif)
            v = evaluate_order(intent, etat, REGLES, LIMITES, SR.POLICY)
            armed.pop(sym, None)
            tentatives += 1
            if not v.approved:
                refus[",".join(v.reject_codes)] += 1
                continue
            pos = neuve
            ouvertures.append(T)
            break                                   # relire le compte d'abord
    if pos is not None:
        m = par_sym[pos.sym]
        fermer(m.c[-1], m.t[-1] + MIN_MS, "fin")
    return trades, refus, tentatives
