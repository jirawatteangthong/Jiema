# -*- coding: utf-8 -*-
# Binance Futures – NW Envelope + MACD Confirm (TF ย่อย)
# พร้อมระบบ SL Lock, Breakeven, Daily Report แบบย่อ, NW Freeze Half TF
# EMA Trend ใช้เป็นตัวเลือกเปิด/ปิดได้

import ccxt, time, json, math, logging, os, requests
from datetime import datetime

# ============================================================
# CONFIG (ปรับได้ทุกค่า พร้อมคำอธิบายท้ายบรรทัด)
# ============================================================

API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_BINANCE_API_KEY")     # API Binance Futures
SECRET  = os.getenv("BINANCE_SECRET",    "YOUR_BINANCE_SECRET")    # Secret Binance

SYMBOL = "BTC/USDT:USDT"                 # คู่เทรด Futures
TIMEFRAME = "15m"                        # TF หลักของ Nadaraya (เช่น 5m, 15m, 1h)
MACD_TF = "5m"                           # TF ย่อยใช้ MACD confirm (เช่น 1m/3m/5m)
MACD_ENABLED = True                      # เปิดปิดการใช้ MACD confirm (True/False)
USE_REPAINT = True                       # เผื่อแยกโหมดในอนาคต

LEVERAGE = 15                            # เลเวอเรจ
POSITION_MARGIN_FRACTION = 0.65          # % Margin ต่อไม้ (0.65 = ใช้ 65% ของ free USDT ต่อไม้)

# ----- EMA Trend Filter (เปิด/ปิดได้) -----
USE_EMA_TREND = False                    # False = ไม่ใช้ EMA filter, Long/Short ตาม lower/upper + MACD
EMA_FAST = 50                            # EMA เร็ว
EMA_SLOW = 100                           # EMA ช้า

# ----- Nadaraya-Watson -----
NW_BANDWIDTH = 8.0                       # ค่า h ของ Gaussian (LuxAlgo ใช้ 8)
NW_MULT = 3.0                            # ค่า mult ของ MAE baseline
NW_FACTOR = 1.5                          # ตัวคูณปรับความกว้างเพิ่มเติม
UPDATE_FRACTION = 0.50                   # คำนวณ NW band ทุก 50% ของ TF (half-TF freeze)

# ----- SL / TP / BE -----
SL_DISTANCE = 2000                       # SL คงที่จาก entry (แต้ม/ดอลลาร์)
TP_OFFSET = 100.0                        # ระยะ “ก่อนถึง” upper/lower สำหรับปิด TP (เช่น 100 เหรียญ)
USE_BREAKEVEN = True                     # เปิดระบบกันทุน (True/False)
BREAKEVEN_OFFSET = 100                   # SL กันทุน = entry ± OFFSET (กำไรเล็กน้อย)

# Daily report (ครั้งเดียว/วัน)
DAILY_REPORT_HH = 23                     # ชั่วโมงที่จะส่งรายงาน
DAILY_REPORT_MM = 59                     # นาทีที่จะส่งรายงาน
STATS_FILE = "daily_pnl.json"            # เก็บสถิติรายวัน
REPORT_SENT_FILE = "daily_report_sent.txt"  # flag ใช้เช็กว่าส่งรายงานวันนี้ไปแล้วหรือยัง

LOOP_SEC = 10                            # Loop เช็คราคา/SL/TP ทุก 10 วินาที
LOG_LEVEL = logging.INFO                 # DEBUG/INFO/WARNING

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
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("YOUR"):
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
    except Exception as e:
        log.warning(f"set_leverage warn: {e}")
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

# ---------- Nadaraya-Watson (LuxAlgo Style Repaint-ish) ----------
def nwe_luxalgo_repaint(closes, h=NW_BANDWIDTH, mult=NW_MULT, factor=NW_FACTOR):
    """
    ใช้ closes (list ของราคาปิด TF หลัก)
    คืนค่า (upper, lower, mid)
    """
    n = len(closes)
    if n < 200:
        return None, None, None

    win = min(499, n - 1)
    coefs = [math.exp(-(i * i) / (2 * (h ** 2))) for i in range(win)]
    den = sum(coefs)

    # endpoint mean (ใช้แท่งล่าสุด)
    num = sum(closes[-1 - j] * coefs[j] for j in range(win))
    mean = num / den

    # MAE แบบ dynamic (simple version)
    win_s = int(h * 10)
    win_s = min(win_s, win - 1)

    diffs = [abs(closes[-1 - i] - closes[-1 - i - 1]) for i in range(1, win_s)]
    mae = (sum(diffs) / len(diffs)) * mult * factor

    return mean + mae, mean - mae, mean

# ---------- MACD ----------
def macd(closes, fast=12, slow=26, signal=9):
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

def macd_up(dp,dn,ep,en):   # ตัดขึ้น
    return dp <= ep and dn > en

def macd_down(dp,dn,ep,en): # ตัดลง
    return dp >= ep and dn < en

# ============================================================
# Position sizing
# ============================================================
def free_usdt(ex):
    bal = ex.fetch_balance({"type":"future"})
    return float((bal.get("USDT") or {}).get("free") or 0.0)

def order_size(ex, price):
    free = free_usdt(ex)
    margin = free * POSITION_MARGIN_FRACTION
    notional = margin * LEVERAGE
    qty = notional / price if price > 0 else 0
    try:
        return float(ex.amount_to_precision(SYMBOL, qty))
    except:
        return round(qty, 3)

# ============================================================
# Daily Stats & Report (one time per day)
# ============================================================
def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            return json.load(open(STATS_FILE,"r"))
        except:
            pass
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "pnl": 0.0,
        "trades": []   # list of dict: {time, side, entry, exit, pnl, reason}
    }

def save_stats(s):
    json.dump(s, open(STATS_FILE,"w"), indent=2)

def has_sent_today():
    if not os.path.exists(REPORT_SENT_FILE):
        return False
    d = open(REPORT_SENT_FILE).read().strip()
    return d == datetime.now().strftime("%Y-%m-%d")

def mark_sent_today():
    open(REPORT_SENT_FILE,"w").write(datetime.now().strftime("%Y-%m-%d"))

def reset_report_if_new_day(stats):
    today = datetime.now().strftime("%Y-%m-%d")
    if stats.get("date") != today:
        # reset สถิติใหม่
        stats["date"] = today
        stats["pnl"] = 0.0
        stats["trades"] = []
        save_stats(stats)
        # ล้าง flag ส่งรายงาน
        open(REPORT_SENT_FILE,"w").write("")

def try_send_daily_report(stats):
    now = datetime.now()
    if not (now.hour == DAILY_REPORT_HH and now.minute == DAILY_REPORT_MM):
        return
    if has_sent_today():
        return

    # ถ้ายังไม่มีเทรดเลยในวันนั้น ส่งแค่บอกว่าไม่มี แล้ว mark เลย
    if not stats["trades"]:
        tg(f"📊 สรุปผลรายวัน {stats['date']}\nวันนี้ไม่มีออเดอร์")
        mark_sent_today()
        return

    total_pnl = stats["pnl"]
    tp_count = sum(1 for t in stats["trades"] if str(t.get("reason","")).startswith("TP"))
    sl_count = sum(1 for t in stats["trades"] if t.get("reason") == "SL")
    be_count = sum(1 for t in stats["trades"] if t.get("reason") == "BE")

    lines = [
        f"📊 สรุปผลรายวัน {stats['date']}",
        f"TP  : {tp_count} ครั้ง",
        f"SL  : {sl_count} ครั้ง",
        f"BE  : {be_count} ครั้ง",
        "────────────",
        f"รวมผลลัพธ์: {total_pnl:+.2f} USDT"
    ]
    tg("\n".join(lines))
    mark_sent_today()
    log.info("📨 Daily report sent.")

# ============================================================
# Main Loop
# ============================================================
def main():
    ex = setup_exchange()
    log.info(f"✅ Started Binance Futures NW Bot ({TIMEFRAME}, MACD={MACD_TF}, NW Freeze={UPDATE_FRACTION}, USE_EMA_TREND={USE_EMA_TREND})")

    stats = load_stats()

    position = None          # {"side","qty","entry","sl"}
    sl_lock = False
    sl_lock_ref = None       # บันทึกว่า ตอนโดน SL ราคาอยู่เหนือ/ใต้ mid
    pending = None           # {"side","touch_price","lower","upper","mid","ts"}

    last_nw_update = 0
    upper = lower = mid = None

    while True:
        try:
            reset_report_if_new_day(stats)
            try_send_daily_report(stats)

            # ---------- ข้อมูล TF หลัก ----------
            candles = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=600)
            closes = [c[4] for c in candles]
            last_close = closes[-1]

            # ---------- EMA Trend (ถ้าเปิดใช้) ----------
            e_fast = e_slow = None
            trend = None
            if USE_EMA_TREND:
                e_fast = ema(closes, EMA_FAST)
                e_slow = ema(closes, EMA_SLOW)
                if e_fast is None or e_slow is None:
                    time.sleep(LOOP_SEC)
                    continue
                trend = "BUY" if e_fast > e_slow else "SELL"

            # ---------- Nadaraya Freeze (half TF) ----------
            now_ts = time.time()
            if "m" in TIMEFRAME:
                tf_minutes = int(TIMEFRAME.replace("m",""))
            else:
                tf_minutes = int(TIMEFRAME.replace("h","")) * 60
            freeze_sec = tf_minutes * 60 * UPDATE_FRACTION

            if upper is None or now_ts - last_nw_update > freeze_sec:
                u,l,m = nwe_luxalgo_repaint(closes)
                if u is None:
                    log.info("[DEBUG] NW not ready")
                    time.sleep(LOOP_SEC)
                    continue
                upper,lower,mid = u,l,m
                last_nw_update = now_ts
                log.info(f"[DEBUG] NW updated: U={upper:.2f}, L={lower:.2f}, M={mid:.2f}")
            else:
                log.info("[DEBUG] Using previous NW band (frozen)")

            # ---------- MACD TF ย่อย (ใช้แท่งปิด) ----------
            macd_vals = None
            if MACD_ENABLED:
                small = ex.fetch_ohlcv(SYMBOL, MACD_TF, limit=200)
                mcloses = [c[4] for c in small[:-1]]  # ใช้เฉพาะแท่งปิด
                macd_vals = macd(mcloses)

            # ---------- อ่านโพซิชันจริง ----------
            try:
                pos_list = ex.fetch_positions([SYMBOL])
                amt = 0.0; live_side = None
                for p in pos_list:
                    if p.get("symbol") == SYMBOL and float(p.get("contracts") or 0) != 0:
                        amt = float(p["contracts"])
                        live_side = p["side"]
                        break
            except:
                amt=0.0; live_side=None

            # ถ้า exchange ปิด position แต่ local ยังมี -> sync
            if amt == 0 and position is not None:
                log.info("⚠ Position disappeared on exchange, reset local state.")
                position = None

            # ======================================================
            # MANAGE OPEN POSITION
            # ======================================================
            if position and amt > 0:
                last_price = ex.fetch_ticker(SYMBOL)["last"]
                side = position["side"]
                entry = position["entry"]
                sl = position["sl"]

                # คำนวณ TP ตาม upper/lower -/+ TP_OFFSET
                long_tp = upper - TP_OFFSET if TP_OFFSET > 0 else upper
                short_tp = lower + TP_OFFSET if TP_OFFSET > 0 else lower

                # ---------------- SL Touch ----------------
                if side=="long" and last_price <= sl:
                    pnl = (last_price-entry)*amt
                    # เช็กว่า SL นี้เป็น SL กันทุนหรือยัง (BE)
                    is_be = USE_BREAKEVEN and sl >= entry + BREAKEVEN_OFFSET - 1e-9
                    reason = "BE" if is_be else "SL"

                    stats["pnl"] += pnl
                    stats["trades"].append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "side": "LONG",
                        "entry": entry,
                        "exit": last_price,
                        "pnl": pnl,
                        "reason": reason
                    })
                    ex.create_market_order(SYMBOL,"sell",amt,params={"reduceOnly":True})
                    if is_be:
                        tg(f"🟡 LONG BE {entry:.2f}->{last_price:.2f} PnL={pnl:+.2f}")
                    else:
                        tg(f"🔴 LONG SL {entry:.2f}->{last_price:.2f} PnL={pnl:+.2f}")
                    position=None
                    sl_lock=True
                    sl_lock_ref = last_price > mid if mid is not None else None
                    save_stats(stats); time.sleep(LOOP_SEC); continue

                if side=="short" and last_price >= sl:
                    pnl = (entry-last_price)*amt
                    is_be = USE_BREAKEVEN and sl <= entry - BREAKEVEN_OFFSET + 1e-9
                    reason = "BE" if is_be else "SL"

                    stats["pnl"] += pnl
                    stats["trades"].append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "side": "SHORT",
                        "entry": entry,
                        "exit": last_price,
                        "pnl": pnl,
                        "reason": reason
                    })
                    ex.create_market_order(SYMBOL,"buy",amt,params={"reduceOnly":True})
                    if is_be:
                        tg(f"🟡 SHORT BE {entry:.2f}->{last_price:.2f} PnL={pnl:+.2f}")
                    else:
                        tg(f"🔴 SHORT SL {entry:.2f}->{last_price:.2f} PnL={pnl:+.2f}")
                    position=None
                    sl_lock=True
                    sl_lock_ref = last_price > mid if mid is not None else None
                    save_stats(stats); time.sleep(LOOP_SEC); continue

                # ---------------- TP จาก Upper/Lower (ก่อนถึงเป้า TP_OFFSET) ----------------
                if side=="long" and last_price >= long_tp:
                    pnl = (last_price-entry)*amt
                    stats["pnl"] += pnl
                    stats["trades"].append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "side": "LONG",
                        "entry": entry,
                        "exit": last_price,
                        "pnl": pnl,
                        "reason": "TP_upper"
                    })
                    ex.create_market_order(SYMBOL,"sell",amt,params={"reduceOnly":True})
                    tg(f"✅ LONG TP (Upper-Offset) @ {last_price:.2f} PnL={pnl:+.2f}")
                    position=None
                    save_stats(stats); time.sleep(LOOP_SEC); continue

                if side=="short" and last_price <= short_tp:
                    pnl = (entry-last_price)*amt
                    stats["pnl"] += pnl
                    stats["trades"].append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "side": "SHORT",
                        "entry": entry,
                        "exit": last_price,
                        "pnl": pnl,
                        "reason": "TP_lower"
                    })
                    ex.create_market_order(SYMBOL,"buy",amt,params={"reduceOnly":True})
                    tg(f"✅ SHORT TP (Lower-Offset) @ {last_price:.2f} PnL={pnl:+.2f}")
                    position=None
                    save_stats(stats); time.sleep(LOOP_SEC); continue

                # ---------------- EMA Flip ใช้ mid เป็น TP (ถ้าเปิดใช้ EMA เท่านั้น) ----------------
                if USE_EMA_TREND and e_fast is not None and e_slow is not None:
                    trend_now = "BUY" if e_fast > e_slow else "SELL"

                    if side=="long" and trend_now=="SELL":
                        # รอให้ราคาลงมาชน mid แล้วปิด
                        if last_price <= mid:
                            pnl = (last_price-entry)*amt
                            stats["pnl"] += pnl
                            stats["trades"].append({
                                "time": datetime.now().strftime("%H:%M:%S"),
                                "side": "LONG",
                                "entry": entry,
                                "exit": last_price,
                                "pnl": pnl,
                                "reason": "TP_mid_trend_flip"
                            })
                            ex.create_market_order(SYMBOL,"sell",amt,params={"reduceOnly":True})
                            tg(f"⚠ LONG EMA Flip TP Mid @ {last_price:.2f} PnL={pnl:+.2f}")
                            position=None
                            save_stats(stats); time.sleep(LOOP_SEC); continue

                    if side=="short" and trend_now=="BUY":
                        if last_price >= mid:
                            pnl = (entry-last_price)*amt
                            stats["pnl"] += pnl
                            stats["trades"].append({
                                "time": datetime.now().strftime("%H:%M:%S"),
                                "side": "SHORT",
                                "entry": entry,
                                "exit": last_price,
                                "pnl": pnl,
                                "reason": "TP_mid_trend_flip"
                            })
                            ex.create_market_order(SYMBOL,"buy",amt,params={"reduceOnly":True})
                            tg(f"⚠ SHORT EMA Flip TP Mid @ {last_price:.2f} PnL={pnl:+.2f}")
                            position=None
                            save_stats(stats); time.sleep(LOOP_SEC); continue

                # ---------------- Breakeven / Trailing กันทุน ----------------
                if USE_BREAKEVEN and not sl_lock:
                    if side=="long" and last_close > mid and position["sl"] < entry + BREAKEVEN_OFFSET:
                        position["sl"] = entry + BREAKEVEN_OFFSET
                        log.info(f"🔁 LONG Move SL to BE {position['sl']:.2f}")
                    if side=="short" and last_close < mid and position["sl"] > entry - BREAKEVEN_OFFSET:
                        position["sl"] = entry - BREAKEVEN_OFFSET
                        log.info(f"🔁 SHORT Move SL to BE {position['sl']:.2f}")

                save_stats(stats)
                time.sleep(LOOP_SEC)
                continue

            # ======================================================
            # NO POSITION
            # ======================================================
            if sl_lock:
                # ปลดล็อกเมื่อ "ราคาตัด mid" (ข้ามจากเหนือ->ใต้ หรือ ใต้->เหนือ)
                if mid is not None and sl_lock_ref is not None:
                    now_above = last_close > mid
                    if now_above != sl_lock_ref:
                        sl_lock = False
                        sl_lock_ref = None
                        log.info("🔓 SL Lock released (price crossed mid)")
                time.sleep(LOOP_SEC)
                continue

            # 1) ถ้ามี pending จาก NW touch เก่า → รอ MACD ตัด
            if MACD_ENABLED and pending is not None:
                last_price = ex.fetch_ticker(SYMBOL)["last"]
                side = pending["side"]
                # ต้องใช้ lower/upper/mid ตอน touch เดิม
                p_lower = pending["lower"]
                p_upper = pending["upper"]
                p_mid   = pending["mid"]

                if macd_vals:
                    dp,dn,ep,en = macd_vals
                    if side=="long":
                        macd_side_ok = macd_up(dp,dn,ep,en)
                    else:
                        macd_side_ok = macd_down(dp,dn,ep,en)

                    if macd_side_ok:
                        if side=="long":
                            # ตอน MACD ตัด ราคา ต้องอยู่ระหว่าง lower..mid และ ห้าม > mid
                            if last_price < p_lower or last_price > p_mid:
                                log.info("❌ MACD up but price out of [lower,mid] → cancel pending")
                                pending=None
                            else:
                                qty = order_size(ex, last_price)
                                ex.create_market_order(SYMBOL,"buy",qty)
                                position={"side":"long","qty":qty,"entry":last_price,"sl":last_price-SL_DISTANCE}
                                log.info(f"🚀 OPEN LONG (pending MACD confirm) @ {last_price:.2f}")
                                pending=None
                        else:  # short
                            # ตอน MACD ตัด ราคา ต้องอยู่ระหว่าง mid..upper และ ห้าม < mid
                            if last_price > p_upper or last_price < p_mid:
                                log.info("❌ MACD down but price out of [mid,upper] → cancel pending")
                                pending=None
                            else:
                                qty = order_size(ex, last_price)
                                ex.create_market_order(SYMBOL,"sell",qty)
                                position={"side":"short","qty":qty,"entry":last_price,"sl":last_price+SL_DISTANCE}
                                log.info(f"🚀 OPEN SHORT (pending MACD confirm) @ {last_price:.2f}")
                                pending=None

                save_stats(stats)
                time.sleep(LOOP_SEC)
                continue

            # 2) ไม่มี pending → ตรวจ NW touch ใหม่
            # NOTE: ถ้า USE_EMA_TREND=False จะไม่กรองด้วย EMA เลย
            can_long  = (not USE_EMA_TREND) or (trend == "BUY")
            can_short = (not USE_EMA_TREND) or (trend == "SELL")

            # NW touch LONG
            if can_long and last_close <= lower:
                last_price = ex.fetch_ticker(SYMBOL)["last"]
                if MACD_ENABLED:
                    # สร้าง pending (จำ band ตอนนี้)
                    pending = {
                        "side":"long",
                        "touch_price": last_price,
                        "lower": lower,
                        "upper": upper,
                        "mid": mid,
                        "ts": now_ts
                    }
                    log.info("🟡 LONG touch, waiting MACD up (pending created)")
                else:
                    qty = order_size(ex, last_price)
                    ex.create_market_order(SYMBOL,"buy",qty)
                    position={"side":"long","qty":qty,"entry":last_price,"sl":last_price-SL_DISTANCE}
                    log.info(f"🚀 LONG ENTRY (no MACD) @ {last_price:.2f}")
                save_stats(stats)
                time.sleep(LOOP_SEC)
                continue

            # NW touch SHORT
            if can_short and last_close >= upper:
                last_price = ex.fetch_ticker(SYMBOL)["last"]
                if MACD_ENABLED:
                    pending = {
                        "side":"short",
                        "touch_price": last_price,
                        "lower": lower,
                        "upper": upper,
                        "mid": mid,
                        "ts": now_ts
                    }
                    log.info("🟡 SHORT touch, waiting MACD down (pending created)")
                else:
                    qty = order_size(ex, last_price)
                    ex.create_market_order(SYMBOL,"sell",qty)
                    position={"side":"short","qty":qty,"entry":last_price,"sl":last_price+SL_DISTANCE}
                    log.info(f"🚀 SHORT ENTRY (no MACD) @ {last_price:.2f}")
                save_stats(stats)
                time.sleep(LOOP_SEC)
                continue

            save_stats(stats)
            time.sleep(LOOP_SEC)

        except Exception as e:
            log.exception(f"loop error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
