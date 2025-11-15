# -*- coding: utf-8 -*-
# Binance Futures – EMA50/100 + Nadaraya-Watson Envelope + MACD Confirm (TF ย่อย)
# พร้อมระบบ SL Lock, Breakeven, Daily Report, NW Freeze Half TF
# ---------------------------------------------------------------

import ccxt, time, json, math, logging, os, requests
from datetime import datetime

# ============================================================
# CONFIG (ปรับได้ทุกค่า พร้อมคำอธิบายท้ายบรรทัด)
# ============================================================

API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_BINANCE_API_KEY")
SECRET  = os.getenv("BINANCE_SECRET",    "YOUR_BINANCE_SECRET")

SYMBOL = "BTC/USDT:USDT"                 # คู่เทรด
TIMEFRAME = "15m"                        # TF หลักของ EMA + Nadaraya
MACD_TF = "3m"                           # TF ย่อยใช้ MACD confirm (เช่น 1m/3m/5m)
MACD_ENABLED = True                      # เปิดปิดการใช้ MACD confirm
USE_REPAINT = True                       # ใช้ LuxAlgo Repaint mode

LEVERAGE = 15                            # เลเวอเรจ
POSITION_MARGIN_FRACTION = 0.65          # % Margin ต่อไม้ (0.80 = ใช้ 80% ของ free USDT)

EMA_FAST = 50                            # EMA เร็ว
EMA_SLOW = 100                           # EMA ช้า

NW_BANDWIDTH = 8.0                       # ค่า h ของ Gaussian (LuxAlgo ใช้ 8)
NW_MULT = 3.0                            # ค่า mult ของ MAE baseline
NW_FACTOR = 1.5                         # ตัวคูณปรับความกว้างเพิ่มเติม (คุณปรับแล้วตรง)
UPDATE_FRACTION = 0.50                   # คำนวณ NW band ทุก 50% ของ TF

SL_DISTANCE = 599                        # SL คงที่จาก entry (แต้ม)
USE_BREAKEVEN = True                     # เปิดระบบกันทุน
BREAKEVEN_OFFSET = 100                   # SL กันทุน +100

DAILY_REPORT_HH = 23                     # ส่งรายงานตอน 23:59
DAILY_REPORT_MM = 59
STATS_FILE = "daily_pnl.json"

LOOP_SEC = 10                            # Loop เช็คราคา/SL/TP
LOG_LEVEL = logging.INFO                  # DEBUG/INFO

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")


# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("main")

# ============================================================
# Telegram
# ============================================================
def tg(msg):
    if TELEGRAM_TOKEN.startswith("YOUR"):
        return
    try:
        requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            params={"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        )
    except:
        pass

# ============================================================
# Exchange Setup
# ============================================================
def setup_exchange():
    ex = ccxt.binance({
        "apiKey": API_KEY,
        "secret": SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "future"}
    })
    ex.load_markets()
    try:
        ex.set_leverage(LEVERAGE, SYMBOL)
    except:
        pass
    return ex

# ============================================================
# Indicators
# ============================================================
def ema(series, period):
    if len(series) < period:
        return None
    k = 2 / (period + 1)
    e = sum(series[:period]) / period
    for v in series[period:]:
        e = v * k + e * (1 - k)
    return e

# ---------- Nadaraya-Watson (LuxAlgo Style Repaint) ----------
def nwe_luxalgo_repaint(closes, h=NW_BANDWIDTH, mult=NW_MULT, factor=NW_FACTOR):
    n = len(closes)
    if n < 200:
        return None, None, None

    win = min(499, n - 1)
    coefs = [math.exp(-(i * i) / (2 * (h ** 2))) for i in range(win)]
    den = sum(coefs)

    # Compute endpoint (latest mean)
    num = sum(closes[-1 - j] * coefs[j] for j in range(win))
    mean = num / den

    # Compute dynamic MAE window
    win_s = int(h * 10)
    win_s = min(win_s, win - 1)

    diffs = [abs(closes[-1 - i] - closes[-1 - i - 1]) for i in range(1, win_s)]
    mae = (sum(diffs) / len(diffs)) * mult * factor

    return mean + mae, mean - mae, mean

# ---------- MACD ----------
def macd(closes, fast=12, slow=26, signal=9):
    ef = []
    es = []
    if len(closes) < slow + signal + 5:
        return None
    # fast EMA
    kf = 2 / (fast + 1)
    e = sum(closes[:fast]) / fast
    ef = [None]*(fast-1) + [e]
    for v in closes[fast:]:
        e = v*kf + e*(1-kf)
        ef.append(e)
    # slow EMA
    ks = 2 / (slow + 1)
    e = sum(closes[:slow]) / slow
    es = [None]*(slow-1) + [e]
    for v in closes[slow:]:
        e = v*ks + e*(1-ks)
        es.append(e)

    dif = []
    for a,b in zip(ef, es):
        if a is not None and b is not None:
            dif.append(a-b)
        else:
            dif.append(None)
    dif_clean = [x for x in dif if x is not None]
    if len(dif_clean) < signal+5:
        return None
    ks2 = 2/(signal+1)
    e = sum(dif_clean[:signal]) / signal
    dea = [None]*(signal-1) + [e]
    for v in dif_clean[signal:]:
        e = v*ks2 + e*(1-ks2)
        dea.append(e)

    return dif_clean[-2], dif_clean[-1], dea[-2], dea[-1]

def macd_up(dp,dn,ep,en):
    return dp <= ep and dn > en

def macd_down(dp,dn,ep,en):
    return dp >= ep and dn < en

# ============================================================
# Position sizing
# ============================================================
def free_usdt(ex):
    bal = ex.fetch_balance({"type":"future"})
    return float(bal["USDT"]["free"])

def order_size(ex, price):
    free = free_usdt(ex)
    margin = free * POSITION_MARGIN_FRACTION
    notional = margin * LEVERAGE
    qty = notional / price
    return float(ex.amount_to_precision(SYMBOL, qty))

# ============================================================
# Daily Stats
# ============================================================
def load_stats():
    if os.path.exists(STATS_FILE):
        return json.load(open(STATS_FILE,"r"))
    return {"date":datetime.now().strftime("%Y-%m-%d"),"pnl":0.0,"trades":[]}

def save_stats(stats):
    json.dump(stats, open(STATS_FILE,"w"), indent=2)

def daily_report(stats):
    now = datetime.now()
    if now.hour == DAILY_REPORT_HH and now.minute == DAILY_REPORT_MM:
        if stats["trades"]:
            msg = "📊 Daily Report\n"
            msg += f"PnL: {stats['pnl']:+.2f} USDT\n"
            for t in stats["trades"][-10:]:
                msg += f"{t}\n"
            tg(msg)

# ============================================================
# Main Loop
# ============================================================
def main():
    ex = setup_exchange()
    log.info(f"✅ Started Binance Futures NW Bot ({TIMEFRAME}, MACD {MACD_TF}, NW Freeze={UPDATE_FRACTION})")

    stats = load_stats()

    position = None
    sl_lock = False
    pending_entry = None

    last_nw_update = 0
    upper = lower = mid = None

    while True:
        try:
            ticker = ex.fetch_ticker(SYMBOL)
            last_price = ticker["last"]

            # ---------- load candles TF หลัก ----------
            candles = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=600)
            closes = [c[4] for c in candles]
            last_close = closes[-1]

            # ---------- EMA Trend ----------
            e_fast = ema(closes, EMA_FAST)
            e_slow = ema(closes, EMA_SLOW)
            trend = "BUY" if e_fast > e_slow else "SELL"

            # ---------- NW Update แบบ Freeze ----------
            now = time.time()
            tf_minutes = int(TIMEFRAME.replace("m",""))  # รองรับเฉพาะ m
            freeze_sec = tf_minutes * 60 * UPDATE_FRACTION

            if now - last_nw_update > freeze_sec:
                upper, lower, mid = nwe_luxalgo_repaint(closes)
                last_nw_update = now
                log.info(f"[DEBUG] NW updated: U={upper:.2f}, L={lower:.2f}, M={mid:.2f}")
            else:
                log.info("[DEBUG] Using previous NW band (frozen)")

            if upper is None:
                time.sleep(LOOP_SEC)
                continue

            # ---------- MACD (TF ย่อย) ----------
            macd_ok = None
            if MACD_ENABLED:
                small = ex.fetch_ohlcv(SYMBOL, MACD_TF, limit=200)
                mcloses = [c[4] for c in small]
                mac = macd(mcloses)
                if mac:
                    dp,dn,ep,en = mac
                    macd_ok = macd_up(dp,dn,ep,en) if trend=="BUY" else macd_down(dp,dn,ep,en)

            # ---------- ถ้ามี position → จัดการ SL/TP ----------
            if position:
                side = position["side"]
                entry = position["entry"]
                sl = position["sl"]

                # SL Hit แบบ Touch
                if side=="long" and last_price <= sl:
                    pnl = (last_price-entry)*position["qty"]
                    stats["pnl"] += pnl
                    stats["trades"].append(f"LONG SL {entry}->{last_price} = {pnl:+.2f}")
                    ex.create_market_order(SYMBOL,"sell",position["qty"])
                    position = None
                    sl_lock = True
                    log.info("🔴 LONG SL hit → lock")
                elif side=="short" and last_price >= sl:
                    pnl = (entry-last_price)*position["qty"]
                    stats["pnl"] += pnl
                    stats["trades"].append(f"SHORT SL {entry}->{last_price} = {pnl:+.2f}")
                    ex.create_market_order(SYMBOL,"buy",position["qty"])
                    position = None
                    sl_lock = True
                    log.info("🔴 SHORT SL hit → lock")

                # Break-even
                if USE_BREAKEVEN and position and not sl_lock:
                    if side=="long" and last_close > mid:
                        position["sl"] = entry + BREAKEVEN_OFFSET
                    if side=="short" and last_close < mid:
                        position["sl"] = entry - BREAKEVEN_OFFSET

                save_stats(stats)
                time.sleep(LOOP_SEC)
                continue

            # ---------- ปลดล็อก SL Lock ----------
            if sl_lock:
                if (trend=="BUY" and last_close > mid) or (trend=="SELL" and last_close < mid):
                    sl_lock = False
                    log.info("🔓 SL Lock released")
                time.sleep(LOOP_SEC)
                continue

            # ======================================================
            #           ENTRY LOGIC: NW Touch → MACD Confirm
            # ======================================================

            # ---------------- LONG ----------------
            if trend=="BUY" and last_close <= lower:

                if MACD_ENABLED:
                    if macd_ok is None or macd_ok is False:
                        log.info("🟡 LONG touch but waiting MACD up")
                        pending_entry = {"side":"long"}
                        time.sleep(LOOP_SEC)
                        continue

                    # ถ้า MACD up แล้ว แต่ราคา > mid → ยกเลิก signal
                    if last_price > mid:
                        log.info("❌ MACD up but price > mid → reject signal")
                        pending_entry = None
                        time.sleep(LOOP_SEC)
                        continue

                # เปิด LONG
                qty = order_size(ex, last_price)
                ex.create_market_order(SYMBOL, "buy", qty)
                position = {"side":"long","qty":qty,"entry":last_price,"sl":last_price-SL_DISTANCE}
                pending_entry=None
                log.info(f"🚀 LONG ENTRY @ {last_price}")
                time.sleep(LOOP_SEC)
                continue

            # ---------------- SHORT ----------------
            if trend=="SELL" and last_close >= upper:

                if MACD_ENABLED:
                    if macd_ok is None or macd_ok is False:
                        log.info("🟡 SHORT touch but waiting MACD down")
                        pending_entry = {"side":"short"}
                        time.sleep(LOOP_SEC)
                        continue

                    if last_price < mid:
                        log.info("❌ MACD down but price < mid → reject signal")
                        pending_entry=None
                        time.sleep(LOOP_SEC)
                        continue

                qty = order_size(ex, last_price)
                ex.create_market_order(SYMBOL, "sell", qty)
                position = {"side":"short","qty":qty,"entry":last_price,"sl":last_price+SL_DISTANCE}
                pending_entry=None
                log.info(f"🚀 SHORT ENTRY @ {last_price}")
                time.sleep(LOOP_SEC)
                continue

            # Daily Report
            daily_report(stats)
            save_stats(stats)

            time.sleep(LOOP_SEC)

        except Exception as e:
            log.exception(f"loop error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
