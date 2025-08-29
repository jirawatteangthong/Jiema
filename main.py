# ================== imports ==================
import ccxt, time, requests, logging, threading, json, os, sys, math
from datetime import datetime, timedelta

# ================== CONFIG ==================
API_KEY = os.getenv('BINANCE_API_KEY','YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET  = os.getenv('BINANCE_SECRET','YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING')

SYMBOL      = 'BTC/USDT:USDT'
TIMEFRAME   = '1h'
LEVERAGE    = 32

TP_DISTANCE_POINTS = 1111  # ยังอยู่แต่ไม่ได้ใช้
SL_DISTANCE_POINTS = 1111  # SL เริ่มต้นจากราคาเข้า

# --- Trailing SL Steps (ตามไฟล์เดิม) ---
TRAIL_SL_STEP1_TRIGGER_LONG_POINTS  = 300
TRAIL_SL_STEP1_NEW_SL_POINTS_LONG   = -700
TRAIL_SL_STEP2_TRIGGER_LONG_POINTS  = 500
TRAIL_SL_STEP2_NEW_SL_POINTS_LONG   = 460
TRAIL_SL_STEP3_TRIGGER_LONG_POINTS  = 700
TRAIL_SL_STEP3_NEW_SL_POINTS_LONG   = 650

TRAIL_SL_STEP1_TRIGGER_SHORT_POINTS = 300
TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT  = 700
TRAIL_SL_STEP2_TRIGGER_SHORT_POINTS = 500
TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT  = -460
TRAIL_SL_STEP3_TRIGGER_SHORT_POINTS = 700
TRAIL_SL_STEP3_NEW_SL_POINTS_SHORT  = -650

MANUAL_TP_ALERT_THRESHOLD = 1000
MANUAL_TP_ALERT_INTERVAL  = 600

CROSS_THRESHOLD_POINTS = 1
EMA_FAST_PERIOD = 10
EMA_SLOW_PERIOD = 50

MARGIN_BUFFER_USDT = 5
TARGET_POSITION_SIZE_FACTOR = 0.8

CONFIRMATION_RETRIES = 15
CONFIRMATION_SLEEP   = 5
TP_SL_BE_PRICE_TOLERANCE_PERCENT = 0.005

TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN','YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID','YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

STATS_FILE = 'trading_stats.json'

FAST_LOOP_INTERVAL_SECONDS = 3
EMA_CALC_INTERVAL_SECONDS  = 180
TRADE_COOLDOWN_SECONDS     = 180
ERROR_RETRY_SLEEP_SECONDS  = 60

MONTHLY_REPORT_DAY    = 20
MONTHLY_REPORT_HOUR   = 0
MONTHLY_REPORT_MINUTE = 5

# --- NEW: MACD configs (ใช้ TF เดียวกับบอท) ---
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
SL_MACD_OFFSET_USD = 100.0   # ระยะ -100 ตามที่ขอ (ปรับได้)

# ================== logging ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log', encoding='utf-8'), logging.StreamHandler(sys.stdout)]
)
for h in logging.root.handlers:
    if isinstance(h, logging.StreamHandler):
        h.flush = lambda: sys.stdout.flush()
logger = logging.getLogger(__name__)

# ================== GLOBAL STATE ==================
exchange = None
market_info = None

current_position_details = None
portfolio_balance = 0.0
last_monthly_report_date = None
initial_balance = 0.0
last_ema_position_status = None
last_ema_calc_time = datetime.min
last_trade_closed_time = datetime.min
waiting_for_cooldown = False
last_manual_tp_alert_time = datetime.min
must_wait_new_cross = False
last_cross_bar_time = None
last_entry_cross_bar_time = None

monthly_stats = {
    'month_year': None, 'tp_count': 0, 'sl_count': 0, 'total_pnl': 0.0, 'trades': [],
    'last_report_month_year': None, 'last_ema_cross_signal': None, 'last_ema_position_status': None
}

# ================== Telegram ==================
def send_telegram(msg: str):
    if (not TELEGRAM_TOKEN or TELEGRAM_TOKEN == 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING' or
        not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING'):
        logger.warning("⚠ TELEGRAM creds not set; skip send.")
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        params = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
        requests.get(url, params=params, timeout=10).raise_for_status()
        logger.info("✉ Telegram sent")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

def fmt_usd(x):
    try: return f"{float(x):,.2f}"
    except: return str(x)

def fmt_pts(x):
    try:
        x = int(x)
        return f"+{x}" if x > 0 else f"{x}"
    except:
        return str(x)

# ================== Exchange Setup ==================
def setup_exchange():
    global exchange, market_info
    if not API_KEY or not SECRET or 'YOUR_' in API_KEY or 'YOUR_' in SECRET:
        send_telegram("⛔ Critical: API key/secret not set.")
        sys.exit(1)
    exchange = ccxt.binance({
        'apiKey': API_KEY, 'secret': SECRET, 'enableRateLimit': True,
        'options': {'defaultType': 'future', 'marginMode': 'cross'}, 'timeout': 30000
    })
    exchange.load_markets()
    market_info = exchange.market(SYMBOL)
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
    except Exception as e:
        send_telegram(f"⛔ set_leverage failed: {e}")
        sys.exit(1)
    logger.info("✅ Exchange ready.")

# ================== Balance / Banner ==================
def get_portfolio_balance():
    try:
        bal = exchange.fetch_balance()
        free_usdt = bal.get('USDT',{}).get('free',0) or next(
            (float(a.get('availableBalance',0)) for a in bal.get('info',{}).get('assets',[]) if a.get('asset')=='USDT'), 0.0
        )
        return float(free_usdt)
    except Exception as e:
        logger.error(f"balance error: {e}")
        send_telegram(f"⛔ balance error: {e}")
        return 0.0

def send_startup_banner():
    bal = get_portfolio_balance()
    msg = (
        "🤖 บอทเริ่มทำงาน 💰\n"
        f"💵 ยอดเริ่มต้น: {fmt_usd(bal)} USDT\n"
        f"📊 TF: {TIMEFRAME} | Leverage: {LEVERAGE}x\n"
        f"📈 • EMA Fast: {EMA_FAST_PERIOD}\n"
        f"📉 • EMA Slow: {EMA_SLOW_PERIOD}\n"
        f"❌ SL เริ่มต้น: {SL_DISTANCE_POINTS} points\n"
        # แสดงทั้ง LONG & SHORT
        f"🚀 Step 1 :  {TRAIL_SL_STEP1_TRIGGER_LONG_POINTS}pts → SL {fmt_pts(TRAIL_SL_STEP1_NEW_SL_POINTS_LONG)}pts\n"
        #f"🚀 Step 1 (SHORT): {TRAIL_SL_STEP1_TRIGGER_SHORT_POINTS}pts → SL {fmt_pts(TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT)}pts\n"
        f"🔥 Step 2 :  {TRAIL_SL_STEP2_TRIGGER_LONG_POINTS}pts → SL {fmt_pts(TRAIL_SL_STEP2_NEW_SL_POINTS_LONG)}pts\n"
        #f"🔥 Step 2 (SHORT): {TRAIL_SL_STEP2_TRIGGER_SHORT_POINTS}pts → SL {fmt_pts(TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT)}pts\n"
        f"🎉 Step 3 :  {TRAIL_SL_STEP3_TRIGGER_LONG_POINTS}pts → SL {fmt_pts(TRAIL_SL_STEP3_NEW_SL_POINTS_LONG)}pts\n"
        #f"🎉 Step 3 (SHORT): {TRAIL_SL_STEP3_TRIGGER_SHORT_POINTS}pts → SL {fmt_pts(TRAIL_SL_STEP3_NEW_SL_POINTS_SHORT)}pts\n"
        #f"🧠 MACD SL: ±{int(SL_MACD_OFFSET_USD)} USDT | ข้ามเมื่อ sl_step ≥ 2 | ใช้ครั้งเดียว/ออเดอร์\n"
        "⏳ กำลังรอสัญญาณเปิดออเดอร์..."
    )
    send_telegram(msg)

# ================== Stats helpers ==================
def save_monthly_stats():
    monthly_stats['last_ema_position_status'] = last_ema_position_status
    with open(STATS_FILE,'w',encoding='utf-8') as f: json.dump(monthly_stats,f,indent=4)

def reset_monthly_stats():
    monthly_stats.update({'month_year': datetime.now().strftime('%Y-%m'),
                          'tp_count':0,'sl_count':0,'total_pnl':0.0,'trades':[]})
    save_monthly_stats()

def add_trade_result(reason: str, pnl: float):
    if monthly_stats.get('month_year') != datetime.now().strftime('%Y-%m'): reset_monthly_stats()
    if reason.upper() == 'TP': monthly_stats['tp_count'] += 1
    elif reason.upper().startswith('SL'): monthly_stats['sl_count'] += 1
    monthly_stats['total_pnl'] += pnl
    monthly_stats['trades'].append({'time':datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                    'reason':reason,'pnl':pnl})
    save_monthly_stats()

# ================== Position & Orders helpers ==================
def get_current_position() -> dict|None:
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for p in positions:
            if p.get('symbol')==SYMBOL and float(p.get('contracts',0) or 0)!=0 and p.get('side'):
                return {'symbol':p['symbol'],'side':p['side'],'contracts':abs(float(p.get('contracts',0))),
                        'entry_price':float(p.get('entryPrice',0) or 0),
                        'unrealized_pnl':float(p.get('unrealizedPnl',0) or 0),
                        'liquidation_price':float(p.get('liquidationPrice',0) or 0) if p.get('liquidationPrice') else None,
                        'info':p}
        return None
    except Exception as e:
        logger.error(f"fetch_positions error: {e}")
        send_telegram(f"⛔ fetch_positions error: {e}")
        return None

def cancel_all_open_tp_sl_orders():
    try:
        open_orders = exchange.fetch_open_orders(SYMBOL)
        for o in open_orders:
            if (o['status'] in ['open','pending']) and (o.get('reduceOnly') or o['type'] in
               ['TAKE_PROFIT_MARKET','STOP_MARKET','STOP_LOSS_LIMIT','TAKE_PROFIT_LIMIT']):
                try: exchange.cancel_order(o['id'], SYMBOL); time.sleep(0.2)
                except: pass
    except Exception as e:
        logger.error(f"cancel orders error: {e}")

def round_to_precision(value: float, precision_type: str) -> float:
    if market_info and 'precision' in market_info and precision_type in market_info['precision']:
        return float(exchange.decimal_to_precision(value, ccxt.ROUND, market_info['precision'][precision_type]))
    return round(value, 8)

def price_equal(a: float, b: float, eps: float = 0.0) -> bool:
    """เทียบราคาหลังปัดตาม precision + เผื่อ epsilon ถ้าต้องการ"""
    pa = round_to_precision(a, 'price')
    pb = round_to_precision(b, 'price')
    return abs(pa - pb) <= eps

def set_sl_only_for_position(direction: str, amount: float, sl_price: float) -> bool:
    if not amount or amount <= 0: return False
    cancel_all_open_tp_sl_orders(); time.sleep(0.5)
    try:
        slp = round_to_precision(sl_price, 'price')
        cur = exchange.fetch_ticker(SYMBOL)['last']
        if (direction=='long' and slp>=cur) or (direction=='short' and slp<=cur):
            return False
        sl_side = 'sell' if direction=='long' else 'buy'
        exchange.create_order(SYMBOL, 'STOP_MARKET', sl_side, amount, None,
                              {'stopPrice': slp, 'reduceOnly': True})
        send_telegram(
            "✅ ตั้ง SL สำเร็จ!\n"
            f"🛡 SL: <code>{fmt_usd(slp)}</code>\n"
            f"📊 Direction: <b>{direction.upper()}</b>\n"
            #f"📦 Size: <code>{amount:.8f}</code>"
        )
        return True
    except Exception as e:
        logger.error(f"set_sl error: {e}")
        send_telegram(f"❌ SL Error: {e}")
        return False

# ================== Indicators ==================
def calculate_ema(prices: list[float], period: int) -> float|None:
    if len(prices) < period: return None
    sma = sum(prices[:period]) / period
    e = sma; k = 2/(period+1)
    for px in prices[period:]: e = px*k + e*(1-k)
    return e

def check_ema_cross() -> str|None:
    global last_ema_position_status, last_cross_bar_time
    try:
        limit = max(EMA_FAST_PERIOD, EMA_SLOW_PERIOD)*4
        ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=limit)
        if not ohlcv: return None
        closes = [c[4] for c in ohlcv]
        ema_fast_now  = calculate_ema(closes, EMA_FAST_PERIOD)
        ema_slow_now  = calculate_ema(closes, EMA_SLOW_PERIOD)
        ema_fast_prev = calculate_ema(closes[:-1], EMA_FAST_PERIOD)
        ema_slow_prev = calculate_ema(closes[:-1], EMA_SLOW_PERIOD)
        if None in [ema_fast_now, ema_slow_now, ema_fast_prev, ema_slow_prev]: return None
        curr = 'above' if ema_fast_now>ema_slow_now else 'below'
        prev = 'above' if ema_fast_prev>ema_slow_prev else 'below'
        cross = None
        if prev=='below' and curr=='above' and ema_fast_now>(ema_slow_now + CROSS_THRESHOLD_POINTS):
            cross='long'; last_cross_bar_time = ohlcv[-2][0]
        elif prev=='above' and curr=='below' and ema_fast_now<(ema_slow_now - CROSS_THRESHOLD_POINTS):
            cross='short'; last_cross_bar_time = ohlcv[-2][0]
        if curr != last_ema_position_status:
            last_ema_position_status = curr; save_monthly_stats()
        return cross
    except Exception as e:
        logger.error(f"EMA error: {e}")
        return None

# --- NEW: MACD helper (DIF/DEA) ---
def _ema_series(xs, period):
    k = 2/(period+1); out=[]; e=xs[0]; out.append(e)
    for v in xs[1:]:
        e = v*k + e*(1-k); out.append(e)
    return out

def macd_from_closes(closes):
    if len(closes) < MACD_SLOW + MACD_SIGNAL + 2: return None
    ef = _ema_series(closes, MACD_FAST)
    es = _ema_series(closes, MACD_SLOW)
    start = len(ef) - len(es)
    dif = [ef[start+i] - es[i] for i in range(len(es))]
    dea = _ema_series(dif, MACD_SIGNAL)
    dif_prev, dif_now = dif[-2], dif[-1]
    dea_prev, dea_now = dea[-2], dea[-1]
    return dif_prev, dif_now, dea_prev, dea_now

def macd_cross_down(dif_prev, dif_now, dea_prev, dea_now):  # DIF ตัดลง
    return (dif_prev >= dea_prev) and (dif_now < dea_now)
def macd_cross_up(dif_prev, dif_now, dea_prev, dea_now):    # DIF ตัดขึ้น
    return (dif_prev <= dea_prev) and (dif_now > dea_now)

# ================== Orders ==================
def calculate_order_details(available_usdt: float, price: float) -> tuple[float,float]:
    if price<=0 or LEVERAGE<=0 or TARGET_POSITION_SIZE_FACTOR<=0: return (0,0)
    amt_step = exchange.market(SYMBOL)['limits']['amount'].get('step', 0.001)
    min_amt  = exchange.market(SYMBOL)['limits']['amount'].get('min', 0.001)
    min_cost = exchange.market(SYMBOL)['limits']['cost'].get('min', 5.0)
    invest = max(0.0, available_usdt - MARGIN_BUFFER_USDT)
    if invest <= 0: return (0,0)
    target_notional_raw = invest * TARGET_POSITION_SIZE_FACTOR * LEVERAGE
    target_notional = max(target_notional_raw, min_cost, min_amt*price)
    contracts = float(exchange.amount_to_precision(SYMBOL, target_notional/price))
    if contracts < min_amt: contracts = min_amt
    req_margin = (contracts*price)/LEVERAGE
    return (contracts, req_margin)

def open_market_order(direction: str, current_price: float) -> tuple[bool, float|None]:
    global current_position_details
    if last_trade_closed_time and (datetime.now()-last_trade_closed_time).total_seconds() < TRADE_COOLDOWN_SECONDS:
        send_telegram("⏱️ Cooldown กำลังทำงาน"); return False, None
    bal = get_portfolio_balance()
    amount, _ = calculate_order_details(bal, current_price)
    if amount<=0: return False, None
    side = 'buy' if direction=='long' else 'sell'
    try:
        exchange.create_market_order(SYMBOL, side, amount)
        ok, entry = confirm_position_entry(direction, amount)
        return ok, entry
    except Exception as e:
        logger.error(f"open order error: {e}")
        send_telegram(f"❌ Open order error: {e}")
        return False, None

def confirm_position_entry(expected_direction: str, expected_contracts: float) -> tuple[bool, float | None]:
    global current_position_details
    time.sleep(2)
    for _ in range(CONFIRMATION_RETRIES):
        pos = get_current_position()
        if pos and pos.get('side') == expected_direction:
            current_position_details = {
                'symbol': SYMBOL, 'side': expected_direction, 'contracts': pos['contracts'],
                'entry_price': pos['entry_price'], 'unrealized_pnl': pos['unrealized_pnl'],
                'liquidation_price': pos['liquidation_price'],
                'sl_step': 0, 'sl_price': None, 'tp_price': None, 'initial_sl_price': None,
                'macd_used': False  # <<< MACD EXIT ใช้ได้ครั้งเดียว/ออเดอร์
            }
            send_telegram(
                "💶 เปิดโพซิชัน <b>{}</b> สำเร็จ!\n"
                "📦 ขนาด: <code>{:.8f}</code> Contracts\n"
                "🎯 Entry: <code>{}</code>\n".format(
                    expected_direction.upper(),
                    float(pos['contracts']),
                    fmt_usd(pos['entry_price'])
                )
            )
            return True, pos['entry_price']
        time.sleep(CONFIRMATION_SLEEP)
    send_telegram("⛔ ยืนยันโพซิชันไม่สำเร็จ")
    return False, None

# ================== MONITOR (รวม MACD EXIT ครั้งเดียว/ออเดอร์) ==================
def monitor_position(current_market_price: float):
    global current_position_details, last_ema_position_status, last_trade_closed_time
    global waiting_for_cooldown, last_manual_tp_alert_time, must_wait_new_cross

    pos_info = get_current_position()

    # A) มีโพซิชัน → อัปเดต
    if pos_info and current_position_details:
        current_position_details.update({
            'entry_price':pos_info['entry_price'],'contracts':pos_info['contracts'],'side':pos_info['side'],
            'unrealized_pnl':pos_info['unrealized_pnl'],'liquidation_price':pos_info['liquidation_price']
        })
        side = current_position_details['side']              # 'long'/'short'
        entry = float(current_position_details['entry_price'])
        qty   = float(current_position_details['contracts'])
        sl_step = int(current_position_details.get('sl_step',0))
        sl_price = current_position_details.get('sl_price')

        # ตั้ง SL เริ่มต้น
        if sl_price is None:
            sl = entry - SL_DISTANCE_POINTS if side=='long' else entry + SL_DISTANCE_POINTS
            current_position_details['sl_price'] = sl
            current_position_details['initial_sl_price'] = sl
            current_position_details['tp_price'] = 0
            set_sl_only_for_position(side, qty, sl)

        # === MACD-based SL (ข้ามเมื่อ sl_step >= 2 และยังไม่เคยใช้ MACD เลย) ===
        if sl_step < 2 and not current_position_details.get('macd_used', False):
            try:
                # ใช้แท่งปิดเพื่อกัน intrabar flip
                ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=200)
                closes = [c[4] for c in ohlcv[:-1]]
                macd_vals = macd_from_closes(closes)
                if macd_vals:
                    dif_prev, dif_now, dea_prev, dea_now = macd_vals
                    long_exit  = macd_cross_down(dif_prev, dif_now, dea_prev, dea_now)
                    short_exit = macd_cross_up(dif_prev, dif_now, dea_prev, dea_now)
                    if (side=='long' and long_exit) or (side=='short' and short_exit):
                        last_price = current_market_price
                        if side=='long':
                            is_profit = (last_price - entry) > 0
                            new_sl = (entry - SL_MACD_OFFSET_USD) if is_profit else (last_price - SL_MACD_OFFSET_USD)
                        else:
                            is_profit = (entry - last_price) > 0
                            new_sl = (entry + SL_MACD_OFFSET_USD) if is_profit else (last_price + SL_MACD_OFFSET_USD)

                        # ไม่ตั้งซ้ำถ้า SL เดิมเท่ากันในเชิง precision
                        if current_position_details.get('sl_price') is not None and \
                           price_equal(current_position_details['sl_price'], new_sl):
                            # ถือว่าใช้ MACD แล้วเพื่อกันยิงซ้ำ
                            current_position_details['macd_used'] = True
                        else:
                            ok = set_sl_only_for_position(side, qty, new_sl)
                            if ok:
                                current_position_details['sl_price'] = new_sl
                                current_position_details['macd_used'] = True
                                send_telegram(
                                    "⚠️ <b>MACD ตัดกันมีความเสี่ยง_ต้องตั้งSlใหม่</b>\n"
                                    #f"📊 Direction: <b>{side.upper()}</b>\n"
                                    f"🎯 Entry: <code>{fmt_usd(entry)}</code>\n"
                                    f"📈 Last: <code>{fmt_usd(last_price)}</code>\n"
                                    f"🛡 SL ใหม่: <code>{fmt_usd(round_to_precision(new_sl,'price'))}</code>\n"
                                    f"📌 สถานะ: {'กำไร' if is_profit else 'ขาดทุน'} | sl_step={sl_step}"
                                )
            except Exception as e:
                logger.warning(f"MACD section error: {e}")

        # ===== Trailing SL Steps (เดิม) =====
        pnl_points = (current_market_price - entry) if side=='long' else (entry - current_market_price)

        t1 = TRAIL_SL_STEP1_TRIGGER_LONG_POINTS  if side=='long' else TRAIL_SL_STEP1_TRIGGER_SHORT_POINTS
        t2 = TRAIL_SL_STEP2_TRIGGER_LONG_POINTS  if side=='long' else TRAIL_SL_STEP2_TRIGGER_SHORT_POINTS
        t3 = TRAIL_SL_STEP3_TRIGGER_LONG_POINTS  if side=='long' else TRAIL_SL_STEP3_TRIGGER_SHORT_POINTS

        sl1 = entry + TRAIL_SL_STEP1_NEW_SL_POINTS_LONG if side=='long' else entry + TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT
        sl2 = entry + TRAIL_SL_STEP2_NEW_SL_POINTS_LONG if side=='long' else entry + TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT
        sl3 = entry + TRAIL_SL_STEP3_NEW_SL_POINTS_LONG if side=='long' else entry + TRAIL_SL_STEP3_NEW_SL_POINTS_SHORT

        if sl_step == 0 and pnl_points >= t1:
            current_position_details['sl_step'] = 1
            current_position_details['sl_price'] = sl1
            set_sl_only_for_position(side, qty, sl1)

        elif sl_step == 1 and pnl_points >= t2:
            current_position_details['sl_step'] = 2
            current_position_details['sl_price'] = sl2
            set_sl_only_for_position(side, qty, sl2)

        elif sl_step == 2 and pnl_points >= t3:
            current_position_details['sl_step'] = 3
            current_position_details['sl_price'] = round_to_precision(sl3, 'price')
            set_sl_only_for_position(side, qty, current_position_details['sl_price'])

        elif sl_step == 3 and pnl_points > MANUAL_TP_ALERT_THRESHOLD:
            now = datetime.now()
            if (now - last_manual_tp_alert_time).total_seconds() >= MANUAL_TP_ALERT_INTERVAL:
                last_manual_tp_alert_time = now
                send_telegram(
                    "🔔 <b>ก๊อกๆกำไรทะลุเป้าเกินไป รีบกดClose All ด่วนๆ</b>\n"
                    f"💰 กำไร: <b>{pnl_points:+,.0f} points</b>\n"
                    f"🎯 Entry: <code>{fmt_usd(entry)}</code>\n"
                    f"📈 Now: <code>{fmt_usd(current_market_price)}</code>"
                )
        return

    # B) ปิดโพซิชันแล้ว แต่บอทยังจำ → เคลียร์
    elif (not pos_info) and current_position_details:
        entry = current_position_details.get('entry_price'); side = current_position_details.get('side')
        qty   = current_position_details.get('contracts'); sl_step = current_position_details.get('sl_step',0)
        if entry and qty and side:
            closed = current_market_price
            pnl = (closed - entry)*qty if side=='long' else (entry - closed)*qty
            reason = "TP" if sl_step in (2,3) else "SL"
            add_trade_result(reason, pnl)
        try: cancel_all_open_tp_sl_orders()
        except: pass
        current_position_details = None   # macd_used reset ด้วย
        last_trade_closed_time = datetime.now()
        waiting_for_cooldown = True
        last_ema_position_status = None
        save_monthly_stats()
        send_telegram(
            "📊 ปิดโพซิชัน <b>{}</b>\n"
            "PnL: <b>{:+.2f} USDT</b>\n"
            "เหตุผล: <b>{}</b>".format(side.upper(), float(pnl), reason)
        )
        return

    else:
        return

# ================== main loop ==================
def main():
    setup_exchange()
    reset_monthly_stats()
    send_startup_banner()

    while True:
        try:
            price = exchange.fetch_ticker(SYMBOL)['last']
            monitor_position(price)

            now = datetime.now()
            global last_ema_calc_time
            if (now - last_ema_calc_time).total_seconds() >= EMA_CALC_INTERVAL_SECONDS:
                last_ema_calc_time = now
                sig = check_ema_cross()
                if sig and not current_position_details:
                    open_market_order(sig, price)

            time.sleep(FAST_LOOP_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"main loop error: {e}")
            time.sleep(ERROR_RETRY_SLEEP_SECONDS)

if __name__ == "__main__":
    main()
