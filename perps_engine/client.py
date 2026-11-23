"""
client.py - acces API en LECTURE SEULE.

Aucune fonction d'ecriture n'existe dans ce fichier. Pas commentee, pas protegee
par un drapeau : absente. Un moteur qui ne peut pas ecrire ne peut pas deraper,
quelle que soit la suite du code.

Le garde-fou n'est pas une intention, c'est une absence de capacite.
"""

from __future__ import annotations

import time
from typing import Any

import requests

_FORBIDDEN = ("order", "close", "cancel", "modify", "position/")


class ReadOnlyClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._s = requests.Session()
        self._s.headers.update({"Authorization": f"Bearer {api_key}",
                                "Accept": "application/json"})

    def _get(self, path: str, **params) -> Any:
        if any(f in path.lower() for f in _FORBIDDEN) and "positions" not in path:
            raise RuntimeError(f"Chemin interdit en lecture seule : {path}")
        r = self._s.get(f"{self._base}{path}", params=params or None, timeout=self._timeout)
        r.raise_for_status()
        return r.json().get("data", r.json())

    # --- lectures autorisees, liste close ---------------------------------
    def list_accounts(self):            return self._get("/accounts")
    def get_account(self, aid):         return self._get(f"/accounts/{aid}")
    def trading_policy(self, aid):      return self._get(f"/accounts/{aid}/trading-policy")
    def open_positions(self, aid):      return self._get("/positions", account_id=aid)
    def working_orders(self, aid):      return self._get("/orders", account_id=aid, status="working")
    def fills(self, aid, limit=50):     return self._get("/fills", account_id=aid, limit=limit)

    def quote(self, market_id: str) -> dict:
        q = self._get(f"/markets/{market_id}/quote")
        q["_fetched_at_ms"] = int(time.time() * 1000)
        return q
