# Bilan des 38 trades v2.4 (5 au 21 nov 2026)

Lecture seule de `sandbox-trades.log` et de `sandbox-table.log` ; le moteur n'a pas été touché par ce bilan. Script : `bilan.py`. Détail trade par trade : `trades.csv`, avec la date, le marché, le sens, l'entrée, la sortie, la raison de sortie, la durée, le brut, les frais, le portage, le net et le résultat en R.

Période : du démarrage de la v2.4 (5 nov 2026, 20:19 UTC) au démarrage de la v2.6 (21 nov 2026, 01:07 UTC). Le premier trade, une vente d'or ouverte le 5 nov à 05:47, a été fermé par la v2.4 à son démarrage : il est compté dans les 38.

R = net ÷ (taille × distance de l'entrée au stop). Le risque visé est de 1 000 $ par trade, frais compris.

## Trades v2.4 et v2.5 (sortie rapide sur signal)

| | Or | BTC | Total |
|---|---|---|---|
| Trades | 21 | 17 | 38 |
| Net | **+3 475 $** | **−1 682 $** | **+1 793 $** |
| Brut | +3 863 $ | −504 $ | +3 359 $ |
| Frais | 361 $ | 1 161 $ | 1 522 $ |
| Frais / brut | 9 % | brut négatif | 45 % |
| Réussite | 57 % | 41 % | 50 % |
| Gain moyen | +0,52 R | +0,31 R | +0,44 R |
| Perte moyenne | −0,25 R | −0,39 R | −0,33 R |
| Profit factor | 2,76 | 0,55 | 1,31 |
| Drawdown max | 865 $ | 2 862 $ | 2 133 $ |
| Sorties | signal 18, objectif 2, stop 1 | signal 17 | signal 35, objectif 2, stop 1 |

Lecture : le BTC perd même avant frais, et ses frais (3 points de base par côté) pèsent plus que son brut. L'or gagne, avec des frais 6 fois plus bas (0,5 point de base par côté).

## Les 17 trades d'avant la v2.4 (à part)

À cause d'un bug de clôture, chaque sortie sur signal échouait : les positions allaient au stop ou à l'objectif.

| | Or | BTC | Total |
|---|---|---|---|
| Trades | 6 | 11 | 17 |
| Net | +2 190 $ | −3 358 $ | −1 168 $ |
| Brut | +2 366 $ | −2 072 $ | +294 $ |
| Frais | 113 $ | 1 213 $ | 1 326 $ |
| Réussite | 50 % | 27 % | 35 % |
| Gain moyen / perte moyenne | +1,98 / −1,05 R | +1,89 / −1,15 R | +1,94 / −1,12 R |
| Profit factor | 1,79 | 0,59 | 0,89 |
| Drawdown max | 2 764 $ | 6 181 $ | 7 928 $ |
| Sorties | stop 3, objectif 3 | stop 8, objectif 3 | stop 11, objectif 6 |

## Votes du calendrier (`python -m perps_engine.table`)

- 103 votes : 36 entrées prises, 67 refusées par le garde-fou. 20 trades fermés avant la table.
- Calendrier : pour 100, contre 3. Les 35 trades « pour » font +1 328 $ (+38 $ en moyenne). Le seul trade « contre » (BTC, 13 nov, Core PPI) a fait −583 $.
- Les deux autres « contre » (6 nov, emploi américain) étaient des signaux refusés ; s'ils avaient été pris, ils auraient perdu.
- Trop tôt pour juger : 1 trade « contre » sur les 10 nécessaires. Le calendrier est pourtant passé en veto le 20 nov (v2.6), à la demande de Yc.

## Après ce bilan

- v2.6 (21 nov, 01:07 UTC) : or seul, tenu jusqu'au stop ou à l'objectif, veto news, vrais frais. Un premier trade d'or : stop, −931 $.
- v2.7 (22 nov, 00:44 UTC, go de Yc) : une stratégie par compte, sur les 10 comptes sandbox. Voir `recherche/sieges/RESULTATS.md`, partie 7, et `point_comptes.py`.
