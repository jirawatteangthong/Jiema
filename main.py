# ======================================================
# Binance Futures Nadaraya-Watson Bot (LuxAlgo Half-TF + MACD confirm)
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
TIMEFRAME = "15m"           # "15m", "5m", "1h", ...
LEVERAGE = 15
POSITION_MARGIN_FRACTION = 0.60

SL_DISTANCE = 500           # SL จาก entry (USD)
BE_OFFSET = 100             # ระยะ SL กันทุน (USD)
NW_H = 8.0
NW_MULT = 2.0
NW_FACTOR = 1.0           # ปรับแคบ/กว้าง (calibration)
UPDATE_FRACTION = 0.5       # อัปเดต band ทุกครึ่ง TF
CHECK_INTERVAL = 10         # เช็กสัญญาณทุก 10 วินาที
DAILY_HOUR = 23             # ส่งรายงานเวลา 23:00

# MACD params (real-time confirm)
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# ========== LOG ==========
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("LuxAlgo_NW_MACD")

# ========== TELEGRAM ==========
def tg_send(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except: pass

# ========== DAILY REPORT ==========
STATS_FILE = "daily_stats.json"
def report_daily(stats):
    now = datetime.now()
    if now.hour == DAILY_HOUR and stats:
        pnl = sum(t["pnl"] for t in stats)
        msg = [f"📊 สรุปรายวัน {now:%Y-%m-%d}",
               f"รวมกำไร: {pnl:+.2f} USDT", "────────────"]
        for t in stats[-10:]:
            msg.append(f"{t['side']} | {t['entry']:.2f}→{t['exit']:.2f} | "
                       f"{t['pnl']:+.2f} ({t['reason']})")
        tg_send("\n".join(msg))
        open(STATS_FILE, "w").write(json.dumps([]))

# ========== LUXALGO NADARAYA-WATSON ==========
def nwe_luxalgo_repaint(closes, h=8.0, mult=3.0, factor=1.5):
    n=len(closes)
    if n<100: return None,None,None
    win=min(499,n-1)
    # non-uniform weight scaled by factor (affects mean before normalization)
    coefs=[math.exp(-(i**2)/(2*(h**2))) for i in range(win)]
    # if you want factor to affect weight-shape, apply here (or use factor later on mae)
    # We'll apply factor to mae at the end (simple & stable)
    den=sum(coefs)
    out=[]
    for idx in range(win,n):
        num=sum(closes[idx-j]*coefs[j] for j in range(win))
        out.append(num/den)
    if not out: return None,None,None
    mean=out[-1]
    win_s=int(h*10)
    if win_s>len(out): win_s=len(out)
    diffs=[abs(closes[-i]-out[-i]) for i in range(1,win_s+1)]
    mae=(sum(diffs)/len(diffs))*mult*factor
    return mean+mae, mean-mae, mean

# ========== MACD (real-time) ==========
def ema_series(values, period):
    if len(values) < period:
        return []
    k = 2.0/(period+1)
    out = [None]*(period-1)
    s = sum(values[:period])/period
    out.append(s)
    e = s
    for v in values[period:]:
        e = v*k + e*(1-k)
        out.append(e)
    return out

def macd_from_closes(closes):
    # returns dif_prev, dif_now, dea_prev, dea_now  (or None if not enough data)
    if len(closes) < MACD_SLOW + MACD_SIGNAL + 2:
        return None
    ef = ema_series(closes, MACD_FAST)
    es = ema_series(closes, MACD_SLOW)
    dif = []
    for i in range(len(closes)):
        if i < len(ef) and i < len(es) and ef[i] is not None and es[i] is not None:
            dif.append(ef[i] - es[i])
    dea = ema_series(dif, MACD_SIGNAL)
    if len(dif) < 2 or len(dea) < 2:
        return None
    # pick last two: prev index -2, now index -1
    return dif[-2], dif[-1], dea[-2], dea[-1]

def macd_up(dif_p,dif_n,dea_p,dea_n):
    return dif_p <= dea_p and dif_n > dea_n

def macd_down(dif_p,dif_n,dea_p,dea_n):
    return dif_p >= dea_p and dif_n < dea_n

# ========== EXCHANGE ==========
def setup_exchange():
    ex=ccxt.binance({
        "apiKey":API_KEY,
        "secret":SECRET,
        "enableRateLimit":True,
        "options":{"defaultType":"future"}
    })
    ex.load_markets()
    try:
        ex.set_leverage(LEVERAGE,SYMBOL)
    except Exception as e:
        log.warning(f"set_leverage warn: {e}")
    return ex

def free_usdt(ex):
    try:
        bal=ex.fetch_balance({'type':'future'})
        return float((bal.get('USDT') or {}).get('free') or 0.0)
    except:
        return 0.0

def calc_order_size(ex,price):
    free=free_usdt(ex)
    margin=free*POSITION_MARGIN_FRACTION
    notional=margin*LEVERAGE
    qty=notional/price if price>0 else 0
    try: return float(ex.amount_to_precision(SYMBOL,qty))
    except: return round(qty,3)

# ========== MAIN ==========
def main():
    last_nw_update=0
    upper=lower=mid=None

    ex=setup_exchange()
    log.info(f"✅ Started Binance Futures LuxAlgo Bot ({TIMEFRAME}, Half-TF + MACD confirm)")
    sl_lock=False
    position=None
    stats=json.load(open(STATS_FILE)) if os.path.exists(STATS_FILE) else []
    pending_entry = None   # {'side':'long'/'short','price':..., 'created':ts}

    while True:
        try:
            # -------- fetch ----------
            ohlcv=ex.fetch_ohlcv(SYMBOL,TIMEFRAME,limit=600)   # bigger limit for stability
            df=pd.DataFrame(ohlcv,columns=["time","open","high","low","close","vol"])
            # use closed-bars for NW calc (semi-repaint decision)
            closes_for_nw = df["close"].iloc[:-1].tolist()   # use closed bars only
            # use live closes (including last) for MACD/entry checks (real-time)
            closes_live = df["close"].tolist()
            close = closes_live[-1]

            # -------- Determine TF seconds & update timing ----------
            tf_minutes=1
            if "m" in TIMEFRAME:
                tf_minutes=int(TIMEFRAME.replace("m",""))
            elif "h" in TIMEFRAME:
                tf_minutes=int(TIMEFRAME.replace("h",""))*60
            tf_seconds=tf_minutes*60
            now_ts=time.time()

            # update NW band only every UPDATE_FRACTION * TF
            if now_ts - last_nw_update < tf_seconds * UPDATE_FRACTION and all(x is not None for x in [upper,lower,mid]):
                log.info("[DEBUG] Using previous NW band (frozen half TF)")
            else:
                res = nwe_luxalgo_repaint(closes_for_nw if len(closes_for_nw)>0 else closes_live, NW_H, NW_MULT, NW_FACTOR)
                if res is None or any(v is None for v in res):
                    log.info("[DEBUG] NW calc not ready (need more bars)")
                    time.sleep(CHECK_INTERVAL)
                    continue
                upper,lower,mid = res
                last_nw_update = now_ts
                log.info("[DEBUG] Recalculated NW band (half TF update)")

            # -------- compute EMAs & trend ----------
            ema50 = df["close"].ewm(span=50,adjust=False).mean().iloc[-1]
            ema100 = df["close"].ewm(span=100,adjust=False).mean().iloc[-1]
            trend = "BUY" if ema50>ema100 else "SELL"
            log.info(f"[DEBUG] Close={close:.2f}, Upper={upper:.2f}, Lower={lower:.2f}, Mid={mid:.2f}, Trend={trend}")

            # -------- unlock SL-lock if cross mid ----------
            if sl_lock:
                if (trend=="BUY" and close>mid) or (trend=="SELL" and close<mid):
                    sl_lock=False
                    log.info("🔓 SL lock released")

            # -------- check live position on exchange ----------
            try:
                pos = ex.fetch_positions([SYMBOL])[0]
                amt = float(pos["contracts"])
                live_side = pos["side"] if amt>0 else None
            except Exception:
                amt = 0
                live_side = None

            # -------- detect initial NW touch signal --------
            # Only generate touch signal when no pending and no open position and not locked
            if not sl_lock and amt==0 and pending_entry is None:
                if trend=="BUY" and close <= lower:
                    # create pending entry and WAIT for MACD confirm
                    pending_entry = {"side":"long","price":close,"created":now_ts}
                    log.info(f"🟡 Pending LONG (waiting MACD) @ {close:.2f}")
                elif trend=="SELL" and close >= upper:
                    pending_entry = {"side":"short","price":close,"created":now_ts}
                    log.info(f"🟡 Pending SHORT (waiting MACD) @ {close:.2f}")

            # -------- if pending -> check MACD confirm (real-time) ----------
            if pending_entry and amt==0:
                macd_vals = macd_from_closes(closes_live)
                if macd_vals:
                    dif_p,dif_n,dea_p,dea_n = macd_vals
                    side = pending_entry["side"]
                    # check cross
                    if side=="long" and macd_up(dif_p,dif_n,dea_p,dea_n):
                        # MACD cut up; now check price vs mid: if price > mid -> cancel pending and wait new NW touch
                        if close > mid:
                            log.info("✖ MACD up but price > mid → cancel pending, will wait new NW touch")
                            pending_entry = None
                        else:
                            # confirm entry
                            qty = calc_order_size(ex, close)
                            if qty>0:
                                try:
                                    ex.create_market_order(SYMBOL,"buy",qty)
                                    position={"side":"long","entry":close,"sl":close-SL_DISTANCE}
                                    log.info(f"✅ OPEN LONG @ {close:.2f} qty={qty:.4f} SL={position['sl']:.2f}")
                                    pending_entry = None
                                except Exception as e:
                                    log.warning(f"order err: {e}")
                    elif side=="short" and macd_down(dif_p,dif_n,dea_p,dea_n):
                        # MACD cut down; check price vs mid: if price < mid -> cancel pending and wait new NW touch
                        if close < mid:
                            log.info("✖ MACD down but price < mid → cancel pending, will wait new NW touch")
                            pending_entry = None
                        else:
                            qty = calc_order_size(ex, close)
                            if qty>0:
                                try:
                                    ex.create_market_order(SYMBOL,"sell",qty)
                                    position={"side":"short","entry":close,"sl":close+SL_DISTANCE}
                                    log.info(f"✅ OPEN SHORT @ {close:.2f} qty={qty:.4f} SL={position['sl']:.2f}")
                                    pending_entry = None
                                except Exception as e:
                                    log.warning(f"order err: {e}")
                # if no macd_vals yet, keep waiting (do not cancel automatically)

            # -------- manage existing position (monitor TP/SL/BE) ----------
            if amt>0 and position:
                if position["side"]=="long":
                    # TP touch
                    if close >= upper:
                        try:
                            ex.create_market_order(SYMBOL,"sell",amt,params={"reduceOnly":True})
                        except: pass
                        pnl = close - position["entry"]
                        stats.append({"side":"LONG","entry":position["entry"],"exit":close,"pnl":pnl,"reason":"TP"})
                        log.info(f"✅ TP hit +{pnl:.2f}")
                        position=None
                    # SL hit
                    elif close <= position["sl"]:
                        try:
                            ex.create_market_order(SYMBOL,"sell",amt,params={"reduceOnly":True})
                        except: pass
                        pnl = close - position["entry"]
                        stats.append({"side":"LONG","entry":position["entry"],"exit":close,"pnl":pnl,"reason":"SL"})
                        log.warning(f"❌ SL hit {pnl:.2f}")
                        sl_lock=True; position=None
                    # move SL to BE if closed above mid (and BE improvement)
                    elif close > mid and position["sl"] < position["entry"] + BE_OFFSET:
                        position["sl"] = position["entry"] + BE_OFFSET
                        log.info(f"🔁 Move SL to breakeven {position['sl']:.2f}")

                elif position["side"]=="short":
                    if close <= lower:
                        try:
                            ex.create_market_order(SYMBOL,"buy",amt,params={"reduceOnly":True})
                        except: pass
                        pnl = position["entry"] - close
                        stats.append({"side":"SHORT","entry":position["entry"],"exit":close,"pnl":pnl,"reason":"TP"})
                        log.info(f"✅ TP hit +{pnl:.2f}")
                        position=None
                    elif close >= position["sl"]:
                        try:
                            ex.create_market_order(SYMBOL,"buy",amt,params={"reduceOnly":True})
                        except: pass
                        pnl = position["entry"] - close
                        stats.append({"side":"SHORT","entry":position["entry"],"exit":close,"pnl":pnl,"reason":"SL"})
                        log.warning(f"❌ SL hit {pnl:.2f}")
                        sl_lock=True; position=None
                    elif close < mid and position["sl"] > position["entry"] - BE_OFFSET:
                        position["sl"] = position["entry"] - BE_OFFSET
                        log.info(f"🔁 Move SL to breakeven {position['sl']:.2f}")

            # -------- daily report & persist stats ----------
            report_daily(stats)
            open(STATS_FILE,"w").write(json.dumps(stats,indent=2))

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.warning(f"loop error: {e}")
            time.sleep(CHECK_INTERVAL)

if __name__=="__main__":
    main()
