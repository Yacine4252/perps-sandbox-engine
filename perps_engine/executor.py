"""
executor.py - passage d'ordres, SANDBOX UNIQUEMENT (v2.0).

Pourquoi un fichier separe
--------------------------
client.py reste en lecture seule, et engine.py (le moteur d'observation)
refuse toujours de demarrer s'il y trouve un verbe d'ecriture. L'ecriture
vit ici, et nulle part ailleurs.

Ce module ne peut PAS toucher un vrai compte, par construction :
  - il refuse toute cle qui ne commence pas par fp_test_
  - il refuse tout serveur autre que sandbox.myfundedperpetuals.com
  - la plateforme separe de toute facon les deux mondes : une cle de test
    ne voit aucun compte de defi, une cle live ne voit aucun compte
    sandbox (doc /sandbox.md)

Passer en reel exigera de modifier ce fichier volontairement, et des tests
casseront. C'est voulu.

Idempotence (doc /idempotency.md)
---------------------------------
Chaque intention d'ordre recoit UNE cle, ecrite au journal AVANT l'envoi.
Une reponse perdue (coupure, timeout, 429, 5xx) est rejouee avec la MEME
cle et le MEME corps : la plateforme rend l'ordre existant au lieu d'en
creer un second. On ne fabrique jamais une nouvelle cle pour reessayer un
resultat incertain.
"""

from __future__ import annotations

import json
import uuid
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote as _urlquote

import requests

SANDBOX_BASE = "https://sandbox.myfundedperpetuals.com/v1"
TEST_KEY_PREFIX = "fp_test_"
FINAL_STATUSES = frozenset({"filled", "rejected", "canceled", "expired"})
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Glissement tolere a l'entree, en points de base (minimum plateforme : 20).
# A la sortie, aucune tolerance n'est envoyee : le reglage du compte s'applique.
ENTRY_SLIPPAGE_BPS = 30.0


class SandboxOnlyError(RuntimeError):
    """Tentative d'utiliser l'executeur hors sandbox."""


class ApiError(RuntimeError):
    """Reponse d'erreur de l'API, avec l'enveloppe documentee."""

    def __init__(self, status: int, code: str, message: str,
                 request_id: Optional[str] = None,
                 rule_id: Optional[str] = None, details: Any = None) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.rule_id = rule_id
        self.details = details

    def as_dict(self) -> dict:
        return {"status": self.status, "code": self.code,
                "message": self.message, "request_id": self.request_id,
                "rule_id": self.rule_id, "details": self.details}


def new_idempotency_key(prefix: str = "sbx") -> str:
    return f"{prefix}-{uuid.uuid4()}"


def mask_key(key: str) -> str:
    """Affichable sans danger : prefixe + 4 derniers caracteres."""
    if not key:
        return "(vide)"
    return f"{key[:8]}****{key[-4:]}" if len(key) > 12 else "****"


def _brief(order: Optional[dict]) -> dict:
    if not isinstance(order, dict):
        return {}
    keep = ("id", "client_order_id", "status", "market_id", "side", "size",
            "leverage", "fill_price", "expected_price", "fee", "realized_pnl",
            "attached_stop_loss_price", "attached_take_profit_price",
            "stop_loss_order_id", "take_profit_order_id",
            "position_side_after", "position_size_after", "reduce_only")
    return {k: order.get(k) for k in keep if k in order}


def _retry_after(r: Any) -> float:
    try:
        return min(max(float(r.headers.get("Retry-After", 1)), 1.0), 30.0)
    except (TypeError, ValueError, AttributeError):
        return 5.0


def _parse(r: Any) -> Any:
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    if 200 <= r.status_code < 300:
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    raise ApiError(r.status_code, err.get("code", "http_error"),
                   err.get("message", f"HTTP {r.status_code}"),
                   err.get("request_id"), err.get("rule_id"),
                   err.get("details"))


class SandboxExecutor:
    """
    Acces complet a l'API SANDBOX : lectures et ordres.

    `session` et `sleep` sont injectables pour les tests : aucun test ne
    parle au vrai serveur.
    """

    def __init__(self, api_key: str, base_url: str = SANDBOX_BASE,
                 timeout: float = 10.0, session: Any = None,
                 sleep: Callable[[float], None] = time.sleep,
                 journal_path: Optional[str] = None,
                 max_attempts: int = 3,
                 fill_wait_seconds: float = 15.0) -> None:
        if not isinstance(api_key, str) or not api_key.startswith(TEST_KEY_PREFIX):
            raise SandboxOnlyError(
                "Cle refusee : l'executeur n'accepte qu'une cle de test "
                f"({TEST_KEY_PREFIX}...). Une cle live ne passera jamais ici.")
        if (base_url or "").rstrip("/") != SANDBOX_BASE:
            raise SandboxOnlyError(
                f"Serveur refuse : {base_url!r}. Seul {SANDBOX_BASE} est autorise.")
        self._base = SANDBOX_BASE
        self._timeout = timeout
        self._sleep = sleep
        self._journal_path = journal_path
        self._max_attempts = max(1, int(max_attempts))
        self._fill_wait = fill_wait_seconds
        self._s = session if session is not None else requests.Session()
        self._auth = {"Authorization": f"Bearer {api_key}",
                      "Accept": "application/json"}
        self.key_display = mask_key(api_key)

    # -- journal -----------------------------------------------------------
    def _journal(self, entry: dict) -> None:
        if not self._journal_path:
            return
        entry = dict(entry)
        entry["ts"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(self._journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # -- transport ---------------------------------------------------------
    def _request(self, method: str, path: str, *, body: Optional[dict] = None,
                 params: Optional[dict] = None,
                 idem_key: Optional[str] = None) -> Any:
        headers = dict(self._auth)
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idem_key is not None:
            headers["Idempotency-Key"] = idem_key
        url = f"{self._base}{path}"

        for attempt in range(1, self._max_attempts + 1):
            try:
                r = self._s.request(method, url, headers=headers, json=body,
                                    params=params, timeout=self._timeout)
            except (requests.ConnectionError, requests.Timeout):
                if attempt >= self._max_attempts:
                    raise
                self._sleep(min(2 ** attempt, 10))
                continue
            if r.status_code in RETRYABLE_STATUS and attempt < self._max_attempts:
                wait = (_retry_after(r) if r.status_code == 429
                        else float(min(2 ** attempt, 10)))
                self._sleep(wait)
                continue
            return _parse(r)
        raise RuntimeError("boucle de requete epuisee")   # pragma: no cover

    # -- lectures ----------------------------------------------------------
    def list_accounts(self) -> list:
        return self._request("GET", "/accounts") or []

    def get_account(self, account_id: str) -> dict:
        return self._request("GET", f"/accounts/{account_id}") or {}

    def trading_policy(self, account_id: str) -> dict:
        return self._request("GET", f"/accounts/{account_id}/trading-policy") or {}

    def list_markets(self) -> list:
        return self._request("GET", "/markets") or []

    def quote(self, market_id: str) -> dict:
        q = self._request("GET", f"/markets/{_urlquote(market_id, safe='')}/quote")
        q = dict(q or {})
        q["_fetched_at_ms"] = int(time.time() * 1000)
        return q

    def positions(self, account_id: str, status: str = "open",
                  limit: int = 100) -> list:
        return self._request("GET", "/positions",
                             params={"account_id": account_id,
                                     "status": status, "limit": limit}) or []

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/orders/{order_id}") or {}

    def find_order(self, client_order_id: str) -> Optional[dict]:
        data = self._request("GET", "/orders",
                             params={"client_order_id": client_order_id})
        if isinstance(data, list):
            return data[0] if data else None
        return data or None

    def wait_final(self, order: dict) -> dict:
        """Un ordre accepte commence 'pending' : on le suit jusqu'au bout."""
        deadline = time.time() + self._fill_wait
        current = order
        while (current.get("status") not in FINAL_STATUSES
               and current.get("id") and time.time() < deadline):
            self._sleep(1.0)
            try:
                current = self.get_order(current["id"]) or current
            except (ApiError, requests.RequestException):
                break
        return current

    def _recover(self, client_order_id: str) -> Optional[dict]:
        """Reponse perdue : l'ordre existe peut-etre. On le cherche."""
        try:
            return self.find_order(client_order_id)
        except (ApiError, requests.RequestException):
            return None

    # -- ecritures ---------------------------------------------------------
    def open_market(self, *, account_id: str, market_id: str, side: str,
                    size: float, leverage: float, stop_loss_price: float,
                    take_profit_price: Optional[float], expected_price: float,
                    slippage_bps: float = ENTRY_SLIPPAGE_BPS,
                    client_order_id: Optional[str] = None,
                    idem_key: Optional[str] = None) -> dict:
        """
        Ordre au marche, stop et objectif ATTACHES dans le meme ordre :
        la position n'existe jamais une seconde sans stop.
        """
        if side not in ("buy", "sell"):
            raise ValueError(f"cote inconnu : {side!r}")
        if not stop_loss_price or stop_loss_price <= 0:
            raise ValueError("aucun ordre sans stop")
        if size <= 0 or leverage <= 0 or expected_price <= 0:
            raise ValueError("taille, levier et prix doivent etre positifs")
        long = side == "buy"
        if (long and stop_loss_price >= expected_price) or \
           (not long and stop_loss_price <= expected_price):
            raise ValueError("stop du mauvais cote du prix")
        if take_profit_price is not None:
            if (long and take_profit_price <= expected_price) or \
               (not long and take_profit_price >= expected_price):
                raise ValueError("objectif du mauvais cote du prix")

        key = idem_key or new_idempotency_key("open")
        coid = client_order_id or f"sbx-{uuid.uuid4().hex[:20]}"
        body = {"client_order_id": coid, "account_id": account_id,
                "market_id": market_id, "type": "market", "side": side,
                "size": size, "leverage": leverage, "margin_mode": "isolated",
                "expected_price": expected_price,
                "slippage_tolerance_bps": slippage_bps,
                "stop_loss_price": stop_loss_price}
        if take_profit_price is not None:
            body["take_profit_price"] = take_profit_price
        return self._send("open", "/orders", body, key, coid)

    def close_position(self, position_id: str, *,
                       expected_price: float,
                       slippage_bps: Optional[float] = None,
                       minimal: bool = False,
                       idem_key: Optional[str] = None,
                       size: Optional[float] = None) -> dict:
        """
        Ferme TOUTE la position (taille omise = fermeture complete).

        v2.4 : la plateforme EXIGE expected_price (le prix milieu observe)
        pour une fermeture au marche. Sans lui, chaque sortie de la v2.2 a ete
        refusee : HTTP 400 invalid_position_close, details
        [{"field": "expected_price", "issue": "is required for market closes"}].
        Il est donc obligatoire ici : aucune requete ne part sans lui.
        La tolerance de glissement n'est pas envoyee : le reglage du compte
        s'applique. `minimal=True` n'envoie que le type et le prix : dernier
        recours si un autre champ etait refuse.
        """
        if not expected_price or expected_price <= 0:
            raise ValueError("expected_price requis pour fermer au marche")
        if size is not None and (minimal or size <= 0):
            raise ValueError("fermeture partielle : taille > 0, jamais en minimal")
        key = idem_key or new_idempotency_key("close")
        if minimal:
            return self._send("close", f"/positions/{position_id}/close",
                              {"type": "market", "expected_price": expected_price},
                              key, None)
        coid = f"sbx-close-{uuid.uuid4().hex[:16]}"
        body = {"client_order_id": coid, "type": "market",
                "expected_price": expected_price}
        if slippage_bps:
            body["slippage_tolerance_bps"] = slippage_bps
        if size is not None:
            body["size"] = size          # partielle (v2.7) ; sinon tout ferme
        return self._send("close", f"/positions/{position_id}/close",
                          body, key, coid)

    def _send(self, kind: str, path: str, body: dict, key: str,
              coid: Optional[str]) -> dict:
        # Ecrit AVANT l'envoi : si la machine coupe, on sait ce qui est parti.
        self._journal({"event": f"{kind}_send", "path": path,
                       "idempotency_key": key, "body": body})
        try:
            order = self._request("POST", path, body=body, idem_key=key)
        except ApiError as e:
            self._journal({"event": f"{kind}_error", "idempotency_key": key,
                           "client_order_id": coid, **e.as_dict()})
            raise
        except (requests.ConnectionError, requests.Timeout) as e:
            found = self._recover(coid) if coid else None
            self._journal({"event": f"{kind}_uncertain", "idempotency_key": key,
                           "client_order_id": coid, "detail": str(e),
                           "recovered": bool(found)})
            if not found:
                raise
            order = found
        order = self.wait_final(order if isinstance(order, dict) else {})
        self._journal({"event": f"{kind}_result", "idempotency_key": key,
                       "order": _brief(order)})
        return order
