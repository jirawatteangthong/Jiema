# ======================================================
# Binance Futures Nadaraya-Watson Bot (LuxAlgo Precision v2)
# ======================================================
import os, math, time, json, logging, requests
from datetime import datetime
import pandas as pd
import ccxt
from dotenv import load_dotenv
load_dotenv(override=True)

# ========== ENV ==========
API_KEY = os.getenv("BINANCE_API_KEY", "")
SECRET  = os.getenv("BINANCE_SECRET", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ========== CONFIG ==========
SYMBOL = "BTC/USDT:USDT"
TIMEFRAME = "15m"          # ตั้ง TF เช่น "15m" "1h"
LEVERAGE = 15
POSITION_MARGIN_FRACTION = 0.80

SL_DISTANCE = 300          # SL จาก entry (USD)
BE_OFFSET = 100            # ระยะ SL กันทุน (USD)
NW_H = 8.0
NW_MULT = 3.0
NW_FACTOR = 1.49           # ขยาย band LuxAlgo (1.45-1.55 ตรงที่สุด)
CHECK_INTERVAL = 10        # เช็กทุก 10 วินาที

# ========== LOG ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger("LuxAlgo_NW")

# ========== TELEGRAM ==========
def tg_send(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except: pass

# ========== DAILY REPORT ==========
STATS_FILE = "daily_stats.json"
DAILY_HOUR = 23
def report_daily(stats):
    now = datetime.now()
    if now.hour == DAILY_HOUR and stats:
        pnl = sum(t["pnl"] for t in stats)
        msg = [f"📊 สรุปรายวัน {now:%Y-%m-%d}", f"รวมกำไร: {pnl:+.2f} USDT", "────────────"]
        for t in stats[-10:]:
            msg.append(f"{t['side']} | {t['entry']:.2f}→{t['exit']:.2f} | {t['pnl']:+.2f} ({t['reason']})")
        tg_send("\n".join(msg))
        open(STATS_FILE, "w").write(json.dumps([]))

# ========== LUXALGO NADARAYA-WATSON ==========
def nwe_luxalgo_repaint(closes, h=8.0, mult=3.0, factor=1.5):
    n = len(closes)
    if n < 100: return None, None, None
    win = min(499, n - 1)
    coefs = [math.exp(-(i**2)/(2*(h**2))) for i in range(win)]
    den = sum(coefs)
    out_series = []
    for idx in range(win, n):
        num = sum(closes[idx-j]*coefs[j] for j in range(win))
        out_series.append(num/den)
    if not out_series: return None, None, None
    mean = out_series[-1]
    win_s = int(h * 10)
    if win_s > len(out_series): win_s = len(out_series)
    diffs = [abs(closes[-i]-out_series[-i]) for i in range(1, win_s+1)]
    mae = (sum(diffs)/len(diffs)) * mult * factor
    upper, lower = mean + mae, mean - mae
    return upper, lower, mean

# ========== EXCHANGE ==========
def setup_exchange():
    ex = ccxt.binance({
        "apiKey": API_KEY,
        "secret": SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "future"}
    })
    ex.load_markets()
    ex.set_leverage(LEVERAGE, SYMBOL)
    return ex

def free_usdt(ex):
    try:
        bal = ex.fetch_balance({'type': 'future'})
        return float(bal['USDT']['free'])
    except:
        return 0.0

def calc_order_size(ex, price):
    free = free_usdt(ex)
    margin = free * POSITION_MARGIN_FRACTION
    notional = margin * LEVERAGE
    qty = notional / price
    try:
        return float(ex.amount_to_precision(SYMBOL, qty))
    except:
        return round(qty, 3)

# ========== MAIN LOOP ==========
def main():
    ex = setup_exchange()
    log.info(f"✅ Started Binance Futures LuxAlgo Bot ({TIMEFRAME}, repaint mode)")
    sl_lock = False
    position = None
    stats = json.load(open(STATS_FILE)) if os.path.exists(STATS_FILE) else []

    while True:
        try:
            # ดึงข้อมูลล่าสุดตาม TF จริง
            ohlcv = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=505)
            df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","vol"])
            closes = df["close"].tolist()

            upper, lower, mid = nwe_luxalgo_repaint(closes, NW_H, NW_MULT, NW_FACTOR)
            if None in (upper, lower, mid):
                time.sleep(CHECK_INTERVAL); continue

            ema50 = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
            ema100 = df["close"].ewm(span=100, adjust=False).mean().iloc[-1]
            trend = "BUY" if ema50 > ema100 else "SELL"
            close = df["close"].iloc[-1]

            log.info(f"[DEBUG] Close={close:.2f}, Upper={upper:.2f}, Lower={lower:.2f}, Mid={mid:.2f}, Trend={trend}")

            # ปลดล็อก SL lock ถ้าราคาตัด mid zone
            if sl_lock:
                if (trend=="BUY" and close>mid) or (trend=="SELL" and close<mid):
                    sl_lock = False
                    log.info("🔓 SL lock released")

            # ตรวจโพซิชัน
            try:
                pos = ex.fetch_positions([SYMBOL])[0]
                amt = float(pos["contracts"])
                side = pos["side"] if amt>0 else None
            except:
                amt, side = 0, None

            # ENTRY
            if not sl_lock and amt == 0:
                if trend=="BUY" and close<=lower:
                    qty = calc_order_size(ex, close)
                    ex.create_market_order(SYMBOL, "buy", qty)
                    position={"side":"long","entry":close,"sl":close - SL_DISTANCE}
                    log.info(f"📈 LONG entry @ {close:.2f}, SL={close-SL_DISTANCE:.2f}")
                elif trend=="SELL" and close>=upper:
                    qty = calc_order_size(ex, close)
                    ex.create_market_order(SYMBOL, "sell", qty)
                    position={"side":"short","entry":close,"sl":close + SL_DISTANCE}
                    log.info(f"📉 SHORT entry @ {close:.2f}, SL={close+SL_DISTANCE:.2f}")

            # MANAGE POSITION
            elif amt>0 and position:
                if position["side"]=="long":
                    if close>=upper:
                        ex.create_market_order(SYMBOL,"sell",amt,params={"reduceOnly":True})
                        pnl=(close-position["entry"])
                        stats.append({"side":"LONG","entry":position["entry"],"exit":close,"pnl":pnl,"reason":"TP"})
                        log.info(f"✅ TP hit +{pnl:.2f}"); position=None
                    elif close<=position["sl"]:
                        ex.create_market_order(SYMBOL,"sell",amt,params={"reduceOnly":True})
                        pnl=(close-position["entry"])
                        stats.append({"side":"LONG","entry":position["entry"],"exit":close,"pnl":pnl,"reason":"SL"})
                        log.warning(f"❌ SL hit {pnl:.2f}")
                        sl_lock=True; position=None
                    elif close>mid and position["sl"] < position["entry"]+BE_OFFSET:
                        position["sl"] = position["entry"] + BE_OFFSET
                        log.info(f"🔁 Move SL to breakeven {position['sl']:.2f}")

                elif position["side"]=="short":
                    if close<=lower:
                        ex.create_market_order(SYMBOL,"buy",amt,params={"reduceOnly":True})
                        pnl=(position["entry"]-close)
                        stats.append({"side":"SHORT","entry":position["entry"],"exit":close,"pnl":pnl,"reason":"TP"})
                        log.info(f"✅ TP hit +{pnl:.2f}"); position=None
                    elif close>=position["sl"]:
                        ex.create_market_order(SYMBOL,"buy",amt,params={"reduceOnly":True})
                        pnl=(position["entry"]-close)
                        stats.append({"side":"SHORT","entry":position["entry"],"exit":close,"pnl":pnl,"reason":"SL"})
                        log.warning(f"❌ SL hit {pnl:.2f}")
                        sl_lock=True; position=None
                    elif close<mid and position["sl"] > position["entry"]-BE_OFFSET:
                        position["sl"] = position["entry"] - BE_OFFSET
                        log.info(f"🔁 Move SL to breakeven {position['sl']:.2f}")

            # สรุปรายวัน
            report_daily(stats)
            open(STATS_FILE,"w").write(json.dumps(stats,indent=2))
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            log.warning(f"loop error: {e}")
            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
