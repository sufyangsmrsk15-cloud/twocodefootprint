"""
Liquidity Matrix Telegram Bot - Real-Time Setup Alert
--------------------------------
1st Alert: Pre-NY liquidity snapshot (4:55 PM PK)
2nd Alert: Only when setup found + price touches entry zone (anytime during NY session)
"""

import os
import time
import json
import math
import requests
from datetime import datetime, timedelta, time as dtime
from apscheduler.schedulers.background import BackgroundScheduler

# ------------------ CONFIG ------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TD_API_KEY = os.getenv("TWELVE_API_KEY")

# Trading symbols
SYMBOL_XAU = "XAU/USD"
SYMBOL_BTC = "BTC/USD"

# Session times (Pakistan Time)
NY_SESSION_START_PK = dtime(hour=17, minute=0)  # 5:00 PM PK
NY_SESSION_END_PK = dtime(hour=22, minute=0)    # 10:00 PM PK

# Strategy params
XAU_SL_PIPS = 20
BTC_SL_USD = 350
RR = 4

# Global variables to track setups
current_setups = {
    "XAU": None,
    "BTC": None
}

# ------------------ HELPERS ------------------

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print("Telegram send error:", e)

def twelvedata_get_series(symbol: str, interval: str = "15min", outputsize: int = 100):
    base = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "format": "JSON",
        "apikey": TD_API_KEY
    }
    r = requests.get(base, params=params, timeout=12)
    r.raise_for_status()
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData error: {data}")
    return list(reversed(data["values"]))

def parse_candles(raw_candles):
    out = []
    for c in raw_candles:
        out.append({
            "datetime": datetime.fromisoformat(c["datetime"]),
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
            "volume": float(c.get("volume") or 0)
        })
    return out

def get_current_price(symbol):
    try:
        data = twelvedata_get_series(symbol, "1min", 1)
        if data:
            return float(data[0]['close'])
    except Exception as e:
        print(f"Error getting current price for {symbol}: {e}")
    return None

# ------------------ STRATEGY LOGIC ------------------

def detect_sweep_and_green(candles_15m, lookback=6):
    if len(candles_15m) < lookback+2:
        return {"signal": False}
    window = candles_15m[-(lookback+1):]
    for i in range(1, len(window)-1):
        if window[i]["low"] < window[i-1]["low"] and window[i]["low"] < window[i+1]["low"]:
            body = abs(window[i]["open"] - window[i]["close"])
            lower_wick = window[i]["close"] - window[i]["low"]
            range_ = window[i]["high"] - window[i]["low"]
            if lower_wick / range_ > 0.4 and window[i+1]["close"] > window[i+1]["open"]:
                return {"signal": True, "sweep_candle": window[i], "confirm_candle": window[i+1]}
    return {"signal": False}

def compute_liquidity_zones(candles):
    lows = [c["low"] for c in candles]
    highs = [c["high"] for c in candles]
    return {"recent_low": min(lows), "recent_high": max(highs), "last_close": candles[-1]["close"]}

# ------------------ CORE JOBS ------------------

def job_pre_alert():
    now = datetime.utcnow() + timedelta(hours=5)
    send_telegram_message(f"🕒 <b>Pre-NY Session</b>\nTime (PK): {now.strftime('%H:%M')}")
    try:
        raw_15m_xau = twelvedata_get_series(SYMBOL_XAU, "15min", 96)
        candles = parse_candles(raw_15m_xau)
        liq = compute_liquidity_zones(candles)
        send_telegram_message(f"📊 <b>XAU/USD Liquidity</b>\nLow: {liq['recent_low']}\nHigh: {liq['recent_high']}\nLast: {liq['last_close']}")
    except Exception as e:
        send_telegram_message(f"Pre-alert error: {e}")

def job_continuous_monitoring():
    now_pk = datetime.utcnow() + timedelta(hours=5)
    if NY_SESSION_START_PK <= now_pk.time() <= NY_SESSION_END_PK:
        print(f"🕒 Monitoring {now_pk.strftime('%H:%M')}")
    else:
        print("💤 Outside NY session hours")

def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(job_pre_alert, 'cron', hour=11, minute=55)
    sched.add_job(job_continuous_monitoring, 'interval', minutes=5)
    sched.start()
    print("🤖 Bot Running...")
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()

if __name__ == "__main__":
    print("Starting Liquidity Matrix Bot...")
    start_scheduler()

