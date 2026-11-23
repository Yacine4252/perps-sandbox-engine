"""
sandbox_run.py - le moteur passe a l'acte, en SANDBOX seulement (v2.0).

Ce qui change par rapport au moteur d'observation
-------------------------------------------------
Rien dans la strategie, rien dans le garde-fou : memes signaux (signals.py),
meme dimensionnement (risk.py). La seule difference : quand un signal neuf
s'ouvre et que le garde-fou approuve, un ordre part vraiment, sur un compte
sandbox a solde simule. Le moteur d'observation (engine.py) continue a
tourner a cote, intact, avec sa cle live en lecture seule.

Regles d'execution
------------------
  - une position a la fois sur tout le compte (RiskPolicy, max 1)
  - entree seulement sur un signal NEUF, vu naitre par ce processus : pas
    d'entree en plein milieu d'un signal apres un redemarrage
  - une seule tentative par signal : refuse = on attend le suivant
  - stop et objectif poses DANS l'ordre d'entree : aucune seconde sans stop
  - exposition plafonnee a 3x l'equity (regle de Yc, 23 oct)
  - sortie quand le signal retombe sous exit_z ou s'inverse ; sinon le stop
    ou l'objectif ferment la position cote plateforme
  - le moteur ne touche JAMAIS une position qu'il n'a pas ouverte lui-meme
  - un trou de donnees n'est pas un signal : on ne sort pas sur une panne

Table des associes (v2.5)
-------------------------
Avant chaque entree, les sieges votent (table.py) : le momentum propose,
le calendrier (calendrier.py) dit s'il tombe pres d'une annonce USD forte.
En v2.5 tous les sieges sont en OBSERVATION : leurs votes sont ecrits dans
sandbox-table.log et ne changent aucun trade. Les trades restent ceux de la
v2.4 jusqu'au bilan des 70 trades.

v2.6 (20 nov 2026, decision de Yc au point de mi-parcours, 38 trades v2.4) :
  - le BTC est en pause : surveille (prix, signaux, sortie d'une position
    deja ouverte) mais plus aucune entree ; chaque signal est note comme
    verdict refuse MARCHE_EN_PAUSE ;
  - le calendrier passe en veto : pas d'entree de 30 min avant a 60 min apres
    une annonce USD forte ; le signal bloque est note VETO_CALENDRIER ;
  - frais par marche (FEE_BPS), ceux que la plateforme facture vraiment ;
  - l'or garde sa position jusqu'au stop ou a l'objectif (HOLD_TO_TARGET), sans
    sortie sur signal : la meilleure sortie sur 11 mois d'or rejoues
    (recherche/sieges/sorties_or.py), go de Yc le 20 nov.
Un signal bloque passe quand meme par le garde-fou : son verdict garde
l'entree, le stop et la taille, et le carnet suit ce qu'il aurait donne.

Commandes (dans C:\\dev\\perps-engine)
--------------------------------------
  python -m perps_engine.sandbox_run --check   verification, aucun ordre
  python -m perps_engine.sandbox_run           lancement
  python -m perps_engine.table                 bilan des votes
  creer un fichier HALT-SANDBOX                 arret propre
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from . import persistence
from .alerte import raise_alert
from .basket import DEFAULT_BASKET
from .calendrier import Calendar
from .executor import (ApiError, SANDBOX_BASE, SandboxExecutor,
                       SandboxOnlyError)
from .risk import (AccountRules, AccountState, OrderIntent, PlatformLimits,
                   RiskPolicy, evaluate_order)
from .signals import PriceBuffer, SignalConfig
from .strategy import MomentumStrategy
from .table import MomentumSeat, Table
from .variantes import Atr1h, Profil, TendanceSeat, charger_profil, gerer

VERSION = "2.7-sandbox"
KEY_ENV = "PERPS_TEST_KEY"
ACCOUNT_ENV = "PERPS_SANDBOX_ACCOUNT"

POLL_SECONDS = 60
SAVE_EVERY_SECONDS = 300
SNAPSHOT_EVERY_SECONDS = 900
SUMMARY_EVERY_SECONDS = 3600
POLICY_REFRESH_SECONDS = 3600
ENTRY_WINDOW_SECONDS = 180        # un signal neuf reste "armable" 3 cycles
ADOPT_WINDOW_SECONDS = 600
# Au-dela de ce trou dans les prix (panne, marche ferme le week-end), la
# serie est jetee et rechauffee : un momentum calcule par-dessus un trou de
# deux jours inventerait un mouvement qui n'a jamais eu lieu.
GAP_RESET_SECONDS = 1800
TAKER_FEE_BPS = 3.0               # marche absent de FEE_BPS : prudent
# v2.6 : frais reellement factures par la plateforme, par cote, mesures sur
# les trades sandbox (meme tarif en sortie stop, objectif ou signal). Ils
# entrent dans la taille : le risque de 1 000 $ inclut l'aller-retour.
FEE_BPS = {"BTC": 3.0, "GOLD": 0.5}

# Symbole -> identifiants candidats, par ordre de preference. Le premier
# disponible en sandbox est retenu au demarrage.
MARKETS = {
    "BTC": ["binance|BTCUSDT"],
    "GOLD": ["binance|XAUUSDT", "hyperliquid|xyz:GOLD"],
}

# Meme discipline que le moteur d'observation, plus le plafond 3x.
POLICY = RiskPolicy(max_notional_multiple=3.0)

# Risque par trade de Sandbox 1 (le compte sans fiche). 22 nov 2026, go de Yc :
# 1 % -> 0,3 %. A 1 %, une perte coutait environ 1 100 $ et le compte n'avait
# plus qu'environ 9 000 $ au-dessus de son plancher (8 pertes de suite).
RISQUE_SANDBOX_1 = 0.003

# Raisons de signals.compute() qui traduisent une panne de donnees, pas
# un changement de marche.
DATA_GAP_REASONS = ("aucun prix", "prix perime")

# Mode de chaque siege de la table. v2.5 : observation seulement.
# v2.6 (20 nov, decision de Yc) : le calendrier passe en veto. Sur 38 trades
# v2.4, les 3 signaux tombes pres d'une annonce USD forte ont tous perdu.
TABLE_MODES = {"calendrier": "veto"}

# v2.6 : marches surveilles mais plus trades. Le BTC a perdu 1 682 $ sur 17
# trades v2.4 (-521 $ meme avant frais) ; l'or a gagne 3 475 $ sur 21.
PAUSED_MARKETS = ("BTC",)

# Marches dont la position reste ouverte jusqu'au stop ou a l'objectif poses
# sur la plateforme, sans sortie sur signal (ni apres un trou de prix : le
# stop et l'objectif la protegent). Vide : sortie sur signal partout.
# v2.6 (go de Yc, 20 nov) : l'or. Rejeu de 11 mois, net de frais et de portage :
# +16,0 R / +1,3 R / +4,3 R (8 mois / 3 mois / reel) contre -7,8 / -6,9 / +0,7
# pour la sortie sur signal.
HOLD_TO_TARGET = ("GOLD",)


@dataclass
class Paths:
    halt: str = "HALT-SANDBOX"
    lock: str = "sandbox.lock"
    trades: str = "sandbox-trades.log"
    signals: str = "sandbox-signals.log"
    orders: str = "sandbox-orders.log"
    state: str = "state-sandbox.json"
    observation_state: str = "state.json"
    book: str = "sandbox-positions.json"
    summary: str = "resume-sandbox.txt"
    alert: str = "ALERTE.txt"
    prices: str = "prix-sandbox.csv"      # archive lue par graphique.py
    table: str = "sandbox-table.log"      # votes des sieges (v2.5)
    calendar: str = "calendrier-ff.json"  # copie du calendrier economique


# --------------------------------------------------------------------------
# Petits outils purs (testes un par un)
# --------------------------------------------------------------------------

def load_dotenv(path: str = ".env") -> list:
    """Charge CLE=VALEUR sans ecraser l'environnement. Rend les cles lues."""
    loaded: list = []
    if not os.path.exists(path):
        return loaded
    with open(path, encoding="utf-8-sig") as f:     # -sig : BOM du Bloc-notes
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.lower().startswith("export "):
                k = k[7:].strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
                loaded.append(k)
    return loaded


def pick_account(accounts: list, wanted: str = "") -> dict:
    wanted = (wanted or "").strip()
    seen = ", ".join(f"{a.get('account_number')} ({a.get('status')})"
                     for a in accounts) or "aucun"
    if wanted:
        for a in accounts:
            if wanted in (a.get("account_number"), a.get("id")):
                if a.get("status") != "active":
                    raise RuntimeError(
                        f"Le compte {wanted} n'est pas actif ({a.get('status')}).")
                return a
        raise RuntimeError(f"Compte {wanted} introuvable avec cette cle. "
                           f"Comptes vus : {seen}")
    active = [a for a in accounts if a.get("status") == "active"]
    if len(active) == 1:
        return active[0]
    if not active:
        raise RuntimeError(f"Aucun compte sandbox actif. Comptes vus : {seen}")
    raise RuntimeError("Plusieurs comptes sandbox actifs : ajoute dans .env "
                       f"la ligne {ACCOUNT_ENV}=FP-...  Comptes vus : {seen}")


def pick_markets(listed: list, wanted: Optional[dict] = None) -> dict:
    wanted = MARKETS if wanted is None else wanted
    by_id = {m.get("market_id"): m for m in listed if isinstance(m, dict)}
    out = {}
    for sym, candidates in wanted.items():
        for mid in candidates:
            if mid in by_id:
                out[sym] = by_id[mid]
                break
    return out


def round_price(p: float) -> float:
    return round(p, 2) if abs(p) >= 100 else round(p, 6)


def mid_of(q: Optional[dict]) -> Optional[float]:
    if not q:
        return None
    mid = q.get("mid")
    if mid is None and q.get("bid") and q.get("ask"):
        mid = (float(q["bid"]) + float(q["ask"])) / 2
    try:
        mid = float(mid) if mid is not None else None
    except (TypeError, ValueError):
        return None
    return mid if mid and mid > 0 else None


def net_pnl(p: dict) -> float:
    """Gain net d'une position fermee : prix + portage - frais."""
    return (float(p.get("realized_pnl") or 0) - float(p.get("fees") or 0)
            + float(p.get("funding") or 0))


def fit_to_market(size: float, leverage: float, lev_hi: float,
                  market: dict, price: float) -> tuple:
    """
    Adapte la taille au pas du marche (arrondi VERS LE BAS : moins de
    risque, jamais plus) et le levier a un entier (vers le HAUT : moins de
    marge immobilisee, en restant sous le plafond de liquidation).
    """
    decimals = int(market.get("size_decimals", 3) or 0)
    step = float(market.get("size_step") or 10 ** -decimals)
    units = math.floor(size / step + 1e-9)
    sized = round(units * step, decimals)
    if sized <= 0 or sized < float(market.get("min_size") or 0):
        return None, None, "TAILLE_SOUS_MINIMUM"
    if sized * price < float(market.get("min_notional") or 0):
        return None, None, "NOTIONNEL_SOUS_MINIMUM"
    lev = max(1, math.ceil(leverage - 1e-9))
    cap = min(float(market.get("max_leverage") or lev), float(lev_hi))
    if lev > cap:
        return None, None, "LEVIER_ENTIER_HORS_FENETRE"
    return sized, lev, None


def account_state(acct: dict, rules: AccountRules, open_positions: list,
                  closed_positions: list, now: float,
                  quote_age_ms: int = 0) -> AccountState:
    """
    Etat du compte pour le garde-fou, a partir des chiffres de la
    plateforme elle-meme.

    Perte du jour : la plateforme publie daily_loss_floor = equite du debut
    de journee - allocation. On en deduit l'equite de minuit (heure de New
    York) sans calcul de fuseau, donc sans erreur de fuseau.
    """
    risk = acct.get("risk") or {}
    balance = float(acct.get("balance") or 0)
    equity = float(risk.get("equity", acct.get("equity", balance)) or balance)

    pnl_today = 0.0
    floor = risk.get("daily_loss_floor")
    if floor is not None and rules.daily_loss_pct > 0:
        start_of_day = float(floor) + rules.starting_balance * rules.daily_loss_pct
        pnl_today = equity - start_of_day

    now_ms = now * 1000
    since_ms = now_ms - 24 * 3600 * 1000
    trades_24h = sum(1 for p in list(open_positions) + list(closed_positions)
                     if float(p.get("opened_at") or 0) >= since_ms)

    last_loss = None
    for p in closed_positions:
        if net_pnl(p) < 0:
            t = float(p.get("closed_at") or 0)
            if t and (last_loss is None or t > last_loss):
                last_loss = t
    minutes = (now_ms - last_loss) / 60000 if last_loss else None

    return AccountState(equity=equity, balance=balance,
                        realized_pnl_today=pnl_today,
                        open_positions=len(open_positions),
                        trades_last_24h=trades_24h,
                        minutes_since_last_loss=minutes,
                        quote_age_ms=int(quote_age_ms))


def update_arming(armed: dict, prev_dirs: dict, dirs: dict,
                  now: float) -> list:
    """
    Arme une entree a chaque signal NEUF (passage a buy ou sell).
    Desarme si le signal change ou si la fenetre d'entree est passee.
    Rend les transitions observees : [(sym, avant, apres), ...].
    """
    changes = []
    for sym, d in dirs.items():
        p = prev_dirs.get(sym)
        if d != p:
            changes.append((sym, p, d))
            if d in ("buy", "sell"):
                armed[sym] = {"dir": d, "since": now}
            else:
                armed.pop(sym, None)
        elif sym in armed and armed[sym]["dir"] != d:
            armed.pop(sym, None)
        prev_dirs[sym] = d
    for sym in list(armed):
        if now - armed[sym]["since"] > ENTRY_WINDOW_SECONDS:
            armed.pop(sym, None)
    return changes


def exits_to_make(managed: dict, open_positions: list, dirs: dict) -> list:
    """Positions DU MOTEUR a fermer : signal retombe ou inverse."""
    open_by_id = {p.get("id"): p for p in open_positions}
    out = []
    for pid, info in managed.items():
        pos = open_by_id.get(pid)
        if pos is None:
            continue
        held = "buy" if pos.get("side") == "long" else "sell"
        if dirs.get(info["sym"]) != held:
            out.append(pid)
    return out


def _market_ids_in(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("market_ids", {}) or {}
    except (OSError, ValueError, AttributeError):
        return {}


def restore_buffers(strategies: dict, market_ids: dict, own_path: str,
                    observation_path: str) -> dict:
    """
    Reprend les series de prix pour eviter 3 h de chauffe.

    Regle : on ne recolle JAMAIS une serie prise sur un autre marche. L'or de
    Hyperliquid et l'or de Binance different d'environ 0.2 % : les raccorder
    creerait un faux mouvement de prix, donc un faux signal.
    """
    done: set = set()
    report = {"own": [], "observation": [], "warmup": []}

    own = persistence.load(own_path)
    if own.get("ok"):
        saved_ids = _market_ids_in(own_path)
        ok = {s: strategies[s] for s in strategies
              if saved_ids.get(s) == market_ids.get(s) and s in own["markets"]}
        rec = persistence.restore(ok, own)
        report["own"] = rec["restored"]
        done |= {r["sym"] for r in rec["restored"]}

    obs = persistence.load(observation_path)
    if obs.get("ok"):
        obs_ids = {m.symbol: m.market_id for m in DEFAULT_BASKET}
        ok = {s: strategies[s] for s in strategies
              if s not in done and obs_ids.get(s) == market_ids.get(s)
              and s in obs["markets"]}
        rec = persistence.restore(ok, obs)
        report["observation"] = rec["restored"]
        done |= {r["sym"] for r in rec["restored"]}

    report["warmup"] = [s for s in strategies if s not in done]
    return report


def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _save_json_atomic(path: str, payload: dict) -> bool:
    try:
        d = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".sbx-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return True
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except Exception:
        return False


def acquire_single_instance(path: str):
    """Un seul moteur sandbox a la fois (tache planifiee + lancement a la main)."""
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


# --------------------------------------------------------------------------
# La boucle
# --------------------------------------------------------------------------

class Runner:
    def __init__(self, api, account: dict, markets: dict, strategies: dict,
                 policy_raw: dict, *, policy: RiskPolicy = POLICY,
                 cfg: Optional[SignalConfig] = None,
                 paths: Optional[Paths] = None,
                 fee_bps: float = TAKER_FEE_BPS, echo: bool = True,
                 clock: Callable[[], float] = time.time,
                 table: Optional[Table] = None,
                 paused: tuple = (),
                 hold: tuple = (),
                 fees_by_sym: Optional[dict] = None,
                 profil: Optional[Profil] = None) -> None:
        self.api = api
        self.table = table              # None : pas de vote (comme la v2.4)
        self.paused = tuple(paused)     # surveilles, jamais trades (v2.6)
        self.hold = tuple(hold)         # jusqu'au stop ou a l'objectif
        self.fees_by_sym = dict(fees_by_sym or {})
        # v2.7 : fiche du compte (None = Sandbox 1, v2.6 a l'identique)
        self.profil = profil
        self.atr = Atr1h() if profil is not None and profil.suiveur_atr else None
        self.attente: dict = {}         # signaux qui attendent leur confirmation
        self.account = account
        self.account_id = account["id"]
        self.account_number = account.get("account_number") or self.account_id
        self.markets = markets
        self.strategies = strategies
        self.policy = policy
        self.cfg = cfg or SignalConfig()
        self.paths = paths or Paths()
        self.fee_bps = fee_bps
        self.echo = echo
        self.clock = clock
        self._set_policy(policy_raw)

        self.prev_dirs = {s: st.direction for s, st in strategies.items()}
        self.armed: dict = {}
        self.exit_failures: dict = {}
        self.pending_adopt: Optional[dict] = None
        self.inactive_logged = False
        self.auth_alerted = False
        self.measures: dict = {}
        self.started_at = clock()
        self.last = {"save": self.started_at, "snapshot": 0.0,
                     "summary": 0.0, "policy": self.started_at}

        base, ext = os.path.splitext(self.paths.book)
        self.book_path = f"{base}-{self.account_number}{ext}"
        book = _load_json(self.book_path) or {}
        self.managed: dict = book.get("managed", {}) or {}
        self.history: list = book.get("history", []) or []
        self.start_equity: Optional[float] = book.get("start_equity")

    # -- outils ------------------------------------------------------------
    def _set_policy(self, raw: dict) -> None:
        self.policy_raw = raw or {}
        self.rules = AccountRules.from_policy(
            self.policy_raw, float(self.account.get("starting_balance") or 0))

    def _log(self, path: str, entry: dict) -> None:
        entry = dict(entry)
        entry["ts"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        if self.echo:
            print(json.dumps(entry, ensure_ascii=True, default=str)[:300])

    def _mid(self, market_id: str) -> Optional[float]:
        try:
            return mid_of(self.api.quote(market_id))
        except Exception:
            return None

    def _archive_prices(self, now: float, prices: dict) -> None:
        """
        Chaque prix vu est ajoute a prix-sandbox.csv. La memoire du moteur ne
        garde que 24 h ; l'archive garde tout, pour revoir n'importe quel
        trade sur le graphique. Meme horodatage que la serie en memoire :
        graphique.py n'en fait jamais de doublon.
        """
        if not prices:
            return
        try:
            path = self.paths.prices
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            with open(path, "a", encoding="utf-8", newline="") as f:
                if new:
                    f.write("ts,sym,price\n")
                for sym, p in sorted(prices.items()):
                    f.write(f"{round(now, 3)},{sym},{p}\n")
        except OSError:
            pass

    def save_book(self) -> None:
        _save_json_atomic(self.book_path, {
            "account_id": self.account_id,
            "account_number": self.account_number,
            "start_equity": self.start_equity,
            "managed": self.managed,
            "history": self.history[-500:]})

    def save_state(self) -> None:
        persistence.save(self.paths.state, self.strategies, extra={
            "market_ids": {s: m["market_id"] for s, m in self.markets.items()}})
        self.save_book()

    def log_start(self, restore_report: dict) -> None:
        self._log(self.paths.trades, {
            "event": "start", "version": VERSION,
            "account": self.account_number, "server": SANDBOX_BASE,
            "markets": {s: m["market_id"] for s, m in self.markets.items()},
            "risk_pct_of_balance": self.policy.risk_pct_of_balance,
            "max_notional_multiple": self.policy.max_notional_multiple,
            "max_trades_24h": self.policy.max_trades_24h,
            "managed_positions": list(self.managed),
            "table": self.table.modes if self.table else None,
            "paused_markets": list(self.paused),
            "hold_to_target": list(self.hold),
            "profil": self.profil.resume() if self.profil else None,
            "fee_bps": self.fees_by_sym or self.fee_bps,
            "restored": restore_report})

    # -- un cycle ----------------------------------------------------------
    def cycle(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        report = {"transitions": [], "exits": [], "entries": []}

        # 1. prix et signaux ------------------------------------------------
        dirs, gaps, seen_before, seen_prices = {}, [], {}, {}
        for sym, strat in self.strategies.items():
            market_id = self.markets[sym]["market_id"]
            price = self._mid(market_id)
            if price is not None:
                seen_prices[sym] = price
            last = strat.buffer.last
            if (price is not None and last is not None
                    and now - last[0] > GAP_RESET_SECONDS):
                # Trou trop long : on ne sait plus evaluer ce marche. Serie
                # neuve, signal neutre ; une position du moteur sur ce marche
                # sera fermee plus bas (on ne garde pas ce qu'on ne voit plus).
                strat.buffer = PriceBuffer(max_minutes=1440)
                strat.direction = None
                self.measures.pop(sym, None)
                self._log(self.paths.signals, {
                    "event": "rewarm", "sym": sym,
                    "gap_minutes": round((now - last[0]) / 60)})
            strat.observe(now, price)
            before = strat.direction
            seen_before[sym] = (self.measures.get(sym) or {}).get("z") is not None
            strat.decide(market_id, now)
            m = dict(strat.last_measure)
            if m.get("reason") in DATA_GAP_REASONS:
                strat.direction = before        # une panne n'est pas un signal
                dirs[sym] = before
                gaps.append(sym)
            else:
                dirs[sym] = m.get("direction")
            self.measures[sym] = m
        self._archive_prices(now, seen_prices)

        for sym, frm, to in update_arming(self.armed, self.prev_dirs, dirs, now):
            m = self.measures.get(sym, {})
            born_unseen = to in ("buy", "sell") and not seen_before.get(sym)
            if born_unseen:
                # Signal deja en cours a la sortie de chauffe : on ne l'a pas
                # vu naitre, son prix d'entree serait faux. On attend le suivant.
                self.armed.pop(sym, None)
            self._log(self.paths.signals, {
                "event": "signal_change", "sym": sym, "from": frm, "to": to,
                "z": m.get("z"), "price": m.get("price"),
                "stop_distance_pct": m.get("stop_distance_pct"),
                "armed": sym in self.armed,
                "note": "deja en cours a la sortie de chauffe" if born_unseen else None})
            report["transitions"].append((sym, frm, to))

        # 2. compte ---------------------------------------------------------
        if now - self.last["policy"] >= POLICY_REFRESH_SECONDS:
            self._set_policy(self.api.trading_policy(self.account_id))
            self.last["policy"] = now
        acct = self.api.get_account(self.account_id)
        positions = self.api.positions(self.account_id, "open") or []
        equity = float((acct.get("risk") or {}).get("equity",
                                                    acct.get("balance", 0)) or 0)
        if self.start_equity is None and equity > 0:
            self.start_equity = equity
            self.save_book()
        self._reconcile(positions, now)

        if acct.get("status") != "active":
            if not self.inactive_logged:
                self.inactive_logged = True
                msg = (f"[SANDBOX] compte {self.account_number} "
                       f"{acct.get('status')} - plus aucun ordre")
                raise_alert(msg, self.paths.alert)
                self._log(self.paths.trades, {"event": "account_inactive",
                                              "status": acct.get("status")})
            self._periodic(now, acct)
            return report

        # 3. sorties --------------------------------------------------------
        for pid in exits_to_make(self.managed, positions, dirs):
            info = self.managed[pid]
            if info.get("sym") in self.hold:
                continue                 # le stop et l'objectif decident
            try:
                order = self._close(pid, info.get("market_id")
                                    or self.markets[info["sym"]]["market_id"])
                self._log(self.paths.trades, {
                    "event": "exit_signal", "position_id": pid,
                    "sym": info["sym"], "reason": self.measures.get(
                        info["sym"], {}).get("reason"),
                    "status": order.get("status"),
                    "fill_price": order.get("fill_price"),
                    "realized_pnl": order.get("realized_pnl"),
                    "attempts_before": self.exit_failures.pop(pid, {}).get("n", 0)})
                report["exits"].append(pid)
            except Exception as e:
                self._exit_failed(pid, info, e, now)
        if report["exits"]:
            acct = self.api.get_account(self.account_id)
            positions = self.api.positions(self.account_id, "open") or []

        # 3 bis. gestion des positions gardees (v2.7, fiche du compte) ------
        if self.profil is not None and self._manage(positions, seen_prices, now):
            acct = self.api.get_account(self.account_id)
            positions = self.api.positions(self.account_id, "open") or []

        # 4. entrees --------------------------------------------------------
        for sym, side in self._candidates(dirs, gaps, now):
            arm = {"dir": side}
            delib = self._deliberate(sym, arm["dir"], now)
            vetoes = list(delib["bloque_par"]) if delib else []
            if vetoes:
                self._log(self.paths.trades, {
                    "event": "entry_vetoed", "sym": sym, "side": arm["dir"],
                    "by": vetoes,
                    "reasons": {n: delib["votes"][n].get("raison")
                                for n in vetoes}})
            # v2.6 : un signal bloque (veto ou marche en pause) passe quand
            # meme par le garde-fou. Son verdict, refuse, garde l'entree, le
            # stop et la taille qu'il aurait eus. Aucun ordre ne part.
            blocks = ["VETO_" + n.upper() for n in vetoes]
            if sym in self.paused:
                blocks.append("MARCHE_EN_PAUSE")
            result = self._try_entry(sym, arm["dir"], acct, positions, now,
                                     blocks=tuple(blocks))
            if vetoes:
                result = "veto:" + ",".join(vetoes)
            if delib is not None:
                self.table.record(delib, result)
            self.armed.pop(sym, None)            # une tentative par signal
            report["entries"].append((sym, result))
            if result in ("filled", "pending", "uncertain"):
                break                             # relire le compte d'abord

        self._periodic(now, acct)
        return report

    # -- v2.7 : signaux a tenter et gestion apres l'entree ---------------------
    def _candidates(self, dirs: dict, gaps: list, now: float) -> list:
        """Signaux a tenter ce cycle : [(marche, sens)]. Avec une confirmation
        (fiche du compte), un signal neuf attend N minutes ; il n'est tente que si
        son z est encore au-dela du seuil, dans le meme sens."""
        attente = self.profil.confirmation_min if self.profil else 0
        if not attente:
            return [(s, a["dir"]) for s, a in list(self.armed.items())
                    if a and dirs.get(s) == a["dir"] and s not in gaps]
        for s in list(self.armed):
            arm = self.armed.pop(s)
            if arm and dirs.get(s) == arm["dir"] and s not in gaps:
                self.attente[s] = {"dir": arm["dir"], "due": now + attente * 60}
                self._log(self.paths.signals, {"event": "entry_waiting", "sym": s,
                                               "side": arm["dir"], "minutes": attente})
        out = []
        for s, w in list(self.attente.items()):
            if now < w["due"]:
                continue
            del self.attente[s]
            z = (self.measures.get(s) or {}).get("z")
            sign = 1 if w["dir"] == "buy" else -1
            if z is not None and z * sign >= self.profil.confirmation_z and s not in gaps:
                out.append((s, w["dir"]))
            else:
                self._log(self.paths.trades, {"event": "entry_unconfirmed", "sym": s,
                                              "side": w["dir"], "z": z,
                                              "seuil": self.profil.confirmation_z})
        return out

    def _half_size(self, sym: str, size) -> Optional[float]:
        m = self.markets.get(sym) or {}
        decimals = int(m.get("size_decimals", 3) or 0)
        step = float(m.get("size_step") or 10 ** -decimals)
        half = round(math.floor(float(size or 0) / 2 / step + 1e-9) * step, decimals)
        return half if half > 0 and half >= float(m.get("min_size") or 0) else None

    def _manage(self, positions: list, prices: dict, now: float) -> bool:
        """Sorties decidees par la fiche du compte (temps, retour a +x R, stop
        suiveur, moitie a +1 R), au marche, au prix du cycle. Le stop et
        l'objectif poses a l'entree restent sur la plateforme. Rend True si un
        ordre est parti."""
        open_ids = {p.get("id") for p in positions}
        acted = False
        for pid, info in list(self.managed.items()):
            sym = info.get("sym")
            if pid not in open_ids or sym not in self.hold:
                continue
            atr = self.atr.valeur(sym, now) if self.atr else None
            action = gerer(self.profil, info, prices.get(sym), now, atr)
            if action is None:
                continue
            kind, reason = action
            market_id = info.get("market_id") or self.markets[sym]["market_id"]
            size = None
            try:
                if kind == "partiel":
                    size = self._half_size(sym, info.get("size"))
                    if not size:
                        info["partiel_fait"] = True     # trop petit pour couper
                        continue
                    order = self._close(pid, market_id, size=size)
                    info["partiel_fait"] = True
                    info["size"] = round(float(info["size"]) - size, 8)
                else:
                    order = self._close(pid, market_id)
                self._log(self.paths.trades, {
                    "event": "exit_managed", "position_id": pid, "sym": sym,
                    "kind": kind, "reason": reason, "size": size,
                    "status": order.get("status"),
                    "fill_price": order.get("fill_price"),
                    "realized_pnl": order.get("realized_pnl")})
                acted = True
            except Exception as e:
                self._exit_failed(pid, info, e, now)
        if acted:
            self.save_book()
        return acted

    # -- table des associes ----------------------------------------------------
    def _deliberate(self, sym: str, direction: str,
                    now: float) -> Optional[dict]:
        if self.table is None:
            return None
        m = self.measures.get(sym, {})
        try:
            return self.table.deliberate({"sym": sym, "side": direction,
                                          "now": now, "z": m.get("z"),
                                          "price": m.get("price")})
        except Exception as e:
            # La table ne doit jamais empecher le moteur de faire son metier.
            self._log(self.paths.signals, {"event": "table_error",
                                           "detail": f"{type(e).__name__}: {e}"})
            return None

    # -- sortie --------------------------------------------------------------
    def _close(self, pid: str, market_id: str,
               size: Optional[float] = None) -> dict:
        # La plateforme exige le prix observe pour fermer au marche : on le
        # lit juste avant d'envoyer la sortie.
        price = mid_of(self.api.quote(market_id))
        if price is None:
            raise RuntimeError(f"pas de prix pour {market_id} : sortie remise")
        if size:
            # Fermeture partielle (v2.7) : jamais de repli "minimal", qui
            # fermerait toute la position.
            return self.api.close_position(pid, expected_price=price, size=size)
        try:
            return self.api.close_position(pid, expected_price=price)
        except ApiError as e:
            if e.status in (400, 422):
                # Requete refusee sur sa forme : une seule nouvelle tentative,
                # avec le strict minimum (type d'ordre et prix observe).
                return self.api.close_position(pid, expected_price=price,
                                               minimal=True)
            raise

    def _exit_failed(self, pid: str, info: dict, e: Exception,
                     now: float) -> None:
        """
        Une sortie refusee se retente a chaque cycle, mais le journal ne la
        note qu'une fois par demi-heure (v2.2 en avait ecrit 4 998 lignes),
        et l'alerte part une seule fois, a la 3e tentative.
        """
        f = self.exit_failures.setdefault(pid, {"n": 0, "last_log": None})
        f["n"] += 1
        detail = (e.as_dict() if isinstance(e, ApiError)
                  else {"detail": f"{type(e).__name__}: {e}"})
        if f["last_log"] is None or now - f["last_log"] >= 1800:
            f["last_log"] = now
            self._log(self.paths.trades, {"event": "exit_error",
                                          "position_id": pid,
                                          "sym": info.get("sym"),
                                          "attempts": f["n"], **detail})
        if f["n"] == 3:
            raise_alert(f"[SANDBOX] sortie refusee pour {info.get('sym')} "
                        f"({detail.get('code') or detail.get('detail')}) - "
                        "le stop et l'objectif restent poses", self.paths.alert)

    # -- entree --------------------------------------------------------------
    def _try_entry(self, sym: str, direction: str, acct: dict,
                   positions: list, now: float, blocks: tuple = ()) -> str:
        market = self.markets[sym]
        m = self.measures.get(sym, {})
        dist = m.get("stop_distance")
        if not dist or dist <= 0:
            return "no_stop_distance"

        t0 = time.time()
        try:
            mid = mid_of(self.api.quote(market["market_id"]))
        except Exception:
            mid = None
        if mid is None:
            self._log(self.paths.trades, {"event": "entry_skipped",
                                          "sym": sym, "reason": "pas de prix"})
            return "no_quote"
        age_ms = int((time.time() - t0) * 1000)

        sign = 1 if direction == "buy" else -1
        stop = round_price(mid - sign * dist)
        tp = round_price(mid + sign * dist * self.cfg.tp_r_multiple)
        intent = OrderIntent(market_id=market["market_id"], side=direction,
                             entry_price=mid, stop_price=stop,
                             take_profit_price=tp)

        closed = self.api.positions(self.account_id, "closed") or []
        state = account_state(acct, self.rules, positions, closed, now,
                              quote_age_ms=age_ms)
        fee_bps = self.fees_by_sym.get(sym, self.fee_bps)
        limits = PlatformLimits.from_policy(
            self.policy_raw, taker_fee_bps=fee_bps,
            max_platform_leverage=float(market.get("max_leverage") or 50))
        v = evaluate_order(intent, state, self.rules, limits, self.policy)
        rejects = list(v.reject_codes) + [b for b in blocks
                                          if b not in v.reject_codes]
        approved = bool(v.approved) and not blocks
        self._log(self.paths.trades, {
            "event": "verdict", "sym": sym, "side": direction, "z": m.get("z"),
            "entry": mid, "stop": stop, "tp": tp, "approved": approved,
            "rejects": rejects, "size": v.computed_size, "fee_bps": fee_bps,
            "leverage": v.computed_leverage,
            "risk_budget": v.computed.get("risk_budget"),
            "notional": v.computed.get("notional"),
            "capped_by_exposure": v.computed.get("capped_by_exposure", False),
            "equity": state.equity, "pnl_today": round(state.realized_pnl_today, 2),
            "trades_24h": state.trades_last_24h,
            "open_positions": state.open_positions})
        if not approved:
            return "rejected:" + ",".join(rejects)

        lev_hi = (v.computed.get("lev_window") or [0, 0])[1]
        size, lev, why = fit_to_market(v.computed_size, v.computed_leverage,
                                       lev_hi, market, mid)
        if why:
            self._log(self.paths.trades, {"event": "entry_skipped",
                                          "sym": sym, "reason": why})
            return "unfit:" + why

        try:
            order = self.api.open_market(
                account_id=self.account_id, market_id=market["market_id"],
                side=direction, size=size, leverage=lev,
                stop_loss_price=stop, take_profit_price=tp,
                expected_price=mid)
        except ApiError as e:
            self._log(self.paths.trades, {"event": "entry_error", "sym": sym,
                                          **e.as_dict()})
            if e.status in (401, 403) and not self.auth_alerted:
                self.auth_alerted = True
                raise_alert("[SANDBOX] ordre refuse par l'API "
                            f"({e.code}) - la cle a-t-elle le droit de trader ?",
                            self.paths.alert)
            return f"api_error:{e.status}"
        except Exception as e:
            # Resultat incertain : la position sera adoptee si elle apparait.
            self.pending_adopt = {"sym": sym, "market_id": market["market_id"],
                                  "since": now, "side": direction,
                                  "stop": stop, "tp": tp, "size": size}
            self._log(self.paths.trades, {"event": "entry_uncertain",
                                          "sym": sym,
                                          "detail": f"{type(e).__name__}: {e}"})
            return "uncertain"

        status = order.get("status")
        self._log(self.paths.trades, {
            "event": "entry_order", "sym": sym, "side": direction,
            "status": status, "order_id": order.get("id"),
            "size": size, "leverage": lev, "fill_price": order.get("fill_price"),
            "stop": stop, "tp": tp,
            "stop_order": order.get("stop_loss_order_id"),
            "tp_order": order.get("take_profit_order_id")})
        if status in ("rejected", "canceled", "expired"):
            return status

        info = {"sym": sym, "market_id": market["market_id"], "side": direction,
                "size": size, "leverage": lev,
                "entry": order.get("fill_price") or mid, "stop": stop, "tp": tp,
                "order_id": order.get("id"), "opened_at": now}
        try:
            fresh = self.api.positions(self.account_id, "open") or []
        except Exception:
            fresh = []
        pos = self._unmanaged_on(fresh, market["market_id"], now)
        if pos is not None:
            self.managed[pos["id"]] = info
            self.save_book()
        else:
            self.pending_adopt = dict(info, since=now)
        return "filled" if status == "filled" else "pending"

    def _unmanaged_on(self, positions: list, market_id: str,
                      since: float) -> Optional[dict]:
        # Le compte etait vide a l'evaluation (une position max) : une
        # position sur ce marche, apparue depuis, ne peut etre que la notre.
        # Marge de 120 s pour tolerer une horloge de PC un peu decalee.
        for p in positions:
            if p.get("id") in self.managed:
                continue
            if (p.get("market_id") == market_id and
                    float(p.get("opened_at") or 0) >= (since - 120) * 1000):
                return p
        return None

    # -- rapprochement -------------------------------------------------------
    def _reconcile(self, positions: list, now: float) -> None:
        open_ids = {p.get("id") for p in positions}
        gone = [pid for pid in self.managed if pid not in open_ids]
        if gone:
            try:
                closed = self.api.positions(self.account_id, "closed") or []
            except Exception:
                closed = []
            by_id = {p.get("id"): p for p in closed}
            for pid in gone:
                info = self.managed.pop(pid)
                rec = by_id.get(pid) or {}
                opened, closed_at = rec.get("opened_at"), rec.get("closed_at")
                entry = {
                    "position_id": pid, "sym": info.get("sym"),
                    "side": info.get("side"),
                    "entry": rec.get("entry_price", info.get("entry")),
                    "exit": rec.get("exit_price"),
                    "close_reason": rec.get("close_reason"),
                    "gross": rec.get("realized_pnl"), "fees": rec.get("fees"),
                    "funding": rec.get("funding"),
                    "net": round(net_pnl(rec), 2) if rec else None,
                    "hold_min": (round((closed_at - opened) / 60000, 1)
                                 if opened and closed_at else None)}
                self.history.append(entry)
                self._log(self.paths.trades, {"event": "position_closed", **entry})
            self.save_book()

        if self.pending_adopt:
            pa = self.pending_adopt
            pos = self._unmanaged_on(positions, pa["market_id"], pa["since"])
            if pos is not None:
                info = {k: v for k, v in pa.items() if k != "since"}
                info.setdefault("opened_at", pa["since"])
                self.managed[pos["id"]] = info
                self.pending_adopt = None
                self._log(self.paths.trades, {"event": "position_adopted",
                                              "position_id": pos["id"],
                                              "sym": pa["sym"]})
                self.save_book()
            elif now - pa["since"] > ADOPT_WINDOW_SECONDS:
                self.pending_adopt = None

    # -- taches periodiques --------------------------------------------------
    def _periodic(self, now: float, acct: dict) -> None:
        if self.table is not None:
            try:
                for note in self.table.refresh(now):   # calendrier : toutes les 3 h
                    self._log(self.paths.signals, note)
            except Exception as e:
                self._log(self.paths.signals, {"event": "table_error",
                                               "detail": f"{type(e).__name__}: {e}"})
        if now - self.last["save"] >= SAVE_EVERY_SECONDS:
            self.save_state()
            self.last["save"] = now
        if now - self.last["snapshot"] >= SNAPSHOT_EVERY_SECONDS:
            self.last["snapshot"] = now
            risk = acct.get("risk") or {}
            self._log(self.paths.signals, {
                "event": "snapshot",
                "equity": risk.get("equity", acct.get("balance")),
                "daily_loss_room": risk.get("daily_loss_room"),
                "managed": list(self.managed),
                "markets": {s: {"dir": self.prev_dirs.get(s), "z": m.get("z"),
                                "reason": m.get("reason")}
                            for s, m in self.measures.items()}})
        if now - self.last["summary"] >= SUMMARY_EVERY_SECONDS:
            self.last["summary"] = now
            self.write_summary(acct, now)

    def write_summary(self, acct: dict, now: float) -> None:
        risk = acct.get("risk") or {}
        eq = float(risk.get("equity", acct.get("balance", 0)) or 0)
        start = self.start_equity if self.start_equity is not None else eq
        nets = [h.get("net") for h in self.history if h.get("net") is not None]
        wins = sum(1 for n in nets if n > 0)
        losses = sum(1 for n in nets if n < 0)
        L = [f"SANDBOX  {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
             f"compte {self.account_number} ({acct.get('status')})",
             f"  equity {eq:,.2f} $   au depart du moteur {start:,.2f} $   "
             f"ecart {eq - start:+,.2f} $",
             f"  trades du moteur fermes : {len(self.history)}  "
             f"(gagnants {wins}, perdants {losses})  net {sum(nets):+,.2f} $",
             f"  tourne depuis {(now - self.started_at) / 3600:.1f} h"]
        if self.managed:
            for pid, i in self.managed.items():
                L.append(f"  position ouverte : {i.get('sym')} {i.get('side')} "
                         f"{i.get('size')} @ {i.get('entry')}  stop {i.get('stop')}"
                         f"  objectif {i.get('tp')}")
        else:
            L.append("  position ouverte : aucune")
        for sym, m in sorted(self.measures.items()):
            L.append(f"  {sym:5} {self.prev_dirs.get(sym) or 'neutre':6} "
                     f"z={m.get('z')}  {m.get('reason') or ''}")
        if self.table is not None:
            try:
                L.extend(f"  {line}" for line in self.table.summary_lines(now))
            except Exception:
                pass
        for h in self.history[-5:]:
            net = h.get("net")
            L.append(f"  recent : {h.get('sym')} {h.get('side')}  net "
                     f"{'?' if net is None else format(net, '+,.2f')} $  "
                     f"({h.get('close_reason')}, {h.get('hold_min')} min)")
        try:
            with open(self.paths.summary, "w", encoding="utf-8") as f:
                f.write("\n".join(L) + "\n")
        except OSError:
            pass

    # -- boucle infinie ------------------------------------------------------
    def run_forever(self) -> int:
        while True:
            if os.path.exists(self.paths.halt):
                self.save_state()
                self._log(self.paths.trades, {"event": "halt", "state_saved": True})
                print(f"Arrete par {self.paths.halt}. Supprime ce fichier pour relancer.")
                return 0
            try:
                self.cycle()
            except ApiError as e:
                self._log(self.paths.trades, {"event": "error", **e.as_dict()})
                if e.status in (401, 403):
                    raise_alert(f"[SANDBOX] acces refuse par l'API ({e.code}) "
                                "- moteur sandbox arrete", self.paths.alert)
                    self.save_state()
                    return 3
            except Exception as e:
                self._log(self.paths.trades, {"event": "error",
                                              "detail": f"{type(e).__name__}: {e}"})
            time.sleep(POLL_SECONDS)


# --------------------------------------------------------------------------
# Verification sans ordre
# --------------------------------------------------------------------------

def check(api, account: dict, markets: dict, policy_raw: dict,
          out: Callable[[str], None] = print,
          calendar: Optional[Calendar] = None,
          policy: RiskPolicy = POLICY, wanted: Optional[dict] = None) -> int:
    rules = AccountRules.from_policy(policy_raw,
                                     float(account.get("starting_balance") or 0))
    acct = api.get_account(account["id"])
    risk = acct.get("risk") or {}
    eq = float(risk.get("equity", acct.get("balance", 0)) or 0)
    positions = api.positions(account["id"], "open") or []
    closed = api.positions(account["id"], "closed") or []
    state = account_state(acct, rules, positions, closed, time.time())

    out("VERIFICATION SANDBOX - aucun ordre ne part")
    out(f"  serveur   : {SANDBOX_BASE}")
    out(f"  cle       : {api.key_display}")
    out(f"  compte    : {account.get('account_number')}  {acct.get('status')}  "
        f"equity {eq:,.2f} $  depart {rules.starting_balance:,.0f} $")
    out(f"  regles    : perte/jour {rules.daily_loss_pct * 100:g} %  "
        f"drawdown {rules.drawdown_pct * 100:g} % ({rules.drawdown_type})  "
        f"cible {rules.profit_target_pct * 100:g} %")
    out(f"  positions : {len(positions)} ouverte(s), "
        f"{state.trades_last_24h} trade(s) sur 24 h (limite {policy.max_trades_24h})")

    ok = acct.get("status") == "active"
    blocked = False
    for sym, cands in (wanted or MARKETS).items():
        m = markets.get(sym)
        if not m:
            out(f"  {sym:5} : aucun marche disponible parmi {cands}")
            ok = False
            continue
        mid = None
        try:
            mid = mid_of(api.quote(m["market_id"]))
        except Exception as e:
            out(f"  {sym:5} : {m['market_id']}  prix illisible ({e})")
        if mid is None:
            ok = False
            continue
        intent = OrderIntent(m["market_id"], "buy", mid,
                             round_price(mid * 0.995), round_price(mid * 1.01))
        limits = PlatformLimits.from_policy(
            policy_raw, taker_fee_bps=FEE_BPS.get(sym, TAKER_FEE_BPS),
            max_platform_leverage=float(m.get("max_leverage") or 50))
        v = evaluate_order(intent, state, rules, limits, policy)
        head = f"  {sym:5} : {m['market_id']}  prix {mid:,.2f}"
        if not v.approved:
            blocked = True
            out(f"{head}  -> refuserait maintenant : {', '.join(v.reject_codes)}")
            continue
        size, lev, why = fit_to_market(v.computed_size, v.computed_leverage,
                                       (v.computed.get("lev_window") or [0, 0])[1],
                                       m, mid)
        if why:
            out(f"{head}  -> {why}")
            continue
        out(f"{head}  -> exemple achat, stop a 0,5 % : taille {size}, levier "
            f"{lev}x, risque {v.computed['risk_budget']:,.0f} $, notionnel "
            f"{size * mid:,.0f} $ ({size * mid / eq:.2f}x l'equity)")
    if calendar is not None:
        # Le calendrier n'empeche jamais le lancement : s'il est injoignable,
        # son siege s'abstient et le moteur trade comme en v2.4.
        now = time.time()
        try:
            calendar.refresh(now, force=True)
            lines = calendar.check_lines(
                now, mode=TABLE_MODES.get("calendrier", "observe"))
        except Exception as e:
            lines = [f"calendrier : illisible ({type(e).__name__}) - le moteur "
                     "tourne quand meme, ce siege s'abstiendra"]
        for line in lines:
            out(f"  {line}")
    if acct.get("status") != "active":
        out(f"  compte non actif ({acct.get('status')}) : choisis un autre compte "
            f"sandbox ({ACCOUNT_ENV} dans .env)")
    if ok and blocked:
        out("  Note : le garde-fou bloque pour l'instant (codes ci-dessus). Le "
            "moteur peut tourner, il attendra que le compte soit libre.")
    out("OK : tout est pret. Lancement : python -m perps_engine.sandbox_run"
        if ok else "A CORRIGER avant de lancer (voir ci-dessus).")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Point d'entree
# --------------------------------------------------------------------------

def reglages(profil: Optional[Profil], markets: dict) -> dict:
    """Reglages du moteur pour un compte. Sans fiche : la v2.6 de Sandbox 1."""
    if profil is None:
        return {"markets": markets,
                "policy": replace(POLICY, risk_pct_of_balance=RISQUE_SANDBOX_1),
                "cfg": SignalConfig(),
                "paused": PAUSED_MARKETS, "hold": HOLD_TO_TARGET,
                "modes": dict(TABLE_MODES), "tendance": False}
    mk = {s: m for s, m in markets.items() if s in profil.marches}
    modes = dict(TABLE_MODES)
    if profil.tendance4h:
        modes["tendance4h"] = "veto"
    return {"markets": mk,
            "policy": replace(POLICY, risk_pct_of_balance=profil.risque_pct,
                              min_rr=profil.rr_min),
            "cfg": replace(SignalConfig(), tp_r_multiple=profil.objectif_r),
            "paused": (), "hold": tuple(mk) if profil.tenir else (),
            "modes": modes, "tendance": profil.tendance4h}


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check_only = "--check" in argv
    paths = Paths()
    # v2.7 : un dossier par compte, avec sa fiche. La cle reste dans le .env
    # du dossier du moteur, jamais copiee.
    try:
        profil = charger_profil("strategie.json")
    except (ValueError, TypeError) as e:
        print(f"ARRET : fiche de strategie illisible ({e}).")
        return 2
    if profil is not None:
        os.environ[ACCOUNT_ENV] = profil.compte
    load_dotenv(".env")
    if profil is not None and not os.environ.get(KEY_ENV):
        # Seulement pour un dossier de compte qui a sa fiche : sans fiche, le
        # moteur ne lit que le .env du dossier ou il tourne (sinon un test
        # lance ailleurs trouverait la vraie cle).
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), ".env"))

    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        print(f"ARRET : {KEY_ENV} absente. Ajoute dans .env la ligne "
              f"{KEY_ENV}=fp_test_...")
        return 2
    try:
        api = SandboxExecutor(key, journal_path=paths.orders)
    except SandboxOnlyError as e:
        print(f"ARRET : {e}")
        return 2

    try:
        account = pick_account(api.list_accounts(),
                               os.environ.get(ACCOUNT_ENV, ""))
        markets = pick_markets(api.list_markets())
        policy_raw = api.trading_policy(account["id"])
    except ApiError as e:
        print(f"ARRET : l'API sandbox refuse ({e}). Verifie la cle : "
              "environnement Test, acces Read And Trade.")
        return 2
    except requests.RequestException as e:
        print(f"ARRET : serveur sandbox injoignable ({type(e).__name__}). "
              "Verifie la connexion Internet, puis relance.")
        return 2
    except RuntimeError as e:
        print(f"ARRET : {e}")
        return 2
    r = reglages(profil, markets)
    markets = r["markets"]
    if not markets:
        print("ARRET : aucun des marches prevus n'est disponible en sandbox.")
        return 2

    calendar = Calendar(paths.calendar)
    if check_only:
        return check(api, account, markets, policy_raw, calendar=calendar,
                     policy=r["policy"], wanted={s: MARKETS[s] for s in markets})

    lock = acquire_single_instance(paths.lock)
    if lock is None:
        print("ARRET : un moteur sandbox tourne deja dans ce dossier.")
        return 2
    seats = [MomentumSeat(), calendar] + ([TendanceSeat()] if r["tendance"] else [])
    table = Table(seats, journal_path=paths.table, modes=r["modes"])

    cfg = r["cfg"]
    strategies = {s: MomentumStrategy(cfg) for s in markets}
    ids = {s: m["market_id"] for s, m in markets.items()}
    restored = restore_buffers(strategies, ids, paths.state,
                               paths.observation_state)
    runner = Runner(api, account, markets, strategies, policy_raw,
                    policy=r["policy"], cfg=cfg, paths=paths, table=table,
                    paused=r["paused"], fees_by_sym=FEE_BPS,
                    hold=r["hold"], profil=profil)
    runner.log_start(restored)
    if restored["warmup"]:
        print(f"Chauffe de {cfg.warmup_minutes} min pour : "
              f"{', '.join(restored['warmup'])} (aucun signal avant).")
    return runner.run_forever()


if __name__ == "__main__":
    sys.exit(main())
