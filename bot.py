#!/usr/bin/env python3
"""
Pro Liquidity Matrix Bot (complete A->Z)
- Pre-alert at PKT 16:55 and post-open analysis at PKT 17:05 (NY session).
- Uses TwelveData (time_series) to fetch 5m & 15m candles.
- Detects sweep + green confirmation, retail stop clusters (stop-hunt zones),
  volume footprint spikes (big-player heuristic), builds trade plan avoiding retail stops,
  and sends Telegram messages.
Notes:
- Replace TELEGRAM_TOKEN, TELEGRAM_CHAT_ID and TD_API_KEY with your credentials or set env vars.
- Tune thresholds (RETAIL_CLUSTER_BAND, LOOKBACK_15M, MIN_VOLUME_SPIKE_RATIO, etc.) to your taste.
"""
import os
import time
import math
import requests
import pytz
from datetime import datetime, timedelta, time as dtime
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------- CONFIG ----------------
# Provide your tokens here or set environment variables TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TD_API_KEY
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8178081661:AAH6yqv3JbtWBoXE28HR_Jdwi8g4vthGaiI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5969642968")
TD_API_KEY = os.getenv("TD_API_KEY", "76354ebb3e514dd29aae42ba73c1ab4a")

# Symbols
SYMBOL_XAU = "XAU/USD"
SYMBOL_BTC = "BTC/USD"

# Pakistan timezone (Asia/Karachi)
PK_TZ = pytz.timezone("Asia/Karachi")

# PK times for alerts (NY session focused)
PRE_ALERT_PK = dtime(hour=16, minute=55)  # 16:55 PKT
POST_ALERT_PK = dtime(hour=17, minute=5)  # 17:05 PKT

# Strategy parameters (tweakable)
LOOKBACK_15M = 12      # 15m candles to scan for sweep
LOOKBACK_5M = 36       # number of 5m candles used for retail cluster detection (~3 hours)
XAU_SL_PIPS = 20       # pips for XAU (1 pip = 0.01)
BTC_SL_USD = 350
RR = 4.0
MIN_VOLUME_SPIKE_RATIO = 1.5  # volume spike ratio to flag footprint
RETAIL_CLUSTER_BAND = 0.15    # band (in price units) to consider cluster near price (adjust for XAU volatility)
MAX_ALERTS_PER_DAY = 2

# Networking / misc
REQUEST_TIMEOUT = 12
LOG_PREFIX = "[ProLiquidityBot]"

# ---------------- HELPERS ----------------
def send_telegram_message(text: str):
    """Send a message via Telegram Bot API (HTML)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"{LOG_PREFIX} Telegram send error:", e)
        return None

def twelvedata_get_series(symbol: str, interval: str = "15min", outputsize: int = 200):
    """Fetch time series from TwelveData and return oldest->newest list of candles."""
    if not TD_API_KEY:
        raise RuntimeError("TwelveData API key not set.")
    base = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "format": "JSON",
        "apikey": TD_API_KEY
    }
    r = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData error or invalid response: {data}")
    return list(reversed(data["values"]))

def parse_candles(raw_candles):
    """Convert TwelveData candle dicts to list of dicts with numeric fields and datetime objects (tz-aware)."""
    out = []
    for c in raw_candles:
        dt = datetime.fromisoformat(c["datetime"])
        out.append({
            "datetime": dt.replace(tzinfo=pytz.UTC),
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
            "volume": float(c.get("volume") or 0)
        })
    return out

# ---------------- PATTERN DETECTION ----------------
def detect_sweep_and_green(candles_15m, lookback=LOOKBACK_15M):
    """
    Detect 'sweep + green confirm' pattern on 15m timeframe.
    Returns detection dict with keys:
      - signal (bool), sweep_candle (dict), confirm_candle (dict), reason
    """
    if len(candles_15m) < lookback + 2:
        return {"signal": False, "reason": "not_enough_data"}
    window = candles_15m[-(lookback+1):]  # oldest -> newest
    for i in range(1, len(window)-1):
        c = window[i]
        prev_c = window[i-1]
        next_c = window[i+1]
        if c["low"] < prev_c["low"] and c["low"] < next_c["low"]:
            body = abs(c["open"] - c["close"])
            lower_wick = (c["open"] - c["low"]) if c["open"] > c["close"] else (c["close"] - c["low"])
            rng = max(c["high"] - c["low"], 1e-6)
            if lower_wick / rng > 0.35:
                confirm = next_c
                if confirm["close"] > confirm["open"]:
                    return {
                        "signal": True,
                        "sweep_candle": c,
                        "confirm_candle": confirm,
                        "sweep_index_from_end": len(window) - (i+1)
                    }
    return {"signal": False, "reason": "no_sweep_found"}

def compute_liquidity_zones(candles):
    """Return recent low/high (simple liquidity snapshot)."""
    if not candles:
        return {"recent_low": None, "recent_high": None, "last_close": None}
    lows = [c["low"] for c in candles]
    highs = [c["high"] for c in candles]
    return {"recent_low": min(lows), "recent_high": max(highs), "last_close": candles[-1]["close"]}

def detect_retail_stop_cluster(candles_5m, band=RETAIL_CLUSTER_BAND, lookback_minutes=180):
    """
    Heuristic detection of retail stop clusters:
    - Looks for concentration of highs or lows in a narrow price band over lookback window.
    Returns {'side': 'BUY'/'SELL'/'none', 'cluster_price': float, 'count': int, 'band': band}
    """
    if not candles_5m:
        return {"side": "none"}
    n = min(len(candles_5m), int(lookback_minutes/5))
    window = candles_5m[-n:]
    highs = [c["high"] for c in window]
    lows = [c["low"] for c in window]
    def cluster_metric(values):
        values_sorted = sorted(values)
        max_count = 0
        best_center = None
        for v in values_sorted:
            low_bound = v - band/2
            high_bound = v + band/2
            cnt = sum(1 for x in values_sorted if low_bound <= x <= high_bound)
            if cnt > max_count:
                max_count = cnt
                best_center = v
        return max_count, best_center
    high_cnt, high_center = cluster_metric(highs)
    low_cnt, low_center = cluster_metric(lows)
    threshold = max(3, int(n * 0.08))
    if high_cnt >= threshold:
        return {"side": "SELL", "cluster_price": high_center, "count": high_cnt, "band": band}
    if low_cnt >= threshold:
        return {"side": "BUY", "cluster_price": low_center, "count": low_cnt, "band": band}
    return {"side": "none"}

def detect_volume_footprint(candles, spike_ratio=MIN_VOLUME_SPIKE_RATIO):
    """
    Simple big-player footprint detection:
    - checks for last-candle volume spike relative to recent mean and follow-through direction.
    Returns dict {'footprint': bool, 'vol': last_vol, 'mean': mean, 'dir_same': bool}
    """
    vols = [c["volume"] for c in candles if c["volume"] is not None]
    if len(vols) < 6:
        return {"footprint": False}
    recent = vols[-6:]
    mean = sum(recent[:-1]) / max(1, len(recent[:-1]))
    last_vol = recent[-1]
    if mean <= 0:
        return {"footprint": False}
    if last_vol > mean * spike_ratio:
        last = candles[-1]
        prev = candles[-2]
        dir_same = ((last["close"] > last["open"]) and (prev["close"] > prev["open"])) or ((last["close"] < last["open"]) and (prev["close"] < prev["open"]))
        return {"footprint": True, "vol": last_vol, "mean": mean, "dir_same": dir_same}
    return {"footprint": False}

# ---------------- TRADE PLAN BUILDER ----------------
def build_trade_plan(symbol, sweep, confirm, latest_15m, latest_5m, liquidity, retail_cluster, footprint):
    """
    Build trade plan:
    - Side from confirm candle (green => LONG)
    - Entry as retest/ midpoint beyond confirm open with buffer
    - Avoid entry inside retail cluster on same side by nudging entry beyond cluster
    - SL beyond sweep extreme, TP based on RR
    """
    side = "LONG" if confirm["close"] > confirm["open"] else "SHORT"
    sweep_low = sweep["low"]
    sweep_high = sweep["high"]
    confirm_open = confirm["open"]
    confirm_close = confirm["close"]
    is_xau = "XAU" in symbol.upper()
    pip_val = 0.01 if is_xau else 1.0
    sl_distance = XAU_SL_PIPS * pip_val if is_xau else BTC_SL_USD

    if side == "LONG":
        entry_candidate = max(confirm_open + pip_val*2, (confirm_close + sweep_low) / 2)
        if retail_cluster["side"] == "BUY":
            cluster = retail_cluster["cluster_price"]
            if abs(entry_candidate - cluster) <= retail_cluster.get("band", RETAIL_CLUSTER_BAND):
                entry_candidate = cluster + retail_cluster.get("band", RETAIL_CLUSTER_BAND) + pip_val*1
        sl = sweep_low - sl_distance
        rr_distance = entry_candidate - sl
        tp = entry_candidate + rr_distance * RR
    else:
        entry_candidate = min(confirm_open - pip_val*2, (confirm_close + sweep_high) / 2)
        if retail_cluster["side"] == "SELL":
            cluster = retail_cluster["cluster_price"]
            if abs(entry_candidate - cluster) <= retail_cluster.get("band", RETAIL_CLUSTER_BAND):
                entry_candidate = cluster - retail_cluster.get("band", RETAIL_CLUSTER_BAND) - pip_val*1
        sl = sweep_high + sl_distance
        rr_distance = sl - entry_candidate
        tp = entry_candidate - rr_distance * RR

    confidence = 0.5
    confidence += 0.2  # pattern base
    if footprint.get("footprint"):
        confidence += 0.15
    if retail_cluster["side"] == "none":
        confidence += 0.1
    confidence = min(0.95, confidence)

    entry = round(entry_candidate, 3 if is_xau else 2)
    sl = round(sl, 3 if is_xau else 2)
    tp = round(tp, 3 if is_xau else 2)
    tp1 = round(entry + (tp - entry) * 0.5, 3 if is_xau else 2) if side == "LONG" else round(entry - (entry - tp) * 0.5, 3 if is_xau else 2)

    plan = {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "tp1": tp1,
        "confidence": confidence,
        "logic": "Sweep + Green confirm; avoid retail stops; footprint checked"
    }
    return plan

def format_plan_message(analysis):
    """Format HTML message for Telegram."""
    if "error" in analysis:
        return f"⚠ Error: {analysis['error']}"
    if not analysis.get("plan"):
        return (f"ℹ <b>{analysis['symbol']}</b>\nNo high-confidence sweep+confirm found.\n"
                f"Liquidity (24h): Low {analysis['liquidity']['recent_low']}, High {analysis['liquidity']['recent_high']}\n"
                f"Latest 15m close: {analysis['latest_15m']['close']}")
    p = analysis["plan"]
    msg = f"<b>Pro Liquidity Setup — {p['symbol']}</b>\n"
    msg += f"Logic: {p['logic']}\n"
    msg += f"Side: <b>{p['side']}</b>\n"
    msg += f"Entry: <code>{p['entry']}</code>\nSL: <code>{p['sl']}</code>\nTP: <code>{p['tp']}</code>\nTP1: <code>{p['tp1']}</code>\n"
    msg += f"Confidence: {int(p['confidence']*100)}%\n\n"
    msg += f"Liquidity (24h): Low {analysis['liquidity']['recent_low']}, High {analysis['liquidity']['recent_high']}\n"
    msg += f"Latest 15m close: {analysis['latest_15m']['close']}\n"
    if analysis.get("retail_cluster") and analysis["retail_cluster"]["side"] != "none":
        rc = analysis["retail_cluster"]
        msg += f"Retail cluster detected on {rc['side']} at ~{round(rc['cluster_price'],3)} ({rc['count']} hits)\n"
    if analysis.get("footprint") and analysis["footprint"].get("footprint"):
        fp = analysis["footprint"]
        msg += f"Volume footprint: spike {round(fp['vol'],2)} vs mean {round(fp['mean'],2)}; dir_same: {fp['dir_same']}\n"
    msg += "\nTrade Management:\n- TP1 -> move SL to BE\n- TP2 -> scale out 50%\n- Use proper position sizing\n\n---\nPowered by Pro Liquidity Bot"
    return msg

# ---------------- MAIN ANALYSIS ----------------
def get_and_analyze(symbol, interval_15m="15min", interval_5m="5min"):
    result = {"symbol": symbol}
    try:
        raw15 = twelvedata_get_series(symbol, interval=interval_15m, outputsize=200)
        raw5 = twelvedata_get_series(symbol, interval=interval_5m, outputsize=300)
        c15 = parse_candles(raw15)
        c5 = parse_candles(raw5)
        result["latest_15m"] = c15[-1]
        result["latest_5m"] = c5[-1]
        result["liquidity"] = compute_liquidity_zones(c15[-96:]) if len(c15) >= 96 else compute_liquidity_zones(c15)
        detection = detect_sweep_and_green(c15, lookback=LOOKBACK_15M)
        result["detection"] = detection
        if detection.get("signal"):
            sweep = detection["sweep_candle"]
            confirm = detection["confirm_candle"]
            retail_cluster = detect_retail_stop_cluster(c5, band=RETAIL_CLUSTER_BAND, lookback_minutes=LOOKBACK_5M*5)
            footprint = detect_volume_footprint(c5[-8:])
            plan = build_trade_plan(symbol, sweep, confirm, c15[-1], c5[-1], result["liquidity"], retail_cluster, footprint)
            result["plan"] = plan
            result["retail_cluster"] = retail_cluster
            result["footprint"] = footprint
        return result
    except Exception as e:
        return {"error": str(e), "symbol": symbol}

# ---------------- SCHEDULER TASKS ----------------
alerts_today = {"count": 0, "date": None}

def job_pre_alert():
    now_pk = datetime.now(PK_TZ)
    text = f"🕒 <b>Pre-NY Alert</b>\nTime (PK): {now_pk.strftime('%Y-%m-%d %H:%M')}\nScanning liquidity for XAU & BTC..."
    send_telegram_message(text)
    try:
        x = get_and_analyze(SYMBOL_XAU)
        b = get_and_analyze(SYMBOL_BTC)
        send_telegram_message(format_plan_message(x))
        send_telegram_message(format_plan_message(b))
    except Exception as e:
        send_telegram_message(f"Pre-alert error: {e}")

def job_post_open():
    global alerts_today
    now_pk = datetime.now(PK_TZ)
    if alerts_today["date"] != now_pk.date():
        alerts_today = {"count": 0, "date": now_pk.date()}
    if alerts_today["count"] >= MAX_ALERTS_PER_DAY:
        print(f"{LOG_PREFIX} max alerts reached for today.")
        return
    text = f"🕒 <b>NY Post-Open Alert</b>\nTime (PK): {now_pk.strftime('%Y-%m-%d %H:%M')}\nScanning for sweep+confirm on 15m..."
    send_telegram_message(text)
    try:
        x = get_and_analyze(SYMBOL_XAU)
        b = get_and_analyze(SYMBOL_BTC)
        for analysis in (x, b):
            if "plan" in analysis and analysis["plan"]["confidence"] >= 0.65:
                send_telegram_message(format_plan_message(analysis))
                alerts_today["count"] += 1
            else:
                # still send non-plan summary so you see liquidity snapshot
                send_telegram_message(format_plan_message(analysis))
    except Exception as e:
        send_telegram_message(f"Post-open error: {e}")

def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    pk_offset = 5  # PKT is UTC+5
    pre_utc_hour = (PRE_ALERT_PK.hour - pk_offset) % 24
    pre_utc_min = PRE_ALERT_PK.minute
    post_utc_hour = (POST_ALERT_PK.hour - pk_offset) % 24
    post_utc_min = POST_ALERT_PK.minute
    sched.add_job(job_pre_alert, 'cron', hour=pre_utc_hour, minute=pre_utc_min)
    sched.add_job(job_post_open, 'cron', hour=post_utc_hour, minute=post_utc_min)
    print(f"{LOG_PREFIX} Scheduler started. Pre-alert at PK {PRE_ALERT_PK.strftime('%H:%M')}, Post-open at PK {POST_ALERT_PK.strftime('%H:%M')}")
    sched.start()
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()

# ---------------- CLI ----------------
if __name__ == "__main__":
    if (not TELEGRAM_TOKEN) or (not TD_API_KEY) or ("YOUR" in TELEGRAM_TOKEN):
        print("Please set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID and TD_API_KEY before running.")
    else:
        print(f"{LOG_PREFIX} Starting Pro Liquidity Bot...")
        start_scheduler()
