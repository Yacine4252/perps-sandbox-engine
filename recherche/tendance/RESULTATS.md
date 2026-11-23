# Suivi de tendance à horizon long (22 nov 2026)

Demande de Claude app, go de Yc. Script : `tendance_long.py`. Lecture de données publiques, aucun ordre, moteur non modifié. Le détail par année est dans `resultats/tendance_long.txt` et `resultats/tendance_long.json`.

**Données**
- BTCUSDT spot Binance, bougies journalières et 4 h, du 1er janv 2019 au 21 nov 2026.
- Or : contrat GC=F journalier (yfinance, installé avec l'accord de Yc), du 2 janv 2019 au 20 nov 2026.

**Systèmes, sans rien optimiser**
- Cassure de Donchian 20/10 et 55/20 (style Turtle).
- Momentum de série temporelle sur 1, 4 et 12 semaines, évalué chaque semaine.
- Stop à 2 ou 3 ATR(20). Entrées et sorties à l'ouverture suivante.
- Frais par côté : BTC 3 points de base, or 0,5. Portage du sandbox compté, dans les deux sens.
- Risque de 0,5 % par trade.
- Compte à drawdown statique de 3 % : il meurt à −6 R cumulés depuis son départ.

## Résumé (espérance par trade, profit factor, drawdown max, plus longue série de pertes)

| Marché | Système | Stop | Trades | Espérance | PF | DD max | Pertes de suite | Compte à 3 % |
|---|---|---|---|---|---|---|---|---|
| BTC 1 j | Donchian 55/20 | 3 ATR | 39 | +1,17 R | 3,74 | 4,0 R | 5 | survit, quelle que soit l'année de départ |
| BTC 1 j | Momentum 4 sem. | 3 ATR | 95 | +1,02 R | 4,11 | 4,5 R | 6 | survit, quelle que soit l'année de départ |
| BTC 1 j | Donchian 20/10 | 3 ATR | 78 | +0,87 R | 3,13 | 5,9 R | 6 | survit, quelle que soit l'année de départ |
| BTC 1 j | Momentum 1 sem. | 3 ATR | 211 | +0,11 R | 1,35 | 5,5 R | 10 | survit |
| BTC 4 h | Momentum 4 sem. | 3 ATR | 127 | +2,26 R | 4,29 | 8,1 R | 9 | survit |
| BTC 4 h | Donchian 55/20 | 3 ATR | 219 | +0,48 R | 1,87 | 17,6 R | 6 | meurt si départ en 2025 |
| BTC 4 h | Momentum 1 sem. | 2 ATR | 261 | +0,32 R | 1,39 | 24,3 R | 16 | meurt (4 années de départ sur 8) |
| Or 1 j | Momentum 12 sem. | 3 ATR | 40 | +0,44 R | 1,87 | 10,2 R | 6 | survit de justesse (plus bas −6,0 R) |
| Or 1 j | Donchian 55/20 | 2 ATR | 27 | +0,58 R | 1,86 | 11,9 R | 6 | meurt depuis 2019 (−10,8 R) |
| Or 1 j | Donchian 20/10 | 2 ATR | 60 | +0,04 R | 1,07 | 17,2 R | 9 | meurt |
| Or 1 j | Momentum 1 et 4 sem. | 2-3 ATR | 98-203 | ≈ 0 R | 0,96-1,07 | 12-26 R | 9-11 | meurt |

## Lecture

- **BTC en journalier** : les deux systèmes classiques gagnent sur 8 ans, avec de petits drawdowns (4 à 6 R avec un stop à 3 ATR). Un compte à 3 % de drawdown statique aurait survécu, quelle que soit l'année de départ. Les années faibles sont 2020, 2022, 2023 et 2025 (proches de zéro ou légèrement négatives). Une grosse partie du total vient de 2019 et 2021, deux années de forte hausse.
- **BTC en 4 h** : totaux plus gros, mais drawdowns de 8 à 28 R. Avec 3 % de drawdown statique, plusieurs départs meurent.
- **Or** : à peu près rien entre 2019 et 2023. Les gains viennent de quelques gros trades pendant la hausse de 2024 à 2026. La plupart des variantes auraient grillé un compte à 3 %.

## Limites

- Peu de trades en journalier (39 à 100 en 8 ans) : les chiffres par année sont très bruités.
- Pas de glissement.
- Le portage est celui du sandbox, pas le funding réel de Binance.
- GC=F est le contrat à terme continu, non ajusté. Les changements d'échéance créent des sauts de prix, qui peuvent fausser quelques cassures et quelques stops.
- Rien n'a été optimisé, mais 10 variantes ont été regardées. La meilleure est forcément un peu flattée.

## Lancement en vrai, 22 nov 2026 (go de Yc)

Yc : « tu as 10 comptes, trouve la meilleure manière ». Les 5 variantes de l'or les plus faibles des études (objectif 1,5 R, moitié fermée à +1 R, sortie à +0,5 R, entrée confirmée, stop suiveur) laissent leur compte à un vrai système différent. Le stop suiveur (S8) garde l'or tant que sa position est ouverte.

Moteur : `perps_engine/tendance_lente.py`, lancé par `lancer_tendance.py <dossier>` (tâches PerpsS5, PerpsS6, PerpsS7, PerpsS9). Mêmes règles que `tendance_long.py` : un test exige les mêmes trades sur les 8 ans, pour les 5 systèmes, en 1 j et en 4 h. Fiche `tendance.json` dans chaque dossier ; l'ancienne fiche de l'or est gardée sous `strategie-or-archive.json`.

| Compte | Système | Stop | Risque par trade |
|---|---|---|---|
| S5 FP-00000001 | Momentum 4 semaines, journalier | 3 ATR | 0,5 % (500 $) |
| S6 FP-00000002 | Cassure 20 jours, sortie 10 jours | 3 ATR | 0,5 % |
| S7 FP-00000003 | Momentum 4 semaines, ATR en 4 h | 3 ATR | 0,3 % (300 $) |
| S9 FP-00000004 | Cassure 55 jours, sortie 20 jours | 3 ATR | 0,5 % |

- Décision une fois par bougie terminée (minuit UTC en journalier, toutes les 4 h pour S7), stop posé sur la plateforme à l'entrée, pas d'objectif.
- Au premier démarrage (22 nov, 16:21-16:22 UTC), les 4 systèmes étaient vendeurs sur le BTC depuis fin octobre ou début novembre. Les 4 comptes ont vendu vers 61 100-61 300 $. Stop à 70 500-70 700 $ (15 %) en journalier, à 65 926 $ (7,5 %) pour S7.
- À juger sur des mois : 5 à 12 trades par an et par système.

Même jour : risque de Sandbox 1 passé de 1 % à 0,3 % par trade (`RISQUE_SANDBOX_1`), redémarrage par HALT-SANDBOX, sans redémarrer le PC.

## L'or en 4 h et en journalier, avec plus de sources (23 nov 2026)

Demande de Claude app, accord de Yc. Script : `tendance_or.py` ; détail dans `resultats/tendance_or.txt` et `.json`. Prix publics seulement. Le moteur, les paramètres et le .env ne sont pas touchés. Rien n'est réglé sur les trades du sandbox : mêmes 5 systèmes, mêmes stops, même simulation que `tendance_long.py`. Rien de nouveau à installer : yfinance 1.8.0 est dans le .venv depuis le 21 nov.

**Données de l'or**

| Source | Période |
|---|---|
| PAXGUSDT spot Binance (jeton adossé à l'or, coté 24 h/24 comme le sandbox), 1 j et 4 h | 28 août 2020 → 22 nov 2026 (6,2 ans) |
| GC=F en 1 h, regroupé en 4 h (yfinance ne donne que 730 jours en 1 h) | juillet 2024 → nov 2026 (2,4 ans) |
| XAUUSDT perpétuel Binance, 4 h : l'or du sandbox | 11 déc 2025 → nov 2026 (11 mois), contrôle seulement |

**Résultats, meilleures variantes**

| Données | Système | Stop | Trades | Espérance | PF | DD max | Années 2020-2023 | Années 2024-2026 |
|---|---|---|---|---|---|---|---|---|
| PAXG 4 h | Momentum 4 sem. | 3 ATR | 107 | +0,38 R | 1,53 | 25,5 R | toutes négatives | +0,5 à +2,6 R/trade |
| PAXG 4 h | Momentum 12 sem. | 3 ATR | 66 | +0,30 R | 1,40 | 18,3 R | toutes négatives | positives |
| PAXG 4 h | Donchian 20 ou 55, momentum 1 sem. | 2-3 ATR | 161-349 | −0,03 à +0,09 R | 0,95-1,16 | 17-53 R | proches de zéro ou négatives | faibles |
| PAXG 1 j | Momentum 12 sem. | 3 ATR | 39 | +0,42 R | 2,05 | 6,6 R | toutes négatives | positives |
| PAXG 1 j | Donchian 55/20 | 2 ATR | 28 | +0,75 R | 2,13 | 12,4 R | toutes négatives | positives |
| GC=F 4 h | Momentum 4 sem. | 2 ATR | 36 | +1,20 R | 2,41 | 9,4 R | pas de données | 2024 −0,25, puis positives |
| XAUUSDT 4 h | Momentum 4 sem. | 3 ATR | 15 | +0,90 R | 2,11 | 5,8 R | pas de données | 11 mois seulement |
| BTC 1 j (rappel) | Momentum 4 sem. | 3 ATR | 95 | +1,02 R | 4,11 | 4,5 R | positives sauf 2025 ≈ 0 | |

**Lecture**
- Sur l'or, la tendance n'a payé que pendant la grande hausse de 2024 à 2026. De 2020 à 2023, les meilleures variantes perdent chaque année, en 4 h comme en journalier ; les autres sont proches de zéro.
- Les bons chiffres de GC=F en 4 h et de XAUUSDT ne couvrent que cette hausse : c'est le piège.
- Drawdowns de 18 à 53 R en 4 h : un compte sandbox à 6 % (−12 R à 0,5 % de risque) aurait sauté. Seul le momentum 12 semaines en journalier (−6,6 R) aurait tenu, et il ne gagne que depuis 2024.
- Conclusion : pas assez solide pour mettre la tendance de l'or sur un compte. Le BTC reste le seul marché où la tendance lente tient sur toutes les périodes.

## ETH, SOL et Nasdaq 100 (23 nov 2026)

Demande de Yc : « Oui, teste-les ». Script : `tendance_marches.py` ; détail dans `resultats/tendance_marches.txt` et `.json`. Prix publics seulement. Mêmes 5 systèmes, mêmes stops, même simulation, rien n'est réglé. La plateforme propose ETH, SOL, et le Nasdaq sous XYZ100 et QQQ. Elle ne publie pas les frais de ces marchés : on prend ceux du BTC (3 points de base par côté, portage de 0,0276 % par jour).

**Données**
- ETHUSDT spot Binance : 1 j et 4 h, depuis janv 2019 (7,9 ans).
- SOLUSDT spot Binance : depuis août 2020 (6,3 ans).
- Nasdaq NQ=F (yfinance) : journalier depuis 2019 ; en 4 h sur 2,4 ans seulement.

**Résultats en journalier, stop 3 ATR**

| Marché | Système | Trades | Espérance | PF | DD max | Années négatives | Même sens que le BTC |
|---|---|---|---|---|---|---|---|
| ETH | Momentum 4 sem. | 83 | +0,72 R | 3,11 | 4,9 R | 2019, 2023 (petites) | 86 % du temps |
| ETH | Donchian 55/20 | 35 | +1,28 R | 4,96 | 3,9 R | 2019, 2022, 2023 (petites) | 97 % |
| SOL | Momentum 4 sem. | 66 | +1,07 R | 4,24 | 4,5 R | 2023 (petite) | 79 % |
| SOL | Donchian 55/20 | 29 | +1,91 R | 6,27 | 4,3 R | 2023, 2025 | 90 % |
| Nasdaq | Momentum 4 sem. | 86 | −0,16 R | 0,64 | 16,7 R | presque toutes | 65 % |
| Nasdaq | Donchian 55/20 | 30 | −0,15 R | 0,71 | 6,0 R | presque toutes | 84 % |

En 4 h, ETH et SOL gagnent aussi : momentum 4 semaines à +1,9 R sur ETH et +1,8 R sur SOL avec 3 ATR. Le Nasdaq perd avec les 10 variantes.

**Lecture**
- **ETH et SOL** : la tendance lente marche comme sur le BTC, gagnante presque chaque année, avec de petits drawdowns. Mais elle est dans le même sens que le BTC 80 à 97 % du temps. C'est le même pari, pas un marché de plus pour diversifier : si la crypto se retourne d'un coup, tout perd ensemble.
- **Nasdaq** : ça ne marche pas. Avant les coûts, le Donchian fait à peine +0,15 R par trade. Les ventes perdent dans un marché qui monte. Le portage, compté comme pour le BTC faute de chiffre officiel, mange le reste.
- **Pour le bilan du 29 nov** : seul candidat sérieux, un compte ETH ou SOL en tendance lente journalière, à la place d'une variante or perdante. À savoir avant : il doublerait le pari crypto. Il faut aussi un petit changement du moteur, qui ne connaît que BTC et or ; ce changement demande le go de Yc.
