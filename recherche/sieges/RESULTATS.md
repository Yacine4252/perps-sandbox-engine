# Rejeu du momentum v2.4 et vote des sièges (6 nov 2026)

Aucun ordre, aucune modification du moteur. Données publiques Binance.

## Ce qui a été fait

- Bougies Binance 1 min BTCUSDT et XAUUSDT du 11 déc 2025 au 6 nov 2026 (331 jours, aucun trou), avec le volume taker buy et l'historique du funding (`/fapi/v1/fundingRate`).
- Le signal est calculé par le code du moteur lui-même, minute par minute : `MomentumStrategy` et `SignalConfig`, importés tels quels.
- Les entrées (`update_arming`), le garde-fou (`evaluate_order`, `POLICY`) et les arrondis viennent aussi directement de `sandbox_run.py` et `risk.py`.
- Paramètres v2.4 inchangés :
  - entrée quand le z sur 60 min atteint 2, sortie quand il repasse sous 1 ;
  - volatilité mesurée sur 240 min ;
  - stop à 2 écarts-types, objectif à 2 R ;
  - plancher de coût : stop d'au moins 0,30 % ;
  - une position à la fois, 3 trades par 24 h, pause de 45 min après une perte.
- Frais réels : BTC 3 bps et or 0,5 bps par côté. R = distance d'entrée au stop. Frais en R = frais × (entrée + sortie) / R.
- Vérification contre le moteur réel, du 23 oct au 6 nov :
  - **Signaux** : 29 des 36 signaux BTC et 23 des 27 signaux or réellement émis se retrouvent dans le rejeu, même sens, à 5 min près.
  - **Valeur de z** : au même instant, l'écart médian est de 0,15 (BTC) et 0,12 (or).
  - **Origine des écarts** : prix milieu de la plateforme d'un côté, clôture Binance de l'autre.
  - **Entrées** : elles tombent souvent à la minute près et au même prix. Exemple : BTC vente du 26 oct à 79 883.

## 1. Rejeu fidèle (avec le garde-fou du moteur)

| | BTC | Or | Total |
|---|---|---|---|
| Trades | 488 | 299 | 787 |
| Par jour | 1,5 | 0,9 | 2,4 |
| Espérance avant frais | −0,01 R | +0,04 R | +0,01 R |
| Frais par trade | 0,10 R | 0,02 R | 0,07 R |
| **Espérance nette** | **−0,11 R** | **+0,02 R** | **−0,06 R** |
| Réussite | 28 % | 39 % | 32 % |
| Profit factor | 0,61 | 1,09 | 0,76 |
| Total | −52,8 R | +5,2 R | −47,6 R |
| Drawdown max | 60,7 R | 5,2 R | 56,0 R |
| Pertes consécutives max | 16 | 10 | 16 |

- 8 premiers mois : −0,06 R net (583 trades). 3 derniers mois : −0,07 R net (204 trades).
- **Sorties** : 95 % sur signal (durée médiane 27 min), 4 % à l'objectif, moins de 1 % au stop. Une sortie sur signal rapporte en médiane −0,1 à −0,2 R. Les rares objectifs à +2 R compensent à peu près, avant frais.
- **Avant la v2.4**, le bug de clôture empêchait les sorties sur signal : les trades du sandbox allaient au stop ou à l'objectif. Ces 17 trades ne représentent donc pas la v2.4.
- **Garde-fou** : sur 2 131 tentatives d'entrée, 1 344 ont été refusées, surtout par la limite de 3 trades par 24 h (1 274 fois). C'est elle qui fixe le rythme : environ 2,4 trades par jour.
- **Or** : son léger gain dans ce rejeu (+0,02 R) ne tient pas sur l'ensemble de ses signaux (−0,02 R, partie 2). Ce n'est pas un avantage fiable.

## 2. Tous les signaux (l'échantillon pour juger les sièges)

Chaque signal neuf devient un trade, sans garde-fou :
- **BTC** : 1 204 signaux, avant frais −0,02 R, net −0,10 R.
- **Or** : 927 signaux, avant frais 0,00 R, net −0,02 R.

## 3. Les sièges

Définitions, fixées avant de regarder les résultats :
- **Tendance 4 h** : EMA 20 contre EMA 50 des clôtures 4 h.
- **Structure H1** : sens de la dernière cassure (BOS ou CHoCH). Les sommets et creux sont confirmés par 3 bougies H1 de chaque côté.
- **Volatilité** : rang centile de l'ATR 14 H1 sur 30 jours. Pour au-dessus du 67e centile, contre sous le 33e.
- **Liquidité** : un niveau gêne le trade quand il se trouve entre l'entrée et l'objectif à 2 R. Les niveaux sont le plus haut et le plus bas de la veille, et ceux des dernières sessions Asie, Londres et New York, s'ils n'ont pas encore été traversés. Contre si un niveau gêne, pour si le chemin est libre.
- **Order flow : delta/CVD.** Delta = 2 × taker buy − volume. Pour si le CVD des 30 et des 60 dernières minutes va dans le sens du signal, contre si les deux divergent.
- **Order flow : absorption.** Contre si, sur 15 min, le delta est parmi les 20 % les plus forts de la semaine dans le sens du signal, et que le prix avance moins que ce que ce delta produit d'habitude.
  - Un prix qui n'avance pas du tout n'arrive jamais au moment d'un signal momentum : 0 cas sur 193 dans un premier essai.
- **Order flow : funding.** Contre si le dernier funding réglé est au-dessus du 90e centile des 30 derniers jours (et positif) pour un achat. Pour une vente : sous le 10e centile, et négatif.
- **Order flow (le 5e siège)** : contre si une des trois composantes s'oppose, pour si le delta/CVD est pour.

Méthode :
- Apprentissage sur 8 mois, du 11 déc au 10 août. Confirmation sur 3 mois, du 11 août au 6 nov, sans rien changer.
- **Écart** = espérance des trades « pour » moins celle des trades « contre », en R par trade. Pour l'absorption et le funding, qui ne votent que « contre », on compare à « pas d'objection ».
- **Test du hasard** : on décale les votes dans le temps des centaines de fois. Leur rythme est gardé, mais leur lien avec les trades est cassé.
- **p** = part des décalages qui font aussi bien que le vrai vote.
- **Retenu** si p < 5 % sur 8 mois. **Confirmé** si l'écart garde le même sens sur 3 mois, avec p < 5 %.

Écart net (p) — BTC :

| Siège | 8 mois | 3 mois |
|---|---|---|
| Tendance 4 h | −0,01 (0,78) | −0,01 (0,89) |
| Structure H1 | −0,02 (0,56) | +0,09 (0,12) |
| Volatilité | −0,05 (0,32) | −0,03 (0,75) |
| Liquidité | −0,02 (0,53) | +0,08 (0,26) |
| Delta/CVD | −0,09 (0,62) | +0,14 (0,63) |
| Absorption | +0,02 (0,73) | −0,13 (0,20) |
| Funding | −0,01 (0,80) | +0,03 (0,85) |
| Order flow | +0,01 (0,85) | −0,04 (0,68) |

Écart net (p) — Or :

| Siège | 8 mois | 3 mois |
|---|---|---|
| Tendance 4 h | +0,05 (0,24) | +0,06 (0,27) |
| Structure H1 | **+0,09 (0,02) retenu** | +0,01 (0,42) non confirmé |
| Volatilité | −0,08 (0,25) | +0,12 (0,24) |
| Liquidité | +0,04 (0,42) | +0,10 (0,14) |
| Delta/CVD | +0,09 (0,15) | +0,14 (0,11) |
| Absorption | −0,02 (0,88) | −0,05 (0,87) |
| Funding | +0,11 (0,16) | −0,12 (0,47) |
| Order flow | +0,08 (0,13) | +0,10 (0,24) |

**Verdict : aucun siège ne fait mieux que le hasard, ni sur le BTC ni sur l'or, ni avant ni après frais.**

- **Or, structure H1** : seul siège retenu sur 8 mois. Pour +0,03 R contre −0,07 R. Sur 3 mois : pour −0,03 R contre −0,04 R, écart nul.
- **Un retenu sur 16 tests**, c'est ce que le hasard donne à lui seul (0,8 attendu à 5 %).
- **Avant frais**, c'est pareil :
  - BTC volatilité ressort à l'envers sur 8 mois (les marchés calmes font mieux, −0,10 R, p = 0,03), non confirmé sur 3 mois (−0,10 R, p = 0,13) ;
  - or structure H1 : p = 0,02, non confirmé sur 3 mois.
- **Delta/CVD sur le BTC** : le CVD va presque toujours dans le sens du signal (816 trades sur 827). Le siège n'a rien à trier, car le signal momentum naît d'une poussée acheteuse ou vendeuse.
- **Plus proches d'un effet sur l'or** : delta/CVD et liquidité. Ils vont dans le même sens sur les deux périodes, mais restent trop faibles pour être distingués du hasard.
- **Sur les trades du rejeu fidèle**, aucun siège n'est significatif non plus : tous les p sont au-dessus de 0,16.
- **Filtre par la structure H1 sur l'or** : sur 8 mois, il garde des trades à +0,02 R, contre −0,01 R pour tous. Sur 3 mois : −0,03 R pour les deux.

## 4. La sortie : v2.4 contre avant la v2.4

Avant la v2.4, chaque sortie sur signal échouait (HTTP 400). La position restait donc jusqu'au stop ou à l'objectif. Les deux versions sont rejouées avec le même signal, les mêmes entrées et le même garde-fou ; seule la sortie change (`comparer_sorties.py`).

Le portage facturé par la plateforme est compté à part. Il est payé quel que soit le sens : 0,028 % du notionnel par jour sur le BTC, 0,015 % sur l'or, mesuré sur les 21 trades du sandbox.

| | v2.4 (sortie sur signal) | Avant v2.4 (stop ou objectif) |
|---|---|---|
| Trades | 787 | 540 |
| Par jour | 2,4 | 1,6 |
| Durée médiane | 27 min | 4 h 38 |
| Objectif / stop / signal | 4 / 0 / 95 % | 36 / 64 / 0 % |
| Avant frais | +0,01 R | +0,08 R |
| Frais par trade | 0,07 R | 0,06 R |
| **Net par trade** | **−0,06 R** | **+0,02 R** |
| Net, portage compris | −0,06 R | +0,01 R |
| Total net sur 331 jours | −47,6 R | +10,8 R |
| Drawdown max | 56 R | 34 R |

Total net en R (v2.4 / avant) :
- BTC −52,8 / −14,9 ; or +5,2 / +25,7.
- 8 premiers mois −34,0 / +14,8 ; 3 derniers mois −13,6 / −3,9.
- Or sur les 3 derniers mois : −1,1 / −7,3. Ici, c'est la version d'avant qui fait moins bien.
- La version d'avant fait mieux 7 mois sur 12.

Signal par signal, à entrées identiques (tous les signaux, écart = avant − v2.4) :
- **BTC** : +0,04 R par signal, p = 0,35. Pas significatif, ni sur 8 mois ni sur 3 mois.
- **Or** : +0,16 R par signal sur 8 mois (p = 0,001), mais −0,02 R sur les 3 derniers mois (p = 0,88). Non confirmé.
- L'erreur type est calculée par semaine, car des signaux proches dans le temps ne sont pas indépendants.

Lecture :
- **Pourquoi la version d'avant perd moins.** La sortie sur signal coupe le trade au bout d'environ 27 min, le plus souvent sur une petite perte. Les trades sont aussi plus courts, donc plus nombreux : la v2.4 paie 55 R de frais en 331 jours, contre 34 R.
- **Ce n'est pas un avantage solide pour autant.** Portage compris, la version d'avant tourne autour de +0,01 R par trade, avec un drawdown de 34 R. Son gain vient surtout de l'or sur les 8 premiers mois, et ne se répète pas sur les 3 derniers.
- **Vérification.** 9 des 17 trades réels d'avant la v2.4 sont retrouvés à l'identique : même entrée à la minute près, même sortie au stop ou à l'objectif. Les autres divergent à cause des coupures du PC et de la règle d'une position à la fois. Le taux d'objectifs atteints est le même : 35 % en réel, 36 % dans le rejeu.

### Suivi des vrais trades après leur sortie (`suivi_trades.py`)

Le moteur ne garde que la sortie réelle : quand il ferme sur signal, le stop et l'objectif partent avec la position. `suivi_trades.py` tourne à côté du moteur, sans le modifier. Il refait tout le suivi à chaque lancement, à partir du journal du moteur et des bougies Binance : aucun trade ne peut être oublié.

Pour chaque trade fermé, et pour chaque signal refusé par le garde-fou, il note :
- où le prix est allé après la sortie (15 min, 1 h, 4 h, 24 h), en R dans le sens du trade ;
- le meilleur et le pire mouvement dans les 24 h ;
- ce qu'aurait donné le trade tenu jusqu'au stop ou à l'objectif.

Il ne change rien au moteur : ses notes servent au bilan. Résumé dans `resultats/suivi_trades.txt`, détail dans `resultats/suivi_trades.csv`.

Depuis le 9 nov 2026, la tâche Windows « PerpsSuivi » le relance toute seule toutes les 15 min, sans fenêtre, dans la session de Yc :
- **Session fermée** : la tâche ne tourne pas, mais elle rattrape tout au lancement suivant.
- **Contrôle** : chaque lancement ajoute une ligne à `resultats/suivi_trades_lancements.log`.
- **Arrêt** : `schtasks /delete /tn PerpsSuivi /f`.

Au 9 nov 2026 à 16 h 49 UTC :
- **8 trades pris.**
  - Réel : +401 $, soit +0,06 R par trade.
  - Tenus jusqu'au stop ou à l'objectif : −2 425 $. Tenir fait mieux 2 fois et moins bien 6 fois.
  - La vente d'or du 6 nov, encore ouverte le 8 nov avec +725 $ latents, a touché son stop le 9 nov.
- **Après la sortie**, le prix revient en moyenne un peu contre le trade : −0,14 R à 1 h, −0,27 R à 4 h. La sortie sur signal a donc plus souvent protégé que coupé trop tôt.
- **5 signaux refusés**, tous par la limite de 3 trades par 24 h. Pris avec la sortie sur signal, ils auraient perdu environ 1 300 $.

Chaque trade est tenu seul : en réalité, une position gardée plus longtemps en bloque d'autres. Avec 8 trades, tout cela reste du bruit. On refera le point au bilan des 70 trades.

## 5. Table structurelle : stop et objectif sur la structure

Ton idée du 6 nov au soir : une table où chaque siège a un rôle, avec un RR de 3, 3 trades par jour, un stop sous le dernier plus bas plutôt qu'au milieu de l'OB, et un objectif sur la structure. Code : `structure_v1.py`, `tester_structure.py`.

Les rôles :
- **Guetteur** : repère le gros mouvement.
- **Stratège multi-unités** : la dernière cassure de structure en H4 et en H1 doit aller dans le sens du trade.
- **Architecte** : l'order block d'origine du mouvement, soit la dernière bougie opposée avant le plus bas (ou le plus haut). Entrée au bord de l'OB. Stop sous le plus bas du mouvement, plus 0,1 ATR. Objectif sur le premier niveau de liquidité à 3 R ou plus (4 R ou plus en session New York) : veille, sessions, sommets ou creux H1 non pris.
- **Trésorier** : taille = risque / stop.
- **Calendrier** : s'abstient, faute d'historique des annonces.

Les règles de jeu :
- un ordre ou une position à la fois ;
- 3 trades par 24 h, dont un gardé pour New York ;
- pause de 45 min après une perte ;
- ordre limite annulé après 4 h, ou si l'objectif est atteint sans retour sur l'OB ;
- un stop touché dans la minute du remplissage compte comme une perte ;
- position fermée après 24 h.

**Version 1** : déclencheur momentum du moteur, OB 5 min, plancher de coût du moteur (stop ≥ 0,30 %). **14 trades en 331 jours.** Inexploitable. Sur 2 131 signaux :
- 1 401 avaient une structure H4/H1 pas alignée ;
- 631 avaient un stop sous le plancher de coût ;
- sur les 66 ordres posés, 37 ont vu le prix filer à l'objectif sans revenir sur l'OB.

**Version 2** : déclencheur = n'importe quelle cassure de structure 15 min, OB 15 min, plancher de coût calculé avec les vrais frais (BTC 0,30 %, or 0,05 %). Ces changements ont été décidés d'après les comptages de la version 1, pas d'après ses gains.

| | Total | BTC | Or |
|---|---|---|---|
| Trades | 162 (0,5 par jour) | 53 | 109 |
| Réussite | 20 % | 28 % | 17 % |
| Objectif visé en moyenne | 5,8 R | 5,1 R | 6,1 R |
| Net par trade | −0,07 R | +0,32 R | −0,27 R |
| 8 premiers mois | −0,01 R | +0,22 R | −0,14 R |
| 3 derniers mois | −0,30 R | +1,03 R (7 trades) | −0,65 R |

- **BTC** : positif, mais pas significatif (p = 0,30, 53 trades). Sur 8 mois seuls, p = 0,53 : il n'aurait pas été retenu.
- **Or** : les stops structurels sont très serrés (15 bps en médiane) et sautent 83 % du temps.
- **Session New York** : −0,34 R par trade sur 36 trades. Elle ne fait pas mieux que les autres.
- **Entonnoir** : sur 4 409 cassures, 3 005 n'étaient pas alignées H4/H1. Il restait 592 occasions, environ 1,8 par jour. Une seule à la fois peut être en cours, et l'OB doit être revisité : 162 seulement se remplissent.

**Verdict.** Un RR de 3 et 3 trades par jour ne se rejoignent pas avec ces règles. La structure alignée offre moins de 2 occasions par jour sur les deux marchés, et les trades pris perdent après frais. C'est cohérent avec le test d'AMD_Scalper : entrer au retour sur un OB ou un FVG, avec des remplissages honnêtes, ne donne pas d'avantage sur le BTC et l'or 1 min.

## 6. La meilleure équipe de sièges (1 530 combinaisons)

Code : `combiner.py`. Ce qui a été essayé :
- **3 bases** : momentum v2.4, momentum tenu jusqu'au stop ou à l'objectif, table structurelle v2.
- **2 marchés** : BTC et or.
- **Les 127 équipes possibles des 7 sièges, avec 2 règles** :
  - « veto » : on garde le trade si aucun siège de l'équipe ne s'y oppose ;
  - « unanime » : on le garde si tous les sièges de l'équipe sont pour.
- **Le cas sans filtre**, en plus.

Méthode :
- **Recherche sur 8 mois seulement.** Critère : le t de l'espérance nette, c'est-à-dire le gain moyen divisé par son erreur type. Il faut au moins 30 trades.
- **Confirmation sur 3 mois**, sans rien changer.
- **Test du hasard sur la recherche entière** : 200 recherches refaites avec des votes décalés dans le temps.

| | 8 mois | 3 mois |
|---|---|---|
| Meilleure combinaison : or, momentum tenu jusqu'au stop ou à l'objectif, structure H1 + funding (les deux pour) | 342 trades, +0,28 R (t = 3,5) | 143 trades, **−0,07 R** |
| Même base, sans filtre | 649 trades, +0,15 R | 278 trades, −0,05 R |

- **Les 10 meilleures sur 8 mois** sont toutes de la même famille : or, stop/objectif, avec le funding. Sur 3 mois, toutes sont nulles ou négatives.
- **Test du hasard** : avec des votes mélangés, la meilleure combinaison trouvée atteint t = 3,0 en médiane, et 3,8 une fois sur 20. La vraie (3,5) ne sort pas du lot : p = 0,21.
- **Sur 1 394 combinaisons jugeables**, 710 gagnent sur 8 mois. Parmi elles, 323 gagnent encore sur 3 mois, soit 45 %. Ce n'est pas mieux qu'à pile ou face.
- **Rejouée avec le garde-fou du moteur**, sur l'or seul :
  - 8 mois : +0,15 R par trade (218 trades, +32,9 R) ;
  - 3 mois : +0,05 R par trade (60 trades, +2,9 R, p = 0,40) ;
  - portage compris, +0,03 R sur 3 mois : pas distinguable de zéro.

**Verdict : aucune combinaison ne fait mieux que le hasard.** La « meilleure » est un accident des 8 premiers mois. Le funding de l'or a voté juste à cette période, puis à l'envers sur les 3 mois suivants (déjà vu en partie 3 : +0,11 R puis −0,12 R).

### Combinaison figée le 6 nov 2026, 18 h 28 UTC

La meilleure combinaison est figée telle quelle dans `candidat_fige.json` : règles, date du gel et empreintes du code.

Pour la revérifier sur des données neuves, lancer `verifier_candidat.py`. Il :
- ne compte que les trades ouverts après le gel ;
- avertit si le code a changé depuis.

**Revérifiée le 21 nov 2026** (bougies jusqu'au 21 nov 00:38 UTC, signaux recalculés) : depuis le gel, avec le garde-fou, 18 trades, +0,31 R par trade, +5,7 R au total (p = 0,07). Sur tous les signaux gardés : 25 trades, +13,5 R. Sur la même période, l'or tenu sans filtre fait +4,3 R. C'est encourageant, mais 18 trades ne suffisent pas, et la combinaison avait échoué sur les 3 mois de confirmation. À revérifier au bilan du 29 nov.

Rythme attendu : environ 0,7 trade par jour avec le garde-fou. Il faut une centaine de trades pour conclure, donc environ 5 mois. Au bilan des 70 trades v2.4, il n'y en aura qu'une vingtaine : c'est un premier coup d'œil, pas un verdict.

## 7. Amélioration à partir des données (20 nov 2026, v2.6)

Au point de mi-parcours (38 trades v2.4), Yc a levé la règle des 70 trades et demandé d'améliorer le moteur à partir des données.

**Installé dans le moteur (v2.6)**, avec son go :
- or seul : le BTC a perdu 1 682 $ sur 17 trades réels (−521 $ même avant frais) et −52,8 R dans le rejeu de 331 jours. Il reste surveillé : ses signaux sont notés (`MARCHE_EN_PAUSE`) et suivis par le carnet ;
- pas d'entrée de 30 min avant à 60 min après une annonce USD forte : les 3 signaux concernés depuis le 5 nov ont tous perdu. Les signaux bloqués sont notés (`VETO_CALENDRIER`) et suivis ;
- frais réels par marché dans la taille : or 0,5 point de base par côté, BTC 3.

**Étude des sorties et des règles de Yc** (`sorties_or.py`, proposées en partie par Muse, son agent) : or seul, signal et garde-fou du moteur, net de frais et de portage. Bougies figées dans `donnees_2026-11-21`, signaux recalculés dans `cache_2026-11-21`. Total en R :

| | 8 mois (déc–août) | 3 mois (août–5 nov) | Réel (5–21 nov) |
|---|---|---|---|
| Sortie rapide (actuelle) | −7,8 | −6,9 | +0,7 |
| Garder jusqu'au stop ou à l'objectif | **+16,0** | **+1,3** | **+4,3** |
| Hybride (perte coupée au signal, gain gardé avec stop à l'entrée) | +8,2 | −0,5 | −0,5 |
| Rapide + filtre VWAP du jour | −5,9 | −11,8 | +0,2 |
| Rapide + arrêt pour la journée après 2 pertes | −8,8 | −9,1 | −2,2 |

Signal par signal (tous les signaux, sans garde-fou), garder moins rapide : +0,15 R (p = 0,001) sur 8 mois, −0,01 R (p = 0,89) sur 3 mois, +0,05 R sur la période réelle. L'hybride est à environ 0 partout.

Règle de décision fixée avant le calcul (mieux sur les deux périodes, en total et signal par signal, ou pour un filtre : les signaux écartés perdent sur les deux) :
- l'hybride passe, de justesse : son avantage vient de l'ordre des trades, pas des signaux eux-mêmes ;
- garder ne passe pas, à cause d'une égalité signal par signal sur les 3 derniers mois. C'est pourtant la meilleure sortie du rejeu réaliste sur les trois périodes ;
- VWAP et arrêt après 2 pertes : rejetés.

**Activé le 20 nov 2026 (« Go garder » de Yc)** : `HOLD_TO_TARGET = ("GOLD",)`. L'or garde sa position jusqu'au stop ou à l'objectif, sans sortie sur signal.

Nuance sur les vrais trades d'or du 5 au 20 nov :
- les 21 trades pris ont fait +3 475 $ avec la sortie rapide ; tenus, ils auraient fait −2 116 $ ;
- les 25 signaux refusés auraient fait −2 735 $ avec la sortie rapide, et +4 817 $ tenus ;
- sur les 46 signaux, tenir fait +2 701 $ et la sortie rapide +740 $.

Pour chaque trade tenu, le carnet estime désormais aussi la sortie rapide, au prix de la fin du signal. Le bilan de l'étape 2, dimanche 29 nov 2026 au soir, comparera les deux sur les vrais trades de la v2.6.

**Gestion du stop, testée le 21 nov 2026** (`gestion_or.py`, demande de Yc via Muse ; historique seulement, moteur inchangé). Même méthode, base « tenir » ; le veto des news ne peut pas être rejoué. Total en R :

| | 8 mois | 3 mois | 15 jours |
|---|---|---|---|
| Tenir (v2.6) | **+16,0** | **+1,3** | +4,3 |
| Moitié fermée à +1 R, reste au stop ou à +2 R | +7,7 | +1,0 | +0,9 |
| Stop remonté à +0,5 R après +1 R | +13,7 | +1,2 | −4,8 |
| Sortie après 4 h sans +1 R | +19,0 | −4,5 | +8,0 |
| Sortie après 8 h sans +1 R | +20,1 | −3,9 | +5,3 |

Aucune ne bat « tenir » sur les deux périodes. Signal par signal, la sortie partielle et le stop à +0,5 R sont négatifs partout, et le time-stop fait à peu près jeu égal. La v2.6 reste telle quelle ; le time-stop à 8 h sera revérifié au bilan du 29 nov.

**Stratégie pour le compte Sandbox 2, testée le 21 nov 2026** (`strategie_or.py`, demande de Yc via Muse). Base « tenir », une seule chose change à la fois. Total en R :

| | 8 mois | 3 mois | 15 jours | 11,5 mois | Drawdown max |
|---|---|---|---|---|---|
| Tenir, objectif 2 R (v2.6) | +16,0 | +1,3 | +4,3 | +21,6 | 26,3 R |
| Objectif 3 R | +31,6 | −2,0 | +6,4 | +36,0 | 25,1 R |
| Objectif 1,5 R (en partie faussé : le garde-fou refuse un objectif sous 1,5 R) | +28,6 | −10,8 | +0,4 | +18,2 | 15,1 R |
| Entrée au-delà de 2,5 écarts-types | −2,2 | −22,1 | −0,5 | −24,9 | 68,0 R |
| Entrées 07:00-17:00 UTC seulement | −17,2 | −1,8 | +7,7 | −11,3 | 22,7 R |
| Sortie après 8 h sans +1 R | +20,1 | −3,9 | +5,3 | +21,5 | 20,8 R |

Aucune ne bat « tenir » sur les deux périodes. Proposé pour Sandbox 2 : la même stratégie avec un objectif à 3 R, à 0,2 % de risque par trade, pour la comparer en vrai à la v2.6. Point de vigilance : avec 1 % de risque, le drawdown historique (environ 25 R) dépasse de loin le plancher de −6 % du compte.

**Duel d'idées, 21 nov 2026** (`duel_or.py`, idées de Muse). Même base « tenir ». Total en R :

| | 8 mois | 3 mois | 15 jours | 11,5 mois | Drawdown max |
|---|---|---|---|---|---|
| Tenir, objectif 2 R (v2.6) | +16,0 | +1,3 | +4,3 | +21,6 | 26,3 R |
| Tendance 4H (moyenne mobile 50 des clôtures 4 h) | +36,5 | −2,3 | +14,4 | +48,6 | 13,4 R |
| Entrée confirmée à 15 min (z encore au-delà de 1,5) | +17,0 | −3,1 | −4,4 | +9,4 | 22,4 R |
| Trailing 1,5 ATR 1 h après +1 R | +3,8 | +2,7 | +0,1 | +6,7 | 23,3 R |

Aucune ne passe la règle. La tendance 4H a le meilleur total et le plus petit drawdown : elle est proposée pour le compte Sandbox 2, à 0,3 % de risque par trade. C'est la meilleure de 9 idées essayées sur les mêmes données : à confirmer en vrai.

**Combo tendance 4H + objectif 3 R et objectif 1,5 R refait proprement** (`duel_or.py`, 22 nov 2026). Total en R :

| | 8 mois | 3 mois | 15 jours | 11,5 mois | Drawdown max |
|---|---|---|---|---|---|
| Tendance 4H + objectif 3 R | +59,8 | −2,1 | +15,4 | +73,1 | 14,8 R |
| Objectif 1,5 R (arrondi corrigé) | +20,0 | −10,7 | +2,3 | +11,6 | 15,0 R |

**Lancement en vrai, 22 nov 2026 à 00:44 UTC (v2.7, go de Yc)** : une stratégie par compte sandbox, sur 10 comptes.
- Sandbox 1 reste en v2.6.
- Les comptes S2 à S10 testent : tendance 4H, objectif 3 R, sortie à 8 h, moitié fermée à +1 R, sortie à +0,5 R après +1 R, entrée confirmée à 15 min, stop suiveur ATR, objectif 1,5 R, et tendance 4H + 3 R.
- Risque de 0,2 à 0,3 % par trade.
- Fiches dans `comptes/S*/strategie.json` ; point des comptes avec `point_comptes.py`.
- À 1 ou 2 trades par jour et par compte, il faudra des centaines de trades pour départager les variantes.

## Limites

- Environ 1 signal sur 5 diffère du moteur réel, à cause de la source de prix.
- Résolution 1 min : si une bougie touche le stop et l'objectif, on compte le stop. Avec 95 % de sorties sur signal, cela ne pèse presque rien.
- Le portage (carry) de la plateforme n'est pas compté : environ 0,001 R pour une tenue de 27 min.
- L'equity du compte reste fixe : le plancher du compte sandbox (−6 %) n'est pas simulé.

## Fichiers

- `telecharger_1m.py` : bougies 1 min et funding. Un verrou par fichier empêche deux lancements simultanés d'écrire en double.
- `signaux_moteur.py` : le code du moteur rejoué minute par minute, résultat mis en cache.
- `rejeu.py` : rejeu fidèle et rejeu de tous les signaux.
- `sieges.py` : les votes.
- `analyse.py` : les tableaux.
- `verifier_live.py` : comparaison avec les signaux réels du moteur.
- `comparer_sorties.py` : v2.4 contre « stop ou objectif seulement ».
- `suivi_trades.py` : le suivi de chaque vrai trade du moteur et de chaque signal refusé, après la sortie. Lancer `suivi_trades.py donnees` ; il met lui-même les bougies à jour.
- `structure_v1.py` et `tester_structure.py` : la table structurelle, versions 1 et 2 (`tester_structure.py donnees cache resultats v2`).
- `combiner.py` : la recherche des 1 530 combinaisons. Résultats dans `resultats/combinaisons.csv` (chaque combinaison sur 8 mois et sur 3 mois) et `resultats/combiner.json`.
- `tests/` : 55 tests sur des bougies fabriquées.
- `resultats/` :
  - un CSV par rejeu, où chaque trade porte les votes des sièges ;
  - `rejeu_fidele_avant_v24_trades.csv` ;
  - `resume.json` et `comparaison_sorties.json`.
- Relancer, depuis `C:\dev\perps-engine\recherche\sieges` avec le Python du venv du moteur :
  - `signaux_moteur.py donnees cache BTCUSDT XAUUSDT` ;
  - puis `analyse.py donnees cache resultats` ;
  - puis `comparer_sorties.py donnees cache resultats`.

**22 nov 2026, 16:21 UTC (go de Yc : « trouve la meilleure manière »)** : les variantes de l'or les plus faibles des tableaux ci-dessus s'arrêtent après un trade chacune (un stop, comme tous les comptes ce matin). S5 (moitié à +1 R), S6 (sortie à +0,5 R), S7 (entrée confirmée) et S9 (objectif 1,5 R) passent au suivi de tendance lent sur le BTC (`recherche/tendance/RESULTATS.md`). Restent sur l'or : S1 (v2.6, risque ramené à 0,3 %), S2 (tendance 4H), S3 (objectif 3 R), S4 (sortie à 8 h), S10 (tendance 4H + 3 R), et S8 (stop suiveur) tant que sa position est ouverte.

## 8. Cassure du range d'ouverture (23 nov 2026)

Idée de Muse pour le trader IA : rester dehors en Asie, trader à Londres et à New York la cassure du range d'ouverture avec confirmation. C'est une règle mécanique : on l'a testée seule, sans IA, sur les bougies 1 min déjà téléchargées (or XAUUSDT du 11 déc 2025 au 23 nov 2026, BTC du 1er nov 2025 au 23 nov 2026). Script : `cassure_ouverture.py donnees resultats`.

La grille a été fixée avant de lancer, puis plus rien n'a été réglé :
- Séances (heure locale, changements d'heure compris) : Asie 18 h New York (pour comparer), Londres 8 h, New York 8 h 30 et 9 h 30.
- Range : 15, 30 ou 60 premières minutes.
- Confirmation : première bougie 5 min qui clôture hors du range, dans les 3 h. Un trade par séance et par marché.
- Stop : de l'autre côté du range, ou au milieu.
- Objectif : 1 R, 2 R, ou aucun. Sortie forcée 6 h après l'ouverture.
- Bornes du plan : trade refusé si le stop est à moins de 0,15 % ou à plus de 3 %.
- Coûts : frais du sandbox plus 1 point de base de glissement par côté.
- Pas de filtre news : il n'existe pas d'archive historique du calendrier.

Résultats, en espérance par trade (1 R = 300 $ à 0,3 % de risque sur 100 000 $) :

| Marché, séance | Meilleure des 18 variantes | Pire | Lecture |
|---|---|---|---|
| Or, Londres | −0,08 R | −0,56 R | toutes perdent |
| Or, New York 8 h 30 | −0,05 R | −0,34 R | toutes perdent |
| Or, New York 9 h 30 | −0,02 R | −0,22 R | toutes perdent |
| Or, Asie | +0,08 R (une seule) | −0,17 R | 17 sur 18 perdent |
| BTC, Londres | +0,07 R | −0,25 R | stop de l'autre côté et sans objectif : +0,05 à +0,07 R |
| BTC, New York 9 h 30 | +0,04 R | −0,14 R | autour de zéro |
| BTC, New York 8 h 30 | +0,03 R | −0,34 R | presque toutes perdent |
| BTC, Asie | −0,20 R | −0,40 R | toutes perdent nettement |

Le sens inverse (vendre la cassure haute) perd aussi, le plus souvent : le prix ne choisit pas mieux un côté que l'autre, et les frais font la différence.

**Conclusion.** Seule, la cassure du range d'ouverture n'a pas d'avantage sur l'or et presque pas sur le BTC, comme le momentum, les filtres et l'AMD scalper avant elle. La seule trace positive (BTC à Londres sans objectif, environ +14 à +22 $ par trade) est petite, et elle sort de 144 variantes essayées : à ne pas prendre pour un avantage sans la revoir sur d'autres années.

Pour le trader IA :
- **Asie dehors : confirmé.** C'est la pire séance, surtout sur le BTC.
- **La cassure peut servir de cadre**, mais une IA qui la suivrait à la lettre perdrait les frais. Son jugement doit apporter autre chose. On le mesurera en comparant chaque trade de l'IA à la cassure mécanique de la même séance.
