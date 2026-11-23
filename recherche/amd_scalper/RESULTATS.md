# Backtest AMD_Scalper_v1 sur bougies Binance 5 min (6 nov 2026)

- Période : 11 déc 2025 → 6 nov 2026, 330 jours, environ 95 000 bougies par marché. Aucun ordre, données publiques.
- Config : référence du 19 août (MNQ1! M5) : SL max 0,5 ATR, time-stop 120 min, FVG valable 30 min, R:R 2 à 4, contact à la mèche, entrée au bord proximal, stop le plus serré + 4 ticks (= 0,05 ATR).
- Frais réels de la plateforme sandbox : BTC 3 bps par côté, or 0,5 bps par côté.
- Lecture A : compression 2 bougies, ATR 3 bougies (champs du script). Lecture B : compression 5 bougies, ATR 14 bougies (brief).
- Mode fidèle : le script tel quel. Il peut entrer sur la bougie de naissance du FVG, n'applique jamais le stop dans la bougie d'entrée et compte un time-stop à 0 R.
- Mode réaliste : l'ordre limite est posé à la clôture de la bougie de naissance, un stop touché dans la bougie d'entrée compte comme une perte, et le time-stop sort au prix de clôture.

Référence MNQ (TradingView, 36 jours) : 124 trades, 3,4 par jour, 1,02 R, PF 3,09, drawdown 6,25 R, réussite 52,85 %.

## Espérance nette par trade

| Mode | Or A | Or B | BTC A | BTC B |
|---|---|---|---|---|
| Fidèle | +0,24 R (1 417 trades) | +0,12 R (290) | −0,13 R (1 758) | −0,26 R (388) |
| Réaliste | −0,88 R (933) | −0,78 R (215) | −1,18 R (1 078) | −1,42 R (249) |

Avant frais, en mode réaliste : or −0,16 R (A) et −0,09 R (B) ; BTC −0,04 R (A) et −0,29 R (B).

En mode réaliste, les 12 mois sont négatifs, sur les deux marchés et avec les deux lectures. Les sessions aussi : Asie, Londres, New York et hors session.

Frais moyens par trade : 0,7 R sur l'or et 1,1 R sur le BTC. Le stop médian vaut 4 bps sur l'or et 7 bps sur le BTC.

## D'où vient l'écart (lecture A, avant frais)

| Étape | Or | BTC |
|---|---|---|
| Script tel quel | +0,91 R | +0,94 R |
| + pas de remplissage sur la bougie de naissance | +0,24 R | +0,29 R |
| + bougie d'entrée honnête | −0,16 R | −0,04 R |
| + time-stop au prix | −0,16 R | −0,04 R |

En mode fidèle, 100 % des trades sont remplis au plus bas (ou au plus haut) de la bougie qui crée le FVG. Or ce FVG n'existe qu'à la clôture de cette bougie.

Une bougie d'entrée honnête change deux choses :
- une bougie qui traverse tout le FVG remplit l'ordre, au lieu d'effacer le FVG sans trade ;
- le stop compte s'il est touché dans la bougie d'entrée.

En réaliste, 35 % (BTC) à 44 % (or) des trades sont stoppés dans la bougie même où l'ordre se remplit.

## Fichiers

- `amd_v1.py` : portage du script.
- `backtest.py` : lanceur.
- `tests/` : 10 tests sur des bougies fabriquées.
- `resultats/` : un CSV de trades par variante, pour vérifier sur TradingView, et `resume.json`.
- Relancer depuis ce dossier : `python -I backtest.py donnees resultats`.
- Mettre les bougies à jour : `python -I telecharger.py donnees BTCUSDT XAUUSDT`.
