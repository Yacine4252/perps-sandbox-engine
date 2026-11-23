"""
graphique.py - les trades du moteur sandbox, sur un graphique (v1).

    python -m perps_engine.graphique             genere la page et l'ouvre
    python -m perps_engine.graphique --no-open   genere seulement

Ce que fait le script
---------------------
1. Archive les prix. Le moteur ne garde que 24 h de prix en memoire
   (state-sandbox.json). Le script recopie ces points dans prix-sandbox.csv,
   sans doublon, ainsi que ceux des copies manuelles prix-sauvegarde*.json.
   Lance-le au moins une fois par jour : l'historique ne se perd plus.
2. Relit sandbox-trades.log. Chaque entree (entry_order) est appariee a sa
   fermeture (position_closed), dans l'ordre, marche par marche : le moteur
   ne tient qu'une position a la fois, l'appariement est donc sans ambiguite.
3. Ecrit graphique-trades.html : bougies de 5 min, et pour chaque trade la
   boite verte (entree -> objectif) et la boite rouge (entree -> stop), du
   debut a la fin du trade, plus la ligne entree -> sortie.

LECTURE SEULE : ne touche ni au moteur, ni a l'API, ni au compte.
La page est autonome (librairie incluse) : elle s'ouvre meme hors ligne.
"""

from __future__ import annotations

import csv
import glob
import html
import json
import os
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

STATE_FILE = "state-sandbox.json"
SAVED_COPIES = "prix-sauvegarde*.json"
PRICES_CSV = "prix-sandbox.csv"
TRADES_LOG = "sandbox-trades.log"
OUTPUT = "graphique-trades.html"
BUCKET_SECONDS = 300                       # bougies de 5 minutes

LIB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor",
                        "lightweight-charts.standalone.production.js")
LIB_CDN = ("https://unpkg.com/lightweight-charts@4.2.3/dist/"
           "lightweight-charts.standalone.production.js")

REASONS = {"stop_loss": "Stop", "take_profit": "Objectif", "tp": "Objectif",
           "user": "Sortie signal", "liquidation": "Liquidation"}


# --------------------------------------------------------------------------
# 1. Prix
# --------------------------------------------------------------------------

def _read_points(path: str) -> dict:
    """{sym: [(ts, prix), ...]} depuis un fichier d'etat, quel que soit son age."""
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for sym, data in (payload.get("markets") or {}).items():
        pts = []
        for item in (data or {}).get("points") or []:
            try:
                t, p = float(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if p > 0:
                pts.append((t, p))
        out[sym] = pts
    return out


def load_prices(csv_path: str = PRICES_CSV) -> dict:
    prices: dict = {}
    if not os.path.exists(csv_path):
        return prices
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                prices.setdefault(row["sym"], []).append(
                    (float(row["ts"]), float(row["price"])))
            except (KeyError, TypeError, ValueError):
                continue
    for sym in prices:
        prices[sym] = sorted(set(prices[sym]))
    return prices


def archive_prices(state_paths: Iterable[str],
                   csv_path: str = PRICES_CSV) -> int:
    """Ajoute au CSV les points pas encore archives. Rend le nombre ajoute."""
    known = {(sym, round(t, 3)) for sym, pts in load_prices(csv_path).items()
             for t, _ in pts}
    new_rows = []
    for path in state_paths:
        for sym, pts in _read_points(path).items():
            for t, p in pts:
                key = (sym, round(t, 3))
                if key not in known:
                    known.add(key)
                    new_rows.append((round(t, 3), sym, p))
    if not new_rows:
        return 0
    new_rows.sort()
    write_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
    with open(csv_path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["ts", "sym", "price"])
        w.writerows(new_rows)
    return len(new_rows)


def candles(points: list, bucket: int = BUCKET_SECONDS) -> list:
    """Bougies OHLC a partir de prix horodates (tries)."""
    out: list = []
    for t, p in sorted(points):
        b = int(t // bucket) * bucket
        if out and out[-1]["time"] == b:
            c = out[-1]
            c["high"] = max(c["high"], p)
            c["low"] = min(c["low"], p)
            c["close"] = p
        else:
            out.append({"time": b, "open": p, "high": p, "low": p, "close": p})
    return out


# --------------------------------------------------------------------------
# 2. Trades
# --------------------------------------------------------------------------

def _epoch(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _num(x) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def load_trades(log_path: str = TRADES_LOG) -> tuple:
    """
    Rend (trades, compte). Un trade : sym, side, entry_ts, entry, stop, tp,
    size, et s'il est ferme : exit_ts, exit, reason, net, hold_min.
    """
    trades: list = []
    queue: dict = {}
    account = None
    if not os.path.exists(log_path):
        return trades, account
    with open(log_path, encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        ev, ts = e.get("event"), _epoch(e.get("ts"))
        if ev == "start" and e.get("account"):
            account = e["account"]
        elif ev == "entry_order" and e.get("status") in ("filled", "pending"):
            t = {"sym": e.get("sym"), "side": e.get("side"), "entry_ts": ts,
                 "entry": _num(e.get("fill_price")), "stop": _num(e.get("stop")),
                 "tp": _num(e.get("tp")), "size": _num(e.get("size")),
                 "open": True}
            trades.append(t)
            queue.setdefault(t["sym"], []).append(t)
        elif ev == "position_closed":
            sym = e.get("sym")
            hold = _num(e.get("hold_min"))
            pending = queue.get(sym) or []
            if pending:
                t = pending.pop(0)
            else:
                t = {"sym": sym, "side": e.get("side"), "stop": None, "tp": None,
                     "size": None, "entry_ts": (ts - hold * 60) if (ts and hold) else ts}
                trades.append(t)
            if _num(e.get("entry")) is not None:
                t["entry"] = _num(e.get("entry"))
            t.update(open=False, exit=_num(e.get("exit")),
                     reason=REASONS.get(e.get("close_reason"),
                                        e.get("close_reason") or "?"),
                     net=_num(e.get("net")), hold_min=hold,
                     exit_ts=(t["entry_ts"] + hold * 60)
                     if (t.get("entry_ts") and hold is not None) else ts)
    return trades, account


def stats(trades: list) -> dict:
    closed = [t for t in trades if not t.get("open") and t.get("net") is not None]
    wins = [t for t in closed if t["net"] > 0]
    losses = [t for t in closed if t["net"] < 0]
    return {"closed": len(closed), "open": sum(1 for t in trades if t.get("open")),
            "wins": len(wins), "losses": len(losses),
            "net": round(sum(t["net"] for t in closed), 2),
            "best": round(max((t["net"] for t in closed), default=0), 2),
            "worst": round(min((t["net"] for t in closed), default=0), 2)}


# --------------------------------------------------------------------------
# 3. Page
# --------------------------------------------------------------------------

def _money(x: Optional[float], sign: bool = False) -> str:
    if x is None:
        return "-"
    s = f"{x:+,.2f}" if sign else f"{x:,.2f}"
    return s.replace(",", " ").replace(".", ",") + " $"


def _price(x: Optional[float]) -> str:
    if x is None:
        return "-"
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


_DAYS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
_MONTHS = ("janv.", "fevr.", "mars", "avr.", "mai", "juin", "juil.", "aout",
           "sept.", "oct.", "nov.", "dec.")


def _local(ts: Optional[float]) -> str:
    """Heure locale du PC, en francais, sans dependre de la langue de Windows."""
    if not ts:
        return "-"
    d = datetime.fromtimestamp(ts)
    return f"{_DAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}  {d:%H:%M}"


MAX_FILL_BUCKETS = 14 * 24 * 12           # 14 jours de cases de 5 min


def _bucket(t: float) -> int:
    return int(t // BUCKET_SECONDS) * BUCKET_SECONDS


def chart_data(prices: dict, trades: list) -> dict:
    """
    Donnees du graphique, marche par marche.

    Un trade dont les prix n'ont pas ete archives (PC en veille, moteur
    arrete...) est dessine quand meme : des cases vides sont ajoutees sur
    l'axe du temps pour sa duree, et la boite s'y pose, sans bougies.
    """
    markets = {}
    for sym in sorted(set(prices) | {t["sym"] for t in trades if t.get("sym")}):
        bars = candles(prices.get(sym, []))
        have = {b["time"] for b in bars}
        empty: set = set()
        items, missing = [], 0
        last_close = bars[-1]["close"] if bars else None
        last_time = bars[-1]["time"] if bars else None
        for t in trades:
            if (t.get("sym") != sym or t.get("entry") is None
                    or t.get("entry_ts") is None):
                continue
            start = _bucket(t["entry_ts"])
            if t.get("open"):
                end = max(last_time or start, start)
                exit_px = last_close if last_close is not None else t["entry"]
            else:
                end = max(_bucket(t.get("exit_ts") or t["entry_ts"]), start)
                exit_px = t.get("exit")
            holes = 0
            x, n = start, 0
            while x <= end and n < MAX_FILL_BUCKETS:
                if x not in have:
                    empty.add(x)
                    holes += 1
                x += BUCKET_SECONDS
                n += 1
            if holes:
                missing += 1
            end = min(end, start + (MAX_FILL_BUCKETS - 1) * BUCKET_SECONDS)
            items.append({
                "side": t.get("side"), "open": bool(t.get("open")),
                "entry_time": start, "exit_time": end,
                "entry": t["entry"], "stop": t.get("stop"), "tp": t.get("tp"),
                "exit": exit_px, "reason": t.get("reason") or "en cours",
                "net": t.get("net")})
        series = sorted(bars + [{"time": x} for x in empty],
                        key=lambda c: c["time"])
        if series:
            # Marge a gauche : un trade tout au debut garde ses etiquettes.
            first = series[0]["time"]
            series = [{"time": first - k * BUCKET_SECONDS}
                      for k in range(6, 0, -1)] + series
        markets[sym] = {"candles": series, "trades": items,
                        "has_prices": bool(bars), "missing": missing}
    return markets


PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trades du moteur</title>
<style>
  :root { --bg:#ffffff; --fg:#1d2330; --muted:#6b7385; --line:#e3e6ec;
          --card:#f6f7f9; --win:#0f9d76; --loss:#d64545; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#12151b; --fg:#e7eaf0; --muted:#9aa3b5; --line:#262b35;
            --card:#1a1e26; --win:#23c48e; --loss:#ef5f5f; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
  main { max-width:1200px; margin:0 auto; padding:20px 16px 40px; }
  h1 { font-size:22px; margin:0 0 4px; }
  h2 { font-size:16px; margin:28px 0 10px; }
  .sub { color:var(--muted); margin:0 0 16px; }
  .stats { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:8px; }
  .stat { background:var(--card); border:1px solid var(--line);
          border-radius:10px; padding:10px 14px; min-width:120px; }
  .stat b { display:block; font-size:18px; }
  .stat span { color:var(--muted); font-size:12px; }
  .win { color:var(--win); } .loss { color:var(--loss); }
  .note { color:var(--muted); font-size:13px; margin:6px 0 0; }
  .legend { display:flex; gap:16px; flex-wrap:wrap; color:var(--muted);
            font-size:13px; margin:14px 0 6px; }
  .sw { display:inline-block; width:12px; height:12px; border-radius:2px;
        vertical-align:-1px; margin-right:6px; }
  .chartbox { position:relative; height:460px; border:1px solid var(--line);
              border-radius:10px; overflow:hidden; margin-bottom:6px; }
  .charttitle { font-weight:600; margin:18px 0 6px; }
  .empty { padding:24px; color:var(--muted); border:1px dashed var(--line);
           border-radius:10px; }
  .tablewrap { overflow-x:auto; }
  table { border-collapse:collapse; width:100%; min-width:760px; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line);
           white-space:nowrap; }
  th { color:var(--muted); font-weight:500; font-size:12px; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  footer { color:var(--muted); font-size:12px; margin-top:24px; }
</style>
</head>
<body>
<main>
  <h1>Trades du moteur</h1>
  <p class="sub">__SUBTITLE__</p>
  <div class="stats">__STATS__</div>
  __NOTE__
  <div class="legend">
    <span><i class="sw" style="background:rgba(15,157,118,.35)"></i>zone de l'objectif</span>
    <span><i class="sw" style="background:rgba(214,69,69,.35)"></i>zone du stop</span>
    <span><i class="sw" style="background:var(--fg);height:2px;vertical-align:3px"></i>de l'entree a la sortie</span>
  </div>
  <div id="charts"></div>
  <h2>Liste des trades</h2>
  <div class="tablewrap">__TABLE__</div>
  <footer>Genere le __GENERATED__ a partir des journaux du moteur. Graphiques :
    TradingView Lightweight Charts (licence Apache 2.0).</footer>
</main>
__LIB__
<script>
const DATA = __DATA__;
(function () {
  const dark = window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches;
  const C = dark
    ? {bg:'#12151b', fg:'#c9cfdb', grid:'#20252e', win:'#23c48e', loss:'#ef5f5f'}
    : {bg:'#ffffff', fg:'#1d2330', grid:'#eef0f4', win:'#0f9d76', loss:'#d64545'};
  // Heure locale du navigateur sur l'axe du temps.
  const OFF = -new Date().getTimezoneOffset() * 60;
  const root = document.getElementById('charts');
  const syms = Object.keys(DATA.markets);
  if (!syms.length || typeof LightweightCharts === 'undefined') {
    root.innerHTML = '<div class="empty">Pas encore de prix archives. Relance '
      + 'la commande dans quelques minutes, une fois le moteur demarre.</div>';
    return;
  }
  for (const sym of syms) {
    const m = DATA.markets[sym];
    const title = document.createElement('div');
    title.className = 'charttitle';
    title.textContent = sym + '  (' + m.trades.length + ' trade'
      + (m.trades.length > 1 ? 's' : '')
      + (m.missing ? ', dont ' + m.missing + ' sans bougies : prix non archives' : '')
      + ')';
    root.appendChild(title);
    const box = document.createElement('div');
    box.className = 'chartbox';
    root.appendChild(box);
    if (!m.candles.length) {
      box.innerHTML = '<div class="empty">Pas de prix archives pour ce marche.</div>';
      continue;
    }
    const chart = LightweightCharts.createChart(box, {
      autoSize: true,
      layout: {background: {color: C.bg}, textColor: C.fg},
      grid: {vertLines: {color: C.grid}, horzLines: {color: C.grid}},
      timeScale: {timeVisible: true, secondsVisible: false, rightOffset: 6},
      crosshair: {mode: 0},
    });
    // L'echelle de prix inclut les stops et objectifs des trades visibles,
    // sinon une boite peut sortir de l'ecran.
    const ts = chart.timeScale();
    const series = chart.addCandlestickSeries({
      upColor: C.win, downColor: C.loss, borderVisible: false,
      wickUpColor: C.win, wickDownColor: C.loss,
      autoscaleInfoProvider: original => {
        const res = original();
        const vr = ts.getVisibleRange();
        const levels = [];
        for (const t of m.trades) {
          if (vr && (t.exit_time + OFF < vr.from || t.entry_time + OFF > vr.to)) continue;
          for (const v of [t.entry, t.stop, t.tp, t.exit]) if (v != null) levels.push(v);
        }
        if (!levels.length) return res;
        const lo = Math.min(...levels), hi = Math.max(...levels);
        // Zone sans bougies : l'echelle se cale sur les niveaux du trade.
        if (!res) return {priceRange: {minValue: lo, maxValue: hi}};
        res.priceRange.minValue = Math.min(res.priceRange.minValue, lo);
        res.priceRange.maxValue = Math.max(res.priceRange.maxValue, hi);
        return res;
      }});
    series.setData(m.candles.map(c => c.open === undefined
      ? {time: c.time + OFF}
      : {time: c.time + OFF, open: c.open, high: c.high, low: c.low,
         close: c.close}));
    // Les reperes de la librairie ne tiennent que sur une vraie bougie. Sur
    // une case vide (prix non archives), le calque les dessine lui-meme.
    const real = new Set(m.candles.filter(c => c.open !== undefined).map(c => c.time));
    const marks = [];
    for (const t of m.trades) {
      const buy = t.side === 'buy';
      t.label = t.reason + (t.net == null ? '' : ' ' + (t.net >= 0 ? '+' : '')
        + Math.round(t.net) + ' $');
      t.entryOnBar = real.has(t.entry_time);
      t.exitOnBar = t.open || real.has(t.exit_time);
      if (t.entryOnBar) marks.push({time: t.entry_time + OFF,
        position: buy ? 'belowBar' : 'aboveBar', color: buy ? C.win : C.loss,
        shape: buy ? 'arrowUp' : 'arrowDown', text: buy ? 'Achat' : 'Vente'});
      if (!t.open && t.exitOnBar) marks.push({time: t.exit_time + OFF,
        position: 'inBar', color: (t.net || 0) >= 0 ? C.win : C.loss,
        shape: 'circle', text: t.label});
    }
    marks.sort((a, b) => a.time - b.time);
    series.setMarkers(marks);

    // Boites et lignes : un calque dessine par-dessus le graphique, recale a
    // chaque image sur les coordonnees du graphique (zoom, defilement).
    const cv = document.createElement('canvas');
    Object.assign(cv.style, {position: 'absolute', left: 0, top: 0,
      pointerEvents: 'none', zIndex: 3});
    box.appendChild(cv);
    function draw() {
      const w = box.clientWidth, h = box.clientHeight;
      const r = window.devicePixelRatio || 1;
      if (cv.width !== Math.round(w * r) || cv.height !== Math.round(h * r)) {
        cv.width = Math.round(w * r); cv.height = Math.round(h * r);
        cv.style.width = w + 'px'; cv.style.height = h + 'px';
      }
      const g = cv.getContext('2d');
      g.setTransform(r, 0, 0, r, 0, 0);
      g.clearRect(0, 0, w, h);
      const plotW = w - chart.priceScale('right').width();
      const plotH = h - ts.height();
      g.save(); g.beginPath(); g.rect(0, 0, plotW, plotH); g.clip();
      for (const t of m.trades) {
        let x1 = ts.timeToCoordinate(t.entry_time + OFF);
        let x2 = ts.timeToCoordinate(t.exit_time + OFF);
        if (x1 === null || x2 === null) continue;
        if (x2 - x1 < 6) x2 = x1 + 6;
        const yE = series.priceToCoordinate(t.entry);
        if (yE === null) continue;
        const zone = (price, color) => {
          if (price == null) return;
          const y = series.priceToCoordinate(price);
          if (y === null) return;
          g.fillStyle = color + '33'; g.strokeStyle = color + 'aa'; g.lineWidth = 1;
          g.fillRect(x1, Math.min(yE, y), x2 - x1, Math.abs(y - yE));
          g.strokeRect(x1 + .5, Math.min(yE, y) + .5, x2 - x1, Math.abs(y - yE));
        };
        zone(t.tp, C.win);
        zone(t.stop, C.loss);
        const yX = series.priceToCoordinate(t.exit);
        if (yX !== null) {
          g.strokeStyle = t.open ? C.fg : ((t.net || 0) >= 0 ? C.win : C.loss);
          g.lineWidth = 2; g.setLineDash(t.open ? [4, 4] : [6, 3]);
          g.beginPath(); g.moveTo(x1, yE); g.lineTo(x2, yX); g.stroke();
          g.setLineDash([]);
        }
        g.fillStyle = C.fg; g.fillRect(x1 - 1, yE - 1, x2 - x1 + 2, 2);
        g.font = '12px system-ui, -apple-system, "Segoe UI", sans-serif';
        g.textAlign = 'center';
        const buy = t.side === 'buy';
        if (!t.entryOnBar) {
          g.fillStyle = buy ? C.win : C.loss;
          g.fillText(buy ? '\\u25B2 Achat' : '\\u25BC Vente', x1, buy ? yE + 18 : yE - 10);
        }
        if (!t.exitOnBar && yX !== null) {
          g.fillStyle = (t.net || 0) >= 0 ? C.win : C.loss;
          g.beginPath(); g.arc(x2, yX, 4, 0, 2 * Math.PI); g.fill();
          g.fillText(t.label, x2, yX + 18);
        }
      }
      g.restore();
      requestAnimationFrame(draw);
    }
    requestAnimationFrame(draw);
    // Tout l'historique tient a l'ecran jusqu'a ~3 jours de bougies ; au-dela,
    // cadrage sur les 3 derniers jours (la molette et le glisser font le reste).
    if (m.candles.length <= 900) {
      ts.fitContent();
    } else {
      const end = m.candles[m.candles.length - 1].time + OFF;
      ts.setVisibleRange({from: end - 3 * 86400, to: end});
    }
  }
})();
</script>
</body>
</html>
"""


def _lib_tag() -> str:
    try:
        with open(LIB_FILE, encoding="utf-8") as f:
            return "<script>\n" + f.read() + "\n</script>"
    except OSError:
        return f'<script src="{LIB_CDN}"></script>'


def build_html(prices: dict, trades: list, account: Optional[str],
               now: Optional[float] = None) -> str:
    now = now or datetime.now().timestamp()
    st = stats(trades)
    rate = f"{st['wins'] / st['closed'] * 100:.0f} %" if st["closed"] else "-"
    cards = [
        ("Trades fermes", str(st["closed"]), ""),
        ("En cours", str(st["open"]), ""),
        ("Gagnants / perdants", f"{st['wins']} / {st['losses']}", ""),
        ("Reussite", rate, ""),
        ("Resultat net", _money(st["net"], True),
         "win" if st["net"] > 0 else "loss" if st["net"] < 0 else ""),
        ("Meilleur / pire", f"{_money(st['best'], True)} / {_money(st['worst'], True)}", ""),
    ]
    stats_html = "".join(
        f'<div class="stat"><b class="{cls}">{html.escape(v)}</b>'
        f'<span>{html.escape(k)}</span></div>' for k, v, cls in cards)
    note = ""
    if st["closed"] < 30:
        note = ('<p class="note">Echantillon trop petit pour juger le signal : '
                f'{st["closed"]} trade(s) ferme(s). Le verdict se fait sur plusieurs '
                'dizaines de trades.</p>')

    rows = []
    for t in sorted(trades, key=lambda x: x.get("entry_ts") or 0, reverse=True):
        net = t.get("net")
        cls = "win" if (net or 0) > 0 else "loss" if (net or 0) < 0 else ""
        side = {"buy": "Achat", "sell": "Vente"}.get(t.get("side"), "?")
        hold = t.get("hold_min")
        rows.append(
            "<tr>"
            f"<td>{html.escape(_local(t.get('entry_ts')))}</td>"
            f"<td>{html.escape(str(t.get('sym') or '?'))}</td>"
            f"<td>{side}</td>"
            f"<td class='num'>{_price(t.get('entry'))}</td>"
            f"<td class='num'>{_price(t.get('stop'))}</td>"
            f"<td class='num'>{_price(t.get('tp'))}</td>"
            f"<td class='num'>{_price(t.get('exit')) if not t.get('open') else 'en cours'}</td>"
            f"<td>{html.escape(t.get('reason') or ('en cours' if t.get('open') else '?'))}</td>"
            f"<td class='num {cls}'>{_money(net, True) if net is not None else '-'}</td>"
            f"<td class='num'>{'-' if hold is None else f'{hold:.0f} min'}</td>"
            "</tr>")
    table = ("<table><thead><tr><th>Entree</th><th>Marche</th><th>Sens</th>"
             "<th>Prix d'entree</th><th>Stop</th><th>Objectif</th><th>Sortie</th>"
             "<th>Fin</th><th>Net</th><th>Duree</th></tr></thead><tbody>"
             + ("".join(rows) or "<tr><td colspan='10'>Aucun trade pour l'instant."
                                 "</td></tr>")
             + "</tbody></table>")

    data = json.dumps({"markets": chart_data(prices, trades)},
                      ensure_ascii=False).replace("</", "<\\/")
    sub = (f"Compte sandbox {account}" if account else "Compte sandbox") + \
        " - prix archives : " + ", ".join(
            f"{s} {len(p)} points" for s, p in sorted(prices.items())) \
        if prices else (f"Compte sandbox {account}" if account else "Compte sandbox")
    out = PAGE
    for key, val in (("__SUBTITLE__", html.escape(sub)),
                     ("__STATS__", stats_html), ("__NOTE__", note),
                     ("__TABLE__", table),
                     ("__GENERATED__", html.escape(_local(now))),
                     ("__DATA__", data)):
        out = out.replace(key, val)
    return out.replace("__LIB__", _lib_tag())


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    sources = [STATE_FILE] + sorted(glob.glob(SAVED_COPIES))
    added = archive_prices(sources, PRICES_CSV)
    prices = load_prices(PRICES_CSV)
    trades, account = load_trades(TRADES_LOG)
    page = build_html(prices, trades, account)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(page)
    st = stats(trades)
    path = Path(OUTPUT).resolve()
    print(f"Page ecrite : {path}")
    print(f"  {st['closed']} trade(s) ferme(s), {st['open']} en cours, "
          f"net {st['net']:+.2f} $ ; {added} nouveau(x) prix archive(s)")
    if "--no-open" not in argv:
        webbrowser.open(path.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
