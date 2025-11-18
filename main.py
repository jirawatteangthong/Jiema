# -*- coding: utf-8 -*-
# Binance Futures NW Bot – EMA Trend + Nadaraya-Watson + MACD Confirm
# SL Lock, Breakeven, Freeze NW, Daily Report + TP by Mid When EMA Flip
# ---------------------------------------------------------------

import ccxt, time, json, math, logging, os, requests
from datetime import datetime

# ============================================================
# CONFIG
# ============================================================

API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_BINANCE_API_KEY")
SECRET  = os.getenv("BINANCE_SECRET",    "YOUR_BINANCE_SECRET")

SYMBOL = "BTC/USDT:USDT"
TIMEFRAME = "15m"
MACD_TF = "5m"                       # MACD TF ย่อย (ใช้แท่งปิดเท่านั้น)
MACD_ENABLED = True                  # เปิด/ปิด MACD confirm

LEVERAGE = 15
POSITION_MARGIN_FRACTION = 0.65      # ใช้ทุน 65% ต่อไม้

EMA_FAST = 50
EMA_SLOW = 100

NW_BANDWIDTH = 8.0
NW_MULT = 3.0
NW_FACTOR = 1.50
UPDATE_FRACTION = 0.50               # freeze NW ครึ่ง TF

SL_DISTANCE = 599
USE_BREAKEVEN = True
BREAKEVEN_OFFSET = 100

DAILY_REPORT_HH = 23
DAILY_REPORT_MM = 59
STATS_FILE = "daily_pnl.json"

LOOP_SEC = 10
LOG_LEVEL = logging.INFO

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")


# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("main")

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
# Exchange
# ============================================================
def setup_exchange():
    ex = ccxt.binance({
        "apiKey": API_KEY,
        "secret": SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "future"}
    })
    ex.load_markets()
    try: ex.set_leverage(LEVERAGE, SYMBOL)
    except: pass
    return ex

# ============================================================
# Indicators
# ============================================================
def ema(series, period):
    if len(series) < period: return None
    k = 2/(period+1)
    e = sum(series[:period])/period
    for v in series[period:]:
        e = v*k + e*(1-k)
    return e

# --- Nadaraya-Watson Repaint ---
def nwe_luxalgo_repaint(closes, h=NW_BANDWIDTH, mult=NW_MULT, factor=NW_FACTOR):
    n=len(closes)
    if n<200: return None,None,None
    win=min(499,n-1)
    coefs=[math.exp(-(i*i)/(2*(h**2))) for i in range(win)]
    den=sum(coefs)
    mean=sum(closes[-1-j]*coefs[j] for j in range(win))/den
    win_s=min(int(h*10), win-1)
    diffs=[abs(closes[-1-i] - closes[-2-i]) for i in range(1,win_s)]
    mae=(sum(diffs)/len(diffs))*mult*factor
    return mean+mae, mean-mae, mean

# --- MACD (ใช้แท่งปิด TF ย่อย) ---
def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow+signal+5: return None
    # fast ema
    kf=2/(fast+1); e=sum(closes[:fast])/fast
    ef=[None]*(fast-1)+[e]
    for v in closes[fast:]: e=v*kf + e*(1-kf); ef.append(e)
    # slow ema
    ks=2/(slow+1); e=sum(closes[:slow])/slow
    es=[None]*(slow-1)+[e]
    for v in closes[slow:]: e=v*ks + e*(1-ks); es.append(e)
    # dif
    dif=[a-b if a and b else None for a,b in zip(ef,es)]
    dif_clean=[x for x in dif if x is not None]
    if len(dif_clean) < signal+5: return None
    ks2=2/(signal+1); e=sum(dif_clean[:signal])/signal
    dea=[None]*(signal-1)+[e]
    for v in dif_clean[signal:]: e=v*ks2 + e*(1-ks2); dea.append(e)
    return dif_clean[-2], dif_clean[-1], dea[-2], dea[-1]

def macd_up(dp,dn,ep,en):   return dp<=ep and dn>en
def macd_down(dp,dn,ep,en): return dp>=ep and dn<en

# ============================================================
# Position sizing
# ============================================================
def free_usdt(ex):
    bal = ex.fetch_balance({"type":"future"})
    return float(bal["USDT"]["free"])

def order_size(ex, price):
    free = free_usdt(ex)
    margin = free*POSITION_MARGIN_FRACTION
    notional = margin*LEVERAGE
    qty = notional/price
    return float(ex.amount_to_precision(SYMBOL, qty))

# ============================================================
# Daily Stats
# ============================================================
def load_stats():
    if os.path.exists(STATS_FILE):
        return json.load(open(STATS_FILE))
    return {"date":datetime.now().strftime("%Y-%m-%d"),"pnl":0.0,"trades":[]}

def save_stats(s): json.dump(s, open(STATS_FILE,"w"), indent=2)

def daily_report(stats):
    now=datetime.now()
    if now.hour==DAILY_REPORT_HH and now.minute==DAILY_REPORT_MM:
        if stats["trades"]:
            msg="📊 Daily Report\n"
            msg+=f"PnL: {stats['pnl']:+.2f} USDT\n"
            for t in stats["trades"][-10:]: msg+=t+"\n"
            tg(msg)

# ============================================================
# MAIN LOOP
# ============================================================
def main():
    ex = setup_exchange()
    log.info(f"✅ Started NW Bot ({TIMEFRAME}, MACD={MACD_TF})")

    stats = load_stats()
    position=None
    sl_lock=False
    pending=None

    last_nw_update=0
    upper=lower=mid=None

    while True:
        try:
            # --- Load TF หลัก ---
            candles = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=600)
            closes = [c[4] for c in candles]
            last_close = closes[-1]

            # --- EMA Trend ---
            e_fast = ema(closes, EMA_FAST)
            e_slow = ema(closes, EMA_SLOW)
            trend = "BUY" if e_fast > e_slow else "SELL"

            # --- Update NW freeze ---
            now=time.time()
            tf_min=int(TIMEFRAME.replace("m",""))
            freeze=tf_min*60*UPDATE_FRACTION

            if now-last_nw_update > freeze:
                upper,lower,mid = nwe_luxalgo_repaint(closes)
                last_nw_update=now
                log.info(f"[DEBUG] NW updated U={upper:.2f} L={lower:.2f} M={mid:.2f}")
            else:
                log.info("[DEBUG] Using frozen NW")

            if upper is None: time.sleep(LOOP_SEC); continue

            # --- Load MACD TF ย่อย (ใช้แท่งปิด) ---
            macd_ok=None
            if MACD_ENABLED:
                small=ex.fetch_ohlcv(SYMBOL, MACD_TF, limit=200)
                mcloses=[c[4] for c in small]
                mac = macd(mcloses)
                if mac:
                    dp,dn,ep,en = mac
                    macd_ok = macd_up(dp,dn,ep,en) if trend=="BUY" else macd_down(dp,dn,ep,en)

            # --- If in position: SL, TP-Flip, BE ---
            if position:
                side=position["side"]; entry=position["entry"]; sl=position["sl"]; qty=position["qty"]

                # --- SL Hit ---
                if side=="long" and last_close <= sl:
                    pnl=(last_close-entry)*qty
                    stats["pnl"]+=pnl; stats["trades"].append(...)
                    ex.create_market_order(SYMBOL,"sell",qty)
                    position=None; sl_lock=True
                    log.info("🔴 LONG SL hit → lock")
                elif side=="short" and last_close >= sl:
                    pnl=(entry-last_close)*qty
                    stats["pnl"]+=pnl; stats["trades"].append(...)
                    ex.create_market_order(SYMBOL,"buy",qty)
                    position=None; sl_lock=True
                    log.info("🔴 SHORT SL hit → lock")

                # --- TP by Mid When EMA Flip ---
                new_trend = "BUY" if e_fast > e_slow else "SELL"
                if side=="long" and new_trend=="SELL" and last_close>=mid:
                    pnl=(last_close-entry)*qty
                    stats["pnl"]+=pnl; stats["trades"].append(f"LONG TP-mid = {pnl:+.2f}")
                    ex.create_market_order(SYMBOL,"sell",qty)
                    position=None
                    log.info("🟢 LONG TP by mid (EMA flip)")
                elif side=="short" and new_trend=="BUY" and last_close<=mid:
                    pnl=(entry-last_close)*qty
                    stats["pnl"]+=pnl; stats["trades"].append(f"SHORT TP-mid = {pnl:+.2f}")
                    ex.create_market_order(SYMBOL,"buy",qty)
                    position=None
                    log.info("🟢 SHORT TP by mid (EMA flip)")

                # --- Breakeven ---
                if USE_BREAKEVEN and position and not sl_lock:
                    if side=="long" and last_close>mid: position["sl"]=entry+BREAKEVEN_OFFSET
                    if side=="short" and last_close<mid: position["sl"]=entry-BREAKEVEN_OFFSET

                save_stats(stats)
                time.sleep(LOOP_SEC); continue

            # --- Unlock SL ---
            if sl_lock:
                if (trend=="BUY" and last_close>mid) or (trend=="SELL" and last_close<mid):
                    sl_lock=False; log.info("🔓 SL unlock")
                time.sleep(LOOP_SEC); continue

            # ======================================================
            # ENTRY: NW Touch → MACD Confirm → ตรวจ "Touch Zone"
            # ======================================================

            # --- LONG Touch ---
            if trend=="BUY" and last_close <= lower:
                pending={"side":"long","touch_price":last_close}
                log.info("🟡 LONG NW touch → waiting MACD")
                time.sleep(LOOP_SEC); continue

            # --- SHORT Touch ---
            if trend=="SELL" and last_close >= upper:
                pending={"side":"short","touch_price":last_close}
                log.info("🟡 SHORT NW touch → waiting MACD")
                time.sleep(LOOP_SEC); continue

            # --- PENDING ENTRY LOGIC ---
            if pending:
                side=pending["side"]; tp=pending["touch_price"]

                if not MACD_ENABLED:
                    pass
                else:
                    # MACD ยังไม่ confirm → continue
                    if macd_ok is not True:
                        time.sleep(LOOP_SEC); continue

                # MACD confirm แล้ว → ตรวจว่าอยู่ใน "touch–mid"
                if side=="long":
                    if not (tp <= last_close <= mid):
                        log.info("❌ LONG取消: price left touch-mid zone")
                        pending=None; time.sleep(LOOP_SEC); continue

                    qty=order_size(ex,last_close)
                    ex.create_market_order(SYMBOL,"buy",qty)
                    position={"side":"long","entry":last_close,"qty":qty,"sl":last_close-SL_DISTANCE}
                    log.info(f"🚀 LONG ENTRY @ {last_close}")
                    pending=None
                    time.sleep(LOOP_SEC); continue

                if side=="short":
                    if not (mid <= last_close <= tp):
                        log.info("❌ SHORT取消: price left touch-mid zone")
                        pending=None; time.sleep(LOOP_SEC); continue

                    qty=order_size(ex,last_close)
                    ex.create_market_order(SYMBOL,"sell",qty)
                    position={"side":"short","entry":last_close,"qty":qty,"sl":last_close+SL_DISTANCE}
                    log.info(f"🚀 SHORT ENTRY @ {last_close}")
                    pending=None
                    time.sleep(LOOP_SEC); continue

            # Daily
            daily_report(stats); save_stats(stats)
            time.sleep(LOOP_SEC)

        except Exception as e:
            log.exception(f"loop error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
