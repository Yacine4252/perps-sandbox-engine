"""Chargement des bougies 1 min et du funding, agregation en 1 h / 4 h / 1 jour,
heure de New York (avec l'heure d'ete). Python standard uniquement.

Convention de temps : une bougie est reperee par son heure d'OUVERTURE (ms UTC).
Elle n'est connue qu'a sa CLOTURE. Un signal pris a la cloture de la bougie 1 min
d'indice i a lieu a T = t[i] + 1 min, et ne peut utiliser que des bougies dont la
cloture est <= T.
"""
import csv
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone

MIN_MS = 60_000
HEURE_MS = 3_600_000
JOUR_MS = 86_400_000


@dataclass
class Bougies:
    t: list    # ouverture, ms UTC
    o: list
    h: list
    l: list
    c: list
    v: list = None    # volume (unite de base)
    tb: list = None   # volume taker buy (unite de base)


def charger_1m(chemin, depuis_ms=None):
    t, o, h, l, c, v, tb = [], [], [], [], [], [], []
    with open(chemin, newline="") as f:
        r = csv.reader(f)
        next(r)
        for x in r:
            ti = int(x[0])
            if depuis_ms is not None and ti < depuis_ms:
                continue
            t.append(ti)
            o.append(float(x[1]))
            h.append(float(x[2]))
            l.append(float(x[3]))
            c.append(float(x[4]))
            v.append(float(x[5]))
            tb.append(float(x[6]))
    return Bougies(t, o, h, l, c, v, tb)


def charger_funding(chemin):
    ft, fr = [], []
    with open(chemin, newline="") as f:
        r = csv.reader(f)
        next(r)
        for x in r:
            ft.append(int(x[0]))
            fr.append(float(x[1]))
    paires = sorted(zip(ft, fr))
    return [p[0] for p in paires], [p[1] for p in paires]


def agreger(b, periode_ms):
    """Bougies de periode_ms alignees sur minuit UTC. La bougie k couvre
    [t[k], t[k] + periode_ms) et n'est connue qu'a t[k] + periode_ms."""
    t, o, h, l, c = [], [], [], [], []
    for i in range(len(b.t)):
        k = b.t[i] // periode_ms * periode_ms
        if t and t[-1] == k:
            if b.h[i] > h[-1]:
                h[-1] = b.h[i]
            if b.l[i] < l[-1]:
                l[-1] = b.l[i]
            c[-1] = b.c[i]
        else:
            t.append(k)
            o.append(b.o[i])
            h.append(b.h[i])
            l.append(b.l[i])
            c.append(b.c[i])
    return Bougies(t, o, h, l, c)


def derniere_fermee(agr, periode_ms, T):
    """Indice de la derniere bougie agregee FERMEE a l'instant T, -1 si aucune."""
    return bisect_right(agr.t, T - periode_ms) - 1


_DST = {}


def _dimanche(annee, mois, rang):
    jour1 = datetime(annee, mois, 1, tzinfo=timezone.utc).weekday()   # lundi = 0
    return 1 + (6 - jour1) % 7 + 7 * (rang - 1)


def decalage_new_york(t_ms):
    """Ecart UTC -> New York en ms : -4 h en heure d'ete (du 2e dimanche de mars
    07:00 UTC au 1er dimanche de novembre 06:00 UTC), -5 h sinon."""
    an = datetime.fromtimestamp(t_ms / 1000, timezone.utc).year
    if an not in _DST:
        debut = datetime(an, 3, _dimanche(an, 3, 2), 7, tzinfo=timezone.utc)
        fin = datetime(an, 11, _dimanche(an, 11, 1), 6, tzinfo=timezone.utc)
        _DST[an] = (int(debut.timestamp() * 1000), int(fin.timestamp() * 1000))
    debut, fin = _DST[an]
    return -4 * HEURE_MS if debut <= t_ms < fin else -5 * HEURE_MS


def centile(trie, q):
    """Centile q (0-100) d'une liste TRIEE, interpolation lineaire (comme numpy)."""
    n = len(trie)
    if n == 0:
        return None
    pos = (n - 1) * q / 100.0
    bas = int(pos)
    haut = min(bas + 1, n - 1)
    return trie[bas] + (trie[haut] - trie[bas]) * (pos - bas)


def date_utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
