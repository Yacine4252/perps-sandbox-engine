# perps-sandbox-engine

An autonomous perpetual-futures trading engine for the MyFundedPerps REST API. It is **sandbox only**: it trades simulated balances and is built so that it cannot reach a real account.

> **Unofficial, not affiliated with MyFundedPerps.** This is a personal research prototype. It is not financial advice and makes no promise of profit. So far it has **shown no trading edge** (see [Honest results](#honest-results)).

Code comments and research notes are in French.

---

## What it does

- **Watches** BTC and gold perps (`binance|BTCUSDT`, `binance|XAUUSDT`, `hyperliquid|xyz:GOLD`) by polling the sandbox API once a minute.
- **Computes a momentum signal**: a z-score of 60-minute returns, with volatility measured over 240 minutes.
- **Applies hysteresis** (`perps_engine/signals.py`):
  - a signal opens when |z| ≥ 2.0;
  - it only closes when |z| falls back below 1.0, or when it reverses on a fresh |z| ≥ 2.0.
- **Places the stop and target inside the entry order**, so a position is never open without a stop. The stop is set at 2σ, with a cost floor; the target is 2R.
- **Sizes every trade through a risk guard** before any order is sent (`perps_engine/risk.py`).
- **Logs every decision** as JSON lines: signals, verdicts, orders, closes, errors.
- **Runs one strategy per sandbox account** (v2.7). It can run several sandbox accounts at once:
  - each account gets its own `strategie.json` profile (`perps_engine/variantes.py`);
  - a separate slow trend-following engine for BTC runs on daily and 4-hour candles (`perps_engine/tendance_lente.py`).

### Sandbox only: the live host is refused

`perps_engine/executor.py` is the only module that can write to the API, and it refuses to work outside the sandbox:

- any API key that does not start with `fp_test_` raises `SandboxOnlyError`;
- any base URL other than `https://sandbox.myfundedperpetuals.com/v1` raises `SandboxOnlyError`;
- the read-only client (`client.py`) has no write verbs, and the observation engine refuses to start if it finds one.

Going live would require deliberately editing this file, and the tests would fail. That is intentional.

### Risk guardrails (`perps_engine/risk.py`)

| Rule | Value |
|---|---|
| Risk per trade, fees included | at most **1 % of starting balance** (hard cap), and at most 25 % of the remaining drawdown room. Sandbox account 1 currently runs at 0.3 % |
| Exposure | notional **≤ 3× equity** |
| Trades | at most 3 per 24 h, one open position at a time per account |
| After a loss | 45-minute cooldown |
| Stop distance | at least 5× the round-trip cost |
| News veto | no entry from 30 min before to 60 min after a high-impact USD event (Forex Factory calendar) |
| Stale data | an order is refused if the quote is too old |
| Kill switch | create a `HALT-SANDBOX` file and the engine stops cleanly. A lock file prevents two instances from running |

Every refused trade is logged with its reasons (`COOLDOWN_ACTIVE`, `VETO_CALENDRIER`, and so on).

### API reliability

- **REST polling, no WebSocket**: one cycle every 60 s.
- **Idempotency**: each order intent gets one `Idempotency-Key` header, written to the log *before* sending. A lost response is retried with the same key and body, so the platform returns the existing order instead of creating a second one. Each order also carries a `client_order_id`, used to look the order up after a timeout.
- **Backoff**:
  - HTTP 429 waits for `Retry-After` (clamped to 1–30 s);
  - 500, 502, 503, 504 and network errors back off exponentially (2 s, 4 s, …, capped at 10 s);
  - at most 3 attempts.

### Offline replay with the real code

`recherche/sieges/signaux_moteur.py` and `rejeu.py` replay the engine minute by minute on public Binance 1-minute candles. They import the engine's own `MomentumStrategy`, `SignalConfig` and `evaluate_order`, so the backtest runs the same code as the live loop. When checked against the live sandbox, 52 of 63 real signals were found in the replay, on the same side and within 5 minutes.

### Trade chart page

`python -m perps_engine.graphique` builds `graphique-trades.html`, a self-contained page (TradingView Lightweight Charts bundled, Apache 2.0) that works offline. It shows 5-minute candles and, for each trade, the entry-to-target and entry-to-stop boxes and the entry-to-exit line.

---

## Install (Windows or Linux, Python 3.12)

1. Get the code and install the dependencies:
   ```bash
   git clone <this repo>
   cd perps-sandbox-engine
   python -m venv .venv
   .venv/Scripts/pip install -r requirements.txt     # Linux: .venv/bin/pip
   ```
2. Create a **sandbox** key in the MyFundedPerps dashboard (Test environment, Read and Trade access). It starts with `fp_test_`.
3. Copy `.env.example` to `.env` and fill in `PERPS_TEST_KEY` and `PERPS_SANDBOX_ACCOUNT`. **Never commit `.env`.**
4. Check the setup, then start the engine:
   ```bash
   python -m perps_engine.sandbox_run --check        # key, account, markets: no order sent
   python -m perps_engine.sandbox_run                # start the engine
   python -m pytest -q tests                         # 221 tests, no network needed
   ```
5. Other commands:
   - `python lancer_compte.py <account folder> [--check]` runs another sandbox account. The folder holds its `strategie.json` profile and receives its logs.
   - `python point_comptes.py` prints a one-line report per account.

A sample of what the engine logs is in [`examples/sample-trades-log.jsonl`](examples/sample-trades-log.jsonl). It shows one real sandbox trade (gold long, closed at its target, +$570.66 simulated), with the account number removed.

---

## Honest results

- **Sandbox only**: every number below is simulated money.
- **No edge demonstrated.**
- **13-day offline replay**: an earlier replay came out at **−$1,452.74 net**.
- **331-day replay with the real engine code** (1-minute candles, 11 Dec 2025 to 6 Nov 2026, real fees): 787 trades.

  | | Net per trade | Profit factor | Total |
  |---|---|---|---|
  | All trades | −0.06 R | 0.76 | −47.6 R |
  | BTC | −0.11 R | — | — |
  | Gold | +0.02 R | — | — |

  Gold's slight gain does not hold across all its signals, so it is not a reliable edge.
- **Live sandbox, account 1, 5 to 20 Nov 2026**: 38 closed trades, +$1,793 net (gold +$3,475, BTC −$1,682). This is far too few trades to mean anything. BTC was paused on 20 Nov.
- **Ideas tested that showed no edge after costs** (notes in `recherche/*/RESULTATS.md`):
  - a 5-minute fair-value-gap scalper;
  - signal filters and "seat" voting;
  - stop-management variants;
  - opening-range breakout (London, New York, Asia).
- **Slow trend-following**: Donchian and 4-week momentum on BTC daily candles looked robust on 2019–2026 history. It has only been running live on the sandbox since 22 Nov 2026, so nothing can be concluded yet.

**No profit is promised or implied.** Fees and funding are modelled. A strategy that looks good on history can still lose.

---

## Layout

```
perps_engine/        engine: signals, risk guard, sandbox executor, runners, chart page
tests/               221 unit tests (fake keys, mocked HTTP: no network, no orders)
recherche/           offline research: replays and backtests (code and notes only, no price data)
examples/            short log sample, account number removed
lancer_compte.py     run one sandbox account from its strategy profile
lancer_tendance.py   run the slow trend engine on one sandbox account
point_comptes.py     one-line report per sandbox account
```

Not included on purpose: `.env` and any key, raw logs, account state files, and price CSVs. Research scripts download public Binance or yfinance data themselves.

## Disclaimer

Unofficial and not affiliated with, endorsed by, or sponsored by MyFundedPerps. For research and education only. Trading perpetual futures carries a high risk of loss.
