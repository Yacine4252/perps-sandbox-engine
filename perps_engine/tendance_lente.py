"""
tendance_lente.py - suivi de tendance lent, un compte sandbox par systeme
(22 nov 2026, go de Yc : "tu as 10 comptes, trouve la meilleure maniere").

Pourquoi
--------
Sur 8 ans (recherche/tendance/RESULTATS.md), seul le suivi de tendance lent
sur le BTC montre un avantage solide. Le momentum rapide sur l'or n'en a
jamais montre. Ce moteur fait tourner le suivi de tendance en vrai, sur des
comptes sandbox dedies, a cote du moteur momentum, qui n'est pas modifie.

Regles, les memes que le test (recherche/tendance/tendance_long.py)
--------------------------------------------------------------------
  donchian20 / donchian55 : a la cloture d'une bougie au-dela du plus haut
      (plus bas) des 20 / 55 bougies precedentes, position dans ce sens.
      Sortie a la cloture sous le plus bas (au-dessus du plus haut) des
      10 / 20 bougies precedentes.
  tsmom1 / tsmom4 / tsmom12 : a la derniere bougie de chaque semaine (UTC),
      achat si la cloture depasse celle d'il y a 1 / 4 / 12 semaines, vente
      sinon. On se retourne quand le signe change.
  Stop fixe a k ATR(20) de l'entree, pose sur la plateforme avec l'ordre.
  Pas d'objectif : la sortie vient du signal ou du stop.
  Le test entre a l'ouverture de la bougie suivante ; ici, dans la minute qui
  suit la cloture, au prix du sandbox.

Premier demarrage : le moteur rejoue l'historique. Si le systeme a une
position ouverte aujourd'hui, il la prend, avec un stop neuf a k ATR du prix
du moment (sauf si le prix a deja passe le stop du systeme).

Securite
--------
Meme executeur que le moteur momentum : cle fp_test_ et serveur sandbox
seulement. Meme garde-fou de taille : risque frais compris, plafond 3x,
plancher du compte, perte du jour. Un seul moteur par dossier (verrou
sandbox.lock, le meme que le moteur momentum). Arret par le fichier
HALT-SANDBOX. La cle reste dans le .env du dossier du moteur : elle n'est lue
que pour un dossier place sous comptes/.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from .alerte import raise_alert
from .executor import ApiError, SANDBOX_BASE, SandboxExecutor, SandboxOnlyError
from .risk import AccountRules, OrderIntent, PlatformLimits, evaluate_order
from .sandbox_run import (ACCOUNT_ENV, FEE_BPS, KEY_ENV, MARKETS, POLICY, Paths,
                          _load_json, _save_json_atomic, account_state,
                          acquire_single_instance, fit_to_market, load_dotenv,
                          mid_of, pick_account, pick_markets, round_price)

VERSION = "tendance-1.0"
FICHE = "tendance.json"
ETAT = "tendance-etat.json"
SPOT = "https://api.binance.com/api/v3/klines"
USER_AGENT = "perps-engine/tendance-1.0"
SYMBOLES = {"BTC": "BTCUSDT"}
INTERVALLES = {"1d": 86_400_000, "4h": 14_400_000}
SYSTEMES = ("donchian20", "donchian55", "tsmom1", "tsmom4", "tsmom12")
SEMAINE_MS = 7 * 86_400_000
ATR_N = 20
POLL_SECONDS = 60
SNAPSHOT_EVERY_SECONDS = 900
MARGE_CLOTURE_MS = 60_000          # une minute apres la cloture, Binance a fini
ESSAIS_SORTIE_JOURNAL = 30         # cycles a attendre la fiche d'une position fermee
RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPTES = os.path.join(RACINE, "comptes")


# --------------------------------------------------------------------------
# La fiche du compte
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProfilTendance:
    nom: str
    compte: str
    systeme: str
    intervalle: str = "1d"
    marche: str = "BTC"
    stop_atr: float = 3.0
    risque_pct: float = 0.005
    rattrapage: bool = True

    def resume(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def charger_tendance(chemin: str = FICHE) -> ProfilTendance:
    if not os.path.exists(chemin):
        raise ValueError(f"pas de fiche {chemin}")
    with open(chemin, encoding="utf-8-sig") as f:
        d = json.load(f)
    inconnus = sorted(set(d) - {f.name for f in fields(ProfilTendance)})
    if inconnus:
        raise ValueError(f"{chemin} : champ(s) inconnu(s) {inconnus}")
    p = ProfilTendance(**d)
    if not str(p.compte).startswith("FP-"):
        raise ValueError(f"{chemin} : compte invalide {p.compte!r}")
    if p.systeme not in SYSTEMES:
        raise ValueError(f"{chemin} : systeme inconnu {p.systeme!r}")
    if p.intervalle not in INTERVALLES:
        raise ValueError(f"{chemin} : intervalle inconnu {p.intervalle!r}")
    if p.marche not in SYMBOLES:
        raise ValueError(f"{chemin} : marche inconnu {p.marche!r}")
    if not 1.0 <= float(p.stop_atr) <= 5.0:
        raise ValueError(f"{chemin} : stop hors de [1 ; 5] ATR")
    if not 0 < float(p.risque_pct) <= 0.01:
        raise ValueError(f"{chemin} : risque par trade hors de ]0 ; 1 %]")
    return p


# --------------------------------------------------------------------------
# Bougies et regles (fonctions pures, testees contre le backtest)
# --------------------------------------------------------------------------

def _get_json(url: str):
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    return r.json()


def bougies(sym: str, intervalle: str, now: float, n: int = 1000,
            fetch: Callable = _get_json) -> list:
    """Bougies spot Binance TERMINEES avant `now` : [(t, o, h, l, c), ...],
    t = heure d'ouverture en ms."""
    data = fetch(f"{SPOT}?symbol={SYMBOLES[sym]}&interval={intervalle}&limit={n}")
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]))
            for k in data if int(k[6]) < now * 1000]


def atr(b: list, n: int = ATR_N) -> list:
    """ATR moyenne simple, comme le backtest. None tant qu'il manque n bougies."""
    out, tr = [None] * len(b), []
    for k in range(len(b)):
        _, _, h, l, c = b[k]
        pc = b[k - 1][4] if k else c
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        if k >= n:
            out[k] = sum(tr[k - n + 1:k + 1]) / n
    return out


def fin_de_semaine(t_ouverture: int, intervalle_ms: int) -> bool:
    """La bougie se termine le lundi a 00:00 UTC : derniere de la semaine."""
    fin = datetime.fromtimestamp((t_ouverture + intervalle_ms) / 1000, timezone.utc)
    return fin.weekday() == 0 and fin.hour == 0 and fin.minute == 0


def signal(b: list, k: int, systeme: str, intervalle_ms: int):
    """A la cloture de la bougie k : 1, -1, ("sortie", haut, bas) ou None."""
    if systeme.startswith("donchian"):
        ent, sor = (20, 10) if systeme == "donchian20" else (55, 20)
        if k < ent:
            return None
        c = b[k][4]
        if c > max(x[2] for x in b[k - ent:k]):
            return 1
        if c < min(x[3] for x in b[k - ent:k]):
            return -1
        return ("sortie", max(x[2] for x in b[k - sor:k]),
                min(x[3] for x in b[k - sor:k]))
    recul = int(systeme[5:]) * (SEMAINE_MS // intervalle_ms)
    if k < recul or not fin_de_semaine(b[k][0], intervalle_ms):
        return None
    return 1 if b[k][4] > b[k - recul][4] else -1


def voulu(sig, cloture: float, sens: Optional[int]) -> Optional[int]:
    """Ce que le systeme veut apres la cloture : 0 sortir, 1 ou -1 ce sens,
    None rien de neuf."""
    if isinstance(sig, tuple):
        if (sens == 1 and cloture < sig[2]) or (sens == -1 and cloture > sig[1]):
            return 0
        return None
    if sig in (1, -1) and sig != sens:
        return sig
    return None


def rejouer(b: list, systeme: str, k_stop: float, intervalle_ms: int) -> tuple:
    """
    Le backtest, sans les couts : trades passes, position ouverte a la fin,
    et l'action decidee a la derniere cloture (pas encore executee).
    """
    a = atr(b)
    trades, pos, v = [], None, None
    for k in range(1, len(b)):
        t, o, h, l, c = b[k]
        if v is not None:
            if pos and v != pos["sens"]:
                trades.append(dict(pos, sortie=o, t_sortie=t, motif="signal"))
                pos = None
            if v in (1, -1) and pos is None and a[k - 1]:
                dist = k_stop * a[k - 1]
                pos = {"sens": v, "entree": o, "stop": o - v * dist,
                       "dist": dist, "t": t}
            v = None
        if pos and ((pos["sens"] == 1 and l <= pos["stop"]) or
                    (pos["sens"] == -1 and h >= pos["stop"])):
            prix = min(o, pos["stop"]) if pos["sens"] == 1 else max(o, pos["stop"])
            trades.append(dict(pos, sortie=prix, t_sortie=t, motif="stop"))
            pos = None
        w = voulu(signal(b, k, systeme, intervalle_ms), c,
                  pos["sens"] if pos else None)
        if w is not None:
            v = w
    return trades, pos, v


def minimum_bougies(p: "ProfilTendance") -> int:
    """Bougies necessaires pour decider : la fenetre du systeme, plus l'ATR."""
    if p.systeme.startswith("donchian"):
        fenetre = 20 if p.systeme == "donchian20" else 55
    else:
        fenetre = int(p.systeme[5:]) * (SEMAINE_MS // INTERVALLES[p.intervalle])
    return max(fenetre, ATR_N) + 1


def sens_du_systeme(pos: Optional[dict], v: Optional[int]) -> Optional[int]:
    """La position que le systeme veut maintenant, apres l'action en attente."""
    sens = pos["sens"] if pos else None
    if v is not None:
        sens = None if v == 0 else v
    return sens


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _cote(sens: int) -> str:
    return "buy" if sens == 1 else "sell"


# --------------------------------------------------------------------------
# Le moteur
# --------------------------------------------------------------------------

class TendanceRunner:
    def __init__(self, api, account: dict, market: dict, profil: ProfilTendance,
                 policy_raw: dict, *, paths: Optional[Paths] = None,
                 fetch: Callable = _get_json,
                 clock: Callable[[], float] = time.time,
                 etat_path: str = ETAT, echo: bool = True) -> None:
        self.api = api
        self.account = account
        self.account_id = account["id"]
        self.account_number = account.get("account_number") or self.account_id
        self.market = market
        self.market_id = market["market_id"]
        self.profil = profil
        self.paths = paths or Paths()
        self.fetch = fetch
        self.clock = clock
        self.echo = echo
        self.etat_path = etat_path
        self.intervalle_ms = INTERVALLES[profil.intervalle]
        self.fee_bps = FEE_BPS.get(profil.marche, 3.0)
        # Le garde-fou du moteur momentum, au risque de la fiche. Pas de
        # pause apres une perte ni d'objectif minimal : le systeme ne decide
        # qu'a la cloture, et n'a pas d'objectif.
        self.policy = replace(POLICY, risk_pct_of_balance=float(profil.risque_pct),
                              cooldown_after_loss_min=0, min_rr=0.0)
        self.policy_raw = policy_raw or {}
        self.rules = AccountRules.from_policy(
            self.policy_raw, float(account.get("starting_balance") or 0))
        etat = _load_json(etat_path) or {}
        self.derniere: Optional[int] = etat.get("derniere_bougie")
        self.position: Optional[dict] = etat.get("position")
        self.a_journaliser: dict = etat.get("a_journaliser") or {}
        self.history: list = etat.get("history") or []
        self.dernier_snapshot = 0.0
        self.erreurs_vues: dict = {}
        self.etrangeres_vues: set = set()

    # -- outils ------------------------------------------------------------
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

    def sauver(self) -> None:
        _save_json_atomic(self.etat_path, {
            "version": VERSION, "compte": self.account_number,
            "profil": self.profil.resume(), "derniere_bougie": self.derniere,
            "position": self.position, "a_journaliser": self.a_journaliser,
            "history": self.history[-200:]})

    def log_start(self) -> None:
        self._log(self.paths.trades, {
            "event": "start", "version": VERSION,
            "account": self.account_number, "server": SANDBOX_BASE,
            "markets": {self.profil.marche: self.market_id},
            "risk_pct_of_balance": self.policy.risk_pct_of_balance,
            "max_notional_multiple": self.policy.max_notional_multiple,
            "profil": self.profil.resume(), "fee_bps": self.fee_bps,
            "derniere_bougie": _iso(self.derniere),
            "managed_positions": [self.position["id"]] if self.position else []})

    def _sens_actuel(self) -> Optional[int]:
        return self.position["sens"] if self.position else None

    # -- un cycle ----------------------------------------------------------
    def cycle(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        rapport = {"decision": None, "ouverte": None, "fermee": None}
        positions = self.api.positions(self.account_id, "open") or []
        self._rapprocher(positions)
        if self._bougie_due(now):
            b = bougies(self.profil.marche, self.profil.intervalle, now,
                        fetch=self.fetch)
            rapport.update(self._decider(b, now))
        if now - self.dernier_snapshot >= SNAPSHOT_EVERY_SECONDS:
            self._snapshot(now)
        return rapport

    def _bougie_due(self, now: float) -> bool:
        if self.derniere is None:
            return True
        # la bougie apres `derniere` se termine a derniere + 2 intervalles
        return now * 1000 >= self.derniere + 2 * self.intervalle_ms + MARGE_CLOTURE_MS

    def _decider(self, b: list, now: float) -> dict:
        p = self.profil
        if len(b) < minimum_bougies(p):
            raise RuntimeError(f"seulement {len(b)} bougies {p.intervalle} recues "
                               f"({minimum_bougies(p)} necessaires)")
        rapport: dict = {"decision": None, "ouverte": None, "fermee": None}

        if self.derniere is None:
            # Premier demarrage : la position que le systeme aurait aujourd'hui.
            _, pos, v = rejouer(b, p.systeme, p.stop_atr, self.intervalle_ms)
            vise = sens_du_systeme(pos, v)
            note = None
            if vise is not None and v is None and pos is not None:
                mid = mid_of(self.api.quote(self.market_id))
                if mid is not None and ((vise == 1 and mid <= pos["stop"]) or
                                        (vise == -1 and mid >= pos["stop"])):
                    vise, note = None, "prix deja au-dela du stop du systeme"
            if not p.rattrapage:
                vise, note = None, "rattrapage desactive"
            self._log(self.paths.signals, {
                "event": "rattrapage", "systeme": p.systeme,
                "intervalle": p.intervalle, "bougies": len(b),
                "derniere_bougie": _iso(b[-1][0]),
                "position_du_systeme": ({"sens": pos["sens"], "depuis": _iso(pos["t"]),
                                         "entree": pos["entree"],
                                         "stop": round(pos["stop"], 2)}
                                        if pos else None),
                "action_en_attente": v, "prise": vise, "note": note})
            rapport["decision"] = vise
            if vise is not None and self.position is None:
                rapport["ouverte"] = self._ouvrir(vise, b, positions_ouvertes(self), now,
                                                  "rattrapage")
            self.derniere = b[-1][0]
            self.sauver()
            return rapport

        nouvelles = [k for k in range(len(b)) if b[k][0] > self.derniere]
        if not nouvelles:
            return rapport
        vise = self._sens_actuel()
        for k in nouvelles:
            s = signal(b, k, p.systeme, self.intervalle_ms)
            w = voulu(s, b[k][4], vise)
            self._log(self.paths.signals, {
                "event": "bougie", "systeme": p.systeme, "bougie": _iso(b[k][0]),
                "cloture": b[k][4],
                "signal": list(s) if isinstance(s, tuple) else s,
                "action": w, "position_avant": vise})
            if w is not None:
                vise = None if w == 0 else w
        rapport["decision"] = vise
        actuel = self._sens_actuel()
        if vise != actuel:
            if self.position is not None:
                rapport["fermee"] = self._fermer("signal" if vise is None else "retournement")
            if vise is not None:
                rapport["ouverte"] = self._ouvrir(vise, b, positions_ouvertes(self),
                                                  now, "signal")
        self.derniere = b[-1][0]
        self.sauver()
        return rapport

    # -- entree --------------------------------------------------------------
    def _ouvrir(self, sens: int, b: list, positions: list, now: float,
                motif: str) -> str:
        a = atr(b)[-1]
        if not a:
            return "pas_d_atr"
        dist = self.profil.stop_atr * a
        t0 = time.time()
        mid = mid_of(self.api.quote(self.market_id))
        if mid is None:
            raise RuntimeError("pas de prix sandbox : entree remise au cycle suivant")
        age_ms = int((time.time() - t0) * 1000)
        side = _cote(sens)
        stop = round_price(mid - sens * dist)
        if stop <= 0:
            return "stop_negatif"
        intent = OrderIntent(market_id=self.market_id, side=side, entry_price=mid,
                             stop_price=stop, take_profit_price=None)
        acct = self.api.get_account(self.account_id)
        closed = self.api.positions(self.account_id, "closed") or []
        state = account_state(acct, self.rules, positions, closed, now,
                              quote_age_ms=age_ms)
        limits = PlatformLimits.from_policy(
            self.policy_raw, taker_fee_bps=self.fee_bps,
            max_platform_leverage=float(self.market.get("max_leverage") or 50))
        v = evaluate_order(intent, state, self.rules, limits, self.policy)
        self._log(self.paths.trades, {
            "event": "verdict", "sym": self.profil.marche, "side": side,
            "motif": motif, "systeme": self.profil.systeme,
            "atr": round(a, 2), "entry": mid, "stop": stop, "tp": None,
            "approved": bool(v.approved), "rejects": list(v.reject_codes),
            "size": v.computed_size, "fee_bps": self.fee_bps,
            "leverage": v.computed_leverage,
            "risk_budget": v.computed.get("risk_budget"),
            "notional": v.computed.get("notional"),
            "capped_by_exposure": v.computed.get("capped_by_exposure", False),
            "equity": state.equity, "pnl_today": round(state.realized_pnl_today, 2),
            "trades_24h": state.trades_last_24h,
            "open_positions": state.open_positions})
        if not v.approved:
            return "refuse:" + ",".join(v.reject_codes)
        lev_hi = (v.computed.get("lev_window") or [0, 0])[1]
        size, lev, why = fit_to_market(v.computed_size, v.computed_leverage,
                                       lev_hi, self.market, mid)
        if why:
            self._log(self.paths.trades, {"event": "entry_skipped",
                                          "sym": self.profil.marche, "reason": why})
            return "taille:" + why
        try:
            order = self.api.open_market(
                account_id=self.account_id, market_id=self.market_id, side=side,
                size=size, leverage=lev, stop_loss_price=stop,
                take_profit_price=None, expected_price=mid)
        except ApiError as e:
            self._log(self.paths.trades, {"event": "entry_error",
                                          "sym": self.profil.marche, **e.as_dict()})
            if e.status in (401, 403):
                raise
            return f"erreur_api:{e.status}"
        status = order.get("status")
        self._log(self.paths.trades, {
            "event": "entry_order", "sym": self.profil.marche, "side": side,
            "status": status, "order_id": order.get("id"), "size": size,
            "leverage": lev, "fill_price": order.get("fill_price"),
            "stop": stop, "tp": None, "motif": motif,
            "stop_order": order.get("stop_loss_order_id")})
        if status in ("rejected", "canceled", "expired"):
            return status
        try:
            fraiches = self.api.positions(self.account_id, "open") or []
        except Exception:
            fraiches = []
        for pos in fraiches:
            if (pos.get("market_id") == self.market_id and
                    float(pos.get("opened_at") or 0) >= (now - 120) * 1000):
                self.position = {"id": pos["id"], "sens": sens,
                                 "entree": order.get("fill_price") or mid,
                                 "stop": stop, "size": size, "ouverte_a": now,
                                 "motif": motif}
                break
        self.sauver()
        return "remplie" if status == "filled" else str(status)

    # -- sortie --------------------------------------------------------------
    def _fermer(self, motif: str) -> str:
        info = dict(self.position)
        pid = info["id"]
        mid = mid_of(self.api.quote(self.market_id))
        if mid is None:
            raise RuntimeError("pas de prix sandbox : sortie remise au cycle suivant")
        try:
            try:
                order = self.api.close_position(pid, expected_price=mid)
            except ApiError as e:
                if e.status in (400, 422):
                    order = self.api.close_position(pid, expected_price=mid,
                                                    minimal=True)
                else:
                    raise
        except ApiError as e:
            if e.status != 404:          # 404 : deja fermee (stop touche)
                raise
            order = {"status": "deja_fermee"}
        self._log(self.paths.trades, {
            "event": "exit_signal", "position_id": pid, "sym": self.profil.marche,
            "reason": f"{self.profil.systeme} : {motif}",
            "status": order.get("status"), "fill_price": order.get("fill_price"),
            "realized_pnl": order.get("realized_pnl")})
        self.a_journaliser[pid] = dict(info, essais=0)
        self.position = None
        self.sauver()
        return str(order.get("status"))

    # -- rapprochement -------------------------------------------------------
    def _rapprocher(self, positions: list) -> None:
        ouvertes = {p.get("id") for p in positions}
        change = False
        if self.position and self.position["id"] not in ouvertes:
            self.a_journaliser[self.position["id"]] = dict(self.position, essais=0)
            self.position = None
            change = True
        if self.a_journaliser:
            try:
                fermees = {p.get("id"): p for p in
                           (self.api.positions(self.account_id, "closed") or [])}
            except Exception:
                fermees = {}
            for pid in list(self.a_journaliser):
                info = self.a_journaliser[pid]
                rec = fermees.get(pid)
                if rec is None and info.get("essais", 0) < ESSAIS_SORTIE_JOURNAL:
                    info["essais"] = info.get("essais", 0) + 1
                    continue
                self.a_journaliser.pop(pid)
                rec = rec or {}
                ouv, fer = rec.get("opened_at"), rec.get("closed_at")
                net = None
                if rec:
                    net = round(float(rec.get("realized_pnl") or 0)
                                - float(rec.get("fees") or 0)
                                + float(rec.get("funding") or 0), 2)
                entree = {
                    "position_id": pid, "sym": self.profil.marche,
                    "side": _cote(info["sens"]),
                    "entry": rec.get("entry_price", info.get("entree")),
                    "exit": rec.get("exit_price"),
                    "close_reason": rec.get("close_reason"),
                    "gross": rec.get("realized_pnl"), "fees": rec.get("fees"),
                    "funding": rec.get("funding"), "net": net,
                    "hold_min": (round((fer - ouv) / 60000, 1)
                                 if ouv and fer else None),
                    "systeme": self.profil.systeme}
                self.history.append(entree)
                self._log(self.paths.trades, {"event": "position_closed", **entree})
            change = True
        if self.position is None:
            for p in positions:
                if p.get("market_id") != self.market_id or p.get("id") in self.a_journaliser:
                    continue
                # Compte dedie : une position sur notre marche est la notre
                # (reponse perdue a l'entree, ou etat efface).
                sens = 1 if p.get("side") in ("long", "buy") else -1
                self.position = {"id": p["id"], "sens": sens,
                                 "entree": p.get("entry_price"), "stop": None,
                                 "size": p.get("size"),
                                 "ouverte_a": float(p.get("opened_at") or 0) / 1000,
                                 "motif": "adoptee"}
                self._log(self.paths.trades, {"event": "position_adopted",
                                              "position_id": p["id"],
                                              "sym": self.profil.marche})
                change = True
                break
        for p in positions:
            if p.get("market_id") != self.market_id and p.get("id") not in self.etrangeres_vues:
                self.etrangeres_vues.add(p.get("id"))
                self._log(self.paths.trades, {
                    "event": "position_etrangere", "position_id": p.get("id"),
                    "market_id": p.get("market_id"),
                    "note": "pas a ce moteur : laissee a son stop et a son objectif"})
        if change:
            self.sauver()

    # -- point -----------------------------------------------------------------
    def _snapshot(self, now: float) -> None:
        acct = self.api.get_account(self.account_id)
        risk = acct.get("risk") or {}
        bal = float(acct.get("balance") or 0)
        prochaine = (self.derniere + 2 * self.intervalle_ms) if self.derniere else None
        self._log(self.paths.signals, {
            "event": "snapshot",
            "equity": float(risk.get("equity", acct.get("equity", bal)) or bal),
            "daily_loss_room": risk.get("daily_loss_room"),
            "managed": [self.position["id"]] if self.position else [],
            "markets": {self.profil.marche: {
                "systeme": self.profil.systeme, "intervalle": self.profil.intervalle,
                "position": self._sens_actuel(),
                "stop": self.position.get("stop") if self.position else None,
                "derniere_bougie": _iso(self.derniere),
                "prochaine_decision": _iso(prochaine)}}})
        self.dernier_snapshot = now

    def run_forever(self) -> int:
        while True:
            if os.path.exists(self.paths.halt):
                self.sauver()
                self._log(self.paths.trades, {"event": "halt", "state_saved": True})
                print(f"Arrete par {self.paths.halt}. Supprime ce fichier pour relancer.")
                return 0
            try:
                self.cycle()
            except ApiError as e:
                self._erreur(e.as_dict())
                if e.status in (401, 403):
                    raise_alert(f"[SANDBOX tendance] acces refuse par l'API ({e.code}) "
                                "- moteur arrete", self.paths.alert)
                    self.sauver()
                    return 3
            except Exception as e:
                self._erreur({"detail": f"{type(e).__name__}: {e}"[:300]})
            time.sleep(POLL_SECONDS)

    def _erreur(self, detail: dict) -> None:
        """Meme panne : une ligne par demi-heure, pas une par minute."""
        cle = json.dumps(detail, sort_keys=True, default=str)[:200]
        maintenant = time.time()
        if maintenant - self.erreurs_vues.get(cle, 0) >= 1800:
            self.erreurs_vues[cle] = maintenant
            self._log(self.paths.trades, {"event": "error", **detail})


def positions_ouvertes(runner: TendanceRunner) -> list:
    """Positions ouvertes, sans celle qu'on vient de fermer (si la plateforme
    la montre encore une seconde, elle ne doit pas bloquer le retournement)."""
    try:
        return [p for p in (runner.api.positions(runner.account_id, "open") or [])
                if p.get("id") not in runner.a_journaliser]
    except Exception:
        return []


# --------------------------------------------------------------------------
# Verification sans ordre
# --------------------------------------------------------------------------

def check(api, account: dict, market: dict, profil: ProfilTendance,
          policy_raw: dict, fetch: Callable = _get_json,
          now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    im = INTERVALLES[profil.intervalle]
    b = bougies(profil.marche, profil.intervalle, now, fetch=fetch)
    _, pos, v = rejouer(b, profil.systeme, profil.stop_atr, im)
    vise = sens_du_systeme(pos, v)
    a = atr(b)[-1]
    mid = mid_of(api.quote(market["market_id"]))
    acct = api.get_account(account["id"])
    risk = acct.get("risk") or {}
    nom = {1: "achat", -1: "vente", None: "aucune"}
    print(f"Moteur {VERSION} - {profil.nom}")
    print(f"  compte {account.get('account_number')}  equite "
          f"{float(risk.get('equity') or acct.get('balance') or 0):,.0f} $".replace(",", " "))
    print(f"  marche {market['market_id']}  prix sandbox {mid}")
    print(f"  systeme {profil.systeme} en {profil.intervalle}, stop {profil.stop_atr:g} ATR, "
          f"risque {profil.risque_pct * 100:g} % par trade")
    print(f"  {len(b)} bougies, la derniere terminee : {_iso(b[-1][0])} UTC, "
          f"cloture {b[-1][4]}, ATR {a:.2f}")
    if pos:
        print(f"  position du systeme : {nom[pos['sens']]} depuis {_iso(pos['t'])}, "
              f"entree {pos['entree']}, stop {pos['stop']:.2f}")
    else:
        print("  position du systeme : aucune")
    print(f"  action decidee a la derniere cloture : {v}")
    print(f"  au demarrage, le moteur prendrait : {nom[vise]}")
    if vise is not None and mid and a:
        stop = round_price(mid - vise * profil.stop_atr * a)
        intent = OrderIntent(market_id=market["market_id"], side=_cote(vise),
                             entry_price=mid, stop_price=stop, take_profit_price=None)
        rules = AccountRules.from_policy(policy_raw or {},
                                         float(account.get("starting_balance") or 0))
        state = account_state(acct, rules, api.positions(account["id"], "open") or [],
                              api.positions(account["id"], "closed") or [], now)
        policy = replace(POLICY, risk_pct_of_balance=float(profil.risque_pct),
                         cooldown_after_loss_min=0, min_rr=0.0)
        limits = PlatformLimits.from_policy(
            policy_raw or {}, taker_fee_bps=FEE_BPS.get(profil.marche, 3.0),
            max_platform_leverage=float(market.get("max_leverage") or 50))
        verdict = evaluate_order(intent, state, rules, limits, policy)
        print(f"  ordre : {_cote(vise)} au prix {mid}, stop {stop} "
              f"({abs(mid - stop) / mid * 100:.2f} %), taille {verdict.computed_size}, "
              f"risque {verdict.computed.get('loss_at_stop')} $, "
              f"{'accepte' if verdict.approved else 'refuse ' + str(list(verdict.reject_codes))}")
    print("  (verification seulement : aucun ordre envoye)")
    return 0


# --------------------------------------------------------------------------
# Demarrage
# --------------------------------------------------------------------------

def _sous_comptes(dossier: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(dossier), COMPTES]) == COMPTES
    except ValueError:
        return False


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check_only = "--check" in argv
    paths = Paths()
    if os.path.exists("strategie.json") and not check_only:
        print("ARRET : ce dossier a une strategie.json (moteur de l'or). "
              "Un seul moteur par compte.")
        return 2
    try:
        profil = charger_tendance(FICHE)
    except (ValueError, TypeError) as e:
        print(f"ARRET : fiche {FICHE} illisible ({e}).")
        return 2
    os.environ[ACCOUNT_ENV] = profil.compte
    load_dotenv(".env")
    if not os.environ.get(KEY_ENV) and _sous_comptes(os.getcwd()):
        # La cle du dossier du moteur, seulement pour un vrai dossier de
        # compte : un test lance ailleurs ne la trouve jamais.
        load_dotenv(os.path.join(RACINE, ".env"))
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        print(f"ARRET : {KEY_ENV} absente.")
        return 2
    try:
        api = SandboxExecutor(key, journal_path=paths.orders)
    except SandboxOnlyError as e:
        print(f"ARRET : {e}")
        return 2
    try:
        account = pick_account(api.list_accounts(), profil.compte)
        markets = pick_markets(api.list_markets(),
                               {profil.marche: MARKETS[profil.marche]})
        policy_raw = api.trading_policy(account["id"])
    except ApiError as e:
        print(f"ARRET : l'API sandbox refuse ({e}).")
        return 2
    except requests.RequestException as e:
        print(f"ARRET : serveur sandbox injoignable ({type(e).__name__}).")
        return 2
    except RuntimeError as e:
        print(f"ARRET : {e}")
        return 2
    market = markets.get(profil.marche)
    if market is None:
        print(f"ARRET : marche {profil.marche} absent du sandbox.")
        return 2
    if check_only:
        return check(api, account, market, profil, policy_raw)
    lock = acquire_single_instance(paths.lock)
    if lock is None:
        print("ARRET : un moteur tourne deja dans ce dossier.")
        return 2
    runner = TendanceRunner(api, account, market, profil, policy_raw, paths=paths)
    runner.log_start()
    return runner.run_forever()


if __name__ == "__main__":
    sys.exit(main())
