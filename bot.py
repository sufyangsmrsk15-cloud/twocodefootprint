"""
Liquidity Matrix Telegram Bot - SHORT SETUP ONLY (New York Session)
Auto starts 5:00 PM PKT → 10:00 PM PKT
"""

import os
import time
import math
import requests
from datetime import datetime, timedelta, time as dtime
from apscheduler.schedulers.background import BackgroundScheduler

# ------------------ CONFIG ------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TD_API_KEY = os.getenv("TWELVE_API_KEY")

SYMBOL_XAU = "XAU/USD"
SYMBOL_BTC = "BTC/USD"

NY_SESSION_START_PK = dtime(hour=17, minute=0)
NY_SESSION_END_PK = dtime(hour=22, minute=0)

XAU_SL_PIPS = 20
BTC_SL_USD = 350
RR = 4

current_setups = {"XAU": None, "BTC": None}

# ------------------ HELPERS ------------------

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print("Telegram Error:", e)

def twelvedata_get_series(symbol: str, interval: str = "15min", outputsize: int = 100):
    base = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "format": "JSON",
        "apikey": TD_API_KEY,
    }
    r = requests.get(base, params=params, timeout=12)
    data = r.json()
    return list(reversed(data["values"])) if "values" in data else []

def parse_candles(raw):
    candles = []
    for c in raw:
        candles.append({
            "datetime": datetime.fromisoformat(c["datetime"]),
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"])
        })
    return candles

def get_current_price(symbol):
    try:
        data = twelvedata_get_series(symbol, "1min", 1)
        return float(data[0]["close"]) if data else None
    except:
        return None

# ------------------ STRATEGY LOGIC ------------------

def detect_sweep_and_red(candles, lookback=6):
    """Detect bearish sweep (fake high + red close)."""
    if len(candles) < lookback + 2:
        return {"signal": False}
    window = candles[-(lookback + 1):]
    for i in range(1, len(window) - 1):
        if window[i]["high"] > window[i-1]["high"] and window[i]["high"] > window[i+1]["high"]:
            body = abs(window[i]["open"] - window[i]["close"])
            upper_wick = window[i]["high"] - max(window[i]["open"], window[i]["close"])
            if upper_wick / (window[i]["high"] - window[i]["low"]) > 0.4:
                next_candle = window[i + 1]
                if next_candle["close"] < next_candle["open"]:
                    return {"signal": True, "sweep": window[i], "confirm": next_candle}
    return {"signal": False}

def calculate_dynamic_support(candles, lookback=10):
    return min(c["low"] for c in candles[-lookback:])

def build_short_plan(symbol, candles, detection, sl_pips):
    sweep = detection["sweep"]
    confirm = detection["confirm"]
    support = calculate_dynamic_support(candles)
    if confirm["close"] < support and confirm["close"] < (confirm["high"] - (confirm["high"] - confirm["low"]) * 0.5):
        entry = support
        sl = sweep["high"] + sl_pips * 0.01
        tp = entry - (entry - sl) * RR
        tp1 = entry - (entry - sl)
        return {
            "symbol": symbol,
            "side": "SHORT",
            "entry": round(entry, 3),
            "sl": round(sl, 3),
            "tp": round(tp, 3),
            "tp1": round(tp1, 3),
            "logic": f"Dynamic Support Break ({round(support, 3)}) + Bearish Confirm",
            "confidence": 0.85,
        }

def check_for_setups():
    print("🔍 Checking setups...")
    try:
        for symbol in [SYMBOL_XAU, SYMBOL_BTC]:
            raw = twelvedata_get_series(symbol, "15min", 200)
            candles = parse_candles(raw)
            detect = detect_sweep_and_red(candles)
            if detect["signal"]:
                if symbol == SYMBOL_XAU:
                    plan = build_short_plan(symbol, candles, detect, XAU_SL_PIPS)
                    current_setups["XAU"] = plan
                else:
                    plan = build_short_plan(symbol, candles, detect, BTC_SL_USD/10)
                    current_setups["BTC"] = plan
                print(f"✅ {symbol} short setup detected")
    except Exception as e:
        print("Setup error:", e)

def check_entry_zones():
    print("🎯 Checking entry zones...")
    for key, setup in current_setups.items():
        if not setup:
            continue
        current_price = get_current_price(setup["symbol"])
        if current_price is None:
            continue
        diff = abs(current_price - setup["entry"]) / setup["entry"] * 100
        if diff <= 0.1:
            send_telegram_message(format_alert(setup, current_price))
            current_setups[key] = None

def format_alert(setup, price):
    return (f"🚨 <b>{setup['symbol']} SHORT ENTRY ALERT</b>\n"
            f"💰 Price: {price}\n🎯 Entry: {setup['entry']}\n"
            f"SL: {setup['sl']} | TP: {setup['tp']}\nLogic: {setup['logic']}\n"
            f"Confidence: {int(setup['confidence']*100)}%\n\n⚡ Ready for entry!")

# ------------------ JOBS ------------------

def job_pre_alert():
    now = datetime.utcnow() + timedelta(hours=5)
    msg = f"🕒 <b>Pre-NY Short Bias Scan</b>\nTime: {now.strftime('%H:%M')}"
    send_telegram_message(msg)

def job_monitor():
    now_pk = datetime.utcnow() + timedelta(hours=5)
    if NY_SESSION_START_PK <= now_pk.time() <= NY_SESSION_END_PK:
        print(f"🕐 NY Active {now_pk.strftime('%H:%M')}")
        check_for_setups()
        check_entry_zones()

def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(job_pre_alert, "cron", hour=11, minute=55)
    sched.add_job(job_monitor, "interval", minutes=5)
    sched.start()
    print("🤖 Short Setup Bot running...")
    while True:
        time.sleep(1)

if __name__ == "__main__":
    start_scheduler()
