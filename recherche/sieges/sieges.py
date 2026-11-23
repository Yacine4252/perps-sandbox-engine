"""Les sieges qui votent sur chaque signal du momentum : POUR (+1), CONTRE (-1),
NEUTRE (0), ou None quand les donnees manquent (le siege s'abstient).

Chaque vote n'utilise que ce qui est connu a l'instant du signal T (cloture de la
bougie 1 min du signal) : bougies 1 h / 4 h / jour deja fermees, bougies 1 min
jusqu'a T, dernier funding deja regle.

Definitions (fixees avant de regarder les resultats) :
  tendance_4h   EMA 20 et EMA 50 des clotures 4 h : POUR si le signal va dans le
                sens EMA20 vs EMA50, CONTRE sinon.
  structure_h1  creux et sommets H1 (3 bougies de chaque cote). Une cloture H1
                au-dessus du dernier sommet non casse = cassure haussiere (BOS si
                la structure etait deja haussiere, CHoCH sinon), idem en baisse.
                POUR si le signal va dans le sens de la derniere cassure.
  volatilite    ATR 14 en H1, rang centile sur les 30 derniers jours (7 jours au
                moins). POUR au-dessus du 67e centile (marche agite : le papier cite
                par le moteur y trouve le momentum le plus fort), CONTRE sous le 33e.
  liquidite     niveaux : plus haut / plus bas de la veille (jour UTC) et des
                dernieres sessions Asie (19 h-2 h), Londres (2 h-8 h) et New York
                (8 h-17 h, heure de New York), pas encore traverses. CONTRE si un de
                ces niveaux se trouve entre l'entree et l'objectif a 2 R (obstacle
                sur le chemin), POUR si le chemin est libre.
  of_delta_cvd  delta = 2 x taker buy - volume, CVD = delta cumule. POUR si le CVD
                des 30 et des 60 dernieres minutes monte dans le sens du signal,
                CONTRE si les deux vont contre, NEUTRE si partage.
  of_absorption sur les 15 dernieres minutes : delta dans le sens du signal parmi
                les 20 % les plus forts des 7 derniers jours, ET prix qui avance
                MOINS que ce que ce delta produit d'habitude (pente des rendements
                15 min sur le delta 15 min, 7 derniers jours) : CONTRE. Sinon NEUTRE.
                (Un prix qui n'avance pas du tout n'arrive presque jamais au moment
                d'un signal momentum, qui nait justement d'une poussee du prix.)
  of_funding    dernier funding regle : au-dessus du 90e centile des 30 derniers
                jours (et positif) pour un achat, ou sous le 10e (et negatif) pour
                une vente : CONTRE. Sinon NEUTRE.
  order_flow    le 5e siege : CONTRE si une des trois composantes vote contre,
                POUR si le delta/CVD vote pour, NEUTRE sinon.
"""
import math
from bisect import bisect_left, bisect_right

from donnees import (HEURE_MS, JOUR_MS, MIN_MS, agreger, centile,
                     decalage_new_york, derniere_fermee)

POUR, CONTRE, NEUTRE, ABSENT = 1, -1, 0, None
SIEGES = ["tendance_4h", "structure_h1", "volatilite", "liquidite",
          "of_delta_cvd", "of_absorption", "of_funding", "order_flow"]
CONTRE_SEUL = {"of_absorption", "of_funding"}    # ne votent que CONTRE ou NEUTRE
SESSIONS_NY = {"Asie": (19, 7), "Londres": (2, 6), "New York": (8, 9)}   # debut h, duree h
H4_MS = 4 * HEURE_MS
SEPT_JOURS = 7 * 24 * 60


def ema(xs, n):
    out = [None] * len(xs)
    if len(xs) < n:
        return out
    a = 2.0 / (n + 1)
    v = sum(xs[:n]) / n
    out[n - 1] = v
    for k in range(n, len(xs)):
        v = a * xs[k] + (1 - a) * v
        out[k] = v
    return out


def atr_wilder(b, n=14):
    tr = []
    for k in range(len(b.t)):
        if k == 0:
            tr.append(b.h[k] - b.l[k])
        else:
            pc = b.c[k - 1]
            tr.append(max(b.h[k] - b.l[k], abs(b.h[k] - pc), abs(b.l[k] - pc)))
    out = [None] * len(tr)
    if len(tr) < n:
        return out
    v = sum(tr[:n]) / n
    out[n - 1] = v
    for k in range(n, len(tr)):
        v = (tr[k] + (n - 1) * v) / n
        out[k] = v
    return out


def structure(b, n=3):
    """Etat apres chaque bougie : +1 haussier, -1 baissier, 0 inconnu, et le type
    de la derniere cassure (BOS / CHoCH)."""
    etat, genre = [0] * len(b.t), [None] * len(b.t)
    sommet = creux = None
    sommet_casse = creux_casse = True
    cur, g = 0, None
    for k in range(len(b.t)):
        c = b.c[k]
        if not sommet_casse and c > sommet:
            g = "BOS" if cur == 1 else "CHoCH"
            cur, sommet_casse = 1, True
        if not creux_casse and c < creux:
            g = "BOS" if cur == -1 else "CHoCH"
            cur, creux_casse = -1, True
        p = k - n                    # pivot confirme par la bougie k
        if p >= n:
            hp, lp = b.h[p], b.l[p]
            if hp > max(b.h[p - n:p]) and hp >= max(b.h[p + 1:k + 1]):
                sommet, sommet_casse = hp, False
            if lp < min(b.l[p - n:p]) and lp <= min(b.l[p + 1:k + 1]):
                creux, creux_casse = lp, False
        etat[k], genre[k] = cur, g
    return etat, genre


def vote_sens(sens_marche, d):
    if sens_marche == 0:
        return NEUTRE
    return POUR if sens_marche == d else CONTRE


class Contexte:
    """Tout ce dont les sieges ont besoin pour un marche, calcule une fois."""

    def __init__(self, b, funding=None):
        self.b = b
        self.h1 = agreger(b, HEURE_MS)
        self.h4 = agreger(b, H4_MS)
        self.j1 = agreger(b, JOUR_MS)
        self.ema20 = ema(self.h4.c, 20)
        self.ema50 = ema(self.h4.c, 50)
        self.struct, self.struct_genre = structure(self.h1, 3)
        self.atr = atr_wilder(self.h1, 14)
        cvd, s = [], 0.0
        for i in range(len(b.t)):
            s += 2 * b.tb[i] - b.v[i]
            cvd.append(s)
        self.cvd = cvd
        self.d15 = [0.0] * 15 + [cvd[i] - cvd[i - 15] for i in range(15, len(cvd))]
        self.r15 = [0.0] * 15 + [math.log(b.c[i] / b.c[i - 15]) for i in range(15, len(cvd))]
        self.d15_abs = [abs(x) for x in self.d15]
        self.r15_abs = [abs(x) for x in self.r15]
        self.ft, self.fr = funding if funding else ([], [])

    # -- outils -----------------------------------------------------------------
    def _extreme(self, a, T, haut):
        """Plus haut (ou plus bas) des bougies 1 min ouvertes dans [a, T).
        a est une heure pleine : heures entieres en 1 h, la fin en 1 min."""
        hT = T // HEURE_MS * HEURE_MS
        if hT < a:
            hT = a
        k0, k1 = bisect_left(self.h1.t, a), bisect_left(self.h1.t, hT)
        j0, j1 = bisect_left(self.b.t, hT), bisect_left(self.b.t, T)
        src1, src2 = (self.h1.h, self.b.h) if haut else (self.h1.l, self.b.l)
        vals = src1[k0:k1] + src2[j0:j1]
        if not vals:
            return None
        return max(vals) if haut else min(vals)

    def _plage(self, a, z):
        """Plus haut et plus bas des heures pleines [a, z)."""
        k0, k1 = bisect_left(self.h1.t, a), bisect_left(self.h1.t, z)
        if k1 <= k0:
            return None
        return max(self.h1.h[k0:k1]), min(self.h1.l[k0:k1])

    def niveaux(self, T):
        """Niveaux de liquidite pas encore traverses a l'instant T :
        (hauts, bas), chacun une liste de (nom, prix)."""
        hauts, bas = [], []
        jour = (T - MIN_MS) // JOUR_MS * JOUR_MS        # jour de la bougie du signal
        k = bisect_left(self.j1.t, jour - JOUR_MS)
        if k < len(self.j1.t) and self.j1.t[k] == jour - JOUR_MS:
            depuis_h = self._extreme(jour, T, True)
            depuis_l = self._extreme(jour, T, False)
            if depuis_h is None or depuis_h <= self.j1.h[k]:
                hauts.append(("PDH", self.j1.h[k]))
            if depuis_l is None or depuis_l >= self.j1.l[k]:
                bas.append(("PDL", self.j1.l[k]))
        off = decalage_new_york(T)
        local = T + off
        jour_local = local // JOUR_MS * JOUR_MS
        for nom, (debut_h, duree_h) in SESSIONS_NY.items():
            fin = jour_local + (debut_h + duree_h) * HEURE_MS
            while fin > local:
                fin -= JOUR_MS
            debut = fin - duree_h * HEURE_MS
            a, z = debut - off, fin - off                 # en UTC
            plage = self._plage(a, z)
            if plage is None:
                continue
            sh, sl = plage
            depuis_h = self._extreme(z, T, True)
            depuis_l = self._extreme(z, T, False)
            if depuis_h is None or depuis_h <= sh:
                hauts.append((nom, sh))
            if depuis_l is None or depuis_l >= sl:
                bas.append((nom, sl))
        return hauts, bas

    # -- les votes --------------------------------------------------------------
    def voter(self, i, d, entree, objectif):
        """Votes pour un signal de sens d (+1/-1) pris a la cloture de la bougie i."""
        b = self.b
        T = b.t[i] + MIN_MS
        v, info = {}, {}

        k = derniere_fermee(self.h4, H4_MS, T)
        if k >= 0 and self.ema50[k] is not None:
            e20, e50 = self.ema20[k], self.ema50[k]
            v["tendance_4h"] = vote_sens((e20 > e50) - (e20 < e50), d)
        else:
            v["tendance_4h"] = ABSENT

        k = derniere_fermee(self.h1, HEURE_MS, T)
        if k >= 0:
            v["structure_h1"] = vote_sens(self.struct[k], d)
            info["structure_genre"] = self.struct_genre[k]
            fenetre = [a for a in self.atr[max(0, k - 719):k + 1] if a is not None]
            if len(fenetre) >= 168 and self.atr[k] is not None:
                a = self.atr[k]
                rang = (sum(1 for x in fenetre if x < a) + 0.5 * sum(1 for x in fenetre if x == a))
                pct = 100.0 * rang / len(fenetre)
                info["atr_centile"] = pct
                v["volatilite"] = POUR if pct >= 200 / 3 else CONTRE if pct <= 100 / 3 else NEUTRE
            else:
                v["volatilite"] = ABSENT
        else:
            v["structure_h1"] = v["volatilite"] = ABSENT

        hauts, bas = self.niveaux(T)
        if not hauts and not bas:
            v["liquidite"] = ABSENT
        else:
            if d == 1:
                gene = [n for n, p in hauts if entree < p < objectif]
            else:
                gene = [n for n, p in bas if objectif < p < entree]
            info["obstacles"] = "+".join(gene)
            v["liquidite"] = CONTRE if gene else POUR

        # order flow : delta / CVD sur 30 et 60 min
        j30 = bisect_left(b.t, T - 30 * MIN_MS)
        j60 = bisect_left(b.t, T - 60 * MIN_MS)
        if j60 > 0 and b.t[j60] == T - 60 * MIN_MS:
            d30 = self.cvd[i] - self.cvd[j30 - 1]
            d60 = self.cvd[i] - self.cvd[j60 - 1]
            info["cvd30"], info["cvd60"] = d30, d60
            if d30 * d > 0 and d60 * d > 0:
                v["of_delta_cvd"] = POUR
            elif d30 * d < 0 and d60 * d < 0:
                v["of_delta_cvd"] = CONTRE
            else:
                v["of_delta_cvd"] = NEUTRE
        else:
            v["of_delta_cvd"] = ABSENT

        # absorption sur 15 min, comparee aux 7 derniers jours (un point toutes les 5 min)
        v["of_absorption"] = ABSENT
        if i - SEPT_JOURS >= 15:
            ds = self.d15[i - SEPT_JOURS:i:5]
            rs = self.r15[i - SEPT_JOURS:i:5]
            sdd = sum(x * x for x in ds)
            pente = sum(x * y for x, y in zip(ds, rs)) / sdd if sdd > 0 else 0.0
            if pente > 0:
                forte = (self.d15[i] * d > 0
                         and self.d15_abs[i] >= centile(sorted(self.d15_abs[i - SEPT_JOURS:i:5]), 80))
                attendu = pente * self.d15[i] * d            # avance habituelle pour ce delta
                faible = self.r15[i] * d < attendu
                info["absorption"] = int(forte and faible)
                v["of_absorption"] = CONTRE if forte and faible else NEUTRE

        # funding deja regle a l'instant T
        jf = bisect_right(self.ft, T) - 1
        j0 = bisect_right(self.ft, T - 30 * JOUR_MS)
        if jf >= 0 and jf - j0 + 1 >= 30:
            taux = self.fr[jf]
            fen = sorted(self.fr[j0:jf + 1])
            info["funding"] = taux
            if d == 1 and taux > 0 and taux > centile(fen, 90):
                v["of_funding"] = CONTRE
            elif d == -1 and taux < 0 and taux < centile(fen, 10):
                v["of_funding"] = CONTRE
            else:
                v["of_funding"] = NEUTRE
        else:
            v["of_funding"] = ABSENT

        sous = (v["of_delta_cvd"], v["of_absorption"], v["of_funding"])
        if CONTRE in sous:
            v["order_flow"] = CONTRE
        elif v["of_delta_cvd"] == POUR:
            v["order_flow"] = POUR
        elif all(x is ABSENT for x in sous):
            v["order_flow"] = ABSENT
        else:
            v["order_flow"] = NEUTRE
        return v, info
