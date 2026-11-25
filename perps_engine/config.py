"""
config.py - profils de comptes.

Un compte se declare ici en 4 lignes. Tout le reste (plancher, budget quotidien,
cible, marge restante, taille, levier) est derive a l'execution depuis les regles
que la firme expose via son API. Aucun chiffre de risque n'est code en dur ailleurs.

Ajouter un compte = ajouter une entree. Aucune ligne de logique a toucher.
"""

import os
from dataclasses import dataclass
from typing import Optional

from .risk import RiskPolicy
from .signals import SignalConfig
from .basket import DEFAULT_BASKET

def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

DRY_RUN = True          # v1 : le moteur observe et journalise. Il n'execute pas.
POLL_SECONDS = 60
HALT_FILE = "HALT"      # creer ce fichier -> arret au cycle suivant


@dataclass(frozen=True)
class AccountProfile:
    name: str
    account_id: str
    starting_balance: float
    api_base: str = "https://developers.myfundedperpetuals.com/v1"
    api_key_env: str = "PERPS_API_KEY"
    taker_fee_bps: float = 3.0
    market_id: str = "binance|BTCUSDT"
    signal: Optional[SignalConfig] = None
    policy: Optional[RiskPolicy] = None

    @property
    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(f"Variable d'environnement {self.api_key_env} absente (.env)")
        return key

    @property
    def risk_policy(self) -> RiskPolicy:
        return self.policy or RiskPolicy()

    @property
    def signal_config(self) -> SignalConfig:
        return self.signal or SignalConfig()


ACCOUNTS = {
    # Compte d'apprentissage - sert a valider le moteur sur des donnees live.
    "perps_learn": AccountProfile(
        name="perps_learn",
        account_id="VOTRE_ID_DE_COMPTE",   # a remplacer par l'id de votre compte
        starting_balance=10_000.0,
    ),

    # Gabarit pour le prochain compte. Decommenter et remplir.
    # "perps_main": AccountProfile(
    #     name="perps_main",
    #     account_id="...",
    #     starting_balance=50_000.0,
    #     policy=RiskPolicy(risk_pct_of_balance=0.0075, max_trades_24h=2),
    # ),
}

ACTIVE_ACCOUNT = "perps_learn"




BASKET = DEFAULT_BASKET

