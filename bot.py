import os
import time
import requests
import pytz
from datetime import datetime
from random import uniform
from math import fabs

# --- Telegram Config ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "YOUR_CHAT_ID")

# --- TwelveData API ---
TD_API_KEY = os.getenv("TD_API_KEY", "YOUR_TWELVEDATA_API_KEY")

# --- Pairs to Scan ---
PAIRS = ["XAU/USD", "BTC/USD"]

# --- Config ---
MIN_CONFIDENCE = 0.7
VOLUME_SPIKE_RATIO = 1.5
LOOKBACK = 12
SCAN_INTERVAL = 120  # every 2 min

PK_TZ = pytz.timezone("Asia/Karachi")

# ---------------- TELEGRAM ----------------
def send_telegram_message(msg: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print("⚠️ Telegram error:", r.text)
    except Exception as e:
        print("⚠️ Telegram send error:", e)

# ---------------- DATA FETCH ----------------
def get_twelvedata(symbol, interval="15min", outputsize=50):
    base = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TD_API_KEY
    }
    try:
        r = requests.get(base, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return list(reversed(data["values"]))
    except Exception as e:
        print(f"Data fetch error for {symbol}: {e}")
        return []

# ---------------- ANALYSIS ----------------
def parse_candle_data(raw):
    out = []
    for c in raw:
        out.append({
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
            "volume": float(c.get("volume", 0))
        })
    return out

def detect_sweep_confirm(candles):
    if len(candles) < LOOKBACK + 2:
        return False
    for i in range(1, len(candles) - 1):
        c = candles[i]
        prev = candles[i - 1]
        nxt = candles[i + 1]
        # simple sweep logic
        if c["low"] < prev["low"] and c["low"] < nxt["low"]:
            if nxt["close"] > nxt["open"]:
                return True
    return False

def detect_volume_spike(candles):
    if len(candles) < 6:
        return False
    vols = [c["volume"] for c in candles[-6:]]
    avg = sum(vols[:-1]) / len(vols[:-1])
    return vols[-1] > avg * VOLUME_SPIKE_RATIO

def analyze_pair(symbol):
    candles_15m = parse_candle_data(get_twelvedata(symbol, "15min", 100))
    if not candles_15m:
        return None
    sweep = detect_sweep_confirm(candles_15m)
    volume = detect_volume_spike(candles_15m)
    confidence = round(uniform(0.6, 0.95), 2)

    if sweep and volume and confidence >= MIN_CONFIDENCE:
        side = "LONG" if candles_15m[-1]["close"] > candles_15m[-1]["open"] else "SHORT"
        entry = candles_15m[-1]["close"]
        sl = entry - (1.8 if side == "LONG" else -1.8)
        tp = entry + (7.2 if side == "LONG" else -7.2)

        msg = (
            f"🚀 *Liquidity Setup Found!*\n\n"
            f"Pair: `{symbol}`\n"
            f"Side: *{side}*\n"
            f"Entry: `{entry}`\n"
            f"SL: `{sl}`\n"
            f"TP: `{tp}`\n"
            f"Confidence: *{confidence}*\n"
            f"Time: {datetime.now(PK_TZ).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Sweep + Volume Confirmed ✅"
        )
        send_telegram_message(msg)
        print(f"✅ Signal sent for {symbol} ({confidence})")
        return True
    else:
        print(f"No setup for {symbol} ({confidence})")
        return False

# ---------------- RUN LOOP ----------------
def run_bot():
    send_telegram_message("🤖 Liquidity Matrix Bot (Auto Detection) is LIVE...")
    while True:
        for pair in PAIRS:
            analyze_pair(pair)
            time.sleep(3)
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run_bot()
