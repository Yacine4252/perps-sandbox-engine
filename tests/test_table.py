"""Tests de la table des associes et du siege calendrier (v2.5)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from perps_engine import calendrier as cal
from perps_engine import table as tb

ET = timezone(timedelta(hours=-5))                # New York en hiver
FRI = datetime(2027, 1, 15, 8, 30, tzinfo=ET)     # un vendredi, 8 h 30
T = FRI.timestamp()


def ev(dt, title, country="USD", impact="High"):
    return {"title": title, "country": country, "date": dt.isoformat(),
            "impact": impact, "forecast": "", "previous": ""}


# Forme exacte du flux Forex Factory (faireconomy, ff_calendar_thisweek.json).
FEED = [
    ev(FRI - timedelta(days=4, hours=8, minutes=30), "Bank Holiday",
       impact="Holiday"),
    ev(FRI - timedelta(days=2), "CPI m/m"),
    ev(FRI - timedelta(hours=4), "German Prelim CPI m/m", country="EUR"),
    ev(FRI, "Non-Farm Employment Change"),
    ev(FRI, "Unemployment Rate"),
    ev(FRI + timedelta(hours=1, minutes=30), "ISM Services PMI",
       impact="Medium"),
    {"title": "illisible", "country": "USD", "date": "demain", "impact": "High"},
]


def make(tmp_path, feed=FEED):
    calls = []

    def fetch(url):
        calls.append(url)
        if isinstance(feed, Exception):
            raise feed
        return feed

    return cal.Calendar(str(tmp_path / "calendrier-ff.json"), fetch=fetch), calls


# --------------------------------------------------------------------------
# Lecture du flux
# --------------------------------------------------------------------------

def test_feed_is_read_with_its_own_timezone():
    events, end = cal.parse_feed(FEED)
    assert len(events) == 6                          # la ligne illisible saute
    nfp = next(e for e in events if e.title.startswith("Non-Farm"))
    assert nfp.ts == datetime(2027, 1, 15, 13, 30, tzinfo=timezone.utc).timestamp()
    assert (nfp.country, nfp.impact) == ("USD", "high")
    # La semaine Forex Factory finit le dimanche 00:00 (heure de New York).
    assert end == datetime(2027, 1, 17, 0, 0, tzinfo=ET).timestamp()
    assert [e.ts for e in events] == sorted(e.ts for e in events)


@pytest.mark.parametrize("bad", [{"error": "rate limited"}, [], "<html>",
                                 [{"date": "x"}], [1, 2]])
def test_unreadable_feed_is_refused(bad):
    with pytest.raises(ValueError):
        cal.parse_feed(bad)


def test_http_fetch_refuses_html_and_errors(monkeypatch):
    class Resp:
        def __init__(self, status, payload=None):
            self.status_code, self._p = status, payload

        def json(self):
            if self._p is None:
                raise ValueError("html")
            return self._p

    seen = {}

    def get(url, timeout=None, headers=None):
        seen.update(headers or {})
        return replies.pop(0)

    replies = [Resp(200), Resp(429), Resp(200, FEED)]
    monkeypatch.setattr(cal.requests, "get", get)
    with pytest.raises(RuntimeError):
        cal.http_fetch()                         # page de limite, pas du JSON
    with pytest.raises(RuntimeError):
        cal.http_fetch()                         # HTTP 429
    assert cal.http_fetch() == FEED
    assert "@" not in seen["User-Agent"]         # aucune donnee personnelle


# --------------------------------------------------------------------------
# Le vote
# --------------------------------------------------------------------------

def test_vote_window_is_30_min_before_to_60_min_after(tmp_path):
    c, _ = make(tmp_path)
    c.refresh(T - 86400)

    def vote(minutes):
        return c.vote({"now": T + minutes * 60})["vote"]

    assert vote(-31) == "pour" and vote(-30) == "contre" and vote(-5) == "contre"
    assert vote(60) == "contre" and vote(61) == "pour"
    r = c.vote({"now": T - 600})
    assert "Non-Farm Employment Change" in r["raison"] and "dans 10 min" in r["raison"]


def test_foreign_and_medium_news_never_vote_against(tmp_path):
    c, _ = make(tmp_path)
    c.refresh(T - 86400)
    assert c.vote({"now": T - 4 * 3600})["vote"] == "pour"     # CPI allemand
    r = c.vote({"now": T + 75 * 60})                           # ISM dans 15 min
    assert r["vote"] == "pour"
    assert {"titre": "ISM Services PMI", "impact": "medium",
            "minutes": 15} in r["annonces"]


def test_abstains_without_calendar_or_past_the_week(tmp_path):
    c, _ = make(tmp_path, feed=RuntimeError("HTTP 503"))
    assert c.refresh(T)["event"] == "calendar_error"
    r = c.vote({"now": T})
    assert r["vote"] == "abstient" and "HTTP 503" in r["raison"]

    (tmp_path / "b").mkdir()
    c2, _ = make(tmp_path / "b", feed=FEED)
    c2.refresh(T - 86400)
    sunday = datetime(2027, 1, 17, 0, 0, tzinfo=ET).timestamp()
    assert c2.vote({"now": sunday - 12 * 3600})["vote"] == "pour"   # samedi midi
    assert c2.vote({"now": sunday - 10 * 60})["vote"] == "abstient"


# --------------------------------------------------------------------------
# Relecture : polie, robuste, persistante
# --------------------------------------------------------------------------

def test_refresh_is_polite_and_keeps_the_last_good_copy(tmp_path):
    c, calls = make(tmp_path)
    assert c.refresh(T - 86400)["event"] == "calendar_read"
    assert c.refresh(T - 86400 + 3600) is None and len(calls) == 1   # < 3 h

    failures = []

    def down(url):
        failures.append(url)
        raise RuntimeError("HTTP 503")

    c.fetch = down
    t1 = T - 86400 + cal.REFRESH_SECONDS
    assert c.refresh(t1)["event"] == "calendar_error"
    assert c.vote({"now": T - 600})["vote"] == "contre"     # copie gardee
    assert c.refresh(t1 + 60) is None and len(failures) == 1  # attend 30 min
    assert c.refresh(t1 + cal.RETRY_SECONDS) is None          # erreur deja notee
    assert len(failures) == 2


def test_copy_on_disk_survives_a_restart_without_refetch(tmp_path):
    c, _ = make(tmp_path)
    c.refresh(T - 86400)
    again, calls = make(tmp_path)
    assert again.vote({"now": T - 600})["vote"] == "contre"
    assert again.refresh(T - 86400 + 600) is None and calls == []


def test_rescheduled_announcement_leaves_its_old_time(tmp_path):
    c, _ = make(tmp_path)
    c.refresh(T - 86400)
    moved = [dict(e) for e in FEED]
    for e in moved:
        if e["title"] in ("Non-Farm Employment Change", "Unemployment Rate"):
            e["date"] = (FRI + timedelta(hours=3)).isoformat()
    c.fetch = lambda url: moved
    c.refresh(T - 86400 + cal.REFRESH_SECONDS)
    assert c.vote({"now": T - 600})["vote"] == "pour"            # plus a 8 h 30
    assert c.vote({"now": T + 3 * 3600 - 600})["vote"] == "contre"   # 11 h 30


def test_check_and_summary_lines_name_the_next_announcement(tmp_path):
    c, _ = make(tmp_path)
    c.refresh(T - 3 * 3600)
    lines = c.check_lines(T - 3 * 3600)
    assert "Forex Factory" in lines[0] and "2 annonce(s) USD forte(s)" in lines[0]
    assert "Non-Farm Employment Change" in lines[1] and "dans 3 h" in lines[1]
    assert "Non-Farm" in c.summary_line(T - 3 * 3600)
    (tmp_path / "x").mkdir()
    down, _ = make(tmp_path / "x", feed=RuntimeError("HTTP 503"))
    down.refresh(T)
    assert "pas lu (RuntimeError: HTTP 503)" in down.check_lines(T)[0]


# --------------------------------------------------------------------------
# La table
# --------------------------------------------------------------------------

CTX = {"sym": "BTC", "side": "buy", "z": 2.41, "price": 80_000.0, "now": T}


class Against:
    name = "calendrier"

    def vote(self, ctx):
        return {"vote": "contre", "raison": "CPI dans 10 min"}


class Broken:
    name = "structure"

    def vote(self, ctx):
        raise KeyError("fvg")


def test_observation_never_blocks_and_veto_does(tmp_path):
    d = tb.Table([tb.MomentumSeat(), Against()]).deliberate(CTX)
    assert d["bloque_par"] == [] and d["votes"]["calendrier"]["mode"] == "observe"
    assert d["votes"]["momentum"] == {"vote": "pour", "raison": "signal buy z=+2.41",
                                      "mode": "propose"}
    veto = tb.Table([tb.MomentumSeat(), Against()], modes={"calendrier": "veto"})
    assert veto.deliberate(CTX)["bloque_par"] == ["calendrier"]
    with pytest.raises(ValueError):
        tb.Table([Against()], modes={"calendrier": "peut-etre"})


def test_a_broken_seat_abstains_instead_of_crashing(tmp_path):
    d = tb.Table([tb.MomentumSeat(), Broken()],
                 modes={"structure": "veto"}).deliberate(CTX)
    assert d["votes"]["structure"]["vote"] == "abstient"
    assert d["bloque_par"] == []


def test_record_writes_the_vote_and_counts_it(tmp_path):
    path = tmp_path / "sandbox-table.log"
    t = tb.Table([tb.MomentumSeat(), Against()], journal_path=str(path))
    t.record(t.deliberate(CTX), "filled")
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["event"] == "deliberation" and row["resultat"] == "filled"
    assert row["votes"]["calendrier"]["vote"] == "contre"
    assert t.counts == {"calendrier": {"pour": 0, "contre": 1, "abstient": 0}}
    assert "1 vote(s)" in t.summary_lines(T)[-1]


# --------------------------------------------------------------------------
# Bilan : les votes face aux resultats
# --------------------------------------------------------------------------

def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _lines(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _delib(t, sym, side, vote, result="filled"):
    return {"event": "deliberation", "sym": sym, "side": side, "z": 2.2,
            "votes": {"momentum": {"vote": "pour", "mode": "propose"},
                      "calendrier": {"vote": vote, "mode": "observe"}},
            "bloque_par": [], "resultat": result, "ts": _iso(t)}


def test_report_links_each_trade_to_its_vote(tmp_path):
    A, B, C = T, T + 4 * 3600, T + 8 * 3600
    trades = tmp_path / "sandbox-trades.log"
    _lines(trades, [
        {"event": "start", "account": "FP-1", "ts": _iso(T - 86400)},
        # trade d'avant la table
        {"event": "entry_order", "sym": "BTC", "side": "buy", "status": "filled",
         "fill_price": 79_000.0, "ts": _iso(T - 80_000)},
        {"event": "position_closed", "sym": "BTC", "side": "buy", "net": 30.0,
         "hold_min": 20, "ts": _iso(T - 78_800)},
        {"event": "entry_order", "sym": "BTC", "side": "buy", "status": "filled",
         "fill_price": 80_000.0, "ts": _iso(A + 2)},
        {"event": "position_closed", "sym": "BTC", "side": "buy", "net": -190.0,
         "hold_min": 25, "ts": _iso(A + 1500)},
        {"event": "entry_order", "sym": "GOLD", "side": "sell", "status": "filled",
         "fill_price": 4_000.0, "ts": _iso(B + 3)},
        {"event": "position_closed", "sym": "GOLD", "side": "sell", "net": 120.0,
         "hold_min": 40, "ts": _iso(B + 2400)},
    ])
    table = tmp_path / "sandbox-table.log"
    _lines(table, [_delib(A, "BTC", "buy", "contre"),
                   _delib(B, "GOLD", "sell", "pour"),
                   _delib(C, "BTC", "sell", "pour", result="rejected:DAILY_TRADE_CAP")])
    text = "\n".join(tb.report(str(trades), str(table)))
    assert "votes : 3   entrees prises : 2   refusees par le garde-fou : 1" in text
    assert "trades fermes relies a un vote : 2   (fermes avant la table : 1)" in text
    assert "calendrier (observation) - votes : pour 2, contre 1, abstient 0" in text
    assert "trades 'contre'   :   1   net     -190.00 $" in text
    assert "trades 'pour'     :   1   net     +120.00 $" in text
    assert "trop tot pour juger" in text


def test_verdict_needs_enough_trades_on_both_sides():
    few = {"contre": [-10.0] * 9, "pour": [5.0] * 30, "abstient": []}
    assert "trop tot" in tb.verdict(few)
    worse = {"contre": [-50.0] * 10, "pour": [5.0] * 10, "abstient": []}
    assert "candidat au veto" in tb.verdict(worse)
    same = {"contre": [10.0] * 10, "pour": [5.0] * 10, "abstient": []}
    assert "pas de veto" in tb.verdict(same)


def test_report_without_votes_says_so(tmp_path):
    lines = tb.report(str(tmp_path / "none.log"), str(tmp_path / "none2.log"))
    assert "aucun vote" in lines[1]
