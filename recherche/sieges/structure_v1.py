"""Table structurelle v1 : l'idee de Yc du 6 nov 2026, en recherche.
Aucun ordre, moteur non modifie.

Qui fait quoi
-------------
  guetteur    le momentum du moteur (z >= 2 sur 60 min, son propre code) signale
              un gros mouvement et son sens.
  stratege    multi-unites : la derniere cassure de structure en H4 ET en H1
              (BOS ou CHoCH, sommets/creux confirmes par 3 bougies) va dans le
              sens du mouvement.
  architecte  l'order block d'origine du mouvement : la derniere bougie 5 min
              opposee, a ou avant le plus bas (le plus haut pour une vente) des
              60 dernieres minutes. Entree au bord de l'OB (ouverture de cette
              bougie). Stop SOUS le plus bas du mouvement, plus 0,1 ATR 5 min :
              jamais au milieu de l'OB. Objectif : le premier niveau de liquidite
              (veille, sessions, sommets/creux H1 non pris, haut du mouvement)
              a 3 R ou plus, 4 R ou plus en session New York.
  tresorier   taille = risque / distance au stop (comme risk.py) : un stop plus
              large donne une position plus petite, la perte au stop reste 1 R.
  calendrier  pas d'historique des annonces : il s'abstient (comme en v2.5).

Garde-fou : un ordre ou une position a la fois sur le compte ; 3 trades par 24 h
dont un garde pour New York (au plus 2 hors de New York) ; pause de 45 min apres
une perte ; stop d'au moins 0,30 % (plancher de cout du moteur).

Execution honnete : l'ordre limite est pose a la cloture de la minute du signal
et se remplit si le prix revient. Un stop touche dans la minute du remplissage
compte comme une perte ; l'objectif n'est jamais compte dans cette minute-la.
Ordre annule apres 4 h, ou si le prix atteint l'objectif sans revenir. Position
fermee au marche apres 24 h.
"""
import os
import sys
from bisect import bisect_left
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

import rejeu  # noqa: E402
from donnees import (HEURE_MS, JOUR_MS, MIN_MS, agreger,  # noqa: E402
                     decalage_new_york, derniere_fermee)
from sieges import Contexte, atr_wilder, structure  # noqa: E402

M5_MS = 5 * MIN_MS
H4_MS = 4 * HEURE_MS


@dataclass
class Reglages:
    declencheur: str = "momentum"  # "momentum" (z >= 2 du moteur) ou "cassure 15 min"
    ob_min: int = 5                # unite de l'order block : 5 ou 15 min
    fenetre_min: int = 60          # le mouvement : la fenetre ou chercher son origine
    recul_ob: int = 6              # bougies a remonter pour trouver l'OB
    marge_atr: float = 0.1         # marge sous le plus bas, en ATR de l'unite de l'OB
    stop_min_pct: float = 0.30     # plancher de cout du moteur (5 x 0,06 %)
    plancher_frais_reels: bool = False   # True : 5 x l'aller-retour REEL du marche
    rr_min: float = 3.0
    rr_min_ny: float = 4.0
    validite_min: int = 240        # ordre limite annule apres 4 h
    tenue_max_min: int = 1440      # position fermee apres 24 h
    jours_pivots: int = 10         # sommets / creux H1 consideres comme cibles
    max_24h: int = 3
    max_24h_hors_ny: int = 2       # un trade garde pour New York
    pause_perte_min: int = 45
    multi_unites: bool = True


def session(T):
    """Session a l'instant T (heure de New York). Le week-end va du vendredi
    17 h au dimanche 19 h."""
    off = decalage_new_york(T)
    local = T + off
    jour = datetime.fromtimestamp(local / 1000, timezone.utc).weekday()
    h = (local % JOUR_MS) / HEURE_MS
    if (jour == 4 and h >= 17) or jour == 5 or (jour == 6 and h < 19):
        return "week-end"
    if 8 <= h < 17:
        return "New York"
    if 2 <= h < 8:
        return "Londres"
    if h >= 19 or h < 2:
        return "Asie"
    return "hors session"


def pivots(b, n, periode_ms):
    """Sommets et creux confirmes par n bougies de chaque cote : listes de
    (instant de confirmation, prix)."""
    hauts, bas = [], []
    for k in range(len(b.t)):
        p = k - n
        if p >= n:
            if b.h[p] > max(b.h[p - n:p]) and b.h[p] >= max(b.h[p + 1:k + 1]):
                hauts.append((b.t[k] + periode_ms, b.h[p]))
            if b.l[p] < min(b.l[p - n:p]) and b.l[p] <= min(b.l[p + 1:k + 1]):
                bas.append((b.t[k] + periode_ms, b.l[p]))
    return hauts, bas


def cassures(b, n=3):
    """Cassures de structure : (indice de la bougie, sens) a chaque cloture
    au-dela du dernier sommet (ou creux) pas encore casse, sommets et creux
    confirmes par n bougies de chaque cote (meme regle que sieges.structure)."""
    out = []
    sommet = creux = None
    sommet_casse = creux_casse = True
    for k in range(len(b.t)):
        c = b.c[k]
        if not sommet_casse and c > sommet:
            out.append((k, 1))
            sommet_casse = True
        if not creux_casse and c < creux:
            out.append((k, -1))
            creux_casse = True
        p = k - n
        if p >= n:
            if b.h[p] > max(b.h[p - n:p]) and b.h[p] >= max(b.h[p + 1:k + 1]):
                sommet, sommet_casse = b.h[p], False
            if b.l[p] < min(b.l[p - n:p]) and b.l[p] <= min(b.l[p + 1:k + 1]):
                creux, creux_casse = b.l[p], False
    return out


def trouver_ob(m5, k5, d, nb, recul):
    """L'order block d'origine du mouvement, sur les nb dernieres bougies 5 min
    fermees (k5 incluse). Rend (entree, extreme, debut du mouvement) ou None.
    Achat : extreme = plus bas du mouvement ; OB = derniere bougie baissiere a ou
    avant ce plus bas (au plus `recul` bougies avant) ; entree = son ouverture
    (haut du corps). Sans bougie baissiere : le haut du corps de la bougie du
    plus bas. Vente : l'inverse."""
    debut = k5 - nb + 1
    if debut < 0:
        return None
    if d == 1:
        j = min(range(debut, k5 + 1), key=lambda x: (m5.l[x], -x))
        ext = m5.l[j]
        for x in range(j, max(j - recul, 0) - 1, -1):
            if m5.c[x] < m5.o[x]:
                return m5.o[x], min(ext, m5.l[x]), m5.t[j]
        return max(m5.o[j], m5.c[j]), ext, m5.t[j]
    j = max(range(debut, k5 + 1), key=lambda x: (m5.h[x], x))
    ext = m5.h[j]
    for x in range(j, max(j - recul, 0) - 1, -1):
        if m5.c[x] > m5.o[x]:
            return m5.o[x], max(ext, m5.h[x]), m5.t[j]
    return min(m5.o[j], m5.c[j]), ext, m5.t[j]


def choisir_cible(niveaux, entree, risque, d, rr_min):
    """Premier niveau de liquidite a rr_min R ou plus dans le sens du trade :
    (nom, prix) ou None."""
    seuil = entree + d * rr_min * risque
    ok = [(n, p) for n, p in niveaux if (p - seuil) * d >= 0]
    if not ok:
        return None
    return min(ok, key=lambda x: x[1] * d)


class Terrain:
    """Un marche : bougies 1 min, signal du moteur, unites 5 min, 1 h et 4 h."""

    def __init__(self, m, b, R, ctx=None):
        self.m, self.R = m, R
        self.ctx = ctx or Contexte(b)
        self.unite_ms = R.ob_min * MIN_MS
        self.m5 = agreger(b, self.unite_ms)          # unite de l'order block
        self.atr5 = atr_wilder(self.m5, 14)
        self.etat_h1 = self.ctx.struct
        self.etat_h4, _ = structure(self.ctx.h4, 3)
        self.piv_hauts, self.piv_bas = pivots(self.ctx.h1, 3, HEURE_MS)
        self.evenements = self._evenements(b)

    def _evenements(self, b):
        """{indice de la bougie 1 min : sens} des declenchements."""
        m, ev = self.m, {}
        if self.R.declencheur == "momentum":
            for i in range(1, len(m.t)):
                if m.dir[i] != m.dir[i - 1] and m.dir[i] != 0 and m.vu[i]:
                    ev[i] = m.dir[i]
            return ev
        b15 = agreger(b, 15 * MIN_MS)
        for k, d in cassures(b15, 3):
            i = bisect_left(m.t, b15.t[k] + 15 * MIN_MS) - 1     # derniere minute de la bougie
            if i >= 0 and m.t[i] >= b15.t[k]:
                ev[i] = d
        return ev

    def niveaux(self, T, d, entree, t_mouvement):
        hauts, bas = self.ctx.niveaux(T)
        out = list(hauts if d == 1 else bas)
        piv = self.piv_hauts if d == 1 else self.piv_bas
        a = T - self.R.jours_pivots * JOUR_MS
        for tc, p in piv[bisect_left(piv, (a, float("-inf"))):]:
            if tc > T:
                break
            ext = self.ctx._extreme(tc, T, d == 1)
            if ext is None or (ext <= p if d == 1 else ext >= p):
                out.append(("sommet/creux H1", p))
        m = self.m
        j0, j1 = bisect_left(m.t, t_mouvement), bisect_left(m.t, T)
        if j1 > j0:
            out.append(("haut du mouvement" if d == 1 else "bas du mouvement",
                        max(m.h[j0:j1]) if d == 1 else min(m.l[j0:j1])))
        return [(n, p) for n, p in out if (p - entree) * d > 0]

    def preparer(self, i, d):
        """Le plan d'un trade pour un signal de sens d ne a la bougie i,
        ou (None, raison du refus)."""
        R, m = self.R, self.m
        T = m.t[i] + MIN_MS
        if R.multi_unites:
            k1 = derniere_fermee(self.ctx.h1, HEURE_MS, T)
            k4 = derniere_fermee(self.ctx.h4, H4_MS, T)
            if k1 < 0 or k4 < 0 or self.etat_h1[k1] != d or self.etat_h4[k4] != d:
                return None, "structure H4/H1 pas alignee"
        k5 = derniere_fermee(self.m5, self.unite_ms, T)
        if k5 < 0 or self.atr5[k5] is None:
            return None, "donnees"
        ob = trouver_ob(self.m5, k5, d, R.fenetre_min // R.ob_min, R.recul_ob)
        if ob is None:
            return None, "donnees"
        entree, ext, t_mvt = ob
        stop = ext - d * R.marge_atr * self.atr5[k5]
        risque = (entree - stop) * d
        if (m.c[i] - entree) * d <= 0 or risque <= 0:
            return None, "pas de recul possible"
        plancher = (5 * 2 * m.frais_bps / 1e4 if R.plancher_frais_reels else R.stop_min_pct / 100)
        if risque < plancher * entree:
            return None, "stop sous le plancher de cout"
        ny = session(T) == "New York"
        rr_min = R.rr_min_ny if ny else R.rr_min
        cible = choisir_cible(self.niveaux(T, d, entree, t_mvt), entree, risque, d, rr_min)
        if cible is None:
            return None, "pas de niveau a 3 R (4 R a New York)"
        return {"sym": m.sym, "d": d, "i_signal": i, "t_signal": T, "entree": entree,
                "stop": stop, "objectif": cible[1], "cible": cible[0],
                "rr": (cible[1] - entree) * d / risque, "ny": ny}, "ok"


def _touche(d, prix_limite, h, l):
    return l <= prix_limite if d == 1 else h >= prix_limite


def _cloture(pos, prix, T, motif, frais_bps):
    tr = rejeu.cloturer(pos, prix, T, motif, frais_bps)
    tr.update(pos.plan)
    tr["session"] = session(pos.t)
    return tr


def _remplir(m, i, plan, T):
    """Position ouverte par l'ordre limite dans la bougie i (deja touchee)."""
    d = plan["d"]
    prix = min(m.o[i], plan["entree"]) if d == 1 else max(m.o[i], plan["entree"])
    if (prix - plan["stop"]) * d <= 0:
        prix = plan["entree"]
    pos = rejeu.Position(plan["sym"], d, prix, plan["stop"], plan["objectif"],
                         abs(prix - plan["stop"]), i, T, None)
    pos.plan = {k: plan[k] for k in ("t_signal", "i_signal", "cible", "rr", "ny")}
    return pos


def suivre(m, i0, plan, R):
    """Vie d'un ordre seul, sans garde-fou : (trade ou None, motif)."""
    d, pos = plan["d"], None
    for i in range(i0 + 1, len(m.t)):
        T = m.t[i] + MIN_MS
        if pos is None:
            if T - plan["t_signal"] > R.validite_min * MIN_MS:
                return None, "ordre expire (4 h)"
            if _touche(d, plan["entree"], m.h[i], m.l[i]):
                pos = _remplir(m, i, plan, T)
                if _touche(d, pos.stop, m.h[i], m.l[i]):
                    sortie = min(m.o[i], pos.stop) if d == 1 else max(m.o[i], pos.stop)
                    return _cloture(pos, sortie, T, "sl", m.frais_bps), "rempli"
            elif _touche(-d, plan["objectif"], m.h[i], m.l[i]):
                return None, "objectif atteint sans retour"
        else:
            x = rejeu.sortie_plateforme(pos, m.o[i], m.h[i], m.l[i])
            if x:
                return _cloture(pos, x[1], T, x[0], m.frais_bps), "rempli"
            if T - pos.t >= R.tenue_max_min * MIN_MS:
                return _cloture(pos, m.c[i], T, "temps", m.frais_bps), "rempli"
    if pos is not None:
        return _cloture(pos, m.c[-1], m.t[-1] + MIN_MS, "fin", m.frais_bps), "rempli"
    return None, "fin des donnees"


def simuler_independant(t, debut_ms, R):
    """Chaque plan devient un ordre independant, sans garde-fou : l'echantillon
    complet pour juger des filtres. Rend (trades, entonnoir)."""
    m = t.m
    trades, entonnoir = [], Counter()
    for i in sorted(t.evenements):
        if m.t[i] + MIN_MS < debut_ms:
            continue
        entonnoir["signaux"] += 1
        plan, raison = t.preparer(i, t.evenements[i])
        if plan is None:
            entonnoir[raison] += 1
            continue
        entonnoir["plans"] += 1
        tr, motif = suivre(m, i, plan, R)
        entonnoir[motif] += 1
        if tr is not None:
            trades.append(tr)
    trades.sort(key=lambda x: x["t_entree"])
    return trades, entonnoir


def simuler(terrains, debut_ms, R, filtre=None):
    """Les marches ensemble, dans l'ordre du moteur (BTC puis GOLD).
    filtre(sym, i, sens, entree, objectif) -> True : veto des sieges.
    Rend (trades, entonnoir)."""
    par_sym = {t.m.sym: t for t in terrains}
    ptr = {t.m.sym: bisect_left(t.m.t, debut_ms) for t in terrains}
    temps = sorted({u for t in terrains for u in t.m.t[ptr[t.m.sym]:]})

    trades, entonnoir = [], Counter()
    ordre = pos = None
    ouvertures = deque()
    derniere_perte = None

    def fermer(prix, T, motif):
        nonlocal pos, derniere_perte
        tr = _cloture(pos, prix, T, motif, par_sym[pos.sym].m.frais_bps)
        trades.append(tr)
        if tr["r_net"] < 0:
            derniere_perte = T
        pos = None

    for u in temps:
        present = {}
        for t in terrains:
            s = t.m.sym
            p = ptr[s]
            if p < len(t.m.t) and t.m.t[p] == u:
                present[s] = p
                ptr[s] = p + 1
        T = u + MIN_MS

        # 1. position ouverte : stop / objectif poses chez la plateforme
        if pos is not None and pos.sym in present:
            m = par_sym[pos.sym].m
            i = present[pos.sym]
            if i > pos.i:
                x = rejeu.sortie_plateforme(pos, m.o[i], m.h[i], m.l[i])
                if x:
                    fermer(x[1], T, x[0])
            if pos is not None and T - pos.t >= R.tenue_max_min * MIN_MS:
                fermer(m.c[i], T, "temps")

        # 2. ordre limite en attente
        if ordre is not None and ordre["sym"] in present:
            m = par_sym[ordre["sym"]].m
            i = present[ordre["sym"]]
            d = ordre["d"]
            if T - ordre["t_signal"] > R.validite_min * MIN_MS:
                entonnoir["ordre expire (4 h)"] += 1
                ordre = None
            elif _touche(d, ordre["entree"], m.h[i], m.l[i]):
                pos = _remplir(m, i, ordre, T)
                ouvertures.append(T)
                entonnoir["rempli"] += 1
                ordre = None
                if _touche(d, pos.stop, m.h[i], m.l[i]):          # stop dans la minute
                    fermer(min(m.o[i], pos.stop) if d == 1 else max(m.o[i], pos.stop), T, "sl")
            elif _touche(-d, ordre["objectif"], m.h[i], m.l[i]):
                entonnoir["objectif atteint sans retour"] += 1
                ordre = None

        # 3. nouveaux signaux : la table delibere
        for t in terrains:
            s = t.m.sym
            if s not in present:
                continue
            i = present[s]
            d = t.evenements.get(i)
            if d is None or T < debut_ms:
                continue
            entonnoir["signaux"] += 1
            plan, raison = t.preparer(i, d)
            if plan is None:
                entonnoir[raison] += 1
                continue
            entonnoir["plans"] += 1
            if filtre is not None and filtre(s, i, d, plan["entree"], plan["objectif"]):
                entonnoir["veto des sieges"] += 1
                continue
            if pos is not None or ordre is not None:
                entonnoir["refus : deja un ordre ou une position"] += 1
                continue
            while ouvertures and ouvertures[0] < T - JOUR_MS:
                ouvertures.popleft()
            if len(ouvertures) >= R.max_24h:
                entonnoir["refus : 3 trades sur 24 h"] += 1
                continue
            if not plan["ny"] and len(ouvertures) >= R.max_24h_hors_ny:
                entonnoir["refus : place gardee pour New York"] += 1
                continue
            if derniere_perte is not None and T - derniere_perte < R.pause_perte_min * MIN_MS:
                entonnoir["refus : pause apres une perte"] += 1
                continue
            ordre = plan
            entonnoir["ordres poses"] += 1

    if pos is not None:
        m = par_sym[pos.sym].m
        fermer(m.c[-1], m.t[-1] + MIN_MS, "fin")
    return trades, entonnoir
