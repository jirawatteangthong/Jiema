import os
import math
import time
import logging
import requests
import pandas as pd
import ccxt
from datetime import datetime
from dotenv import load_dotenv

# ========== ENV ==========
load_dotenv(override=True)
API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY')
SECRET = os.getenv('BINANCE_SECRET', 'YOUR_BINANCE_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID')

# ========== CONFIG ==========
SYMBOL = "BTC/USDT:USDT"
TIMEFRAME = "15m"
LEVERAGE = 15
POSITION_MARGIN_FRACTION = 0.80  # ใช้ 80% ของ Free USDT เป็น margin
CHECK_INTERVAL = 10  # วินาที

# Nadaraya-Watson Envelope (LuxAlgo)
NW_H = 8.0
NW_MULT = 3.0
NW_WIN = 499

# Daily Report
REPORT_HOUR = 23

# ========== SETUP ==========
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "futures"}
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ========== HELPERS ==========
def nwe_luxalgo_exact(closes, h=8.0, mult=3.0, win=499):
    if len(closes) < win:
        return None, None, None
    weights = [math.exp(-(i ** 2) / (2 * h ** 2)) for i in range(win)]
    den = sum(weights)
    out = sum(closes[-1 - i] * weights[i] for i in range(win)) / den
    mae_vals = [abs(closes[-1 - i] - out) for i in range(win)]
    mae = sum(mae_vals) / len(mae_vals) * mult
    return out + mae, out - mae, out

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

def get_free_usdt():
    try:
        bal = exchange.fetch_balance({'type': 'future'})
        v = (bal.get('USDT') or {}).get('free')
        return float(v) if v else 0.0
    except:
        return 0.0

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except:
        pass

# ========== ORDER FUNCTIONS ==========
def calc_order_size(price):
    free = get_free_usdt()
    margin = free * POSITION_MARGIN_FRACTION
    if margin <= 0:
        return 0.0
    notional = margin * LEVERAGE
    qty = notional / price
    try:
        qty = float(exchange.amount_to_precision(SYMBOL, qty))
    except:
        qty = round(qty, 3)
    return qty

def place_order(side, price_ref):
    qty = calc_order_size(price_ref)
    if qty <= 0:
        logging.info("❌ Margin not enough")
        return None
    try:
        order = exchange.create_market_order(SYMBOL, side, qty)
        logging.info(f"✅ Open {side.upper()} {qty} {SYMBOL}")
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
        logging.warning(f"close_position err: {e}")

# ========== MAIN LOOP ==========
def main():
    logging.info("✅ Started Binance Futures NW Bot (Touch Mode, Margin 80%)")
    sl_lock = False
    pos_state = None
    last_report_date = None

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

        # ---------- Unlock from SL Lock ----------
        if sl_lock:
            if (trend == "BUY" and close > mid) or (trend == "SELL" and close < mid):
                sl_lock = False
                logging.info("🔓 SL Lock released (price crossed mid zone)")

        # ---------- Check Position ----------
        try:
            pos = exchange.fetch_positions([SYMBOL])[0]
            amt = float(pos["contracts"])
            side = pos["side"] if amt > 0 else None
        except:
            amt, side = 0, None

        # ---------- ENTRY ----------
        if not sl_lock and amt == 0:
            if trend == "BUY" and close <= lower:
                place_order("buy", close)
                pos_state = {"side": "long", "entry": close, "sl": close - 300}
                logging.info("📈 LONG ENTRY (touch lower band)")
            elif trend == "SELL" and close >= upper:
                place_order("sell", close)
                pos_state = {"side": "short", "entry": close, "sl": close + 300}
                logging.info("📉 SHORT ENTRY (touch upper band)")

        # ---------- TP / SL ----------
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

        # ---------- Daily Report ----------
        now = datetime.now()
        if (last_report_date is None or now.date() != last_report_date) and now.hour == REPORT_HOUR:
            send_telegram(
                f"📊 สรุปรายวัน {now.strftime('%Y-%m-%d')}\n"
                f"EMA50={ema50:.2f}, EMA100={ema100:.2f}\n"
                f"Upper={upper:.2f}, Lower={lower:.2f}, Mid={mid:.2f}\n"
                f"Trend={trend}"
            )
            last_report_date = now.date()

        time.sleep(CHECK_INTERVAL)

# ========== RUN ==========
if __name__ == "__main__":
    main()
