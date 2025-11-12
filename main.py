import ccxt
import math
import time
import logging
from datetime import datetime, timedelta
import requests
import pandas as pd

# ---------------- CONFIG ---------------- #
API_KEY = "YOUR_BINANCE_API_KEY"
API_SECRET = "YOUR_BINANCE_SECRET"
SYMBOL = "BTC/USDT"
TIMEFRAME = "15m"
LEVERAGE = 20
TP_USD = 100
SL_USD = -400
CHECK_INTERVAL = 10   # วินาที

# Nadaraya-Watson LuxAlgo Parameters
NW_H = 8.0
NW_MULT = 3.0
NW_WIN = 499

# Telegram Settings
TELEGRAM_TOKEN = "YOUR_TELEGRAM_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"
REPORT_HOUR = 23  # ส่งสรุปรายวันตอน 23:00
# ---------------------------------------- #

exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# --------- LuxAlgo Exact NWE Function --------- #
def nwe_luxalgo_exact(closes, h=8.0, mult=3.0, win=499):
    """Exact LuxAlgo Nadaraya-Watson Envelope (non-repaint mode)"""
    if len(closes) < win:
        return None, None, None

    weights = [math.exp(-(i ** 2) / (2 * h ** 2)) for i in range(win)]
    den = sum(weights)
    out = sum(closes[-1 - i] * weights[i] for i in range(win)) / den

    mae_vals = [abs(closes[-1 - i] - out) for i in range(win)]
    mae = sum(mae_vals) / len(mae_vals) * mult

    upper = out + mae
    lower = out - mae
    mid = out
    return upper, lower, mid
# ---------------------------------------------- #

def fetch_candles(symbol, timeframe):
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe, limit=NW_WIN)
        df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
        return df
    except Exception as e:
        logging.warning(f"fetch_candles err: {e}")
        return None

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except:
        pass

def get_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def place_order(side, amount):
    try:
        order = exchange.create_market_order(SYMBOL, side, amount)
        logging.info(f"✅ Opened {side} order: {order['id']}")
        return order
    except Exception as e:
        logging.error(f"❌ Order error: {e}")
        return None

def close_position():
    try:
        position = exchange.fetch_positions([SYMBOL])[0]
        amt = float(position["contracts"])
        side = "sell" if position["side"] == "long" else "buy"
        if amt > 0:
            exchange.create_market_order(SYMBOL, side, amt)
            logging.info(f"✅ Closed position ({side})")
    except Exception as e:
        logging.warning(f"Close pos err: {e}")

# ------------------ MAIN LOOP ------------------ #
def main():
    logging.info("✅ Started Binance NW Bot (loop=10s, mode=Touch)")
    sl_lock = False
    last_report = None

    while True:
        df = fetch_candles(SYMBOL, TIMEFRAME)
        if df is None: 
            time.sleep(CHECK_INTERVAL)
            continue

        closes = df["close"].tolist()
        ema50 = get_ema(df["close"], 50).iloc[-1]
        ema100 = get_ema(df["close"], 100).iloc[-1]
        close = closes[-1]

        upper, lower, mid = nwe_luxalgo_exact(closes, h=NW_H, mult=NW_MULT, win=NW_WIN)
        trend = "BUY" if ema50 > ema100 else "SELL"

        logging.info(f"[DEBUG] Close={close:.2f}, Upper={upper:.2f}, Lower={lower:.2f}, Mid={mid:.2f}, Trend={trend}")

        if not sl_lock:
            if trend == "BUY" and close < lower:
                place_order("buy", 0.01)
                logging.info("📈 LONG ENTRY (touch lower band)")
            elif trend == "SELL" and close > upper:
                place_order("sell", 0.01)
                logging.info("📉 SHORT ENTRY (touch upper band)")
        else:
            logging.info("🔒 SL Lock active, waiting for mid-zone unlock...")

        # ปลดล็อกเมื่อราคาแตะ mid zone
        if sl_lock:
            if (trend == "BUY" and close > mid) or (trend == "SELL" and close < mid):
                sl_lock = False
                logging.info("🔓 SL lock released (price crossed mid zone)")

        # ส่งสรุปรายวัน (วันละ 1 ครั้ง ถ้ามีเทรด)
        now = datetime.now()
        if last_report is None or (now.hour == REPORT_HOUR and now.date() != last_report):
            send_telegram(f"📊 รายงานประจำวันที่ {now.strftime('%Y-%m-%d')}\n"
                          f"EMA50={ema50:.2f}, EMA100={ema100:.2f}\n"
                          f"Upper={upper:.2f}, Lower={lower:.2f}, Mid={mid:.2f}\n"
                          f"Trend={trend}")
            last_report = now.date()

        time.sleep(CHECK_INTERVAL)

# ------------------------------------------------ #

if __name__ == "__main__":
    main()
