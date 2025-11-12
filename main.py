import ccxt
import math
import time
import logging
from datetime import datetime
import requests
import pandas as pd

# ---------------- CONFIG ---------------- #
API_KEY = "YOUR_BINANCE_API_KEY"
API_SECRET = "YOUR_BINANCE_SECRET"
SYMBOL = "BTC/USDT"
TIMEFRAME = "15m"
LEVERAGE = 20
POSITION_SIZE = 0.01
CHECK_INTERVAL = 10  # วินาที

# Nadaraya-Watson (LuxAlgo)
NW_H = 8.0
NW_MULT = 3.0
NW_WIN = 499

# Telegram
TELEGRAM_TOKEN = "YOUR_TELEGRAM_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"
REPORT_HOUR = 23
# ---------------------------------------- #

exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "futures"}  # ✅ ใช้ futures
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ---------------- NWE ---------------- #
def nwe_luxalgo_exact(closes, h=8.0, mult=3.0, win=499):
    if len(closes) < win:
        return None, None, None
    weights = [math.exp(-(i ** 2) / (2 * h ** 2)) for i in range(win)]
    den = sum(weights)
    out = sum(closes[-1 - i] * weights[i] for i in range(win)) / den
    mae_vals = [abs(closes[-1 - i] - out) for i in range(win)]
    mae = sum(mae_vals) / len(mae_vals) * mult
    upper, lower, mid = out + mae, out - mae, out
    return upper, lower, mid

def get_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def fetch_candles(symbol, timeframe):
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe, limit=NW_WIN)
        df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
        return df
    except Exception as e:
        logging.warning(f"fetch_candles err: {e}")
        return None

# ---------------- Telegram ---------------- #
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except:
        pass

# ---------------- Order ---------------- #
def place_order(side, amount):
    try:
        order = exchange.create_market_order(SYMBOL, side, amount)
        logging.info(f"✅ Opened {side} order")
        return order
    except Exception as e:
        logging.error(f"❌ Order error: {e}")
        return None

def close_position(reason=""):
    try:
        pos = exchange.fetch_positions([SYMBOL])[0]
        amt = float(pos["contracts"])
        if amt == 0:
            return
        side = "sell" if pos["side"] == "long" else "buy"
        exchange.create_market_order(SYMBOL, side, amt, None, {"reduceOnly": True})
        logging.info(f"✅ Closed position ({reason})")
    except Exception as e:
        logging.warning(f"Close pos err: {e}")

# ---------------- Main ---------------- #
def main():
    logging.info("✅ Started Binance NW Bot (Touch Mode, loop=10s)")
    sl_lock = False
    last_report_date = None
    pos_state = None

    while True:
        df = fetch_candles(SYMBOL, TIMEFRAME)
        if df is None:
            time.sleep(CHECK_INTERVAL)
            continue

        closes = df["close"].tolist()
        ema50 = get_ema(df["close"], 50).iloc[-1]
        ema100 = get_ema(df["close"], 100).iloc[-1]
        close = closes[-1]
        upper, lower, mid = nwe_luxalgo_exact(closes, NW_H, NW_MULT, NW_WIN)
        trend = "BUY" if ema50 > ema100 else "SELL"

        logging.info(f"[DEBUG] Close={close:.2f}, Upper={upper:.2f}, Lower={lower:.2f}, Mid={mid:.2f}, Trend={trend}")

        # --- SL Lock Unlock ---
        if sl_lock:
            if (trend == "BUY" and close > mid) or (trend == "SELL" and close < mid):
                sl_lock = False
                logging.info("🔓 SL lock released (price crossed mid zone)")

        # --- Check Positions ---
        try:
            pos = exchange.fetch_positions([SYMBOL])[0]
            amt = float(pos["contracts"])
            side = pos["side"] if amt > 0 else None
        except:
            amt, side = 0, None

        # --- Entry ---
        if not sl_lock and amt == 0:
            if trend == "BUY" and close <= lower:
                place_order("buy", POSITION_SIZE)
                pos_state = {"side": "long", "entry": close, "sl": close - 300}
                logging.info("📈 LONG ENTRY (touch lower band)")
            elif trend == "SELL" and close >= upper:
                place_order("sell", POSITION_SIZE)
                pos_state = {"side": "short", "entry": close, "sl": close + 300}
                logging.info("📉 SHORT ENTRY (touch upper band)")

        # --- TP / SL ---
        elif amt > 0 and pos_state:
            if pos_state["side"] == "long":
                if close >= upper:
                    close_position("TP touch upper")
                    pos_state = None
                elif close <= pos_state["sl"]:
                    close_position("SL touch")
                    sl_lock = True
                    pos_state = None
            elif pos_state["side"] == "short":
                if close <= lower:
                    close_position("TP touch lower")
                    pos_state = None
                elif close >= pos_state["sl"]:
                    close_position("SL touch")
                    sl_lock = True
                    pos_state = None

        # --- Daily Report ---
        now = datetime.now()
        if (last_report_date is None or now.date() != last_report_date) and now.hour == REPORT_HOUR:
            send_telegram(f"📊 รายงานประจำวันที่ {now.strftime('%Y-%m-%d')}\n"
                          f"EMA50={ema50:.2f}, EMA100={ema100:.2f}\n"
                          f"Upper={upper:.2f}, Lower={lower:.2f}, Mid={mid:.2f}\n"
                          f"Trend={trend}")
            last_report_date = now.date()

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
