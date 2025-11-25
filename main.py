# -*- coding: utf-8 -*-
# Binance Futures – EMA50/100 + Nadaraya-Watson Envelope + MACD Confirm (TF ย่อย)
# ปรับเพิ่ม: TP buffer, BE via MACD (ทดลอง), EMA on/off, daily summary counts (TP/SL/BE)

import ccxt, time, json, math, logging, os, requests
from datetime import datetime

# ============================================================
# CONFIG (ปรับค่าได้)
# ============================================================
API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_BINANCE_API_KEY")
SECRET  = os.getenv("BINANCE_SECRET",    "YOUR_BINANCE_SECRET")

SYMBOL = "BTC/USDT:USDT"
TIMEFRAME = "15m"
MACD_TF = "5m"               # TF ย่อย สำหรับ confirm entry และ (option) BE check
MACD_ENABLED = True
USE_REPAINT = True

LEVERAGE = 15
POSITION_MARGIN_FRACTION = 0.65

EMA_FAST = 50
EMA_SLOW = 100
EMA_ENABLED = False          # A ปิด (ตามที่สั่ง) — ถ้า False จะใช้ last_close vs mid เพื่อกำหนด trend

NW_BANDWIDTH = 8.0
NW_MULT = 3.0
NW_FACTOR = 1.5
UPDATE_FRACTION = 0.50

SL_DISTANCE = 2000

# Breakeven settings
USE_BREAKEVEN = True
BREAKEVEN_OFFSET = 100
BREAKEVEN_USE_MACD = True   # ถ้า True จะใช้ MACD (MACD_TF) ตามข้อทดลองที่ขอ (ปิดได้)
MACD_FOR_BREAKEVEN_TF = "5m"  # TF ที่ใช้สำหรับตัด BE แบบ MACD (ตามที่ขอทดลอง)

# TP buffer: ปิดก่อนถึง upper/lower เท่าไร (ค่าที่ขอ: 100)
TP_BUFFER = 100

# Daily report (ครั้งเดียว/วัน) — สรุปเฉพาะ Count TP/SL/BE และ PnL
DAILY_REPORT_HH = 23
DAILY_REPORT_MM = 59
STATS_FILE = "daily_pnl.json"
REPORT_SENT_FILE = "daily_report_sent.txt"

LOOP_SEC = 10
LOG_LEVEL = logging.INFO

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
# Telegram helper
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

def nwe_luxalgo_repaint(closes, h=NW_BANDWIDTH, mult=NW_MULT, factor=NW_FACTOR):
    n = len(closes)
    if n < 200:
        return None, None, None

    win = min(499, n - 1)
    coefs = [math.exp(-(i * i) / (2 * (h ** 2))) for i in range(win)]
    den = sum(coefs)

    num = sum(closes[-1 - j] * coefs[j] for j in range(win))
    mean = num / den

    win_s = int(h * 10)
    win_s = min(win_s, win - 1)

    diffs = [abs(closes[-1 - i] - closes[-1 - i - 1]) for i in range(1, win_s)]
    mae = (sum(diffs) / len(diffs)) * mult * factor

    return mean + mae, mean - mae, mean

def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal + 5:
        return None

    kf = 2 / (fast + 1)
    e = sum(closes[:fast]) / fast
    ef = [None]*(fast-1) + [e]
    for v in closes[fast:]:
        e = v*kf + e*(1-kf)
        ef.append(e)

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
# Daily Stats & Report (counts)
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
        "trades": [],
        "counts": {"tp": 0, "sl": 0, "be": 0}
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
        stats["date"] = today
        stats["pnl"] = 0.0
        stats["trades"] = []
        stats["counts"] = {"tp": 0, "sl": 0, "be": 0}
        save_stats(stats)
        open(REPORT_SENT_FILE,"w").write("")

def try_send_daily_report(stats):
    now = datetime.now()
    if not (now.hour == DAILY_REPORT_HH and now.minute == DAILY_REPORT_MM):
        return
    if has_sent_today():
        return
    # only summary counts + pnl
    lines = [
        f"📊 สรุปผลรายวัน {stats['date']}",
        f"Σ PnL: {stats['pnl']:+.2f} USDT",
        f"TP: {stats['counts'].get('tp',0)} | SL: {stats['counts'].get('sl',0)} | BE: {stats['counts'].get('be',0)}",
    ]
    tg("\n".join(lines))
    mark_sent_today()
    log.info("📨 Daily report sent.")

# ============================================================
# Main Loop
# ============================================================
def main():
    ex = setup_exchange()
    log.info(f"✅ Started Binance Futures NW Bot ({TIMEFRAME}, MACD={MACD_TF}, NW Freeze={UPDATE_FRACTION})")

    stats = load_stats()

    position = None          # {"side","qty","entry","sl","be_active":False}
    sl_lock = False
    pending = None           # {"side","touch_price","lower","upper","mid","ts"}

    last_nw_update = 0
    upper = lower = mid = None

    while True:
        try:
            reset_report_if_new_day(stats)
            try_send_daily_report(stats)

            # ---------- TF หลัก ----------
            candles = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=600)
            closes = [c[4] for c in candles]
            last_close = closes[-1]

            # ---------- EMA Trend (option) ----------
            e_fast = ema(closes, EMA_FAST) if EMA_ENABLED else None
            e_slow = ema(closes, EMA_SLOW) if EMA_ENABLED else None
            if EMA_ENABLED and (e_fast is None or e_slow is None):
                time.sleep(LOOP_SEC); continue

            # ---------- NW Freeze ----------
            now_ts = time.time()
            if "m" in TIMEFRAME:
                tf_minutes = int(TIMEFRAME.replace("m",""))
            else:
                tf_minutes = int(TIMEFRAME.replace("h","")) * 60
            freeze_sec = tf_minutes * 60 * UPDATE_FRACTION

            if upper is None or now_ts - last_nw_update > freeze_sec:
                u,l,m = nwe_luxalgo_repaint(closes)
                if u is None:
                    log.info("[DEBUG] NW not ready"); time.sleep(LOOP_SEC); continue
                upper,lower,mid = u,l,m
                last_nw_update = now_ts
                log.info(f"[DEBUG] NW updated: U={upper:.2f}, L={lower:.2f}, M={mid:.2f}")
            else:
                log.info("[DEBUG] Using previous NW band (frozen)")

            # ---------- MACD TF ย่อย (ใช้แท่งปิด) ----------
            macd_side_ok = None
            macd_for_be = None
            if MACD_ENABLED:
                small = ex.fetch_ohlcv(SYMBOL, MACD_TF, limit=200)
                mcloses = [c[4] for c in small[:-1]]
                mac = macd(mcloses)
                if mac:
                    dp,dn,ep,en = mac
                    # macd_side_ok used for entry confirm when pending exists
                    if EMA_ENABLED:
                        trend = "BUY" if e_fast > e_slow else "SELL"
                    else:
                        # EMA disabled -> determine trend by price vs mid
                        trend = "BUY" if last_close > mid else "SELL"
                    if trend=="BUY":
                        macd_side_ok = macd_up(dp,dn,ep,en)
                    else:
                        macd_side_ok = macd_down(dp,dn,ep,en)

                # For BE via MACD we fetch MACD on the BE TF (may be same as MACD_TF)
            # ============================================================
            # Read actual position on exchange (sync)
            # ============================================================
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

                # SL Touch
                if side=="long" and last_price <= sl:
                    pnl = (last_price-entry)*position["qty"]
                    stats["pnl"] += pnl
                    reason = "BE" if position.get("be_active") else "SL"
                    if reason=="BE":
                        stats["counts"]["be"] = stats["counts"].get("be",0) + 1
                    else:
                        stats["counts"]["sl"] = stats["counts"].get("sl",0) + 1
                    stats["trades"].append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "side": "LONG",
                        "entry": entry,
                        "exit": last_price,
                        "pnl": pnl,
                        "reason": reason
                    })
                    ex.create_market_order(SYMBOL,"sell",position["qty"],params={"reduceOnly":True})
                    tg(f"🔴 LONG {reason} {entry:.2f}->{last_price:.2f} PnL={pnl:+.2f}")
                    position=None; sl_lock=True
                    save_stats(stats); time.sleep(LOOP_SEC); continue

                if side=="short" and last_price >= sl:
                    pnl = (entry-last_price)*position["qty"]
                    stats["pnl"] += pnl
                    reason = "BE" if position.get("be_active") else "SL"
                    if reason=="BE":
                        stats["counts"]["be"] = stats["counts"].get("be",0) + 1
                    else:
                        stats["counts"]["sl"] = stats["counts"].get("sl",0) + 1
                    stats["trades"].append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "side": "SHORT",
                        "entry": entry,
                        "exit": last_price,
                        "pnl": pnl,
                        "reason": reason
                    })
                    ex.create_market_order(SYMBOL,"buy",position["qty"],params={"reduceOnly":True})
                    tg(f"🔴 SHORT {reason} {entry:.2f}->{last_price:.2f} PnL={pnl:+.2f}")
                    position=None; sl_lock=True
                    save_stats(stats); time.sleep(LOOP_SEC); continue

                # TP using buffer (ก่อนถึง upper/lower)
                if side=="long" and last_price >= (upper - TP_BUFFER):
                    pnl = (last_price-entry)*position["qty"]
                    stats["pnl"] += pnl
                    stats["counts"]["tp"] = stats["counts"].get("tp",0) + 1
                    stats["trades"].append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "side": "LONG",
                        "entry": entry,
                        "exit": last_price,
                        "pnl": pnl,
                        "reason": "TP_upper_buffer"
                    })
                    ex.create_market_order(SYMBOL,"sell",position["qty"],params={"reduceOnly":True})
                    tg(f"✅ LONG TP (buffer) @ {last_price:.2f} PnL={pnl:+.2f}")
                    position=None
                    save_stats(stats); time.sleep(LOOP_SEC); continue

                if side=="short" and last_price <= (lower + TP_BUFFER):
                    pnl = (entry-last_price)*position["qty"]
                    stats["pnl"] += pnl
                    stats["counts"]["tp"] = stats["counts"].get("tp",0) + 1
                    stats["trades"].append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "side": "SHORT",
                        "entry": entry,
                        "exit": last_price,
                        "pnl": pnl,
                        "reason": "TP_lower_buffer"
                    })
                    ex.create_market_order(SYMBOL,"buy",position["qty"],params={"reduceOnly":True})
                    tg(f"✅ SHORT TP (buffer) @ {last_price:.2f} PnL={pnl:+.2f}")
                    position=None
                    save_stats(stats); time.sleep(LOOP_SEC); continue

                # ถ้า EMA เปลี่ยนทิศ -> ใช้ mid เป็น TP (เหมือนเดิม)
                trend_now = None
                if EMA_ENABLED:
                    trend_now = "BUY" if ema(closes,EMA_FAST) > ema(closes,EMA_SLOW) else "SELL"
                else:
                    trend_now = "BUY" if last_close > mid else "SELL"

                if side=="long" and trend_now=="SELL":
                    if last_price <= mid:
                        pnl = (last_price-entry)*position["qty"]
                        stats["pnl"] += pnl
                        stats["counts"]["tp"] = stats["counts"].get("tp",0) + 1
                        stats["trades"].append({
                            "time": datetime.now().strftime("%H:%M:%S"),
                            "side": "LONG",
                            "entry": entry,
                            "exit": last_price,
                            "pnl": pnl,
                            "reason": "TP_mid_trend_flip"
                        })
                        ex.create_market_order(SYMBOL,"sell",position["qty"],params={"reduceOnly":True})
                        tg(f"⚠ LONG EMA Flip TP Mid @ {last_price:.2f} PnL={pnl:+.2f}")
                        position=None
                        save_stats(stats); time.sleep(LOOP_SEC); continue

                if side=="short" and trend_now=="BUY":
                    if last_price >= mid:
                        pnl = (entry-last_price)*position["qty"]
                        stats["pnl"] += pnl
                        stats["counts"]["tp"] = stats["counts"].get("tp",0) + 1
                        stats["trades"].append({
                            "time": datetime.now().strftime("%H:%M:%S"),
                            "side": "SHORT",
                            "entry": entry,
                            "exit": last_price,
                            "pnl": pnl,
                            "reason": "TP_mid_trend_flip"
                        })
                        ex.create_market_order(SYMBOL,"buy",position["qty"],params={"reduceOnly":True})
                        tg(f"⚠ SHORT EMA Flip TP Mid @ {last_price:.2f} PnL={pnl:+.2f}")
                        position=None
                        save_stats(stats); time.sleep(LOOP_SEC); continue

                # Breakeven / Trailing
                if USE_BREAKEVEN and not sl_lock:
                    # Option A: mid-based (original)
                    if (not BREAKEVEN_USE_MACD):
                        if side=="long" and last_close > mid and position["sl"] < entry + BREAKEVEN_OFFSET:
                            position["sl"] = entry + BREAKEVEN_OFFSET
                            position["be_active"] = True
                            log.info(f"🔁 LONG Move SL to BE {position['sl']:.2f}")
                        if side=="short" and last_close < mid and position["sl"] > entry - BREAKEVEN_OFFSET:
                            position["sl"] = entry - BREAKEVEN_OFFSET
                            position["be_active"] = True
                            log.info(f"🔁 SHORT Move SL to BE {position['sl']:.2f}")
                    else:
                        # Option B: MACD-based BE experimental
                        try:
                            small_be = ex.fetch_ohlcv(SYMBOL, MACD_FOR_BREAKEVEN_TF, limit=200)
                            mcloses_be = [c[4] for c in small_be[:-1]]
                            mac_be = macd(mcloses_be)
                            if mac_be:
                                dp_b,dn_b,ep_b,en_b = mac_be
                                # For long: if macd_down on closed bar -> BE action
                                if side=="long" and macd_down(dp_b,dn_b,ep_b,en_b):
                                    # check unrealized PnL by comparing last_close vs entry
                                    if last_close > entry:
                                        # move SL to BE
                                        if position["sl"] < entry + BREAKEVEN_OFFSET:
                                            position["sl"] = entry + BREAKEVEN_OFFSET
                                            position["be_active"] = True
                                            log.info(f"🔁 LONG MACD-BE Move SL to BE {position['sl']:.2f}")
                                    else:
                                        # negative -> force close now
                                        pnl = (last_price-entry)*position["qty"]
                                        stats["pnl"] += pnl
                                        stats["counts"]["be"] = stats["counts"].get("be",0) + 1
                                        stats["trades"].append({
                                            "time": datetime.now().strftime("%H:%M:%S"),
                                            "side": "LONG",
                                            "entry": entry,
                                            "exit": last_price,
                                            "pnl": pnl,
                                            "reason": "BE_force_close"
                                        })
                                        ex.create_market_order(SYMBOL,"sell",position["qty"],params={"reduceOnly":True})
                                        tg(f"🔴 LONG BE force close @ {last_price:.2f} PnL={pnl:+.2f}")
                                        position=None; sl_lock=True
                                        save_stats(stats); time.sleep(LOOP_SEC); continue

                                if side=="short" and macd_up(dp_b,dn_b,ep_b,en_b):
                                    if last_close < entry:
                                        if position["sl"] > entry - BREAKEVEN_OFFSET:
                                            position["sl"] = entry - BREAKEVEN_OFFSET
                                            position["be_active"] = True
                                            log.info(f"🔁 SHORT MACD-BE Move SL to BE {position['sl']:.2f}")
                                    else:
                                        pnl = (entry-last_price)*position["qty"]
                                        stats["pnl"] += pnl
                                        stats["counts"]["be"] = stats["counts"].get("be",0) + 1
                                        stats["trades"].append({
                                            "time": datetime.now().strftime("%H:%M:%S"),
                                            "side": "SHORT",
                                            "entry": entry,
                                            "exit": last_price,
                                            "pnl": pnl,
                                            "reason": "BE_force_close"
                                        })
                                        ex.create_market_order(SYMBOL,"buy",position["qty"],params={"reduceOnly":True})
                                        tg(f"🔴 SHORT BE force close @ {last_price:.2f} PnL={pnl:+.2f}")
                                        position=None; sl_lock=True
                                        save_stats(stats); time.sleep(LOOP_SEC); continue
                        except Exception as e:
                            log.warning(f"MACD-BE check warn: {e}")

                save_stats(stats)
                time.sleep(LOOP_SEC)
                continue

            # ======================================================
            # NO POSITION
            # ======================================================
            if sl_lock:
                # ปลดล็อกเมื่อราคาข้าม mid (ทั้งขา BUY/SELL ตามที่ขอ)
                if (last_close > mid) or (last_close < mid):
                    # but we only release when price crosses mid in any direction (user wanted wait until mid cross)
                    sl_lock = False
                    log.info("🔓 SL Lock released")
                time.sleep(LOOP_SEC)
                continue

            # 1) ถ้ามี pending จาก NW touch เก่า → รอ MACD ตัด
            if MACD_ENABLED and pending is not None:
                last_price = ex.fetch_ticker(SYMBOL)["last"]
                side = pending["side"]
                p_lower = pending["lower"]
                p_upper = pending["upper"]
                p_mid   = pending["mid"]

                if macd_side_ok:
                    if side=="long":
                        # ตอน MACD ตัด ราคา ต้องอยู่ระหว่าง lower..mid และ ห้าม > mid
                        if last_price < p_lower or last_price > p_mid:
                            log.info("❌ MACD up but price out of [lower,mid] → cancel pending")
                            pending=None
                        else:
                            qty = order_size(ex, last_price)
                            ex.create_market_order(SYMBOL,"buy",qty)
                            position={"side":"long","qty":qty,"entry":last_price,"sl":last_price-SL_DISTANCE,"be_active":False}
                            log.info(f"🚀 OPEN LONG (pending MACD confirm) @ {last_price:.2f}")
                            pending=None
                    else:  # short
                        if last_price > p_upper or last_price < p_mid:
                            log.info("❌ MACD down but price out of [mid,upper] → cancel pending")
                            pending=None
                        else:
                            qty = order_size(ex, last_price)
                            ex.create_market_order(SYMBOL,"sell",qty)
                            position={"side":"short","qty":qty,"entry":last_price,"sl":last_price+SL_DISTANCE,"be_active":False}
                            log.info(f"🚀 OPEN SHORT (pending MACD confirm) @ {last_price:.2f}")
                            pending=None

                save_stats(stats)
                time.sleep(LOOP_SEC)
                continue

            # 2) ไม่มี pending → ตรวจ NW touch ใหม่
            if EMA_ENABLED:
                trend = "BUY" if e_fast > e_slow else "SELL"
            else:
                trend = "BUY" if last_close > mid else "SELL"

            # NW touch LONG
            if trend=="BUY" and last_close <= lower:
                last_price = ex.fetch_ticker(SYMBOL)["last"]
                if MACD_ENABLED:
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
                    position={"side":"long","qty":qty,"entry":last_price,"sl":last_price-SL_DISTANCE,"be_active":False}
                    log.info(f"🚀 LONG ENTRY (no MACD) @ {last_price:.2f}")
                save_stats(stats)
                time.sleep(LOOP_SEC)
                continue

            # NW touch SHORT
            if trend=="SELL" and last_close >= upper:
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
                    position={"side":"short","qty":qty,"entry":last_price,"sl":last_price+SL_DISTANCE,"be_active":False}
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
