"""Suivi de chaque trade du moteur apres sa fermeture.

Pour chaque trade ferme, le suiveur note :
  - ou le prix est alle apres la sortie (15 min, 1 h, 4 h, 24 h), en R dans le
    sens du trade. Positif : le prix a continue, la sortie etait trop tot.
    Negatif : le prix est revenu, la sortie a protege ;
  - le meilleur et le pire mouvement dans les 24 h apres la sortie ;
  - ce que le trade aurait donne s'il avait ete tenu jusqu'au stop ou a
    l'objectif (la sortie d'avant la v2.4).
Meme suivi pour les signaux refuses par le garde-fou (limite de 3 trades par
24 h, etc.), comme s'ils avaient ete pris.

Le moteur ne garde pas ces informations : quand il ferme sur signal, le stop et
l'objectif partent avec la position. Le suiveur tourne a cote, sans modifier le
moteur. Il ne change rien tout seul : ses notes servent aux bilans.

Depuis la v2.6 (20 nov 2026 : or seul, veto news, vrais frais), le resume a
une partie a part pour la v2.6, avec les signaux BTC en pause et les signaux
bloques par les news suivis comme s'ils avaient ete pris.

Aucun trade ne peut etre oublie : chaque lancement refait tout le suivi a
partir du journal du moteur et des bougies 1 min de Binance Futures (la source
de prix du sandbox), qu'il met d'abord a jour. Lecture seule, aucun ordre.

Ecrit resultats/suivi_trades.csv (une ligne par trade ou signal refuse),
resultats/suivi_trades.txt (le resume) et ajoute une ligne par lancement a
resultats/suivi_trades_lancements.log. La tache Windows « PerpsSuivi » le
relance toute seule toutes les 15 min, sans fenetre, dans la session de Yc.

Limites :
  - chaque trade est tenu seul ; en vrai, une position gardee plus longtemps
    en bloque d'autres (une position a la fois, 3 trades par 24 h) ;
  - pour un signal refuse, la sortie sur signal est estimee au prix du journal
    des signaux, quand le signal s'eteint.

Usage, depuis ce dossier, avec le python du venv du moteur :
  python -I suivi_trades.py donnees [depuis AAAA-MM-JJTHH:MM, en UTC]
  (par defaut : demarrage de la v2.4, le 5 nov 2026 a 20:19 UTC)
"""
import bisect
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

import telecharger_1m  # noqa: E402
from donnees import JOUR_MS, MIN_MS, charger_1m, date_utc  # noqa: E402

MOTEUR = os.path.normpath(os.path.join(ICI, "..", ".."))
JOURNAL_TRADES = os.path.join(MOTEUR, "sandbox-trades.log")
JOURNAL_SIGNAUX = os.path.join(MOTEUR, "sandbox-signals.log")
RESULTATS = os.path.join(ICI, "resultats")
SYMBOLES = {"BTC": "BTCUSDT", "GOLD": "XAUUSDT"}
FRAIS = {"BTC": 0.0003, "GOLD": 0.00005}            # par cote, factures par la plateforme
PORTAGE_JOUR = {"BTC": 0.000276, "GOLD": 0.000153}  # du notionnel par jour (comparer_sorties.py)
DEBUT_V24 = "2026-11-05T20:19"
# Causes de refus montrees a part dans la partie v2.6, dans cet ordre.
CAUSES_V26 = (("MARCHE_EN_PAUSE", "SIGNAUX BTC EN PAUSE (surveilles, pas trades)"),
              ("VETO_CALENDRIER", "SIGNAUX BLOQUES PAR LES NEWS"))
HORIZONS = ((15, "15min", "15 min"), (60, "1h", "1 h"), (240, "4h", "4 h"), (1440, "24h", "24 h"))
COLONNES = (["type", "sym", "sens", "entree_utc", "entree", "stop", "objectif", "taille",
             "risque_usd", "refus", "raison_sortie", "sortie_utc", "prix_sortie", "net_usd", "r"]
            + [f"apres_{cle}_r" for _, cle, _ in HORIZONS]
            + ["meilleur_24h_r", "pire_24h_r", "tenu_issue", "tenu_utc", "tenu_net_usd", "tenu_r",
               "ecart_r", "rapide_net_usd", "rapide_r"])


def ms(iso):
    d = datetime.fromisoformat(iso)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def evenements(chemin):
    with open(chemin, encoding="utf-8") as f:
        for ligne in f:
            try:
                yield json.loads(ligne)
            except ValueError:
                continue


def debut_version(chemin, prefixe):
    """Heure (ms) du premier demarrage du moteur dans cette version, ou None."""
    for e in evenements(chemin):
        if e.get("event") == "start" and str(e.get("version", "")).startswith(prefixe):
            return ms(e["ts"])
    return None


def trades_fermes(chemin, depuis_ms):
    """Chaque entree remplie, associee a la fermeture suivante du meme marche
    (le moteur n'a qu'une position a la fois). Seuls comptent les trades fermes
    depuis depuis_ms."""
    ouverts, sorties_signal, trades = {}, {}, []
    for e in evenements(chemin):
        ev = e.get("event")
        if ev == "entry_order" and e.get("status") == "filled":
            ouverts[e["sym"]] = e
        elif ev == "exit_signal":
            sorties_signal[e["position_id"]] = e.get("reason")
        elif ev == "position_closed":
            ent = ouverts.pop(e["sym"], None)
            if ent is None or ms(e["ts"]) < depuis_ms:
                continue
            if abs(ent["fill_price"] - e["entry"]) > 1e-6 * e["entry"]:
                continue  # journal incomplet : entree et fermeture ne vont pas ensemble
            t = ms(ent["ts"])
            trades.append({
                "sym": e["sym"], "sens": ent["side"], "t_entree": t,
                "entree": ent["fill_price"], "stop": ent["stop"], "objectif": ent["tp"],
                "taille": ent["size"], "t_sortie": t + round(e["hold_min"] * MIN_MS),
                "prix_sortie": e["exit"], "net": e["net"],
                "raison": sorties_signal.get(e["position_id"], e["close_reason"])})
    return trades


def fins_signal(chemin_signaux):
    """{marche: [(instant ms, sens qui s'eteint, prix)]} : chaque fin de signal."""
    fins = {}
    for e in evenements(chemin_signaux):
        if e.get("event") == "signal_change" and e.get("from"):
            fins.setdefault(e["sym"], []).append((ms(e["ts"]), e["from"], e["price"]))
    return fins


def ajouter_sortie_rapide(trades, fins):
    """Trade garde jusqu'au stop ou a l'objectif (or, depuis la v2.6) : note
    l'heure et le prix ou le signal s'est eteint avant la vraie sortie, c'est-a-
    dire la sortie rapide qu'aurait faite le moteur jusqu'a la v2.6."""
    for x in trades:
        if x.get("raison") not in ("take_profit", "stop_loss"):
            continue                    # sortie deja faite sur signal
        fin = next(((tf, p) for tf, de, p in fins.get(x["sym"], [])
                    if tf > x["t_entree"] and de == x["sens"]), None)
        if fin and fin[0] < x["t_sortie"]:
            x["t_rapide"], x["prix_rapide"] = fin


def signaux_refuses(chemin_trades, chemin_signaux, depuis_ms):
    """Verdicts refuses par le garde-fou depuis depuis_ms, avec l'heure et le prix
    ou le signal s'est eteint : la sortie qu'aurait faite le moteur, a un cycle pres."""
    fins = fins_signal(chemin_signaux)
    refus, vus = [], set()
    for e in evenements(chemin_trades):
        if e.get("event") != "verdict" or e.get("approved") or not e.get("notional"):
            continue
        t = ms(e["ts"])
        if t < depuis_ms:
            continue
        fin = next(((tf, p) for tf, de, p in fins.get(e["sym"], []) if tf > t and de == e["side"]),
                   None)
        cle = (e["sym"], e["side"], fin[0] if fin else None)
        if cle in vus:
            continue  # meme signal refuse a plusieurs cycles : compte une fois
        vus.add(cle)
        refus.append({
            "sym": e["sym"], "sens": e["side"], "t_entree": t, "entree": e["entry"],
            "stop": e["stop"], "objectif": e["tp"], "taille": e["notional"] / e["entry"],
            "refus": "+".join(e.get("rejects") or []),
            "t_sortie": fin[0] if fin else None, "prix_sortie": fin[1] if fin else None})
    return refus


def tenir(sens, entree, stop, objectif, b, i0):
    """Rejoue la position bougie par bougie a partir de i0 jusqu'au premier niveau
    touche. Stop d'abord si les deux tombent dans la meme minute (prudent) ; un
    saut par-dessus un niveau est rempli a l'ouverture.
    Rend (issue, indice, prix, ambigu, pire prix contre la position avant la sortie)."""
    achat = sens == "buy"
    pire = entree
    for i in range(i0, len(b.t)):
        o, h, l = b.o[i], b.h[i], b.l[i]
        pire = min(pire, l) if achat else max(pire, h)
        if (o <= stop) if achat else (o >= stop):
            return "stop", i, o, False, pire
        if (o >= objectif) if achat else (o <= objectif):
            return "objectif", i, o, False, pire
        touche_stop = (l <= stop) if achat else (h >= stop)
        touche_obj = (h >= objectif) if achat else (l <= objectif)
        if touche_stop:
            return "stop", i, stop, touche_obj, pire
        if touche_obj:
            return "objectif", i, objectif, False, pire
    return "encore ouvert", len(b.t) - 1, b.c[-1], False, pire


def net_dollars(sym, sens, entree, sortie, taille, minutes):
    signe = 1 if sens == "buy" else -1
    brut = signe * (sortie - entree) * taille
    frais = FRAIS[sym] * taille * (entree + sortie)
    portage = PORTAGE_JOUR[sym] * taille * entree * minutes / 1440
    return brut - frais - portage


def prix_a(b, t):
    """Cloture de la derniere bougie fermee a l'instant t ; None si t est apres
    la derniere bougie connue."""
    if t > b.t[-1] + MIN_MS:
        return None
    k = bisect.bisect_right(b.t, t - MIN_MS) - 1
    return b.c[k] if k >= 0 else None


def apres_sortie(b, t_sortie, prix_sortie, sens, r_prix):
    """Mouvement du prix apres la sortie, en R dans le sens du trade : a chaque
    horizon (None s'il n'est pas encore passe), puis le meilleur et le pire
    mouvement dans les 24 h qui suivent (ou jusqu'a la derniere bougie)."""
    signe = 1 if sens == "buy" else -1
    mouvements = []
    for minutes, _, _ in HORIZONS:
        p = prix_a(b, t_sortie + minutes * MIN_MS)
        mouvements.append(None if p is None else signe * (p - prix_sortie) / r_prix)
    i0 = bisect.bisect_left(b.t, (t_sortie // MIN_MS + 1) * MIN_MS)
    i1 = bisect.bisect_left(b.t, t_sortie + JOUR_MS)
    if i0 >= i1:
        return mouvements, None, None
    haut, bas = max(b.h[i0:i1]), min(b.l[i0:i1])
    if signe > 0:
        return mouvements, (haut - prix_sortie) / r_prix, (bas - prix_sortie) / r_prix
    return mouvements, (prix_sortie - bas) / r_prix, (prix_sortie - haut) / r_prix


def suivre(trades, refus, marches):
    """Une ligne par trade ferme ou signal refuse. marches : {sym: Bougies}."""
    lignes = []
    for type_, lot in (("trade", trades), ("refuse", refus)):
        for x in lot:
            b = marches[x["sym"]]
            r_prix = abs(x["entree"] - x["stop"])
            risque = x["taille"] * r_prix
            ligne = {"type": type_, "sym": x["sym"], "sens": x["sens"],
                     "entree_utc": date_utc(x["t_entree"]), "entree": x["entree"],
                     "stop": x["stop"], "objectif": x["objectif"],
                     "taille": round(x["taille"], 6), "risque_usd": round(risque, 2),
                     "refus": x.get("refus", "")}
            if x["t_sortie"] is None:
                ligne["raison_sortie"] = "signal encore actif"
            else:
                if type_ == "trade":
                    net, raison = x["net"], x["raison"]
                else:
                    net = net_dollars(x["sym"], x["sens"], x["entree"], x["prix_sortie"],
                                      x["taille"], (x["t_sortie"] - x["t_entree"]) / MIN_MS)
                    raison = "fin du signal (estimation)"
                mouv, meilleur, pire = apres_sortie(b, x["t_sortie"], x["prix_sortie"],
                                                    x["sens"], r_prix)
                ligne.update(raison_sortie=raison, sortie_utc=date_utc(x["t_sortie"]),
                             prix_sortie=x["prix_sortie"], net_usd=round(net, 2),
                             r=round(net / risque, 3))
                if type_ == "trade":
                    if x.get("t_rapide") is not None:
                        nr = net_dollars(x["sym"], x["sens"], x["entree"], x["prix_rapide"],
                                         x["taille"], (x["t_rapide"] - x["t_entree"]) / MIN_MS)
                        ligne.update(rapide_net_usd=round(nr, 2), rapide_r=round(nr / risque, 3))
                    else:
                        ligne.update(rapide_net_usd=ligne["net_usd"], rapide_r=ligne["r"])
                for (_, cle, _), m in zip(HORIZONS, mouv):
                    ligne[f"apres_{cle}_r"] = None if m is None else round(m, 3)
                ligne["meilleur_24h_r"] = None if meilleur is None else round(meilleur, 3)
                ligne["pire_24h_r"] = None if pire is None else round(pire, 3)
            i0 = bisect.bisect_left(b.t, (x["t_entree"] // MIN_MS + 1) * MIN_MS)
            if i0 < len(b.t):
                issue, i, prix, ambigu, _ = tenir(x["sens"], x["entree"], x["stop"],
                                                  x["objectif"], b, i0)
                minutes = (b.t[i] + MIN_MS / 2 - x["t_entree"]) / MIN_MS
                net_t = net_dollars(x["sym"], x["sens"], x["entree"], prix, x["taille"], minutes)
                ligne.update(tenu_issue=issue + (" (stop et objectif la meme minute)" if ambigu else ""),
                             tenu_utc=date_utc(b.t[i]), tenu_net_usd=round(net_t, 2),
                             tenu_r=round(net_t / risque, 3))
                if ligne.get("r") is not None:
                    ligne["ecart_r"] = round(ligne["tenu_r"] - ligne["r"], 3)
            lignes.append(ligne)
    return lignes


def _r(x):
    return "-" if x is None else f"{x:+.2f}"


def _moyenne(valeurs):
    valeurs = [v for v in valeurs if v is not None]
    return (sum(valeurs) / len(valeurs) if valeurs else None), len(valeurs)


def resume(lignes, depuis, fin_bougies, debut_v26=None):
    out = [f"Suivi des trades du moteur fermes depuis le {depuis.replace('T', ' ')} UTC",
           f"Mis a jour le {date_utc(time.time() * 1000)} UTC ; bougies Binance jusqu'au "
           f"{fin_bougies} UTC.",
           "R = distance entre l'entree et le stop. Apres la sortie, positif : le prix a",
           "continue dans le sens du trade (sortie trop tot) ; negatif : il est revenu (la",
           "sortie a protege). Tenu = garde jusqu'au stop ou a l'objectif, chaque trade seul.", ""]
    si_pris = "Si pris (sortie sur signal, estimee)"
    if debut_v26 is not None:
        minute = debut_v26 - debut_v26 % MIN_MS
        recents = [x for x in lignes if ms(x["entree_utc"]) >= minute]
        out += [f"=== DEPUIS LA V2.6 (or seul, veto news, vrais frais), demarree le "
                f"{date_utc(debut_v26)} UTC ===", ""]
        _section(out, [x for x in recents if x["type"] == "trade"], "trade",
                 "TRADES PRIS", "Reel")
        reste = [x for x in recents if x["type"] == "refuse"]
        for code, titre in CAUSES_V26:
            _section(out, [x for x in reste if code in (x.get("refus") or "")],
                     "refuse", titre, si_pris)
            reste = [x for x in reste if code not in (x.get("refus") or "")]
        _section(out, reste, "refuse", "AUTRES SIGNAUX REFUSES PAR LE GARDE-FOU", si_pris)
        out += [f"=== DEPUIS LA V2.4 (tout, depuis le {depuis.replace('T', ' ')} UTC) ===", ""]
    _section(out, [x for x in lignes if x["type"] == "trade"], "trade", "TRADES PRIS", "Reel")
    _section(out, [x for x in lignes if x["type"] == "refuse"], "refuse",
             "SIGNAUX REFUSES PAR LE GARDE-FOU", si_pris)
    return "\n".join(out)


def _section(out, lot, type_, titre, nom):
    out.append(f"{titre} : {len(lot)}")
    if not lot:
        out.append("")
        return
    reels = [x for x in lot if x.get("net_usd") is not None]
    tenus = [x for x in lot if x.get("tenu_net_usd") is not None]
    out.append(f"  {nom} : {sum(x['net_usd'] for x in reels):+.0f} $, "
               f"moyenne {_r(_moyenne([x['r'] for x in reels])[0])} R")
    out.append(f"  Tenu : {sum(x['tenu_net_usd'] for x in tenus):+.0f} $, "
               f"moyenne {_r(_moyenne([x['tenu_r'] for x in tenus])[0])} R")
    rapides = [x for x in lot if x.get("rapide_net_usd") is not None]
    if any(x["rapide_net_usd"] != x.get("net_usd") for x in rapides):
        out.append(f"  Sortie rapide (estimee) : {sum(x['rapide_net_usd'] for x in rapides):+.0f} $, "
                   f"moyenne {_r(_moyenne([x['rapide_r'] for x in rapides])[0])} R")
    ecarts = [x["ecart_r"] for x in lot if x.get("ecart_r") is not None]
    out.append(f"  Tenir fait mieux {sum(e > 0 for e in ecarts)} fois, "
               f"moins bien {sum(e < 0 for e in ecarts)} fois")
    morceaux = []
    for _, cle, nom_h in HORIZONS:
        m, n = _moyenne([x.get(f"apres_{cle}_r") for x in lot])
        morceaux.append(f"{nom_h} {_r(m)} ({n})")
    out.append("  Apres la sortie, en moyenne (nombre de trades) : " + " | ".join(morceaux))
    out.append("  Detail :")
    for x in lot:
        sens = "achat" if x["sens"] == "buy" else "vente"
        refus = f" [{x['refus']}]" if x.get("refus") else ""
        if x.get("net_usd") is None:
            premier = f"{x['raison_sortie']}"
        else:
            premier = (f"{'reel' if type_ == 'trade' else 'si pris'} {x['net_usd']:+.0f} $ "
                       f"({_r(x['r'])} R), sortie {x['sortie_utc'][11:]} : {x['raison_sortie']}")
            if x.get("rapide_net_usd") not in (None, x["net_usd"]):
                premier += f" ; sortie rapide estimee {x['rapide_net_usd']:+.0f} $"
        out.append(f"  - {x['entree_utc']} {x['sym']} {sens}{refus} : {premier}")
        apres = " | ".join(f"{nom_h} {_r(x.get(f'apres_{cle}_r'))}" for _, cle, nom_h in HORIZONS)
        tenu = ("-" if x.get("tenu_net_usd") is None else
                f"{x['tenu_issue']} {x['tenu_net_usd']:+.0f} $ ({_r(x['tenu_r'])} R)")
        out.append(f"      apres la sortie : {apres} ; tenu : {tenu}")
    out.append("")


def main(dossier, depuis=DEBUT_V24):
    depuis_ms = ms(depuis)
    trades = trades_fermes(JOURNAL_TRADES, depuis_ms)
    ajouter_sortie_rapide(trades, fins_signal(JOURNAL_SIGNAUX))
    refus = signaux_refuses(JOURNAL_TRADES, JOURNAL_SIGNAUX, depuis_ms)
    if not trades and not refus:
        print("Aucun trade ferme ni signal refuse depuis", depuis)
        return "rien a suivre"
    debut = min(x["t_entree"] for x in trades + refus) - JOUR_MS
    marches = {}
    for sym in sorted({x["sym"] for x in trades + refus}):
        telecharger_1m.bougies(dossier, SYMBOLES[sym], debut)  # ajoute les bougies manquantes
        marches[sym] = charger_1m(os.path.join(dossier, f"{SYMBOLES[sym]}_1m.csv"), debut)
    lignes = suivre(trades, refus, marches)
    os.makedirs(RESULTATS, exist_ok=True)
    with open(os.path.join(RESULTATS, "suivi_trades.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLONNES, restval="")
        w.writeheader()
        for ligne in lignes:
            w.writerow({k: ("" if v is None else v) for k, v in ligne.items()})
    fin = date_utc(min(b.t[-1] for b in marches.values()) + MIN_MS)
    t26 = debut_version(JOURNAL_TRADES, "2.6")
    texte = resume(lignes, depuis, fin, t26)
    with open(os.path.join(RESULTATS, "suivi_trades.txt"), "w", encoding="utf-8") as f:
        f.write(texte + "\n")
    print(texte)
    v26 = ""
    if t26 is not None:
        n26 = sum(1 for x in trades if x["t_entree"] >= t26 - t26 % MIN_MS)
        v26 = f" (dont v2.6 : {n26})"
    return (f"{len(trades)} trades{v26}, {len(refus)} signaux refuses, "
            f"bougies jusqu'au {fin} UTC")


def noter_lancement(texte):
    """Une ligne par lancement : la tache Windows tourne sans fenetre, c'est
    ici qu'on voit si elle marche."""
    os.makedirs(RESULTATS, exist_ok=True)
    with open(os.path.join(RESULTATS, "suivi_trades_lancements.log"), "a", encoding="utf-8") as f:
        f.write(f"{date_utc(time.time() * 1000)} UTC  {texte}\n")


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print(__doc__)
        sys.exit(1)
    try:
        bilan = main(*sys.argv[1:])
    except Exception as e:
        noter_lancement(f"ERREUR {type(e).__name__} : {e}")
        raise
    noter_lancement(f"ok : {bilan}")
