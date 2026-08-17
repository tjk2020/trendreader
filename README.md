# Trend Reader

A mobile-friendly web app that reads a stock/index/currency/commodity ticker's
current technical setup and tells you how price has actually moved, historically,
the last several times that ticker showed the same setup — over the following
3-4 weeks.

**Important framing:** this is a historical base-rate tool, not a prediction
engine. Nothing can reliably call direction 3-4 weeks out with real accuracy —
that horizon is short enough that markets price in most obvious information.
What this app gives you instead is an honest, transparent answer to: *"the last
N times this ticker looked like this, what happened next?"* — with the sample
size shown every time, so you can see when there's too little history to trust
the read.

---

## 1. Run it locally

```bash
cd trend-app
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000` on your phone (same wifi network — use your
computer's local IP instead of `localhost`, e.g. `http://192.168.1.23:5000`)
or in your computer's browser to try it first.

**Note on testing:** I wrote and unit-tested the indicator math and backtest
logic against synthetic price data, and it runs correctly end-to-end. I could
not test it against *real* live market data in the environment I built this
in, since it has no internet access. The very first thing to check when you
run it: pull up a well-known ticker (e.g. `AAPL`) and eyeball whether the RSI/
price/moving averages look sane against a chart you trust (TradingView, Yahoo
Finance itself, etc.) before relying on it.

## 2. Get it on your phone as an "app"

Since you're not going through an app store, "installing" it means:

1. Deploy the backend somewhere reachable from your phone (see below), or run
   it on a computer on your home network as above.
2. Open the URL in your phone's browser.
3. Use the browser's **Share → Add to Home Screen** (iOS Safari) or
   **⋮ menu → Install app** (Android Chrome).

It'll then open full-screen from your home screen like a normal app, using the
manifest/service worker already included.

## 3. Deploy it so it's reachable anywhere (not just home wifi)

Cheapest reliable options for a small Flask app like this:

- **Render.com** (free web service tier) — connect a GitHub repo, it detects
  `requirements.txt` and runs `python app.py` (or set the start command to
  `gunicorn app:app` for production — see note below).
- **Railway.app** — similar, generous free tier.
- **PythonAnywhere** — free tier, good for small Flask apps.

For any of these, add `gunicorn` to `requirements.txt` and set the start
command to:
```
gunicorn app:app
```
instead of running `app.py` directly with Flask's dev server (which the app
uses for local testing only).

## 4. Ticker formats (Yahoo Finance symbols, via `yfinance`)

| Asset type | Examples |
|---|---|
| Indian stock (NSE) | `RELIANCE.NS`, `TCS.NS`, `INFY.NS` |
| Indian index | `^NSEI` (Nifty 50), `^NSEBANK` (Bank Nifty), `^BSESN` (Sensex) |
| US stock | `AAPL`, `MSFT`, `NVDA` |
| Global index | `^GSPC` (S&P 500), `^DJI`, `^IXIC`, `^FTSE`, `^N225` |
| Currency pair | `INR=X` (USD/INR), `EURUSD=X`, `GBPUSD=X` |
| Commodity | `GC=F` (gold), `SI=F` (silver), `CL=F` (WTI crude), `NG=F` (nat gas) |

The app has tappable example chips per asset type in the UI, but any valid
Yahoo Finance symbol works in the ticker box.

## 5. What it actually computes

Plain-English version (full formulas are in `app.py` if you want them):

1. Pulls ~6 years of daily price history.
2. Computes standard indicators: 20/50/200-day moving averages, RSI (14),
   MACD, ADX (trend strength), ATR (volatility), Bollinger Bands, volume vs.
   its 20-day average.
3. Buckets today's readings into a "regime" — e.g. *uptrend structure, firm
   momentum, MACD positive and rising, strong trend*.
4. Scans the ticker's own history for every other day that fell into that
   same bucket, and looks at what happened to price over the following 15
   and 20 trading days (roughly 3 and 4 weeks) each time.
5. Reports: how many such instances exist, what % of them were positive, and
   the average/median move — plus a plain "Bullish lean / Bearish lean / No
   clear edge / Not enough history" label so you don't have to do the mental
   math yourself.

If fewer than ~8 historical matches exist, it tells you outright rather than
dressing up a near-meaningless sample as a signal.

## 6. Known limitations / honest gaps

- **No options/IV data.** Given your options background, IV skew/term
  structure and PCR would likely add real signal for Nifty-style trading —
  but that data isn't free. This version is price/volume-technical only. If
  you get access to a paid data source (broker API, etc.) later, that's a
  natural v2 addition and I can help wire it in.
- **`yfinance` scrapes Yahoo Finance** rather than using an official paid
  API — it's free and generally reliable but can occasionally break if Yahoo
  changes something, or rate-limit under heavy use. If a ticker suddenly
  stops working, that's the first thing to suspect.
- **Regime buckets can get sparse** for tickers with short listing history or
  unusual price behavior (e.g. recently-IPO'd stocks, some commodities) —
  the app will tell you when sample size is too low rather than faking
  confidence.
- **Data is delayed/EOD**, not real-time — fine for a 3-4 week horizon tool,
  not for intraday decisions.
- Icons use an inline SVG rather than proper PNG app icons — works on
  Android/Chrome; iOS home-screen icons look better with a real PNG. Happy to
  generate a proper icon set if you want to polish this further.

## 7. Not investment advice

This tool surfaces historical statistics about a ticker's own past behavior.
It doesn't know about news, earnings, macro events, or anything not reflected
in price/volume history. Treat every read as one input among several, not a
signal to act on alone.
