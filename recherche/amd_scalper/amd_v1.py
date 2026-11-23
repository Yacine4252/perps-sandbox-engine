"""Portage Python d'AMD_Scalper_v1.1 (Pine v6, fichier du 19 aout 2026).

Reproduit la logique du script barre par barre, sur bougies cloturees :
compression -> expansion -> FVG -> retour sur le FVG -> stop ou objectif.
Aucun ordre n'est envoye : tout est calcule sur des bougies historiques.

Deux modes :
- fidele   : le script tel quel, y compris ses raccourcis :
             * un FVG peut se declencher sur sa bougie de naissance, au plus bas
               (ou au plus haut) de cette bougie, alors qu'il n'est connu qu'a la cloture ;
             * une bougie qui traverse tout le FVG l'efface sans trade ;
             * un time-stop compte 0 R (moins les frais), quel que soit le prix.
- realiste : l'ordre limite n'existe qu'apres la cloture de la bougie de naissance ;
             une bougie qui touche l'entree puis le stop est une perte ;
             le time-stop sort au prix de cloture.

Les reglages en ticks du MNQ sont convertis en fractions d'ATR :
1 tick = tick_atr x ATR (0,0125 par defaut, soit 4 ticks = 0,05 ATR).
Frais : bps par cote, payes a l'entree et a la sortie.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Sessions en heure de New York, debut inclus et fin exclue, comme time(..., "0930-1100", tz).
SESSIONS = {3: (9 * 60 + 30, 11 * 60), 2: (3 * 60, 6 * 60), 1: (18 * 60, 23 * 60)}
NOMS_SESSIONS = {1: "Asie", 2: "Londres", 3: "New York", 0: "Hors session"}
JOUR_MS = 86_400_000


# ---------------------------------------------------------------------------
#  Outils reproduisant les fonctions Pine
# ---------------------------------------------------------------------------
def pine_round(x: float) -> int:
    """math.round de Pine : 0,5 arrondi vers le haut (Python, lui, arrondit au pair)."""
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def barres(minutes: float, bar_min: float, minimum: int) -> int:
    """Conversion du script : max(minimum, round(minutes / minutes_par_barre))."""
    return max(minimum, pine_round(minutes / bar_min))


def _nieme_dimanche(annee: int, mois: int, rang: int) -> datetime:
    d = datetime(annee, mois, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7 + 7 * (rang - 1))


def decalage_new_york(t: datetime) -> timedelta:
    """UTC-4 en heure d'ete (2e dimanche de mars -> 1er dimanche de novembre, 2 h locale), UTC-5 sinon."""
    debut = _nieme_dimanche(t.year, 3, 2).replace(hour=7, tzinfo=timezone.utc)
    fin = _nieme_dimanche(t.year, 11, 1).replace(hour=6, tzinfo=timezone.utc)
    return timedelta(hours=-4) if debut <= t < fin else timedelta(hours=-5)


def session(t_ms: int) -> int:
    """sessIdx du script : 3 = New York, 2 = Londres, 1 = Asie, 0 = hors session."""
    t = datetime.fromtimestamp(t_ms / 1000, timezone.utc)
    loc = t + decalage_new_york(t)
    m = loc.hour * 60 + loc.minute
    for k in (3, 2, 1):
        a, b = SESSIONS[k]
        if a <= m < b:
            return k
    return 0


def true_range(h, l, c):
    tr = [h[0] - l[0]]
    for i in range(1, len(h)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return tr


def rma(src, n):
    """ta.rma : moyenne simple des n premieres valeurs, puis lissage de poids 1/n."""
    out = [None] * len(src)
    if len(src) < n:
        return out
    v = sum(src[:n]) / n
    out[n - 1] = v
    for i in range(n, len(src)):
        v = (src[i] + (n - 1) * v) / n
        out[i] = v
    return out


def extreme_glissant(x, n, fn):
    """ta.highest / ta.lowest sur les n dernieres barres, barre courante incluse."""
    out = [None] * len(x)
    for i in range(n - 1, len(x)):
        out[i] = fn(x[i - n + 1:i + 1])
    return out


def pivots(h, l, gauche, droite):
    """ta.pivothigh / ta.pivotlow : la valeur apparait 'droite' barres apres le pivot.
    Egalites : stricte a gauche, large a droite (cas rares sur ces marches)."""
    n = len(h)
    ph, pl = [None] * n, [None] * n
    for i in range(gauche + droite, n):
        p = i - droite
        v = h[p]
        if all(v > h[p - k] for k in range(1, gauche + 1)) and all(v >= h[p + k] for k in range(1, droite + 1)):
            ph[i] = v
        w = l[p]
        if all(w < l[p - k] for k in range(1, gauche + 1)) and all(w <= l[p + k] for k in range(1, droite + 1)):
            pl[i] = w
    return ph, pl


# ---------------------------------------------------------------------------
#  Reglages et objets
# ---------------------------------------------------------------------------
@dataclass
class Reglages:
    nom: str = ""
    # 1 . Detection
    comp_min: float = 5            # Compression - fenetre (minutes)
    comp_mult: float = 1.8
    exp_mult: float = 1.5
    atr_min: float = 14            # ATR - periode (minutes)
    entry_mode: str = "Continuation"
    # 2 . FVG
    entry_level: str = "Proximal"  # Proximal, CE, Distal
    touch_meche: bool = True       # Meche (sinon Cloture)
    min_fvg_ticks: float = 4
    max_fvg_atr: float = 3.0
    fvg_max_age_min: float = 30
    # 3 . Risque  (config de reference du 19 aout : SL max 0,5 ATR, time-stop 120 min)
    sl_mode: str = "Le plus serre"  # Meche expansion, Bord distal FVG, Le plus serre
    sl_buffer_ticks: float = 4
    max_sl_atr: float = 0.5
    min_r: float = 2.0
    max_r: float = 4.0
    tp_fixe_si_aucun_pool: bool = False
    max_hold_min: float = 120
    # 4 . Liquidite
    piv_left: int = 5
    piv_right: int = 5
    eq_ticks: float = 4
    use_pdhl: bool = True
    # 8 . Analyse
    no_ts_cap_min: float = 480
    use_ctrl: bool = True
    ctrl_every: int = 50
    # Adaptation crypto
    tick_atr: float = 0.0125       # 1 tick MNQ = 0,0125 ATR (4 ticks = 0,05 ATR)
    frais_bps: float = 3.0         # par cote
    realiste: bool = False
    bar_min: float = 5.0
    # Pour decomposer l'ecart fidele / realiste (None = suit 'realiste') :
    sans_naissance: bool | None = None       # pas de remplissage sur la bougie de naissance
    remplissage_honnete: bool | None = None  # bougie qui traverse : remplie ; stop dans la bougie d'entree
    ts_au_prix: bool | None = None           # time-stop au prix de cloture


@dataclass
class _Fvg:
    prox: float
    dist: float
    dir: int
    born: int
    exp_hi: float
    exp_lo: float
    fired: bool = False


@dataclass
class _Trade:
    dir: int
    entry: float
    sl: float
    tp: float
    rr: float
    risk: float
    open_bar: int
    ctrl: bool
    sess: int
    t_open: int
    t_fvg: int = 0
    ts_done: bool = False
    nt_done: bool = False


@dataclass
class Resultat:
    reglages: Reglages
    stats: dict
    trades: list


class _Compteurs:
    def __init__(self):
        self.n_jours = 0
        self.c_comp = self.c_exp = self.c_fvg = 0
        self.rej_petit = self.rej_grand = 0
        self.c_touch = self.rej_min_r = self.rej_max_r = self.c_sig = 0
        self.n_win = self.n_loss = self.n_time = 0
        self.sum_r = self.sum_brut = self.sum_frais = 0.0
        self.g_win = self.g_loss = 0.0
        self.eq = self.peak = self.max_dd = 0.0
        self.run = self.max_run = 0
        self.lg_win = self.lg_los = self.sh_win = self.sh_los = 0
        self.lg_r = self.sh_r = 0.0
        self.ses_w = [0, 0, 0, 0]
        self.ses_l = [0, 0, 0, 0]
        self.ses_r = [0.0, 0.0, 0.0, 0.0]
        self.ctrl_win = self.ctrl_loss = self.ctrl_time = 0
        self.ctrl_sum = 0.0
        self.nt_win = self.nt_los = self.nt_tim = 0
        self.nt_r = 0.0
        self.rr_sum = self.risk_sum = 0.0
        self.rr_cnt = 0


def _ajouter_pool(pools, px, d, tol):
    for q in pools:
        if q[1] == d and not q[2] and abs(q[0] - px) <= tol:
            return
    pools.append([px, d, False])
    if len(pools) > 80:
        pools.pop(0)


def choisir_tp(pools, d, entree, risque, min_r):
    """Pool non prise, du bon cote, la plus PROCHE qui paie au moins min_r."""
    tp, meilleur = None, float("inf")
    for px, pd, prise in pools:
        if not prise and pd == d:
            ecart = px - entree if d == 1 else entree - px
            if ecart > 0 and ecart / risque >= min_r and ecart < meilleur:
                meilleur, tp = ecart, px
    return tp


# ---------------------------------------------------------------------------
#  Simulation
# ---------------------------------------------------------------------------
def simuler(t, o, h, l, c, R: Reglages) -> Resultat:
    n = len(t)
    n_comp = barres(R.comp_min, R.bar_min, 2)
    n_atr = barres(R.atr_min, R.bar_min, 2)
    max_age = barres(R.fvg_max_age_min, R.bar_min, 1)
    max_hold = barres(R.max_hold_min, R.bar_min, 1)
    cap_brut = max(max_hold + 1, pine_round(R.no_ts_cap_min / R.bar_min))
    atr = rma(true_range(h, l, c), n_atr)
    hi_n = extreme_glissant(h, n_comp, max)
    lo_n = extreme_glissant(l, n_comp, min)
    ph, pl = pivots(h, l, R.piv_left, R.piv_right)
    sess = [session(x) for x in t]
    frais = R.frais_bps / 1e4
    sans_naissance = R.realiste if R.sans_naissance is None else R.sans_naissance
    honnete = R.realiste if R.remplissage_honnete is None else R.remplissage_honnete
    ts_prix = R.realiste if R.ts_au_prix is None else R.ts_au_prix

    pools, fvgs, ouverts, lignes = [], [], [], []
    K = _Compteurs()
    jour_prec = None
    jour_hi = jour_lo = pdh = pdl = None

    def cout_r(tr, sortie):
        return frais * (tr.entry + sortie) / tr.risk

    def r_brut(tr, genre, sortie):
        if genre == "ts" and not ts_prix:
            return 0.0                      # le script compte un time-stop a 0 R
        return (sortie - tr.entry) * tr.dir / tr.risk

    def cloture_officielle(tr, genre, sortie, i):
        brut = r_brut(tr, genre, sortie)
        cout = cout_r(tr, sortie)
        res = brut - cout
        tr.ts_done = True
        if tr.ctrl:
            K.ctrl_sum += res
            if genre == "sl":
                K.ctrl_loss += 1
            elif genre == "tp":
                K.ctrl_win += 1
            else:
                K.ctrl_time += 1
            return
        K.sum_r += res
        K.sum_brut += brut
        K.sum_frais += cout
        K.eq += res
        K.peak = max(K.peak, K.eq)
        K.max_dd = max(K.max_dd, K.peak - K.eq)
        if genre == "sl":
            K.n_loss += 1
            K.g_loss += 1.0 + cout
            K.run += 1
            K.max_run = max(K.max_run, K.run)
            K.ses_l[tr.sess] += 1
            if tr.dir == 1:
                K.lg_los += 1
            else:
                K.sh_los += 1
        elif genre == "tp":
            K.n_win += 1
            K.g_win += res
            K.run = 0
            K.ses_w[tr.sess] += 1
            if tr.dir == 1:
                K.lg_win += 1
            else:
                K.sh_win += 1
        else:
            K.n_time += 1
        K.ses_r[tr.sess] += res
        if tr.dir == 1:
            K.lg_r += res
        else:
            K.sh_r += res
        lignes.append({
            "t_fvg": tr.t_fvg, "t_ouverture": tr.t_open, "t_cloture": t[i], "sens": tr.dir,
            "entree": tr.entry, "stop": tr.sl, "objectif": tr.tp, "rr": tr.rr,
            "risque": tr.risk, "risque_bps": tr.risk / tr.entry * 1e4,
            "sortie": genre, "prix_sortie": sortie, "r_brut": brut, "r_net": res,
            "session": NOMS_SESSIONS[tr.sess],
        })

    def cloture_brute(tr, genre, sortie):
        res = r_brut(tr, genre, sortie) - cout_r(tr, sortie)
        tr.nt_done = True
        if genre == "sl":
            K.nt_los += 1
        elif genre == "tp":
            K.nt_win += 1
        else:
            K.nt_tim += 1
        K.nt_r += res

    for i in range(n):
        hi, lo = h[i], l[i]
        # --- jours : PDH / PDL de la veille (bougie 1D en UTC) -------------------
        jour = t[i] // JOUR_MS
        nouveau_jour = jour != jour_prec
        if nouveau_jour:
            if jour_prec is not None:
                pdh, pdl = jour_hi, jour_lo
            jour_hi, jour_lo, jour_prec = hi, lo, jour
            K.n_jours += 1
        else:
            jour_hi = max(jour_hi, hi)
            jour_lo = min(jour_lo, lo)

        a_i = atr[i]
        tick = R.tick_atr * a_i if a_i is not None else 0.0

        # --- pools de liquidite ---------------------------------------------------
        tol = R.eq_ticks * tick
        if ph[i] is not None:
            _ajouter_pool(pools, ph[i], 1, tol)
        if pl[i] is not None:
            _ajouter_pool(pools, pl[i], -1, tol)
        if R.use_pdhl and nouveau_jour and pdh is not None:
            _ajouter_pool(pools, pdh, 1, tol)
            _ajouter_pool(pools, pdl, -1, tol)
        for p in pools:
            if not p[2] and ((p[1] == 1 and hi >= p[0]) or (p[1] == -1 and lo <= p[0])):
                p[2] = True

        # --- detection : [i-2] compression, [i-1] expansion, [i] confirmation ------
        if i > n_comp + n_atr + 3:
            comp_hi, comp_lo, atr_ref = hi_n[i - 2], lo_n[i - 2], atr[i - 2]
            if comp_hi is not None and atr_ref is not None:
                is_comp = (comp_hi - comp_lo) < R.comp_mult * atr_ref
                if is_comp:
                    K.c_comp += 1
                exp_hi, exp_lo = h[i - 1], l[i - 1]
                is_imp = (exp_hi - exp_lo) > R.exp_mult * atr_ref
                brk_up, brk_dn = exp_hi > comp_hi, exp_lo < comp_lo
                if R.entry_mode == "Continuation":
                    bias = 1 if (brk_up and not brk_dn) else (-1 if (brk_dn and not brk_up) else 0)
                else:
                    bias = 0
                    if brk_up and not brk_dn and c[i - 1] < comp_hi:
                        bias = -1
                    elif brk_dn and not brk_up and c[i - 1] > comp_lo:
                        bias = 1
                if is_comp and is_imp and bias != 0:
                    K.c_exp += 1
                    prox = dist = None
                    if bias == 1 and lo > h[i - 2]:
                        prox, dist = lo, h[i - 2]
                    elif bias == -1 and hi < l[i - 2]:
                        prox, dist = hi, l[i - 2]
                    if prox is not None:
                        gap = abs(prox - dist)
                        if gap < R.min_fvg_ticks * R.tick_atr * atr_ref:
                            K.rej_petit += 1
                        elif gap > R.max_fvg_atr * atr_ref:
                            K.rej_grand += 1
                        else:
                            K.c_fvg += 1
                            fvgs.append(_Fvg(prox, dist, bias, i, exp_hi, exp_lo))

        # --- maintenance des FVG et declenchement (le plus recent d'abord) ---------
        tire = False
        for k in range(len(fvgs) - 1, -1, -1):
            f = fvgs[k]
            perime = i - f.born > max_age
            traverse = (f.dir == 1 and lo < f.dist) or (f.dir == -1 and hi > f.dist)
            if perime or (traverse and not honnete):
                del fvgs[k]
                continue
            if R.entry_level == "Proximal":
                entree = f.prox
            elif R.entry_level == "Distal":
                entree = f.dist
            else:
                entree = (f.prox + f.dist) / 2.0
            if R.touch_meche:
                touche = lo <= entree if f.dir == 1 else hi >= entree
            else:
                touche = c[i] <= entree if f.dir == 1 else c[i] >= entree
            if touche and not f.fired and not tire and not (sans_naissance and i == f.born):
                f.fired = True
                K.c_touch += 1
                d = f.dir
                if R.sl_mode == "Meche expansion":
                    sl_brut = f.exp_lo if d == 1 else f.exp_hi
                elif R.sl_mode == "Bord distal FVG":
                    sl_brut = f.dist
                else:
                    sl_brut = max(f.exp_lo, f.dist) if d == 1 else min(f.exp_hi, f.dist)
                sl = sl_brut - R.sl_buffer_ticks * tick if d == 1 else sl_brut + R.sl_buffer_ticks * tick
                cap = R.max_sl_atr * a_i
                sl = max(sl, entree - cap) if d == 1 else min(sl, entree + cap)
                risque = abs(entree - sl)
                if risque > tick:
                    tp = choisir_tp(pools, d, entree, risque, R.min_r)
                    if tp is None and R.tp_fixe_si_aucun_pool:
                        tp = entree + d * R.min_r * risque
                    if tp is None:
                        K.rej_min_r += 1
                    else:
                        rr = abs(tp - entree) / risque
                        if rr > R.max_r:
                            K.rej_max_r += 1
                        else:
                            K.c_sig += 1
                            tire = True
                            K.rr_sum += rr
                            K.rr_cnt += 1
                            K.risk_sum += risque
                            tr = _Trade(d, entree, sl, tp, rr, risque, i, False, sess[i], t[i], t[f.born])
                            ouverts.append(tr)
                            if honnete and ((d == 1 and lo <= sl) or (d == -1 and hi >= sl)):
                                # rempli puis stoppe dans la meme bougie : perte
                                cloture_officielle(tr, "sl", sl, i)
                                cloture_brute(tr, "sl", sl)
            if honnete and traverse:
                del fvgs[k]

        # --- benchmark de controle : 1 trade sans signal toutes les N barres --------
        if R.use_ctrl and i % R.ctrl_every == 0 and i > n_atr + 5 and a_i is not None:
            cd = 1 if c[i] > o[i] else -1
            crk = K.risk_sum / K.rr_cnt if K.rr_cnt else R.max_sl_atr * a_i
            crr = K.rr_sum / K.rr_cnt if K.rr_cnt else R.min_r
            if crk > tick:
                ce = c[i]
                ouverts.append(_Trade(cd, ce, ce - cd * crk, ce + cd * crr * crk, crr, crk, i, True, sess[i], t[i]))

        # --- resolution : pessimiste, jamais sur la barre d'ouverture -------------
        for k in range(len(ouverts) - 1, -1, -1):
            tr = ouverts[k]
            if tr.open_bar >= i:
                continue
            d = tr.dir
            hit_sl = lo <= tr.sl if d == 1 else hi >= tr.sl
            hit_tp = hi >= tr.tp if d == 1 else lo <= tr.tp
            age = i - tr.open_bar
            if not tr.ctrl and not tr.nt_done:
                if hit_sl:
                    cloture_brute(tr, "sl", tr.sl)
                elif hit_tp:
                    cloture_brute(tr, "tp", tr.tp)
                elif age >= cap_brut:
                    cloture_brute(tr, "ts", c[i])
            if not tr.ts_done:
                if hit_sl:
                    cloture_officielle(tr, "sl", tr.sl, i)
                elif hit_tp:
                    cloture_officielle(tr, "tp", tr.tp, i)
                elif age >= max_hold:
                    cloture_officielle(tr, "ts", c[i], i)
            if tr.ts_done and (tr.ctrl or tr.nt_done):
                del ouverts[k]

    return Resultat(R, _stats(K, lignes, n_comp, n_atr, max_age, max_hold), lignes)


def _div(a, b):
    return a / b if b else 0.0


def _stats(K, lignes, n_comp, n_atr, max_age, max_hold):
    n_tot = K.n_win + K.n_loss + K.n_time
    esp = _div(K.sum_r, n_tot)
    c_tot = K.ctrl_win + K.ctrl_loss + K.ctrl_time
    esp_ctrl = _div(K.ctrl_sum, c_tot)
    nt_tot = K.nt_win + K.nt_los + K.nt_tim
    esp_brut_sltp = _div(K.nt_r, nt_tot)
    risques = sorted(x["risque_bps"] for x in lignes)
    sessions = {}
    for k in (1, 2, 3, 0):
        nb = K.ses_w[k] + K.ses_l[k]
        sessions[NOMS_SESSIONS[k]] = {"n": nb, "esperance": _div(K.ses_r[k], nb)}
    return {
        "barres": {"compression": n_comp, "atr": n_atr, "validite_fvg": max_age, "time_stop": max_hold},
        "jours": K.n_jours,
        "n_trades": n_tot,
        "trades_par_jour": _div(n_tot, K.n_jours),
        "esperance": esp,
        "esperance_avant_frais": _div(K.sum_brut, n_tot),
        "frais_moyens_r": _div(K.sum_frais, n_tot),
        "edge": esp - esp_ctrl,
        "reussite": _div(100.0 * K.n_win, K.n_win + K.n_loss),
        "gagnants": K.n_win,
        "perdants": K.n_loss,
        "time_stops": K.n_time,
        "time_stops_pct": _div(100.0 * K.n_time, n_tot),
        "pf": _div(K.g_win, K.g_loss),
        "total_r": K.sum_r,
        "r_par_jour": _div(K.sum_r, K.n_jours),
        "drawdown_max_r": K.max_dd,
        "pertes_consecutives_max": K.max_run,
        "risque_median_bps": risques[len(risques) // 2] if risques else 0.0,
        "rr_moyen": _div(K.rr_sum, K.rr_cnt),
        "brut_sl_tp": {"n": nt_tot, "esperance": esp_brut_sltp},
        "gestion_rapporte": esp - esp_brut_sltp,
        "controle": {"n": c_tot, "reussite": _div(100.0 * K.ctrl_win, c_tot),
                     "esperance": esp_ctrl, "time_stops": K.ctrl_time},
        "long": {"reussite": _div(100.0 * K.lg_win, K.lg_win + K.lg_los),
                 "esperance": _div(K.lg_r, K.lg_win + K.lg_los)},
        "short": {"reussite": _div(100.0 * K.sh_win, K.sh_win + K.sh_los),
                  "esperance": _div(K.sh_r, K.sh_win + K.sh_los)},
        "sessions": sessions,
        "entonnoir": {"compressions": K.c_comp, "expansions": K.c_exp, "fvg": K.c_fvg,
                      "rejetes_petits": K.rej_petit, "rejetes_grands": K.rej_grand,
                      "contacts": K.c_touch, "rejetes_min_r": K.rej_min_r,
                      "rejetes_max_r": K.rej_max_r, "signaux": K.c_sig},
    }
