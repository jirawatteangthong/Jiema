# ================== imports ==================
import ccxt, time, requests, logging, json, os, sys, math
from datetime import datetime

# ================== CONFIG (ปรับได้) ==================
API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING')   # คีย์ Binance
SECRET  = os.getenv('BINANCE_SECRET',    'YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING') # ซีเคร็ต Binance

SYMBOL            = 'BTC/USDT:USDT'   # สัญลักษณ์ Futures
TIMEFRAME_H1      = '1h'              # TF ใหญ่
TIMEFRAME_M5      = '5m'              # TF ยืนยันเข้า
LEVERAGE          = 35               # เลเวอเรจ
TARGET_POSITION_SIZE_FACTOR = 0.85   # ใช้กี่ % ของเงินว่าง (0.8 = 80%) 
# ---- EMA/MACD Parameters ----
EMA_FAST_H1       = 10                # EMA10 บน H1  (#ปรับได้)
EMA_SLOW_H1       = 50                # EMA50 บน H1  (#ปรับได้)
WAIT_H1_CLOSE     = False             # False=ไม่ต้องรอปิดแท่ง ใช้สัญญาณทันที (มี latch); True=รอแท่งปิด  (#ปรับได้)

EMA200_M5         = 200               # EMA200 บน M5 (#ปรับได้)
MACD_FAST         = 12                # MACD fast (M5) (#ปรับได้)
MACD_SLOW         = 26                # MACD slow (M5) (#ปรับได้)
MACD_SIGNAL       = 9                 # MACD signal (M5) (#ปรับได้)

# ---- SL เริ่มต้นจาก Swing M5 ----
SWING_LOOKBACK_M5 = 50                # ใช้ 50 แท่ง M5 หา swing ล่าสุด (#ปรับได้)
SL_EXTRA_POINTS   = 200.0             # ระยะเผื่อจาก swing (BUY: -200 / SELL: +200) (#ปรับได้)
MAX_INITIAL_SL_POINTS = 1234          # SL เริ่มต้นห่างสุดได้ไม่เกิน 1234 pts

# ---- Trailing SL Steps ----
STEP1_TRIGGER     = 450.0             # เมื่อกำไรถึง +450 จุด (#ปรับได้)
STEP1_SL_OFFSET   = -200.0            # เลื่อน SL มา Entry-200 (BUY) / Entry+200 (SELL) (#ปรับได้)

STEP2_TRIGGER     = 700.0             # เมื่อกำไรถึง +700 จุด (#ปรับได้)
STEP2_SL_OFFSET   = +555.0            # เลื่อน SL มา Entry+500 (BUY) / Entry-500 (SELL) (#ปรับได้) -> แจ้ง TP

STEP3_TRIGGER     = 950.0             # เมื่อกำไรถึง +950 จุด (#ปรับได้)
STEP3_SL_OFFSET   = +830.0            # เลื่อน SL มา Entry+850 (BUY) / Entry-850 (SELL) (#ปรับได้) -> แจ้ง TP

MANUAL_CLOSE_ALERT_TRIGGER = 1300.0   # กำไร > 1300 pts ให้แจ้งปิด (ไม่ปิดอัตโนมัติ) (#ปรับได้)

# ---- สัญญาณ H1 ใหม่ระหว่างถือโพซิชัน ----
NEW_SIGNAL_ACTION       = 'tighten_sl' # 'tighten_sl' = ตั้ง SL ใกล้ราคา / 'close_now' = ปิดทันที (#ปรับได้)
NEW_SIGNAL_SL_OFFSET    = 100.0        # ตั้ง SL ห่างจากราคาปัจจุบัน 100 pts (#ปรับได้)

# ---- Loop/Timing ----
FAST_LOOP_SECONDS       = 3            # รอบเช็คเร็ว (#ปรับได้)
H1_CHECK_INTERVAL_SEC   = 60           # เช็ค H1 ทุก วินาที (#ปรับได้)

# ---- Telegram ----
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# ================== logging ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log', encoding='utf-8'), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ================== GLOBAL STATE ==================
exchange = None
market_info = None

# --- H1 intrabar signal latch (กันสวิงกลางแท่ง) ---
h1_latched_dir = None        # 'long' / 'short' ที่ล็อกไว้ทั้งแท่ง
h1_latch_bar_ts = None       # timestamp ของแท่ง H1 ปัจจุบันที่ล็อก

# --- H1 baseline (ตั้งตอนเริ่ม/ปิดโพซิชัน แล้วรอ cross ใหม่) ---
h1_baseline_dir = None       # 'long' / 'short' ณ ตอนตั้ง baseline
h1_baseline_bar_ts = None    # ts ของแท่ง H1 ตอนตั้ง baseline

# สถานะโพซิชันที่เราถือ
position = None  # dict: {'side','entry','contracts','sl','step','opened_at'}

# สถานะสัญญาณ/แผนเข้าออเดอร์
entry_plan = {
    'h1_dir': None,            # 'long'/'short' จาก EMA10/50 H1 (หลัง cross จาก baseline แล้ว)
    'h1_bar_ts': None,         # ts ของแท่ง H1 ที่ใช้สัญญาณล่าสุด
    'stage': 'idle',           # 'idle' -> 'armed' -> 'wait_macd_cross'
    'm5_last_bar_ts': None,    # กันซ้ำต่อแท่ง M5
    'm5_touch_ts': None,       # เวลาแท่ง M5 ที่แตะ/เลย EMA200
    'macd_initial': None       # 'buy-<' (เริ่ม DIF<DEA) / 'sell->' (เริ่ม DIF>DEA)
}

last_h1_check  = 0.0
last_manual_tp_alert_ts = 0.0

# ================== Telegram ==================
def send_telegram(msg: str):
    if (not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith('YOUR_') or
        not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID.startswith('YOUR_')):
        logger.warning("⚠ TELEGRAM creds not set; skip send.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        params = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
        requests.get(url, params=params, timeout=10).raise_for_status()
    except Exception as e:
        logger.error(f"Telegram error: {e}")

def fmt_usd(x): 
    try: return f"{float(x):,.2f}"
    except: return str(x)

# ================== Exchange ==================
def setup_exchange():
    global exchange, market_info
    if not API_KEY or not SECRET or 'YOUR_' in API_KEY or 'YOUR_' in SECRET:
        send_telegram("⛔ Critical: API key/secret not set.")
        sys.exit(1)
    exchange = ccxt.binance({
        'apiKey': API_KEY,
        'secret': SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future', 'marginMode': 'cross'},
        'timeout': 60000
    })
    exchange.load_markets()
    market_info = exchange.market(SYMBOL)
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
    except Exception as e:
        logger.error(f"set_leverage failed: {e}")
        send_telegram(f"⛔ set_leverage failed: {e}")

def decimal_price(v: float) -> float:
    if not market_info: return round(v, 2)
    return float(exchange.price_to_precision(SYMBOL, v))

# ================== Indicators ==================
def ema_series(values, period):
    if len(values) < period: return None
    k = 2/(period+1)
    e = values[0]
    out = [e]
    for v in values[1:]:
        e = v*k + e*(1-k)
        out.append(e)
    return out

def last_ema(values, period):
    es = ema_series(values, period)
    return es[-1] if es else None

def macd_from_closes(closes):
    if len(closes) < MACD_SLOW + MACD_SIGNAL + 2: return None
    ef = ema_series(closes, MACD_FAST)
    es = ema_series(closes, MACD_SLOW)
    if not ef or not es: return None
    start = len(ef) - len(es)
    dif = [ef[start+i] - es[i] for i in range(len(es))]
    dea = ema_series(dif, MACD_SIGNAL)
    if not dea or len(dea) < 2 or len(dif) < 2: return None
    return dif[-2], dif[-1], dea[-2], dea[-1]  # prev, now

def macd_cross_up(dif_prev, dif_now, dea_prev, dea_now):
    return (dif_prev <= dea_prev) and (dif_now > dea_now)

def macd_cross_down(dif_prev, dif_now, dea_prev, dea_now):
    return (dif_prev >= dea_prev) and (dif_now < dea_now)

def find_recent_swing_low_high_m5(ohlcv_m5, lookback=SWING_LOOKBACK_M5, k=2):
    """หา swing ล่าสุดในหน้าต่าง lookback (pivot ง่าย ๆ)"""
    if len(ohlcv_m5) < lookback + 2*k + 1:
        look = ohlcv_m5[:]
    else:
        look = ohlcv_m5[-lookback:]
    highs = [c[2] for c in look]
    lows  = [c[3] for c in look]

    swing_low = None
    swing_high = None
    for i in range(k, len(look)-k):
        if all(lows[i]  <= lows[i-j]  for j in range(1,k+1)) and all(lows[i]  <= lows[i+j] for j in range(1,k+1)):
            swing_low = look[i][3]
        if all(highs[i] >= highs[i-j] for j in range(1,k+1)) and all(highs[i] >= highs[i+j] for j in range(1,k+1)):
            swing_high = look[i][2]
    if swing_low  is None: swing_low  = min(lows)
    if swing_high is None: swing_high = max(highs)
    return swing_low, swing_high

# ================== Orders / SL ==================
def cancel_all_open_orders(max_retry=3):
    for _ in range(max_retry):
        try:
            orders = exchange.fetch_open_orders(SYMBOL)
            if not orders: return
            for o in orders:
                try:
                    exchange.cancel_order(o['id'], SYMBOL)
                    time.sleep(0.05)
                except Exception as e:
                    logger.warning(f"cancel warn: {e}")
        except Exception as e:
            logger.error(f"cancel_all_open_orders error: {e}")
            time.sleep(0.2)

def set_sl_close_position(side: str, stop_price: float):
    """ตั้ง SL แบบผูกโพซิชัน (closePosition=True) เพื่อล้างเองเมื่อโพซิชันหมด"""
    try:
        sp = decimal_price(stop_price)
        params = {'stopPrice': sp, 'reduceOnly': True, 'closePosition': True}
        order_side = 'sell' if side == 'long' else 'buy'
        exchange.create_order(SYMBOL, 'STOP_MARKET', order_side, None, None, params)
        send_telegram(
            "✅ ตั้ง SL สำเร็จ!\n"
            f"📊 Direction: <b>{side.upper()}</b>\n"
            f"🛡 SL: <code>{fmt_usd(sp)}</code>"
        )
        return True
    except Exception as e:
        logger.error(f"set_sl_close_position error: {e}")
        send_telegram(f"❌ SL Error: {e}")
        return False

def open_market(side: str, price_now: float):
    """เปิดออเดอร์ และตั้ง SL เริ่มจาก swing ตามกฎใหม่"""
    global position
    # คำนวณขนาดคร่าว ๆ (notional 80% ของเงินว่าง * leverage)
    try:
        bal = exchange.fetch_balance()
        free_usdt = bal.get('USDT', {}).get('free', 0.0) or 0.0
    except Exception:
        free_usdt = 0.0
    notional = max(0.0, float(free_usdt)) * TARGET_POSITION_SIZE_FACTOR * LEVERAGE
    if notional <= 0:
        send_telegram("⛔ ไม่พอ margin เปิดออเดอร์")
        return False
    amount = float(exchange.amount_to_precision(SYMBOL, notional / price_now))
    side_ccxt = 'buy' if side == 'long' else 'sell'
    try:
        exchange.create_market_order(SYMBOL, side_ccxt, amount)
        # ดึง entry จริง
        time.sleep(1)
        pos = fetch_position()
        if not pos or pos.get('side') != side:
            send_telegram("⛔ ยืนยันโพซิชันไม่สำเร็จ")
            return False
        position = {
            'side': side,
            'entry': float(pos['entry']),
            'contracts': float(pos['contracts']),
            'sl': None,
            'step': 0,
            'opened_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        send_telegram(
            "✅ เปิดโพซิชัน <b>{}</b>\n"
            "📦 Size: <code>{:.6f}</code>\n"
            "🎯 Entry: <code>{}</code>".format(side.upper(), position['contracts'], fmt_usd(position['entry']))
        )
        # ตั้ง SL เริ่มต้นจาก swing M5
        ohlcv_m5 = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME_M5, limit=max(SWING_LOOKBACK_M5, 60))
        swing_low, swing_high = find_recent_swing_low_high_m5(ohlcv_m5)

        # SL ดิบจาก swing ± extra
        raw_sl = (swing_low - SL_EXTRA_POINTS) if side == 'long' else (swing_high + SL_EXTRA_POINTS)

        # บังคับเพดาน SL เริ่มต้นไม่ให้ห่างจาก entry เกิน MAX_INITIAL_SL_POINTS
        if side == 'long':
            # ห้ามต่ำกว่า (entry - MAX_INITIAL_SL_POINTS)
            sl0 = max(raw_sl, position['entry'] - MAX_INITIAL_SL_POINTS)
        else:
            # ห้ามสูงกว่า (entry + MAX_INITIAL_SL_POINTS)
            sl0 = min(raw_sl, position['entry'] + MAX_INITIAL_SL_POINTS)
           
        if set_sl_close_position(side, sl0):
            position['sl'] = float(sl0)
        return True
    except Exception as e:
        logger.error(f"open_market error: {e}")
        send_telegram(f"❌ Open order error: {e}")
        return False

def tighten_sl_for_new_signal(side: str, price_now: float):
    """เมื่อมีสัญญาณ H1 ใหม่ระหว่างถือโพซิชัน"""
    if NEW_SIGNAL_ACTION == 'close_now':
        try:
            close_side = 'sell' if side == 'long' else 'buy'
            exchange.create_market_order(SYMBOL, close_side, position['contracts'])
            send_telegram("⛑️ ตรวจพบสัญญาณใหม่ → <b>ปิดโพซิชันทันที</b>")
            return True
        except Exception as e:
            logger.error(f"close_now error: {e}")
            send_telegram(f"🦠 close_now error: {e}")
            return False
    else:
        new_sl = (price_now - NEW_SIGNAL_SL_OFFSET) if side=='long' else (price_now + NEW_SIGNAL_SL_OFFSET)
        ok = set_sl_close_position(side, new_sl)
        if ok:
            send_telegram("⛑️ ตรวจพบสัญญาณใหม่ → <b>บังคับ SL ใกล้ราคา</b> เพื่อปิดโพซิชันโดยเร็ว")
        return ok

# ================== Fetch Helpers ==================
def fetch_position():
    """อ่านโพซิชันปัจจุบันแบบย่อ"""
    try:
        ps = exchange.fetch_positions([SYMBOL])
        for p in ps:
            if p.get('symbol') == SYMBOL and float(p.get('contracts', 0) or 0) != 0:
                return {'side': p.get('side'),
                        'contracts': abs(float(p.get('contracts', 0))),
                        'entry': float(p.get('entryPrice', 0) or 0)}
        return None
    except Exception as e:
        logger.error(f"fetch_position error: {e}")
        return None

# ================== H1 Signal Helpers ==================
def get_h1_dir_intrabar() -> tuple[str|None, int|None]:
    """
    อ่านทิศ H1 ปัจจุบัน (เพื่อใช้ตั้ง baseline หรือเช็ก cross อย่างรวดเร็ว)
    - WAIT_H1_CLOSE=True  → ใช้แท่งปิดล่าสุด (o[-2])
    - WAIT_H1_CLOSE=False → ใช้แท่งปัจจุบัน (o[-1])
    """
    limit = max(EMA_SLOW_H1 + 5, 60)
    o = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME_H1, limit=limit)
    if not o or len(o) < 3: return None, None
    if WAIT_H1_CLOSE:
        closes = [c[4] for c in o[:-1]]; ts = o[-2][0]
    else:
        closes = [c[4] for c in o];      ts = o[-1][0]
    ema_fast = last_ema(closes, EMA_FAST_H1)
    ema_slow = last_ema(closes, EMA_SLOW_H1)
    if ema_fast is None or ema_slow is None: return None, ts
    if ema_fast > ema_slow:  return 'long', ts
    if ema_fast < ema_slow:  return 'short', ts
    return None, ts

def get_h1_signal_latched() -> tuple[str|None, int|None]:
    """
    คืนค่า: ('long'/'short'/None, bar_ts)
    - WAIT_H1_CLOSE=True: ใช้แท่งปิดล่าสุด (นิ่งตาม close)
    - WAIT_H1_CLOSE=False: ใช้แท่งปัจจุบัน แต่ "ล็อกสัญญาณแรกของแท่ง" (กันสวิง intrabar)
    """
    global h1_latched_dir, h1_latch_bar_ts
    limit = max(EMA_SLOW_H1 + 5, 60)
    o = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME_H1, limit=limit)
    if not o or len(o) < 3: return None, None

    if WAIT_H1_CLOSE:
        closes = [c[4] for c in o[:-1]]; ts = o[-2][0]
        ema_fast = last_ema(closes, EMA_FAST_H1); ema_slow = last_ema(closes, EMA_SLOW_H1)
        if ema_fast is None or ema_slow is None: return None, ts
        if ema_fast > ema_slow:  return 'long', ts
        if ema_fast < ema_slow:  return 'short', ts
        return None, ts
    else:
        closes = [c[4] for c in o]    # รวมแท่งปัจจุบัน
        ts_current = o[-1][0]
        # ถ้าเปลี่ยนแท่ง → ล้าง latch
        if h1_latch_bar_ts != ts_current:
            h1_latched_dir = None
            h1_latch_bar_ts = ts_current
        # ถ้ามี latch แล้ว → ส่งทิศเดิมทั้งแท่ง
        if h1_latched_dir is not None:
            return h1_latched_dir, ts_current
        # ยังไม่มี latch → คำนวณทิศ แล้วล็อกถ้ามี
        ema_fast = last_ema(closes, EMA_FAST_H1); ema_slow = last_ema(closes, EMA_SLOW_H1)
        if ema_fast is None or ema_slow is None: return None, ts_current
        if ema_fast > ema_slow:
            h1_latched_dir = 'long';  return 'long', ts_current
        elif ema_fast < ema_slow:
            h1_latched_dir = 'short'; return 'short', ts_current
        else:
            return None, ts_current

def reset_h1_baseline(announce=True):
    """ตั้ง baseline = EMA10/50 H1 ปัจจุบัน แล้ว reset entry_plan/latch"""
    global h1_baseline_dir, h1_baseline_bar_ts, entry_plan, h1_latched_dir, h1_latch_bar_ts
    d, ts = get_h1_dir_intrabar()
    h1_baseline_dir = d
    h1_baseline_bar_ts = ts
    # reset latch + แผนเข้า
    h1_latched_dir = None
    h1_latch_bar_ts = None
    entry_plan = {'h1_dir': None, 'h1_bar_ts': None, 'stage':'idle',
                  'm5_last_bar_ts': None, 'm5_touch_ts': None, 'macd_initial': None}
    if announce:
        send_telegram(f"🧰 ตั้งค่า baseline EMA H1: <b>{(d or 'NONE').upper()}</b>\n🕒 รอ cross ใหม่จาก baseline ก่อนเริ่มหาเงื่อนไข M5/MACD")

# ================== Core Logic ==================
def check_m5_env():
    """คืนข้อมูล M5 ล่าสุดที่ต้องใช้สำหรับ logic เข้า (ใช้แท่งปิดล่าสุด)"""
    limit = max(EMA200_M5 + 10, 240)
    o = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME_M5, limit=limit)
    if not o or len(o) < EMA200_M5 + 5: return None
    ts = o[-2][0]               # ใช้แท่งปิดล่าสุด
    closes = [c[4] for c in o[:-1]]
    highs  = [c[2] for c in o[:-1]]
    lows   = [c[3] for c in o[:-1]]
    close_now = closes[-1]
    ema200 = last_ema(closes, EMA200_M5)
    macd = macd_from_closes(closes)
    return {'ts': ts, 'close': close_now, 'high': highs[-1], 'low': lows[-1], 'ema200': ema200, 'macd': macd}

def handle_entry_logic(price_now: float):
    """สถานะไม่มีโพซิชัน → ใช้ baseline + latch + M5+MACD ตามที่กำหนด"""
    global entry_plan, last_h1_check, h1_baseline_dir

    # ถ้ายังไม่มี baseline (เริ่มบอท/เพิ่งปิดโพซิชัน) → ตั้งก่อน
    if h1_baseline_dir is None:
        reset_h1_baseline(announce=True)
        return

    # 1) อ่าน H1 ปัจจุบัน (latch) แล้วเช็กว่า "cross จาก baseline" หรือยัง
    tnow = time.time()
    if tnow - last_h1_check >= H1_CHECK_INTERVAL_SEC or (entry_plan['h1_dir'] is None):
        cur_dir, h1_ts = get_h1_signal_latched()
        last_h1_check = tnow
        if cur_dir and (h1_baseline_dir is not None) and (cur_dir != h1_baseline_dir):
            # Cross ใหม่จาก baseline → อนุญาตตั้งแผน
            entry_plan = {
                'h1_dir': cur_dir, 'h1_bar_ts': h1_ts,
                'stage': 'armed', 'm5_last_bar_ts': None,
                'm5_touch_ts': None, 'macd_initial': None
            }
            send_telegram(f"🧭 H1 CROSS จาก baseline → <b>{cur_dir.upper()}</b>\nรอ M5 แตะ EMA200 + MACD")
        else:
            # ยังไม่ cross → รอต่อ
            return

    if entry_plan['stage'] == 'idle' or entry_plan['h1_dir'] is None:
        return

    # 2) อ่าน M5 สภาวะล่าสุด (แท่งปิด)
    env = check_m5_env()
    if not env or env['ema200'] is None or env['macd'] is None:
        return
    m5_ts   = env['ts']
    close   = env['close']
    high    = env['high']
    low     = env['low']
    ema200  = env['ema200']
    dif_p, dif_n, dea_p, dea_n = env['macd']

    # กันซ้ำแท่งเดิม
    if entry_plan['m5_last_bar_ts'] == m5_ts:
        return
    entry_plan['m5_last_bar_ts'] = m5_ts

    want = entry_plan['h1_dir']

    # 2A) แตะ/เลย EMA200 + MACD initial direction
    if entry_plan['stage'] == 'armed':
        if want == 'long':
            touched = (low <= ema200)             # แตะ/เลยลง
            macd_initial_ok = (dif_n < dea_n)     # เริ่มจาก DIF < DEA
            if touched and macd_initial_ok:
                entry_plan['stage'] = 'wait_macd_cross'
                entry_plan['m5_touch_ts'] = m5_ts
                entry_plan['macd_initial'] = 'buy-<'
                send_telegram("⏳ M5 แตะ/เลย EMA200 ลง → รอ DIF ตัดขึ้นเพื่อเข้า <b>LONG</b>")
        else:
            touched = (high >= ema200)            # แตะ/เลยขึ้น
            macd_initial_ok = (dif_n > dea_n)     # เริ่มจาก DIF > DEA
            if touched and macd_initial_ok:
                entry_plan['stage'] = 'wait_macd_cross'
                entry_plan['m5_touch_ts'] = m5_ts
                entry_plan['macd_initial'] = 'sell->'
                send_telegram("⏳ M5 แตะ/เลย EMA200 ขึ้น → รอ DIF ตัดลงเพื่อเข้า <b>SHORT</b>")

    # 2B) รอ MACD cross + ย้ำ H1 (ใช้ latch) ก่อนเข้า
    elif entry_plan['stage'] == 'wait_macd_cross':
        h1_dir_now, h1_ts_now = get_h1_signal_latched()
        if (h1_dir_now is None) or (h1_dir_now != want):
            send_telegram("🚧 EMA H1 เปลี่ยนสัญญาณ → ยกเลิกแผนเดิมและเริ่มใช้สัญญาณใหม่")
            entry_plan = {'h1_dir': h1_dir_now, 'h1_bar_ts': h1_ts_now,
                          'stage':'armed' if h1_dir_now else 'idle',
                          'm5_last_bar_ts': None, 'm5_touch_ts': None, 'macd_initial': None}
            return

        crossed = macd_cross_up(dif_p, dif_n, dea_p, dea_n) if want=='long' else macd_cross_down(dif_p, dif_n, dea_p, dea_n)
        if crossed:
            ok = open_market(want, price_now)
            # รีเซ็ตแผนไม่ให้ยิงซ้ำ
            entry_plan['stage'] = 'idle'
            entry_plan['m5_touch_ts'] = None
            entry_plan['macd_initial'] = None
            if not ok:
                send_telegram("⛔ เปิดออเดอร์ไม่สำเร็จ")

def monitor_position_and_trailing(price_now: float):
    """ดูแล SL step + ตรวจ H1 ใหม่ระหว่างถือ + รีเซ็ต baseline เมื่อปิดโพซิชัน"""
    global position, last_manual_tp_alert_ts, entry_plan

    # อ่านโพซิชันจริง
    pos_real = fetch_position()
    if not pos_real:
        # โพซิชันปิดแล้ว → เคลียร์คำสั่งค้าง + รีเซ็ต baseline ใหม่
        cancel_all_open_orders(max_retry=3)
        if position:
            pnl_pts = (price_now - position['entry']) if position['side']=='long' else (position['entry'] - price_now)
            send_telegram(
                "📊 ปิดโพซิชัน <b>{}</b>\n"
                "Entry: <code>{}</code> → Last: <code>{}</code>\n"
                "PnL: <b>{:+.2f} pts</b>\n"
                "🧹 เคลียร์คำสั่งเก่าแล้ว\n"
                "🔎 เริ่มจาก baseline ใหม่"
                .format(position['side'].upper(), fmt_usd(position['entry']), fmt_usd(price_now), pnl_pts)
            )
        position = None
        # ตั้ง baseline ใหม่ ณ ตอนนี้ และรอ cross ครั้งถัดไป
        reset_h1_baseline(announce=True)
        return

    # อัปเดต entry/size
    if position:
        position['contracts'] = float(pos_real['contracts'])
        position['entry']     = float(pos_real['entry'])

    # 1) ถ้ามีสัญญาณ H1 ใหม่ "สวนฝั่ง" ระหว่างถือ
    h1_dir_now, _ = get_h1_dir_intrabar()  # ใช้ intrabar ตรวจทิศ ณ ตอนนี้
    if position and h1_dir_now and ((h1_dir_now == 'long' and position['side']=='short') or
                                    (h1_dir_now == 'short' and position['side']=='long')):
        ok = tighten_sl_for_new_signal(position['side'], price_now)
        if ok:
            send_telegram("⚠️ ตรวจพบสัญญาณ H1 ใหม่สวนทาง → บังคับปิดโพชิชัน")

    # 2) Trailing 3 ขั้น
    if not position: return
    side  = position['side']
    entry = position['entry']
    pnl_pts = (price_now - entry) if side=='long' else (entry - price_now)

    # Step 1
    if position['step'] < 1 and pnl_pts >= STEP1_TRIGGER:
        new_sl = (entry + STEP1_SL_OFFSET) if side=='long' else (entry - STEP1_SL_OFFSET)
        if set_sl_close_position(side, new_sl):
            position['sl'] = new_sl
            position['step'] = 1
            send_telegram("🚦 Step1 → เลื่อน SL มา <code>{}</code>".format(fmt_usd(new_sl)))

    # Step 2
    elif position['step'] < 2 and pnl_pts >= STEP2_TRIGGER:
        new_sl = (entry + STEP2_SL_OFFSET) if side=='long' else (entry - STEP2_SL_OFFSET)
        if set_sl_close_position(side, new_sl):
            position['sl'] = new_sl
            position['step'] = 2
            send_telegram("🚦 Step2 → SL = <code>{}</code>  🤑<b>TP</b>".format(fmt_usd(new_sl)))

    # Step 3
    elif position['step'] < 3 and pnl_pts >= STEP3_TRIGGER:
        new_sl = (entry + STEP3_SL_OFFSET) if side=='long' else (entry - STEP3_SL_OFFSET)
        if set_sl_close_position(side, new_sl):
            position['sl'] = new_sl
            position['step'] = 3
            send_telegram("💶 Step3 → SL = <code>{}</code>  💵<b>TP</b>".format(fmt_usd(new_sl)))

    # 3) Manual close alert > 1300 pts
    if pnl_pts >= MANUAL_CLOSE_ALERT_TRIGGER:
        now = time.time()
        if now - last_manual_tp_alert_ts >= 30:  # กันสแปม
            last_manual_tp_alert_ts = now
            send_telegram("🚨 กำไรเกินเป้าแล้ว <b>{:.0f} pts</b>\n พิจารณา <b>ปิดโพซิชัน</b> ".format(MANUAL_CLOSE_ALERT_TRIGGER))

# ================== Startup Banner ==================
def get_free_usdt() -> float | None:
    """
    พยายามดึง free USDT จาก Futures หลายทาง:
    1) bal['USDT']['free'] (ccxt unified)
    2) bal['info']['assets'][i]['availableBalance'] (Binance futures)
    3) สำรอง: bal['free']['USDT'] หรือ bal['total']['USDT']
    """
    try:
        # ระบุว่าเป็น futures ชัด ๆ
        bal = exchange.fetch_balance({'type': 'future'})
    except Exception:
        try:
            bal = exchange.fetch_balance()  # สำรอง
        except Exception:
            return None

    # 1) unified
    v = (bal.get('USDT', {}) or {}).get('free', None)
    if v is not None:
        try: return float(v)
        except: pass

    # 2) binance futures raw
    try:
        for a in (bal.get('info', {}) or {}).get('assets', []):
            if a.get('asset') == 'USDT':
                v = a.get('availableBalance', None)
                if v is not None:
                    return float(v)
    except Exception:
        pass

    # 3) อื่น ๆ
    v = (bal.get('free', {}) or {}).get('USDT', None)
    if v is not None:
        try: return float(v)
        except: pass

    v = (bal.get('total', {}) or {}).get('USDT', None)
    if v is not None:
        try: return float(v)
        except: pass

    return None


def send_startup_banner():
    try:
        bal = get_free_usdt()
        bal_txt = fmt_usd(bal) if (bal is not None) else "—"

        send_telegram(
            "🤖 บอทเริ่มทำงาน 💰\n"
            f"💵 ยอดเริ่มต้น: {bal_txt} USDT\n"
            f"📊 H1 EMA: {EMA_FAST_H1}/{EMA_SLOW_H1}\n"
            f"🧠 M5 : {EMA200_M5} | MACD: {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}\n"
            f"🛡 SL เริ่มต้นจาก Swing{SWING_LOOKBACK_M5} แท่ง ±{int(SL_EXTRA_POINTS)} pts\n"
            f"🚦 Step1: +{int(STEP1_TRIGGER)} → SL {int(STEP1_SL_OFFSET)} pts\n"
            f"🚦 Step2: +{int(STEP2_TRIGGER)} → SL +{int(STEP2_SL_OFFSET)} pts (TP)\n"
            f"🎯 Step3: +{int(STEP3_TRIGGER)} → SL +{int(STEP3_SL_OFFSET)} pts (TP)\n"
            f"🌈 Manual alert > +{int(MANUAL_CLOSE_ALERT_TRIGGER)} pts"
        )
    except Exception as e:
        logger.error(f"banner error: {e}")
# ================== main ==================
def main():
    setup_exchange()
    send_startup_banner()
    # ตั้ง baseline ตั้งแต่เริ่ม
    reset_h1_baseline(announce=True)

    while True:
        try:
            price_now = exchange.fetch_ticker(SYMBOL)['last']
            if position:
                monitor_position_and_trailing(price_now)
            else:
                handle_entry_logic(price_now)
            time.sleep(FAST_LOOP_SECONDS)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"main loop error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
