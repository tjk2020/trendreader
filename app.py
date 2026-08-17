"""
Trend Reader — backend
-----------------------
Fetches daily price history for a ticker (stock / index / currency / commodity),
computes a standard set of technical indicators, classifies the current
"regime" those indicators describe, then looks back through the ticker's own
history for previous times it was in a similar regime and reports how price
actually moved over the following 3-4 weeks in those instances.

This is a historical-analog base-rate tool, not a forecast. It never claims
certainty — every reading ships with the sample size and win-rate it's based
on so you can judge whether to trust it.
"""

from flask import Flask, jsonify, request, send_from_directory
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import time
from datetime import datetime

app = Flask(__name__, static_folder="static", template_folder="templates")

# ----------------------------------------------------------------------------
# Yahoo Finance (via yfinance) rate-limits aggressively by IP, and free cloud
# hosts share a small pool of outbound IPs across many unrelated apps, so
# this server can get rate-limited even if THIS app makes very few requests.
# Two mitigations: (1) a short in-memory cache so repeat/duplicate lookups
# never re-hit Yahoo, (2) a realistic browser session + retry-with-backoff so
# transient blips resolve themselves without the user having to know that.
# ----------------------------------------------------------------------------

_CACHE = {}
_CACHE_TTL_SECONDS = 20 * 60  # 20 minutes

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
})


def fetch_history_with_retry(ticker: str, period: str, retries: int = 3, base_delay: float = 2.0):
    last_exc = None
    for attempt in range(retries):
        try:
            return yf.Ticker(ticker, session=_SESSION).history(period=period, interval="1d", auto_adjust=True)
        except Exception as exc:  # yfinance raises various exception types for 429s
            last_exc = exc
            if "Rate limit" in str(exc) or "Too Many Requests" in str(exc) or "429" in str(exc):
                time.sleep(base_delay * (attempt + 1))
                continue
            raise
    raise last_exc

# ----------------------------------------------------------------------------
# Indicator math (implemented by hand with pandas so the only hard dependency
# beyond Flask/pandas/numpy is yfinance itself).
# ----------------------------------------------------------------------------

def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def compute_macd(close: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def compute_adx(df: pd.DataFrame, period: int = 14):
    high, low, close = df["High"], df["Low"], df["Close"]
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    tr = compute_atr(df, period=1)  # raw true range, not yet smoothed
    prev_close = close.shift(1)
    raw_tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_smooth = raw_tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr_smooth.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr_smooth.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return adx.fillna(0), plus_di.fillna(0), minus_di.fillna(0)


def compute_bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


# ----------------------------------------------------------------------------
# Regime classification — turns continuous indicator values into discrete
# buckets so we can find genuinely comparable historical instances.
# ----------------------------------------------------------------------------

def classify_regime_row(row) -> dict:
    if row["close"] > row["sma50"] > row["sma200"]:
        trend_state = "bull"
    elif row["close"] < row["sma50"] < row["sma200"]:
        trend_state = "bear"
    else:
        trend_state = "mixed"

    rsi = row["rsi14"]
    if rsi < 30:
        momentum_state = "oversold"
    elif rsi < 50:
        momentum_state = "weak"
    elif rsi < 70:
        momentum_state = "strong"
    else:
        momentum_state = "overbought"

    macd_sign = "positive" if row["macd_hist"] > 0 else "negative"
    macd_dir = "rising" if row["macd_hist"] > row["macd_hist_prev"] else "falling"
    macd_state = f"{macd_sign}_{macd_dir}"

    trend_strength = "strong" if row["adx14"] >= 25 else "weak"

    return {
        "trend_state": trend_state,
        "momentum_state": momentum_state,
        "macd_state": macd_state,
        "trend_strength": trend_strength,
    }


def regime_key(regime: dict) -> str:
    return "|".join([
        regime["trend_state"], regime["momentum_state"],
        regime["macd_state"], regime["trend_strength"],
    ])


def plain_english(regime: dict, adx_val: float) -> str:
    trend_map = {"bull": "Uptrend structure (price above rising averages)",
                 "bear": "Downtrend structure (price below falling averages)",
                 "mixed": "No clean trend structure"}
    momentum_map = {"oversold": "momentum is oversold",
                     "weak": "momentum is soft",
                     "strong": "momentum is firm",
                     "overbought": "momentum is stretched/overbought"}
    macd_map = {
        "positive_rising": "MACD is positive and rising",
        "positive_falling": "MACD is positive but losing steam",
        "negative_rising": "MACD is negative but improving",
        "negative_falling": "MACD is negative and worsening",
    }
    strength = f"trend strength is {'strong' if regime['trend_strength']=='strong' else 'weak'} (ADX {adx_val:.0f})"

    return (f"{trend_map[regime['trend_state']]}; {momentum_map[regime['momentum_state']]}; "
            f"{macd_map[regime['macd_state']]}; {strength}.")


# ----------------------------------------------------------------------------
# Core analysis
# ----------------------------------------------------------------------------

def analyze_ticker(ticker: str, lookback_years: int = 6):
    cache_key = ticker.upper()
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1], None

    raw = fetch_history_with_retry(ticker, period=f"{lookback_years}y")
    if raw is None or raw.empty or len(raw) < 220:
        return None, f"Not enough price history found for '{ticker}' (need at least ~1 year of daily data)."

    df = raw.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)

    df["sma20"] = df["Close"].rolling(20).mean()
    df["sma50"] = df["Close"].rolling(50).mean()
    df["sma200"] = df["Close"].rolling(200).mean()
    df["rsi14"] = compute_rsi(df["Close"], 14)
    macd_line, signal_line, hist = compute_macd(df["Close"])
    df["macd"] = macd_line
    df["macd_signal"] = signal_line
    df["macd_hist"] = hist
    df["macd_hist_prev"] = df["macd_hist"].shift(1)
    df["adx14"], df["plus_di"], df["minus_di"] = compute_adx(df)
    df["atr14"] = compute_atr(df, 14)
    bb_u, bb_m, bb_l = compute_bollinger(df["Close"])
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = bb_u, bb_m, bb_l
    df["vol_sma20"] = df["Volume"].rolling(20).mean()
    df["vol_ratio"] = df["Volume"] / df["vol_sma20"].replace(0, np.nan)
    df["close"] = df["Close"]

    # Drop rows before indicators are warmed up (needs 200d SMA + MACD signal settle)
    df = df.dropna(subset=["sma200", "macd_hist_prev", "adx14"]).copy()
    if len(df) < 60:
        return None, f"Not enough warmed-up history for '{ticker}' to run a backtest."

    # Forward returns for backtest matching (computed for every historical row)
    df["fwd_ret_15d"] = df["close"].shift(-15) / df["close"] - 1
    df["fwd_ret_20d"] = df["close"].shift(-20) / df["close"] - 1

    regimes = df.apply(classify_regime_row, axis=1)
    df["regime_key"] = [regime_key(r) for r in regimes]

    latest = df.iloc[-1]
    latest_regime = classify_regime_row(latest)
    latest_key = regime_key(latest_regime)

    # Historical matches: same regime bucket, excluding the most recent 25 rows
    # (those don't have complete forward-return windows yet).
    history = df.iloc[:-25] if len(df) > 25 else df.iloc[0:0]
    matches = history[history["regime_key"] == latest_key]

    def summarize(col):
        sample = matches[col].dropna()
        n = len(sample)
        if n == 0:
            return {"sample_size": 0, "pct_positive": None, "avg_return_pct": None, "median_return_pct": None}
        return {
            "sample_size": int(n),
            "pct_positive": round(float((sample > 0).mean() * 100), 1),
            "avg_return_pct": round(float(sample.mean() * 100), 2),
            "median_return_pct": round(float(sample.median() * 100), 2),
        }

    bt_15 = summarize("fwd_ret_15d")
    bt_20 = summarize("fwd_ret_20d")

    # Blend 15d/20d pct_positive for a single headline read, weighting 20d
    # (closer to the "3-4 weeks" ask) slightly more.
    if bt_15["sample_size"] and bt_20["sample_size"]:
        blended_pct = round(bt_15["pct_positive"] * 0.4 + bt_20["pct_positive"] * 0.6, 1)
        blended_n = min(bt_15["sample_size"], bt_20["sample_size"])
    elif bt_20["sample_size"]:
        blended_pct = bt_20["pct_positive"]
        blended_n = bt_20["sample_size"]
    elif bt_15["sample_size"]:
        blended_pct = bt_15["pct_positive"]
        blended_n = bt_15["sample_size"]
    else:
        blended_pct = None
        blended_n = 0

    if blended_pct is None or blended_n < 8:
        label = "Not enough history"
        confidence_note = ("This exact indicator combination hasn't occurred often enough in "
                            f"{lookback_years} years of history for this ticker to say anything "
                            "reliable. Treat this as a coin flip.")
    elif blended_pct >= 58:
        label = "Bullish lean"
        confidence_note = None
    elif blended_pct <= 42:
        label = "Bearish lean"
        confidence_note = None
    else:
        label = "No clear edge"
        confidence_note = "Historically this setup has been close to a coin flip — no meaningful lean either way."

    if blended_n < 15 and confidence_note is None:
        confidence_note = (f"Small sample ({blended_n} historical instances) — directionally suggestive, "
                            "not statistically strong. Treat as a mild lean, not a call.")

    # Trim price series for charting (last ~180 trading days)
    chart_df = df.tail(180)
    price_series = [
        {
            "date": idx.strftime("%Y-%m-%d"),
            "close": round(float(row["close"]), 4),
            "sma20": None if pd.isna(row["sma20"]) else round(float(row["sma20"]), 4),
            "sma50": None if pd.isna(row["sma50"]) else round(float(row["sma50"]), 4),
            "sma200": None if pd.isna(row["sma200"]) else round(float(row["sma200"]), 4),
        }
        for idx, row in chart_df.iterrows()
    ]

    result = {
        "ticker": ticker,
        "as_of": df.index[-1].strftime("%Y-%m-%d"),
        "last_close": round(float(latest["close"]), 4),
        "price_series": price_series,
        "indicators": {
            "rsi14": round(float(latest["rsi14"]), 1),
            "macd": round(float(latest["macd"]), 4),
            "macd_signal": round(float(latest["macd_signal"]), 4),
            "macd_hist": round(float(latest["macd_hist"]), 4),
            "adx14": round(float(latest["adx14"]), 1),
            "plus_di": round(float(latest["plus_di"]), 1),
            "minus_di": round(float(latest["minus_di"]), 1),
            "atr14": round(float(latest["atr14"]), 4),
            "bb_upper": round(float(latest["bb_upper"]), 4) if not pd.isna(latest["bb_upper"]) else None,
            "bb_lower": round(float(latest["bb_lower"]), 4) if not pd.isna(latest["bb_lower"]) else None,
            "volume_ratio": round(float(latest["vol_ratio"]), 2) if not pd.isna(latest["vol_ratio"]) else None,
        },
        "regime": {
            **latest_regime,
            "plain_english": plain_english(latest_regime, float(latest["adx14"])),
        },
        "backtest": {
            "lookback_years": lookback_years,
            "horizon_15d": bt_15,
            "horizon_20d": bt_20,
        },
        "directional_read": {
            "label": label,
            "probability_up_pct": blended_pct,
            "sample_size": blended_n,
            "confidence_note": confidence_note,
        },
    }
    _CACHE[cache_key] = (time.time(), result)
    return result, None


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")


@app.route("/sw.js")
def sw():
    return send_from_directory("static", "sw.js")


@app.route("/api/analyze")
def api_analyze():
    ticker = request.args.get("ticker", "").strip()
    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400
    try:
        result, err = analyze_ticker(ticker)
    except Exception as exc:  # yfinance / network / bad-symbol errors land here
        msg = str(exc)
        if "Rate limit" in msg or "Too Many Requests" in msg or "429" in msg:
            friendly = ("Yahoo Finance is temporarily rate-limiting this server — this happens "
                        "sometimes on free hosting since many apps share the same outbound IP. "
                        "Wait a few minutes and try again; it usually clears on its own.")
            return jsonify({"error": friendly}), 429
        return jsonify({"error": f"Couldn't fetch or analyze '{ticker}': {exc}"}), 502
    if err:
        return jsonify({"error": err}), 404
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
