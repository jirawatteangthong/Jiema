# ======================================================
# Binance Futures Nadaraya-Watson Bot (LuxAlgo + MACD + Half-TF + One-Daily-Report)
# ======================================================

import os, math, time, json, logging, requests
from datetime import datetime
import pandas as pd
import ccxt
from dotenv import load_dotenv
load_dotenv(override=True)

# ------------------------------------------------------
# ENV
# ------------------------------------------------------
API_KEY = os.getenv("BINANCE_API_KEY", "")
SECRET  = os.getenv("BINANCE_SECRET", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ------------------------------------------------------
# CONFIG
# ------------------------------------------------------
SYMBOL = "BTC/USDT:USDT"
TIMEFRAME = "15m"
LEVERAGE = 15
POSITION_MARGIN_FRACTION = 0.65

SL_DISTANCE = 500
BE_OFFSET = 100

NW_H = 8.0
NW_MULT = 2.0
NW_FACTOR = 1.0

UPDATE_FRACTION = 0.5
CHECK_INTERVAL = 10

DAILY_HOUR = 23
DAILY_REPORT_MIN = 0   # ส่งตอน 23:00

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# ------------------------------------------------------
# LOG
# ------------------------------------------------------
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("NW_MACD_BOT")

# ------------------------------------------------------
# TELEGRAM
# ------------------------------------------------------
def tg_send(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url,json={"chat_id":TELEGRAM_CHAT_ID,"text":msg})
    except: pass

# ======================================================
# DAILY REPORT — SINGLE MESSAGE PER DAY
# ======================================================
STATS_FILE = "daily_stats.json"
REPORT_SENT_FILE = "daily_report_sent.txt"

def load_daily_stats():
    if os.path.exists(STATS_FILE):
        return json.load(open(STATS_FILE))
    return []

def save_daily_stats(stats):
    open(STATS_FILE, "w").write(json.dumps(stats, indent=2))

def has_sent_today():
    if not os.path.exists(REPORT_SENT_FILE): return False
    d = open(REPORT_SENT_FILE).read().strip()
    return d == datetime.now().strftime("%Y-%m-%d")

def mark_sent_today():
    open(REPORT_SENT_FILE,"w").write(datetime.now().strftime("%Y-%m-%d"))

def reset_report_if_new_day():
    today = datetime.now().strftime("%Y-%m-%d")
    if not os.path.exists(REPORT_SENT_FILE):
        return
    if open(REPORT_SENT_FILE).read().strip() != today:
        save_daily_stats([])
        open(REPORT_SENT_FILE,"w").write("")

def try_send_daily_report(stats):
    now = datetime.now()

    # เงื่อนไขเวลา
    if not (now.hour == DAILY_HOUR and now.minute == DAILY_REPORT_MIN):
        return

    if has_sent_today():
        return  # ส่งแล้ววันนี้

    if not stats:
        mark_sent_today()  # ไม่มีเทรดวันนี้ แต่ mark ว่าส่งแล้ว
        return

    total_pnl = sum(t["pnl"] for t in stats)

    msg = [
        f"📊 สรุปผลรายวัน {now:%Y-%m-%d}",
        f"Σ กำไร/ขาดทุน: {total_pnl:+.2f} USDT",
        "────────────"
    ]

    for t in stats[-15:]:
        msg.append(
            f"{t['side']} | {t['entry']:.2f}→{t['exit']:.2f} | "
            f"{t['pnl']:+.2f} ({t['reason']})"
        )

    tg_send("\n".join(msg))
    mark_sent_today()
    log.info("📨 Sent daily report.")

# ======================================================
# LUXALGO NWE (REPAINT)
# ======================================================
def nwe_luxalgo_repaint(closes, h=8.0, mult=3.0, factor=1.5):
    n = len(closes)
    if n < 100:
        return None, None, None

    win = min(499, n - 1)
    coefs = [math.exp(-(i**2)/(2*(h**2))) for i in range(win)]
    den = sum(coefs)

    out = []
    for idx in range(win, n):
        num = sum(closes[idx-j]*coefs[j] for j in range(win))
        out.append(num / den)

    if not out:
        return None,None,None

    mean = out[-1]

    win_s = int(h * 10)
    if win_s > len(out):
        win_s = len(out)

    diffs = [abs(closes[-i] - out[-i]) for i in range(1, win_s+1)]
    mae = (sum(diffs)/len(diffs)) * mult * factor

    return mean + mae, mean - mae, mean

# ======================================================
# MACD REAL-TIME
# ======================================================
def ema_series(values, period):
    if len(values) < period:
        return []
    k = 2/(period+1)
    s = sum(values[:period])/period
    out = [None]*(period-1) + [s]
    e = s
    for v in values[period:]:
        e = v*k + e*(1-k)
        out.append(e)
    return out

def macd_from_closes(closes):
    if len(closes) < MACD_SLOW + MACD_SIGNAL + 2:
        return None
    ef = ema_series(closes, MACD_FAST)
    es = ema_series(closes, MACD_SLOW)
    dif = []
    for i in range(len(closes)):
        if i < len(ef) and i < len(es):
            if ef[i] is not None and es[i] is not None:
                dif.append(ef[i]-es[i])
    dea = ema_series(dif, MACD_SIGNAL)
    if len(dif) < 2 or len(dea) < 2:
        return None
    return dif[-2], dif[-1], dea[-2], dea[-1]

def macd_up(dif_p,dif_n,dea_p,dea_n):
    return dif_p <= dea_p and dif_n > dea_n

def macd_down(dif_p,dif_n,dea_p,dea_n):
    return dif_p >= dea_p and dif_n < dea_n

# ======================================================
# EXCHANGE
# ======================================================
def setup_exchange():
    ex = ccxt.binance({
        "apiKey":API_KEY,
        "secret":SECRET,
        "enableRateLimit":True,
        "options":{"defaultType":"future"}
    })
    ex.load_markets()
    try:
        ex.set_leverage(LEVERAGE,SYMBOL)
    except: pass
    return ex

def free_usdt(ex):
    try:
        bal = ex.fetch_balance({"type":"future"})
        return float((bal.get("USDT") or {}).get("free") or 0)
    except:
        return 0

def calc_order_size(ex, price):
    free = free_usdt(ex)
    margin = free * POSITION_MARGIN_FRACTION
    notional = margin * LEVERAGE
    qty = notional / price
    try: return float(ex.amount_to_precision(SYMBOL, qty))
    except: return round(qty, 3)

# ======================================================
# MAIN
# ======================================================
def main():
    last_nw_update = 0
    upper = lower = mid = None
    pending_entry = None

    ex = setup_exchange()
    log.info(f"✅ Started Binance NW + MACD Bot ({TIMEFRAME}, 80% margin, daily report once/day)")

    stats = load_daily_stats()
    sl_lock = False
    position = None

    while True:
        try:
            reset_report_if_new_day()
            try_send_daily_report(stats)

            # fetch candles
            ohlcv = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=600)
            df = pd.DataFrame(ohlcv,columns=["time","open","high","low","close","vol"])

            closes_nw = df["close"].iloc[:-1].tolist()
            closes_live = df["close"].tolist()
            close = closes_live[-1]

            # TF seconds
            if "m" in TIMEFRAME:
                tf_minutes=int(TIMEFRAME.replace("m",""))
            else:
                tf_minutes=int(TIMEFRAME.replace("h",""))*60
            tf_sec = tf_minutes*60
            now = time.time()

            # update NW half TF
            if now-last_nw_update >= tf_sec*UPDATE_FRACTION:
                res = nwe_luxalgo_repaint(closes_nw, NW_H, NW_MULT, NW_FACTOR)
                if res and all(v is not None for v in res):
                    upper, lower, mid = res
                    last_nw_update = now
                    log.info("[DEBUG] NEW NW band recalculated (half TF)")
            else:
                log.info("[DEBUG] Using frozen NW band")

            # EMAs & trend
            ema50 = df["close"].ewm(span=50).mean().iloc[-1]
            ema100 = df["close"].ewm(span=100).mean().iloc[-1]
            trend = "BUY" if ema50>ema100 else "SELL"

            log.info(f"[DEBUG] Close={close:.2f}, Upper={upper:.2f}, Lower={lower:.2f}, Mid={mid:.2f}, Trend={trend}")

            # SL-lock release
            if sl_lock:
                if (trend=="BUY" and close>mid) or (trend=="SELL" and close<mid):
                    sl_lock=False
                    log.info("🔓 SL-lock released")

            # live position
            try:
                pos = ex.fetch_positions([SYMBOL])[0]
                amt = float(pos["contracts"])
                live_side = pos["side"] if amt>0 else None
            except:
                amt=0
                live_side=None

            # detect NW touch → pending entry
            if not sl_lock and amt==0 and pending_entry is None:
                if trend=="BUY" and close <= lower:
                    pending_entry={"side":"long","price":close,"ts":now}
                    log.info(f"🟡 Pending LONG (MACD) @ {close:.2f}")
                elif trend=="SELL" and close >= upper:
                    pending_entry={"side":"short","price":close,"ts":now}
                    log.info(f"🟡 Pending SHORT (MACD) @ {close:.2f}")

            # pending → check MACD
            if pending_entry and amt==0:
                mac = macd_from_closes(closes_live)
                if mac:
                    dif_p,dif_n,dea_p,dea_n = mac
                    side=pending_entry["side"]

                    if side=="long" and macd_up(dif_p,dif_n,dea_p,dea_n):
                        if close > mid:
                            log.info("✖ MACD up but price > mid → cancel pending")
                            pending_entry=None
                        else:
                            qty=calc_order_size(ex, close)
                            try:
                                ex.create_market_order(SYMBOL,"buy",qty)
                                position={"side":"long","entry":close,"sl":close-SL_DISTANCE}
                                log.info(f"✅ OPEN LONG @ {close:.2f}")
                            except Exception as e:
                                log.warning(f"order error: {e}")
                            pending_entry=None

                    elif side=="short" and macd_down(dif_p,dif_n,dea_p,dea_n):
                        if close < mid:
                            log.info("✖ MACD down but price < mid → cancel pending")
                            pending_entry=None
                        else:
                            qty=calc_order_size(ex, close)
                            try:
                                ex.create_market_order(SYMBOL,"sell",qty)
                                position={"side":"short","entry":close,"sl":close+SL_DISTANCE}
                                log.info(f"✅ OPEN SHORT @ {close:.2f}")
                            except Exception as e:
                                log.warning(f"order error: {e}")
                            pending_entry=None

            # manage open position
            if amt>0 and position:
                side=position["side"]

                # LONG
                if side=="long":
                    if close >= upper:  # TP touch
                        ex.create_market_order(SYMBOL,"sell",amt,params={"reduceOnly":True})
                        pnl = close-position["entry"]
                        stats.append({"side":"LONG","entry":position["entry"],"exit":close,"pnl":pnl,"reason":"TP"})
                        log.info(f"✔ TP +{pnl:.2f}")
                        position=None

                    elif close <= position["sl"]:  # SL touch
                        ex.create_market_order(SYMBOL,"sell",amt,params={"reduceOnly":True})
                        pnl = close-position["entry"]
                        stats.append({"side":"LONG","entry":position["entry"],"exit":close,"pnl":pnl,"reason":"SL"})
                        log.warning(f"❌ SL {pnl:.2f}")
                        sl_lock=True
                        position=None

                    elif close > mid and position["sl"] < position["entry"] + BE_OFFSET:
                        position["sl"] = position["entry"] + BE_OFFSET
                        log.info(f"🔁 Move SL BE {position['sl']:.2f}")

                # SHORT
                elif side=="short":
                    if close <= lower: # TP touch
                        ex.create_market_order(SYMBOL,"buy",amt,params={"reduceOnly":True})
                        pnl = position["entry"]-close
                        stats.append({"side":"SHORT","entry":position["entry"],"exit":close,"pnl":pnl,"reason":"TP"})
                        log.info(f"✔ TP +{pnl:.2f}")
                        position=None

                    elif close >= position["sl"]: # SL
                        ex.create_market_order(SYMBOL,"buy",amt,params={"reduceOnly":True})
                        pnl = position["entry"]-close
                        stats.append({"side":"SHORT","entry":position["entry"],"exit":close,"pnl":pnl,"reason":"SL"})
                        log.warning(f"❌ SL {pnl:.2f}")
                        sl_lock=True
                        position=None

                    elif close < mid and position["sl"] > position["entry"] - BE_OFFSET:
                        position["sl"] = position["entry"] - BE_OFFSET
                        log.info(f"🔁 Move SL BE {position['sl']:.2f}")

            save_daily_stats(stats)
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            log.warning(f"loop error: {e}")
            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
