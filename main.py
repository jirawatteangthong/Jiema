import ccxt
import time
import requests
from datetime import datetime, timedelta
import logging
import threading
import json
import os
import calendar
import sys # <<-- เพิ่มบรรทัดนี้: ต้อง import sys เพื่อใช้ sys.stdout

# ==============================================================================
# 1. ตั้งค่าพื้นฐาน (CONFIGURATION)
#    ⚠️ สำคัญมาก: โปรดตั้งค่า Environment Variables เหล่านี้บน GitHub/Railway
#    เช่น RAILWAY_API_KEY, RAILWAY_SECRET, RAILWAY_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
# ==============================================================================

# --- API Keys & Credentials (ดึงจาก Environment Variables เพื่อความปลอดภัย) ---
API_KEY = os.getenv('RAILWAY_API_KEY', 'YOUR_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('RAILWAY_SECRET', 'YOUR_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('RAILWAY_PASSWORD', 'YOUR_PASSWORD_HERE_FOR_LOCAL_TESTING')

# --- Trade Parameters ---
SYMBOL = 'BTC/USDT:USDT'  # คู่เทรดที่ต้องการ (เช่น BTC/USDT:USDT สำหรับ Perpetual Swap)
TIMEFRAME = '15m'         # Timeframe ของแท่งเทียน (เช่น '1m', '5m', '15m', '1h')
LEVERAGE = 30             # <<-- ตั้ง Leverage เป็น 30
TP_VALUE_POINTS = 501     # ระยะ TP (Take Profit) เป็นจุด (เช่น 500 จุดสำหรับ BTC)
SL_VALUE_POINTS = 999     # ระยะ SL (Stop Loss) เป็นจุด
BE_PROFIT_TRIGGER_POINTS = 350   # กำไรที่ต้องถึงก่อนเลื่อน SL เป็นกันทุน (เป็นจุด)
BE_SL_BUFFER_POINTS = 100        # Buffer สำหรับ SL กันทุน (เป็นจุด) เช่น เลื่อน SL ไปที่ Entry + 100 จุด
PORTFOLIO_PERCENT_TRADE = 0.9 # <<-- ตั้งค่าเป็น 1.0 เพื่อใช้เงินเกือบทั้งหมด ("All in")
CROSS_THRESHOLD_POINTS = 20  # ระยะห่างขั้นต่ำที่ EMA50 ต้องทำได้เหนือ/ใต้ EMA200 ก่อนจะถือว่าเป็นสัญญาณจริง (หน่วยเป็นจุดราคา)

# --- Telegram Notification Settings ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# --- Files & Paths ---
STATS_FILE = 'trading_stats.json' # ไฟล์สำหรับบันทึกสถิติการเทรด

# --- Bot Timing ---
MAIN_LOOP_SLEEP_SECONDS = 360 # 🚀 เวลาหน่วงระหว่างรอบการทำงานหลัก (6 นาที)
ERROR_RETRY_SLEEP_SECONDS = 60 # หน่วงเวลานานขึ้นเมื่อเกิดข้อผิดพลาดร้ายแรง/API Error
MONTHLY_REPORT_DAY = 20       # วันที่ของเดือนที่ต้องการส่งรายงานประจำเดือน (1-28)
MONTHLY_REPORT_HOUR = 0      # ชั่วโมงที่ต้องการส่งรายงานประจำเดือน (0-23)
MONTHLY_REPORT_MINUTE = 5    # นาทีที่ต้องการส่งรายงานประจำเดือน (0-59)

# --- Tolerance สำหรับการระบุสาเหตุการปิดออเดอร์ ---
TP_SL_BE_PRICE_TOLERANCE_PERCENT = 0.005 # 0.5%

# ==============================================================================
# 2. การตั้งค่า Logging
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'), # เพิ่ม encoding
        logging.StreamHandler(sys.stdout) # ระบุ sys.stdout ชัดเจน
    ]
)
# <<-- เพิ่มบรรทัดนี้: บังคับให้ Log ถูกเขียนลงทันที (สำคัญสำหรับการ Debug ปัญหา Crash)
for handler in logging.root.handlers:
    if hasattr(handler, 'flush'):
        handler.flush = lambda: sys.stdout.flush() if isinstance(handler, logging.StreamHandler) else handler.stream.flush()

logger = logging.getLogger(__name__)


# ==============================================================================
# 3. ตัวแปรสถานะการเทรด (GLOBAL TRADE STATE VARIABLES)
# ==============================================================================
current_position = None
entry_price = None
sl_moved = False
portfolio_balance = 0.0
last_monthly_report_date = None
initial_balance = 0.0
current_position_size = 0.0 # ขนาดของโพซิชันปัจจุบันที่เปิดอยู่

# ==============================================================================
# 4. โครงสร้างข้อมูลสถิติ (STATISTICS DATA STRUCTURE)
# ==============================================================================
monthly_stats = {
    'month_year': None,
    'tp_count': 0,
    'sl_count': 0,
    'total_pnl': 0.0,
    'trades': [],
    'last_report_month_year': None,
    'last_ema_cross_signal': None
}

# ==============================================================================
# 5. การตั้งค่า Exchange (CCXT EXCHANGE SETUP)
# ==============================================================================
try:
    if not API_KEY or API_KEY == 'YOUR_API_KEY_HERE_FOR_LOCAL_TESTING' or \
       not SECRET or SECRET == 'YOUR_SECRET_HERE_FOR_LOCAL_TESTING' or \
       not PASSWORD or PASSWORD == 'YOUR_PASSWORD_HERE_FOR_LOCAL_TESTING':
        raise ValueError("API_KEY, SECRET, หรือ PASSWORD ไม่ถูกตั้งค่าใน Environment Variables.")

    exchange = ccxt.okx({
        'apiKey': API_KEY,
        'secret': SECRET,
        'password': PASSWORD,
        'enableRateLimit': True,
        'rateLimit': 1000,
        'options': {'defaultType': 'swap'}
    })
    exchange.set_sandbox_mode(False) # ⚠️ ตั้งค่าให้เป็น False สำหรับบัญชีจริง
    logger.info("✅ เชื่อมต่อกับ OKX Exchange สำเร็จ")
except ValueError as ve:
    logger.critical(f"❌ Configuration Error: {ve}")
    exit()
except Exception as e:
    logger.critical(f"❌ ไม่สามารถเชื่อมต่อกับ OKX Exchange ได้: {e}")
    exit()

# ==============================================================================
# 6. ฟังก์ชันจัดการสถิติ (STATISTICS MANAGEMENT FUNCTIONS)
# ==============================================================================

def save_monthly_stats():
    """บันทึกสถิติการเทรดประจำเดือนลงในไฟล์ JSON."""
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump(monthly_stats, f, indent=4)
        logger.debug(f"💾 บันทึกสถิติการเทรดลงไฟล์ {STATS_FILE} สำเร็จ")
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการบันทึกสถิติ: {e}")

def load_monthly_stats():
    """โหลดสถิติการเทรดประจำเดือนจากไฟล์ JSON."""
    global monthly_stats, last_monthly_report_date
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, 'r') as f:
                loaded_stats = json.load(f)

                monthly_stats['month_year'] = loaded_stats.get('month_year', None)
                monthly_stats['tp_count'] = loaded_stats.get('tp_count', 0)
                monthly_stats['sl_count'] = loaded_stats.get('sl_count', 0)
                monthly_stats['total_pnl'] = loaded_stats.get('total_pnl', 0.0)
                monthly_stats['trades'] = loaded_stats.get('trades', [])
                monthly_stats['last_report_month_year'] = loaded_stats.get('last_report_month_year', None)
                monthly_stats['last_ema_cross_signal'] = loaded_stats.get('last_ema_cross_signal', None)

            logger.info(f"💾 โหลดสถิติการเทรดจากไฟล์ {STATS_FILE} สำเร็จ")

            if monthly_stats['last_report_month_year']:
                try:
                    year, month = map(int, monthly_stats['last_report_month_year'].split('-'))
                    last_monthly_report_date = datetime(year, month, 1).date()
                except ValueError:
                    logger.warning("⚠️ รูปแบบวันที่ last_report_month_year ในไฟล์ไม่ถูกต้อง. จะถือว่ายังไม่มีการส่งรายงาน.")
                    last_monthly_report_date = None
            else:
                last_monthly_report_date = None

            current_month_year_str = datetime.now().strftime('%Y-%m')
            if monthly_stats['month_year'] != current_month_year_str:
                logger.info(f"ℹ️ สถิติที่โหลดมาเป็นของเดือน {monthly_stats['month_year']} ไม่ตรงกับเดือนนี้ {current_month_year_str}. จะรีเซ็ตสถิติสำหรับเดือนใหม่.")
                reset_monthly_stats()

        else:
            logger.info(f"🆕 ไม่พบไฟล์สถิติ {STATS_FILE} สร้างไฟล์ใหม่")
            reset_monthly_stats()

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการโหลดสถิติ: {e}")
        monthly_stats = {
            'month_year': None, 'tp_count': 0, 'sl_count': 0, 'total_pnl': 0.0, 'trades': [],
            'last_report_month_year': None, 'last_ema_cross_signal': None
        }
        last_monthly_report_date = None
        reset_monthly_stats()

def reset_monthly_stats():
    """รีเซ็ตสถิติประจำเดือนสำหรับเดือนใหม่."""
    global monthly_stats
    monthly_stats['month_year'] = datetime.now().strftime('%Y-%m')
    monthly_stats['tp_count'] = 0
    monthly_stats['sl_count'] = 0
    monthly_stats['total_pnl'] = 0.0
    save_monthly_stats()
    logger.info(f"🔄 รีเซ็ตสถิติประจำเดือนสำหรับเดือน {monthly_stats['month_year']}")

def add_trade_result(reason: str, pnl: float):
    """เพิ่มผลการเทรดลงในสถิติประจำเดือน."""
    global monthly_stats
    current_month_year_str = datetime.now().strftime('%Y-%m')

    if monthly_stats['month_year'] != current_month_year_str:
        logger.info(f"🆕 เดือนเปลี่ยนใน add_trade_result: {monthly_stats['month_year']} -> {current_month_year_str}. กำลังรีเซ็ตสถิติประจำเดือน.")
        reset_monthly_stats()

    if reason.upper() == 'TP':
        monthly_stats['tp_count'] += 1
    elif reason.upper() == 'SL' or reason.upper() == 'SL (กันทุน)':
        monthly_stats['sl_count'] += 1

    monthly_stats['total_pnl'] += pnl

    monthly_stats['trades'].append({
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'reason': reason,
        'pnl': pnl
    })
    save_monthly_stats()

# ==============================================================================
# 7. ฟังก์ชันแจ้งเตือน Telegram (TELEGRAM NOTIFICATION FUNCTIONS)
# ==============================================================================

def send_telegram(msg: str):
    """ส่งข้อความไปยัง Telegram."""
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING' or \
       not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING':
        logger.warning("⚠️ TELEGRAM_TOKEN หรือ TELEGRAM_CHAT_ID ไม่ได้ถูกตั้งค่า. ไม่สามารถส่งข้อความ Telegram ได้.")
        return

    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        params = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        logger.info(f"✉️ Telegram: {msg.splitlines()[0]}...")
    except requests.exceptions.Timeout:
        logger.error("⛔️ Error: ไม่สามารถส่งข้อความ Telegram ได้ (Timeout)")
    except requests.exceptions.HTTPError as e:
        telegram_error_msg = e.response.json().get('description', e.response.text)
        logger.error(f"⛔️ Error: ไม่สามารถส่งข้อความ Telegram ได้ (HTTP Error) - รายละเอียด: {telegram_error_msg}")
    except requests.exceptions.RequestException as e:
        logger.error(f"⛔️ Error: ไม่สามารถส่งข้อความ Telegram ได้ (Request Error) - {e}")
    except Exception as e:
        logger.error(f"❌ Unexpected Telegram error: {e}")

# ==============================================================================
# 8. ฟังก์ชันดึงข้อมูล Exchange (EXCHANGE DATA RETRIEVAL FUNCTIONS)
# ==============================================================================

def get_portfolio_balance() -> float:
    """ดึงยอดคงเหลือ USDT ในพอร์ต (เฉพาะบัญชี Trading/Swap)."""
    global portfolio_balance
    retries = 3
    for i in range(retries):
        try:
            params = {'type': 'trading'}
            balance_data = exchange.fetch_balance(params)
            time.sleep(2)

            usdt_balance = float(balance_data.get('USDT', {}).get('free', 0.0))
            if usdt_balance > 0:
                portfolio_balance = usdt_balance
                logger.info(f"💰 ยอดคงเหลือ USDT (Trading Account): {usdt_balance:,.2f}")
                return usdt_balance
            else:
                 logger.warning("⚠️ ไม่พบ 'free' balance ใน USDT, ลองดึงจาก 'total'...")
                 usdt_balance_total = float(balance_data.get('USDT', {}).get('total', 0.0))
                 portfolio_balance = usdt_balance_total
                 logger.info(f"💰 ยอดคงเหลือ USDT (Total): {usdt_balance_total:,.2f}")
                 return usdt_balance_total

        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.warning(f"⚠️ Error fetching balance (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
            if i == retries - 1:
                send_telegram(f"⛔️ API Error: ไม่สามารถดึงยอดคงเหลือได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
            time.sleep(15)
        except Exception as e:
            logger.error(f"❌ Unexpected error in get_portfolio_balance: {e}")
            send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึงยอดคงเหลือได้\nรายละเอียด: {e}")
            return 0.0
    logger.error(f"❌ Failed to fetch balance after {retries} attempts.")
    send_telegram(f"⛔️ API Error: ล้มเหลวในการดึงยอดคงเหลือหลังจาก {retries} ครั้ง.")
    return 0.0

def get_current_position() -> dict | None:
    """ตรวจสอบและดึงข้อมูลโพซิชัน BTC/USDT ปัจจุบัน พร้อม retry."""
    retries = 3
    for i in range(retries):
        try:
            positions = exchange.fetch_positions([SYMBOL])
            time.sleep(2)
            for pos in positions:
                if float(pos.get('info', {}).get('posAmt', 0)) != 0:
                    pos_amount = float(pos['info']['posAmt'])
                    return {
                        'side': 'long' if pos_amount > 0 else 'short',
                        'size': abs(pos_amount),
                        'entry_price': float(pos['entryPrice']),
                        'unrealized_pnl': float(pos['unrealizedPnl']),
                        'pos_id': pos['id']
                    }
            return None
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.warning(f"⚠️ Error fetching positions (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
            if i == retries - 1:
                send_telegram(f"⛔️ API Error: ไม่สามารถดึงโพซิชันได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
            time.sleep(15)
        except Exception as e:
            logger.error(f"❌ Unexpected error in get_current_position: {e}")
            send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึงโพซิชันได้\nรายละเอียด: {e}")
            return None
    logger.error(f"❌ Failed to fetch positions after {retries} attempts.")
    send_telegram(f"⛔️ API Error: ล้มเหลวในการดึงโพซิชันหลังจาก {retries} ครั้ง.")
    return None

# ==============================================================================
# 9. ฟังก์ชันคำนวณ Indicators (INDICATOR CALCULATION FUNCTIONS)
# ==============================================================================

def calculate_ema(prices: list[float], period: int) -> float | None:
    """คำนวณ Exponential Moving Average (EMA)."""
    if len(prices) < period:
        return None

    sma = sum(prices[:period]) / period
    ema = sma
    multiplier = 2 / (period + 1)

    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))

    return ema

def check_ema_cross() -> str | None:
    """ตรวจสอบการตัดกันของ EMA50 และ EMA200 โดยใช้ Threshold เพื่อยืนยัน."""
    try:
        retries = 3
        ohlcv = None
        for i in range(retries):
            try:
                ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=250)
                time.sleep(2)
                break
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                logger.warning(f"⚠️ Error fetching OHLCV (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
                if i == retries - 1:
                    send_telegram(f"⛔️ API Error: ไม่สามารถดึง OHLCV ได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
                time.sleep(15)
            except Exception as e:
                logger.error(f"❌ Unexpected error fetching OHLCV: {e}")
                send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึง OHLCV ได้\nรายละเอียด: {e}")
                return None

        if not ohlcv:
            logger.error(f"❌ Failed to fetch OHLCV after {retries} attempts.")
            send_telegram(f"⛔️ API Error: ล้มเหลวในการดึง OHLCV หลังจาก {retries} ครั้ง.")
            return None

        if len(ohlcv) < 202:
            logger.warning(f"ข้อมูล OHLCV ไม่เพียงพอ. ต้องการอย่างน้อย 202 แท่ง ได้ {len(ohlcv)}")
            return None

        closes = [candle[4] for candle in ohlcv]

        ema50_current = calculate_ema(closes, 50)
        ema200_current = calculate_ema(closes, 200)

        # ยังคงใช้ข้อมูลจากแท่งก่อนหน้าเพื่อดูว่าการ "ตัด" ได้เกิดขึ้นแล้ว
        ema50_prev = calculate_ema(closes[:-1], 50)
        ema200_prev = calculate_ema(closes[:-1], 200)

        if None in [ema50_prev, ema200_prev, ema50_current, ema200_current]:
            logger.warning("ค่า EMA ไม่สามารถคำนวณได้ (เป็น None).")
            return None

        cross_signal = None

        if ema50_prev <= ema200_prev and ema50_current > (ema200_current + CROSS_THRESHOLD_POINTS):
            cross_signal = 'long'
            logger.info(f"🚀 Threshold Golden Cross: EMA50({ema50_current:.2f}) is {CROSS_THRESHOLD_POINTS} points above EMA200({ema200_current:.2f})")

        elif ema50_prev >= ema200_prev and ema50_current < (ema200_current - CROSS_THRESHOLD_POINTS):
            cross_signal = 'short'
            logger.info(f"🔻 Threshold Death Cross: EMA50({ema50_current:.2f}) is {CROSS_THRESHOLD_POINTS} points below EMA200({ema200_current:.2f})")

        return cross_signal

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการคำนวณ EMA: {e}")
        send_telegram(f"⛔️ Error: ไม่สามารถคำนวณ EMA ได้\nรายละเอียด: {e}")
        return None

# ==============================================================================
# 10. ฟังก์ชันจัดการคำสั่งซื้อขาย (ORDER MANAGEMENT FUNCTIONS)
# ==============================================================================

def open_market_order(direction: str, current_price: float) -> tuple[bool, float | None]:
    """เปิดออเดอร์ Market และคืนราคา Entry Price."""
    global current_position_size

    try:
        balance = get_portfolio_balance()
        if balance <= 1:
            send_telegram(f"⛔️ Error: ยอดคงเหลือไม่เพียงพอสำหรับเปิดออเดอร์ ({balance:.2f} USDT).")
            logger.error(f"❌ Balance ({balance:.2f} USDT) is too low to open an order.")
            return False, None

        use_balance_for_trade = balance * PORTFOLIO_PERCENT_TRADE

        market = exchange.market(SYMBOL)
        
        # Log market limits for debugging
        min_amount_btc_from_exchange = market.get('limits', {}).get('amount', {}).get('min', 0)
        min_notional_usdt_from_exchange = market.get('limits', {}).get('cost', {}).get('min', 0)
        
        logger.info(f"ℹ️ Exchange Minimums for {SYMBOL}: Min_Amount_BTC={min_amount_btc_from_exchange:.6f}, Min_Notional_USDT={min_notional_usdt_from_exchange:.2f}")


        # คำนวณขนาดออเดอร์ตามเงินทุนและ Leverage (ใช้ PORTFOLIO_PERCENT_TRADE ที่ 1.0)
        order_size_in_btc_calculated = (use_balance_for_trade * LEVERAGE) / current_price
        logger.info(f"ℹ️ Calculated Order Size (raw): {order_size_in_btc_calculated:.6f} BTC (จาก {use_balance_for_trade:,.2f} USDT * {LEVERAGE}x)")

        order_size_in_btc = order_size_in_btc_calculated

        # ตรวจสอบและปรับขนาดออเดอร์ให้ถึงขั้นต่ำของ Exchange (หากจำเป็น)
        # ตรวจสอบ Min Amount (เป็น BTC)
        if min_amount_btc_from_exchange and order_size_in_btc < min_amount_btc_from_exchange:
            logger.warning(f"⚠️ ขนาด BTC ที่คำนวณได้ ({order_size_in_btc:.6f}) ต่ำกว่าขั้นต่ำของ Exchange ({min_amount_btc_from_exchange:.6f} BTC). จะใช้ขนาดขั้นต่ำแทน.")
            order_size_in_btc = min_amount_btc_from_exchange

        # ตรวจสอบ Min Notional (มูลค่าเป็น USDT)
        current_notional_value = order_size_in_btc * current_price
        if min_notional_usdt_from_exchange and current_notional_value < min_notional_usdt_from_exchange:
            logger.warning(f"⚠️ มูลค่า Notional ที่คำนวณได้ (สำหรับ {order_size_in_btc:.6f} BTC คือ {current_notional_value:.2f} USDT) ต่ำกว่ามูลค่า Notional ขั้นต่ำ ({min_notional_usdt_from_exchange:.2f} USDT).")
            required_btc_for_min_notional = min_notional_usdt_from_exchange / current_price
            
            if required_btc_for_min_notional > order_size_in_btc:
                logger.warning(f"ℹ️ ปรับขนาด BTC จาก {order_size_in_btc:.6f} เป็น {required_btc_for_min_notional:.6f} BTC เพื่อให้ถึงมูลค่า Notional ขั้นต่ำ.")
                order_size_in_btc = required_btc_for_min_notional
            else:
                 logger.info(f"ℹ️ มูลค่า Notional ขั้นต่ำถูกพบหรือเกินแล้ว. ไม่มีการปรับเพิ่มขนาดสำหรับ Notional.")

        # ปรับขนาดสุดท้ายให้ตรงตาม Precision ของ Exchange
        order_size_in_btc = float(exchange.amount_to_precision(SYMBOL, order_size_in_btc))
        logger.info(f"ℹ️ ขนาดออเดอร์สุดท้ายหลังจากปรับขั้นต่ำและ Precision: {order_size_in_btc:.6f} BTC")

        # ตรวจสอบ Margin ที่ต้องการเทียบกับเงินคงเหลือ
        required_notional_for_final_size = order_size_in_btc * current_price
        required_margin_for_final_size = required_notional_for_final_size / LEVERAGE
        
        if balance < required_margin_for_final_size:
             error_msg = f"⛔️ Error: ยอดคงเหลือไม่เพียงพอ ({balance:,.2f} USDT) ที่จะเปิดออเดอร์ขนาด {order_size_in_btc:.6f} BTC. ต้องการ Margin {required_margin_for_final_size:,.2f} USDT."
             send_telegram(error_msg)
             logger.error(error_msg)
             return False, None
        
        if order_size_in_btc <= 0:
            send_telegram("⛔️ Error: ขนาดออเดอร์คำนวณได้เป็นศูนย์หรือติดลบหลังปรับ precision/ขั้นต่ำ.")
            logger.error("❌ Final order size is zero or negative after adjustments.")
            return False, None


        side = 'buy' if direction == 'long' else 'sell'

        params = {
            'tdMode': 'cross',
            'mgnCcy': 'USDT',
        }

        order = None
        for i in range(3):
            try:
                logger.info(f"⚡️ กำลังเปิด Market Order ({direction.upper()}, Size: {order_size_in_btc:.6f} BTC)... (Attempt {i+1}/3)")
                order = exchange.create_order(SYMBOL, 'market', side, order_size_in_btc, params=params)
                time.sleep(2)
                logger.info(f"✅ Market Order ส่งสำเร็จ: {order.get('id', 'N/A')}")
                break
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                logger.warning(f"⚠️ Error creating market order (Attempt {i+1}/3): {e}. Retrying in 15 seconds...")
                if i == 2:
                    send_telegram(f"⛔️ API Error: ไม่สามารถสร้างออเดอร์ตลาดได้ (Attempt {i+1}/3)\nรายละเอียด: {e}")
                time.sleep(15)
            except Exception as e:
                logger.error(f"❌ Unexpected error creating market order: {e}")
                send_telegram(f"⛔️ Unexpected Error: ไม่สามารถสร้างออเดอร์ตลาดได้\nรายละเอียด: {e}")
                return False, None

        if not order:
            logger.error("❌ Failed to create market order after 3 attempts.")
            send_telegram("⛔️ API Error: ล้มเหลวในการสร้างออเดอร์ตลาดหลังจาก 3 ครั้ง.")
            return False, None

        confirmed_pos_info = None
        confirmation_retries = 15 # เพิ่มจำนวน retry ให้มากขึ้น
        confirmation_sleep = 2    # ลดเวลา sleep เล็กน้อย

        for i in range(confirmation_retries):
            logger.info(f"⏳ รอการยืนยันโพซิชัน ({i+1}/{confirmation_retries})...")
            time.sleep(confirmation_sleep)
            confirmed_pos_info = get_current_position()
            size_tolerance = order_size_in_btc * 0.005
            if confirmed_pos_info and \
               confirmed_pos_info['side'] == direction and \
               abs(confirmed_pos_info['size'] - order_size_in_btc) <= size_tolerance:
                logger.info(f"✅ ยืนยันโพซิชัน Entry Price: {confirmed_pos_info['entry_price']:.2f}, Size: {confirmed_pos_info['size']:.6f}")
                current_position_size = confirmed_pos_info['size']
                return True, confirmed_pos_info['entry_price']
            
        logger.error(f"❌ ไม่สามารถยืนยันโพซิชันและ Entry Price ได้หลังเปิด Market Order (หลังจากพยายาม {confirmation_retries} ครั้ง).")
        send_telegram("⛔️ Error: ไม่สามารถยืนยันโพซิชันหลังเปิดออเดอร์ได้. กรุณาตรวจสอบสถานะใน Exchange โดยด่วน!")

        return False, None

    except Exception as e:
        error_msg = f"❌ Error: ไม่สามารถเปิดออเดอร์ตลาดได้ (General Error)\nรายละเอียด: {e}"
        send_telegram(error_msg)
        logger.error(f"❌ Market order failed: {e}", exc_info=True)
        return False, None

# ==============================================================================
# 11. ฟังก์ชันตรวจสอบสถานะ (MONITORING FUNCTIONS)
# ==============================================================================

def monitor_position(pos_info: dict | None, current_price: float):
    """ตรวจสอบสถานะโพซิชันปัจจุบันและจัดการ Stop Loss."""
    global current_position, sl_moved, entry_price, current_position_size
    global monthly_stats

    if not pos_info:
        if current_position:
            logger.info(f"ℹ️ โพซิชัน {current_position.upper()} ถูกปิดแล้ว.")

            closed_price = current_price
            pnl_usdt_actual = 0.0

            if entry_price and current_position_size:
                if current_position == 'long':
                    pnl_usdt_actual = (closed_price - entry_price) * current_position_size
                else:
                    pnl_usdt_actual = (entry_price - closed_price) * current_position_size

            close_reason = "ปิดโดยไม่ทราบสาเหตุ"
            emoji = "❓"

            tp_sl_be_tolerance_points = entry_price * TP_SL_BE_PRICE_TOLERANCE_PERCENT if entry_price else 0

            if current_position == 'long' and entry_price:
                if closed_price >= (entry_price + TP_VALUE_POINTS) - tp_sl_be_tolerance_points:
                    close_reason = "TP"
                    emoji = "✅"
                elif sl_moved and abs(closed_price - (entry_price + BE_SL_BUFFER_POINTS)) <= tp_sl_be_tolerance_points:
                     close_reason = "SL (กันทุน)"
                     emoji = "🛡️"
                elif closed_price <= (entry_price - SL_VALUE_POINTS) + tp_sl_be_tolerance_points:
                    close_reason = "SL"
                    emoji = "❌"
            elif current_position == 'short' and entry_price:
                if closed_price <= (entry_price - TP_VALUE_POINTS) + tp_sl_be_tolerance_points:
                    close_reason = "TP"
                    emoji = "✅"
                elif sl_moved and abs(closed_price - (entry_price - BE_SL_BUFFER_POINTS)) <= tp_sl_be_tolerance_points:
                     close_reason = "SL (กันทุน)"
                     emoji = "🛡️"
                elif closed_price >= (entry_price + SL_VALUE_POINTS) - tp_sl_be_tolerance_points:
                    close_reason = "SL"
                    emoji = "❌"

            send_telegram(f"{emoji} <b>ปิดออเดอร์ด้วย {close_reason}</b>\n<b>PnL (ประมาณ):</b> <code>{pnl_usdt_actual:,.2f} USDT</code>")
            logger.info(f"✅ โพซิชันปิด: {close_reason}, PnL (ประมาณ): {pnl_usdt_actual:.2f}")
            add_trade_result(close_reason, pnl_usdt_actual)

            current_position = None
            entry_price = None
            current_position_size = 0.0
            sl_moved = False
            monthly_stats['last_ema_cross_signal'] = None
            save_monthly_stats()

        return

    current_position = pos_info['side']
    entry_price = pos_info['entry_price']
    unrealized_pnl = pos_info['unrealized_pnl']
    current_position_size = pos_info['size']

    logger.info(f"📊 สถานะปัจจุบัน: {current_position.upper()}, PnL: {unrealized_pnl:,.2f} USDT, ราคา: {current_price:,.1f}, เข้า: {entry_price:,.1f}")

    pnl_in_points = 0
    if current_position == 'long':
        pnl_in_points = current_price - entry_price
    elif current_position == 'short':
        pnl_in_points = entry_price - current_price

    if not sl_moved and pnl_in_points >= BE_PROFIT_TRIGGER_POINTS:
        logger.info(f"ℹ️ กำไรถึงจุดเลื่อน SL: {pnl_in_points:,.0f} จุด (PnL: {unrealized_pnl:,.2f} USDT)")
        move_sl_to_breakeven(current_position, entry_price)

# ==============================================================================
# 12. ฟังก์ชันรายงานประจำเดือน (MONTHLY REPORT FUNCTIONS)
# ==============================================================================
def monthly_report():
    """ส่งรายงานสถิติการเทรดประจำเดือนไปยัง Telegram."""
    global last_monthly_report_date, monthly_stats

    now = datetime.now()
    current_month_year = now.strftime('%Y-%m')

    if last_monthly_report_date and \
       last_monthly_report_date.year == now.year and \
       last_monthly_report_date.month == now.month:
        logger.debug(f"ℹ️ รายงานประจำเดือนสำหรับ {current_month_year} ถูกส่งไปแล้ว.")
        return

    try:
        balance = get_portfolio_balance()

        if monthly_stats['month_year'] != current_month_year:
            logger.info(f"🆕 สถิติประจำเดือนที่ใช้ไม่ตรงกับเดือนนี้ ({monthly_stats['month_year']} vs {current_month_year}). กำลังรีเซ็ตสถิติเพื่อรายงานเดือนนี้.")
            reset_monthly_stats()

        tp_count = monthly_stats['tp_count']
        sl_count = monthly_stats['sl_count']
        total_pnl = monthly_stats['total_pnl']
        pnl_from_start = balance - initial_balance if initial_balance > 0 else 0.0

        message = f"""📊 <b>รายงานสรุปผลประจำเดือน - {now.strftime('%B %Y')}</b>
<b>🔹 กำไรสุทธิเดือนนี้:</b> <code>{total_pnl:+,.2f} USDT</code>
<b>🔹 SL:</b> <code>{sl_count} ครั้ง</code>
<b>🔹 TP:</b> <code>{tp_count} ครั้ง</code>
<b>🔹 คงเหลือปัจจุบัน:</b> <code>{balance:,.2f} USDT</code>
<b>🔹 กำไร/ขาดทุนรวมจากยอดเริ่มต้น:</b> <code>{pnl_from_start:+,.2f} USDT</code>
<b>⏱ บอทยังทำงานปกติ</b> ✅
<b>เวลา:</b> <code>{now.strftime('%H:%M')}</code>"""

        send_telegram(message)
        last_monthly_report_date = now.date()
        monthly_stats['last_report_month_year'] = current_month_year
        save_monthly_stats()
        logger.info("✅ ส่งรายงานประจำเดือนแล้ว.")

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการส่งรายงานประจำเดือน: {e}", exc_info=True)
        send_telegram(f"⛔️ Error: ไม่สามารถส่งรายงานประจำเดือนได้\nรายละเอียด: {e}")

def monthly_report_scheduler():
    """ตั้งเวลาสำหรับส่งรายงานประจำเดือน."""
    global last_monthly_report_date
    
    logger.info("⏰ เริ่ม Monthly Report Scheduler.")
    while True:
        now = datetime.now()
        
        report_day = min(MONTHLY_REPORT_DAY, calendar.monthrange(now.year, now.month)[1])
        
        next_report_time = now.replace(day=report_day, hour=MONTHLY_REPORT_HOUR, minute=MONTHLY_REPORT_MINUTE, second=0, microsecond=0)

        if now >= next_report_time:
            if last_monthly_report_date is None or \
               last_monthly_report_date.year != now.year or \
               last_monthly_report_date.month != now.month:
                 logger.info(f"⏰ ตรวจพบว่าถึงเวลาส่งรายงานประจำเดือน ({now.strftime('%H:%M')}) และยังไม่ได้ส่งสำหรับเดือนนี้. กำลังส่ง...")
                 monthly_report()
            
            if next_report_time.month == 12:
                next_report_time = next_report_time.replace(year=next_report_time.year + 1, month=1)
            else:
                next_report_time = next_report_time.replace(month=next_report_time.month + 1)
            
            max_day_in_next_month = calendar.monthrange(next_report_time.year, next_report_time.month)[1]
            report_day_for_next_month = min(MONTHLY_REPORT_DAY, max_day_in_next_month)
            next_report_time = next_report_time.replace(day=report_day_for_next_month)


        time_to_wait = (next_report_time - datetime.now()).total_seconds()
        if time_to_wait > 0:
            logger.info(f"⏰ กำหนดส่งรายงานประจำเดือนถัดไปในอีก {int(time_to_wait / 86400)} วัน {int((time_to_wait % 86400) / 3600)} ชั่วโมง {int((time_to_wait % 3600) / 60)} นาที.")
            time.sleep(max(time_to_wait, 60))
        else:
            time.sleep(60)

# ==============================================================================
# 13. ฟังก์ชันเริ่มต้นบอท (BOT STARTUP FUNCTIONS)
# ==============================================================================
def send_startup_message():
    """ส่งข้อความแจ้งเตือนเมื่อบอทเริ่มทำงาน."""
    global initial_balance

    try:
        initial_balance = get_portfolio_balance()
        startup_time = datetime.now().strftime('%d/%m/%Y %H:%M:%S')

        message = f"""🔄 <b>บอทเริ่มทำงาน</b>
<b>🤖 EMA Cross Trading Bot</b>
<b>💰 ยอดเริ่มต้น:</b> <code>{initial_balance:,.2f} USDT</code>
<b>⏰ เวลาเริ่ม:</b> <code>{startup_time}</code>
<b>📊 เฟรม:</b> <code>{TIMEFRAME}</code> | <b>Leverage:</b> <code>{LEVERAGE}x</code>
<b>🎯 TP:</b> <code>{TP_VALUE_POINTS}</code> | <b>SL:</b> <code>{SL_VALUE_POINTS}</code>
<b>🔧 ใช้เงิน:</b> <code>{int(PORTFOLIO_PERCENT_TRADE * 100)}%</code> ของพอร์ตต่อออเดอร์
<b>📈 รอสัญญาณ EMA Cross...</b>"""

        send_telegram(message)
        logger.info("✅ ส่งข้อความแจ้งเตือนเมื่อบอทเริ่มทำงาน.")

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการส่งข้อความเริ่มต้น: {e}", exc_info=True)

# ==============================================================================
# 14. ฟังก์ชันหลักของบอท (MAIN BOT LOGIC)
# ==============================================================================
def main():
    """ฟังก์ชันหลักที่รัน Bot."""
    global monthly_stats

    try:
        load_monthly_stats()
        send_startup_message()

        monthly_thread = threading.Thread(target=monthly_report_scheduler, daemon=True)
        monthly_thread.start()

    except Exception as e:
        error_msg = f"⛔️ Error: ไม่สามารถเริ่มต้นบอทได้\nรายละเอียด: {e} | บอทจะลองเริ่มต้นใหม่ใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
        send_telegram(error_msg)
        logger.critical(f"❌ Startup error: {e}", exc_info=True)
        time.sleep(ERROR_RETRY_SLEEP_SECONDS)
        return

    logger.info("🚀 บอทเข้าสู่ Main Loop แล้ว...")
    while True:
        try:
            # <<-- เพิ่มบรรทัดนี้: Log ที่จุดเริ่มต้นของแต่ละรอบ Main Loop เพื่อยืนยันว่าบอททำงานอยู่
            logger.info(f"🔄 เริ่มรอบ Main Loop ({datetime.now().strftime('%H:%M:%S')})")
            
            # --- ดึงข้อมูลสถานะและราคาปัจจุบัน ---
            current_pos_info = get_current_position()

            ticker = None
            try:
                ticker = exchange.fetch_ticker(SYMBOL)
                # ลดเวลา sleep ตรงนี้ลงเล็กน้อย เพื่อให้รอบการทำงานของบอทเร็วขึ้น (แต่ยังคงอยู่ในขีดจำกัด rate limit)
                # หรืออาจจะไม่ต้อง sleep เลยก็ได้ ถ้ามั่นใจว่า fetch_ticker ไม่เกิน rate limit
                # time.sleep(1) 
            except Exception as e:
                logger.warning(f"⚠️ Error fetching ticker: {e}. Retrying in {ERROR_RETRY_SLEEP_SECONDS} seconds...")
                send_telegram(f"⛔️ API Error: ไม่สามารถดึงราคาล่าสุดได้. รายละเอียด: {e.args[0] if e.args else str(e)}")
                time.sleep(ERROR_RETRY_SLEEP_SECONDS) # ใช้เวลาหน่วงสำหรับ Error
                continue

            if not ticker or 'last' not in ticker:
                logger.error("❌ Failed to fetch valid ticker. Skipping loop and retrying.")
                send_telegram("⛔️ Error: ไม่สามารถดึงราคาล่าสุดได้ถูกต้อง. Skipping.")
                time.sleep(ERROR_RETRY_SLEEP_SECONDS) # ใช้เวลาหน่วงสำหรับ Error
                continue

            current_price = float(ticker['last'])
            logger.info(f"💲 ราคาปัจจุบันของ {SYMBOL}: {current_price:,.1f}")

            # --- ตรวจสอบและจัดการโพซิชันปัจจุบัน ---
            monitor_position(current_pos_info, current_price)

            # --- ตรวจสอบสัญญาณและเปิดออเดอร์ (ถ้าไม่มีโพซิชัน) ---
            if not current_pos_info:
                signal = check_ema_cross()

                if signal:
                    logger.info(f"🌟 ตรวจพบสัญญาณ EMA Cross: {signal.upper()}")

                    if signal != monthly_stats.get('last_ema_cross_signal'):
                        logger.info(f"✨ สัญญาณ {signal.upper()} ใหม่ที่ถูกต้อง. กำลังพยายามเปิดออเดอร์.")

                        market_order_success, confirmed_entry_price = open_market_order(signal, current_price)

                        if market_order_success and confirmed_entry_price:
                            set_tpsl_success = set_tpsl_for_position(signal, confirmed_entry_price)

                            if set_tpsl_success:
                                monthly_stats['last_ema_cross_signal'] = signal
                                save_monthly_stats()
                                logger.info(f"✅ เปิดออเดอร์ {signal.upper()} และตั้ง TP/SL สำเร็จ.")
                            else:
                                logger.error(f"❌ เปิดออเดอร์ {signal.upper()} ได้ แต่ตั้ง TP/SL ไม่สำเร็จ. กรุณาตรวจสอบและปิดออเดอร์ด้วยตนเอง!")
                                send_telegram(f"⛔️ <b>ข้อผิดพลาดร้ายแรง:</b> เปิดออเดอร์ {signal.upper()} ได้ แต่ตั้ง TP/SL ไม่สำเร็จ. โพซิชันไม่มี SL/TP! โปรดจัดการด้วยตนเอง!")
                        else:
                            logger.warning(f"⚠️ ไม่สามารถเปิด Market Order {signal.upper()} ได้.")
                    else:
                        logger.info(f"ℹ️ สัญญาณ {signal.upper()} ซ้ำซ้อนกับสัญญาณล่าสุด. ข้ามการเปิดออเดอร์.")
                else:
                    logger.debug("🔎 ไม่มีสัญญาณ EMA Cross.")
            else:
                logger.debug(f"Current Position: {current_pos_info['side'].upper()}. รอการปิดหรือเลื่อน SL.")

            time.sleep(MAIN_LOOP_SLEEP_SECONDS)

        except KeyboardInterrupt:
            logger.info("🛑 บอทหยุดทำงานโดยผู้ใช้ (KeyboardInterrupt).")
            send_telegram("🛑 Bot หยุดทำงานโดยผู้ใช้.")
            break
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            error_msg = f"⛔️ Error: API Error\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
            logger.error(error_msg, exc_info=True)
            send_telegram(error_msg)
            time.sleep(ERROR_RETRY_SLEEP_SECONDS)
        except Exception as e:
            # <<-- บล็อกนี้จะจับ Error อื่นๆ ที่ไม่ได้เกี่ยวกับ Network/Exchange
            error_msg = f"⛔️ Error: เกิดข้อผิดพลาดที่ไม่คาดคิดใน Main Loop\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
            logger.error(error_msg, exc_info=True) # exc_info=True จะแสดง Traceback เต็มๆ
            send_telegram(error_msg)
            time.sleep(ERROR_RETRY_SLEEP_SECONDS)

# ==============================================================================
# 15. จุดเริ่มต้นการทำงานของโปรแกรม (ENTRY POINT)
# ==============================================================================
if __name__ == '__main__':
    main()
