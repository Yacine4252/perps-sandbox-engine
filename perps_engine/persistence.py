"""
persistence.py - la serie de prix survit aux redemarrages.

Pourquoi ce module existe
-------------------------
Le 15 septembre, Windows a redemarre la machine pendant la nuit. Le moteur
tournait depuis dimanche. Les journaux ont survecu (ils sont sur disque),
mais la serie de prix etait en memoire : perdue. Trois heures de chauffe a
refaire, et l'echantillon interrompu.

Un moteur d'observation qui perd son etat a chaque redemarrage n'est pas un
moteur d'observation, c'est une demo. Sur une campagne de trois semaines
visant ~70 transitions, un seul reboot non rattrape coute plusieurs jours.

Ce que le module garantit
-------------------------
  - ecriture atomique : jamais de fichier a moitie ecrit, meme si la machine
    coupe pendant la sauvegarde (ecriture dans un .tmp, puis remplacement)
  - rejet des donnees perimees : une serie vieille de plusieurs heures ne
    doit pas etre rechargee comme si de rien n'etait, sinon le momentum se
    calcule sur un trou
  - degradation propre : fichier absent, corrompu ou illisible -> on
    repart a zero avec une chauffe normale, jamais un plantage
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Optional

STATE_FILE = "state.json"

# Au-dela de ce trou, la serie rechargee ferait mentir le momentum : un
# calcul sur 60 minutes qui enjambe une coupure de 2 h compare deux regimes
# sans rapport. Mieux vaut rechauffer.
MAX_GAP_MINUTES = 30


def save(path: str, strategies: dict, extra: Optional[dict] = None) -> bool:
    """
    Ecrit l'etat de toutes les strategies. Atomique : le fichier final
    n'existe qu'une fois complet.
    """
    payload = {
        "saved_at": time.time(),
        "version": "1.6",
        "markets": {},
    }
    for sym, strat in strategies.items():
        payload["markets"][sym] = {
            "direction": strat.direction,
            "points": [[t, p] for t, p in strat.buffer._pts],
        }
    if extra:
        payload.update(extra)

    try:
        d = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)      # atomique sur Windows comme sur POSIX
            return True
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except Exception:
        return False


def load(path: str) -> dict:
    """
    Relit l'etat. Rend toujours un dict exploitable :
      {"ok": bool, "reason": str, "age_minutes": float, "markets": {...}}

    Un etat trop vieux est REFUSE, pas recharge : une chauffe honnete vaut
    mieux qu'un momentum calcule par-dessus un trou.
    """
    if not os.path.exists(path):
        return {"ok": False, "reason": "aucun etat sauvegarde", "markets": {}}

    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"ok": False, "reason": "etat illisible ou corrompu",
                "markets": {}}

    saved_at = payload.get("saved_at")
    if not isinstance(saved_at, (int, float)):
        return {"ok": False, "reason": "horodatage absent", "markets": {}}

    age = (time.time() - saved_at) / 60
    if age > MAX_GAP_MINUTES:
        return {"ok": False, "age_minutes": round(age, 1),
                "reason": f"etat vieux de {age:.0f} min "
                          f"(max {MAX_GAP_MINUTES}) - rechauffage",
                "markets": {}}

    return {"ok": True, "age_minutes": round(age, 1),
            "reason": "etat repris", "markets": payload.get("markets", {})}


def restore(strategies: dict, state: dict) -> dict:
    """
    Reinjecte les points dans les strategies. Rend le detail de ce qui a
    ete repris, marche par marche, pour que le journal le dise.
    """
    if not state.get("ok"):
        return {"restored": [], "skipped": list(strategies)}

    restored, skipped = [], []
    for sym, strat in strategies.items():
        data = state["markets"].get(sym)
        if not data or not data.get("points"):
            skipped.append(sym)
            continue
        for t, p in data["points"]:
            strat.buffer.add(float(t), float(p))
        strat.direction = data.get("direction")
        restored.append({"sym": sym, "points": len(data["points"]),
                         "direction": data.get("direction")})
    return {"restored": restored, "skipped": skipped}
