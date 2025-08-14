import ccxt
import time
import requests
from datetime import datetime, timedelta
import logging
import threading
import json
import os
import calendar
import sys
import math

# ==============================================================================
# 1. ตั้งค่าพื้นฐาน (CONFIGURATION)
# ==============================================================================

# --- API Keys & Credentials (ดึงจาก Environment Variables เพื่อความปลอดภัย) ---
API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('BINANCE_SECRET', 'YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING')

# --- Trade Parameters ---
SYMBOL = 'BTC/USDT:USDT' # ใช้ 'BTC/USDT:USDT' ตามที่ Exchange คืนมาใน get_current_position()
TIMEFRAME = '1h'
LEVERAGE = 30
TP_DISTANCE_POINTS = 1111 #❤️‍🩹ยกเลิกไปก่อน
SL_DISTANCE_POINTS = 1234

# --- Trailing Stop Loss Parameters (3 Steps) ---
# 📈สำหรับ Long Position: (ราคาวิ่งขึ้น)
TRAIL_SL_STEP1_TRIGGER_LONG_POINTS = 300
TRAIL_SL_STEP1_NEW_SL_POINTS_LONG = -700

TRAIL_SL_STEP2_TRIGGER_LONG_POINTS = 500
TRAIL_SL_STEP2_NEW_SL_POINTS_LONG = 460
# เพิ่มพารามิเตอร์ SL Step 3 (TP จำลอง) ใหม่:
TRAIL_SL_STEP3_TRIGGER_LONG_POINTS = 700  # + points จาก entry
TRAIL_SL_STEP3_NEW_SL_POINTS_LONG = 650   # ตั้ง SL ที่ + points (เหมือน TP)

# 📉สำหรับ Short Position: (ราคาวิ่งลง)
TRAIL_SL_STEP1_TRIGGER_SHORT_POINTS = 300
TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT = 700

TRAIL_SL_STEP2_TRIGGER_SHORT_POINTS = 500
TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT = -460

TRAIL_SL_STEP3_TRIGGER_SHORT_POINTS = 700 # - points จาก entry  
TRAIL_SL_STEP3_NEW_SL_POINTS_SHORT = -650 # ตั้ง SL ที่ - points (เหมือน TP)

#⏳ระบบเตือน Manual TP
MANUAL_TP_ALERT_THRESHOLD = 1000  # แจ้งเตือนเมื่อกำไรเกิน...ให้ปิดด้วยมือ
MANUAL_TP_ALERT_INTERVAL = 600   # แจ้งเตือนซ้ำทุก..วินาที

CROSS_THRESHOLD_POINTS = 1 #ระยะการตัดของema
# --- EMA Parameters ---
EMA_FAST_PERIOD = 9 #📉
EMA_SLOW_PERIOD = 50 #📈

# --- Risk Management ---
MARGIN_BUFFER_USDT = 5
TARGET_POSITION_SIZE_FACTOR = 0.8 # ใช้ 0.8 (80%) ของ Equity ที่ใช้ได้ทั้งหมด

# --- Order Confirmation & Stability ---
CONFIRMATION_RETRIES = 15
CONFIRMATION_SLEEP = 5
TP_SL_BE_PRICE_TOLERANCE_PERCENT = 0.005

# --- Telegram Notification Settings ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# --- Files & Paths ---
STATS_FILE = 'trading_stats.json'

# --- Bot Timing (แยกจังหวะเวลา) ---
FAST_LOOP_INTERVAL_SECONDS = 3 # ⏰สำหรับการจัดการออเดอร์, TP/SL (เร็วขึ้น)
EMA_CALC_INTERVAL_SECONDS = 180 # ⏰สำหรับการคำนวณ EMA และหา Cross Signal (ช้าลง)
TRADE_COOLDOWN_SECONDS = 180 # ⏰เพิ่ม: ระยะเวลา Cooldown
ERROR_RETRY_SLEEP_SECONDS = 60
MONTHLY_REPORT_DAY = 20 #วันที่สรุปรายเดือน
MONTHLY_REPORT_HOUR = 0 #สรุปราย ช.ม. ✖️ไม่ใช่
MONTHLY_REPORT_MINUTE = 5

# ==============================================================================
# 2. การตั้งค่า Logging
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
for handler in logging.root.handlers:
    if isinstance(handler, logging.StreamHandler):
        handler.flush = lambda: sys.stdout.flush()

logger = logging.getLogger(__name__)


# ==============================================================================
# 3. ตัวแปรสถานะการเทรด (GLOBAL TRADE STATE VARIABLES)
# ==============================================================================
current_position_details = None
portfolio_balance = 0.0
last_monthly_report_date = None
initial_balance = 0.0
last_ema_position_status = None
last_ema_calc_time = datetime.min
last_trade_closed_time = datetime.min # *** เพิ่ม: ตัวแปรสำหรับเวลาที่ปิดเทรดล่าสุด **
waiting_for_cooldown = False  # เพิ่มบรรทัดนี้
last_manual_tp_alert_time = datetime.min  # เก็บเวลาที่แจ้งเตือนครั้งล่าสุด
# กันปัญหาเปิดซ้ำโดยไม่ได้มี cross ใหม่
must_wait_new_cross = False
last_cross_bar_time = None
last_entry_cross_bar_time = None

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
    'last_ema_cross_signal': None,
    'last_ema_position_status': None
}

# ==============================================================================
# 5. การตั้งค่า Exchange (CCXT EXCHANGE SETUP)
# ==============================================================================
exchange = None
market_info = None

def setup_exchange():
    global exchange, market_info
    try:
        if not API_KEY or API_KEY == 'YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING' or \
           not SECRET or SECRET == 'YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING':
            raise ValueError("API_KEY หรือ SECRET ไม่ถูกตั้งค่าใน Environment Variables. โปรดแก้ไข.")

        exchange = ccxt.binance({
            'apiKey': API_KEY,
            'secret': SECRET,
            'sandbox': False,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
                'marginMode': 'cross',
            },
            'verbose': False,
            'timeout': 30000,
        })

        exchange.load_markets()
        logger.info("✅ เชื่อมต่อกับ Binance Futures Exchange สำเร็จ และโหลด Markets แล้ว.")

        market_info = exchange.market(SYMBOL)
        if not market_info:
            raise ValueError(f"ไม่พบข้อมูลตลาดสำหรับสัญลักษณ์ {SYMBOL}")

        if 'limits' not in market_info: market_info['limits'] = {}
        if 'amount' not in market_info['limits']: market_info['limits']['amount'] = {}
        if 'cost' not in market_info['limits']: market_info['limits']['cost'] = {}

        amount_step_val = market_info['limits']['amount'].get('step')
        amount_min_val = market_info['limits']['amount'].get('min')
        amount_max_val = market_info['limits']['amount'].get('max')
        cost_min_val = market_info['limits']['cost'].get('min')
        cost_max_val = market_info['limits']['cost'].get('max')

        market_info['limits']['amount']['step'] = float(amount_step_val) if amount_step_val is not None else 0.001
        market_info['limits']['amount']['min'] = float(amount_min_val) if amount_min_val is not None else 0.001
        market_info['limits']['amount']['max'] = float(amount_max_val) if amount_max_val is not None else sys.float_info.max
        market_info['limits']['cost']['min'] = float(cost_min_val) if cost_min_val is not None else 5.0
        market_info['limits']['cost']['max'] = float(cost_max_val) if cost_max_val is not None else sys.float_info.max

        logger.info(f"💡 Market info limits for {SYMBOL}: Amount step={market_info['limits']['amount']['step']}, min={market_info['limits']['amount']['min']}; Cost min={market_info['limits']['cost']['min']}")

        try:
            result = exchange.set_leverage(LEVERAGE, SYMBOL)
            logger.info(f"✅ ตั้งค่า Leverage เป็น {LEVERAGE}x สำหรับ {SYMBOL}: {result}")
        except ccxt.ExchangeError as e:
            if "leverage is not valid" in str(e) or "not valid for this symbol" in str(e):
                logger.critical(f"❌ Error: Leverage {LEVERAGE}x ไม่ถูกต้องสำหรับ {SYMBOL} บน Binance. โปรดตรวจสอบ Max Allowed Leverage.")
            else:
                logger.critical(f"❌ Error ในการตั้งค่า Leverage: {e}", exc_info=True)
            send_telegram(f"⛔️ Critical Error: ไม่สามารถตั้งค่า Leverage ได้.\nรายละเอียด: {e}")
            exit()

    except ValueError as ve:
        logger.critical(f"❌ Configuration Error: {ve}", exc_info=True)
        send_telegram(f"⛔️ Critical Error: การตั้งค่าเริ่มต้นผิดพลาด.\nรายละเอียด: {ve}")
        exit()
    except Exception as e:
        logger.critical(f"❌ ไม่สามารถเชื่อมต่อหรือโหลดข้อมูล Exchange เบื้องต้นได้: {e}", exc_info=True)
        send_telegram(f"⛔️ Critical Error: ไม่สามารถเชื่อมต่อ Exchange ได้\nรายละเอียด: {e}")
        exit()

# ==============================================================================
# 6. ฟังก์ชันจัดการสถิติ (STATISTICS MANAGEMENT FUNCTIONS)
# ==============================================================================

def save_monthly_stats():
    global monthly_stats, last_ema_position_status
    try:
        monthly_stats['last_ema_position_status'] = last_ema_position_status
        with open(os.path.join(os.getcwd(), STATS_FILE), 'w', encoding='utf-8') as f:
            json.dump(monthly_stats, f, indent=4)
        logger.debug(f"💾 บันทึกสถิติการเทรดลงไฟล์ {STATS_FILE} สำเร็จ")
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการบันทึกสถิติ: {e}")

def reset_monthly_stats():
    global monthly_stats, last_ema_position_status
    monthly_stats['month_year'] = datetime.now().strftime('%Y-%m')
    monthly_stats['tp_count'] = 0
    monthly_stats['sl_count'] = 0
    monthly_stats['total_pnl'] = 0.0
    monthly_stats['trades'] = []
    save_monthly_stats()
    logger.info(f"🔄 รีเซ็ตสถิติประจำเดือนสำหรับเดือน {monthly_stats['month_year']}")

def load_monthly_stats():
    global monthly_stats, last_monthly_report_date, last_ema_position_status
    stats_file_path = os.path.join(os.getcwd(), STATS_FILE)
    try:
        if os.path.exists(stats_file_path):
            with open(stats_file_path, 'r', encoding='utf-8') as f:
                loaded_stats = json.load(f)

            monthly_stats['month_year'] = loaded_stats.get('month_year', None)
            monthly_stats['tp_count'] = loaded_stats.get('tp_count', 0)
            monthly_stats['sl_count'] = loaded_stats.get('sl_count', 0)
            monthly_stats['total_pnl'] = loaded_stats.get('total_pnl', 0.0)
            monthly_stats['trades'] = loaded_stats.get('trades', [])
            monthly_stats['last_report_month_year'] = loaded_stats.get('last_report_month_year', None)
            monthly_stats['last_ema_cross_signal'] = loaded_stats.get('last_ema_cross_signal', None)
            last_ema_position_status = loaded_stats.get('last_ema_position_status', None)

            logger.info(f"💾 โหลดสถิติการเทรดจากไฟล์ {STATS_FILE} สำเร็จ")

            if monthly_stats['last_report_month_year']:
                try:
                    year, month = map(int, monthly_stats['last_report_month_year'].split('-'))
                    last_monthly_report_date = datetime(year, month, 1).date()
                except ValueError:
                    logger.warning("⚠️ รูปแบบวันที่ last_report_report_month_year ในไฟล์ไม่ถูกต้อง. จะถือว่ายังไม่มีการส่งรายงาน.")
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
        logger.error(f"❌ เกิดข้อผิดพลาดในการโหลดสถิติ: {e}", exc_info=True)
        if not os.access(os.path.dirname(stats_file_path) or '.', os.W_OK):
            logger.critical(f"❌ ข้อผิดพลาด: ไม่มีสิทธิ์เขียนไฟล์ในไดเรกทอรี: {os.path.dirname(stats_file_path) or '.'}. โปรดตรวจสอบสิทธิ์การเข้าถึงหรือเปลี่ยน STATS_FILE.")

        monthly_stats = {
            'month_year': None, 'tp_count': 0, 'sl_count': 0, 'total_pnl': 0.0, 'trades': [],
            'last_report_month_year': None, 'last_ema_cross_signal': None, 'last_ema_position_status': None
        }
        last_monthly_report_date = None
        last_ema_position_status = None
        reset_monthly_stats()

def add_trade_result(reason: str, pnl: float):
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
    global portfolio_balance
    retries = 3
    for i in range(retries):
        try:
            logger.debug(f"🔍 กำลังดึงยอดคงเหลือ (Attempt {i+1}/{retries})...")
            balance = exchange.fetch_balance()
            time.sleep(0.5)

            free_usdt = balance.get('USDT', {}).get('free', 0)
            if free_usdt == 0:
                for asset_info in balance.get('info', {}).get('assets', []):
                    if asset_info.get('asset') == 'USDT':
                        free_usdt = float(asset_info.get('availableBalance', 0))
                        break

            portfolio_balance = float(free_usdt)
            logger.info(f"💰 ยอดคงเหลือ USDT: {portfolio_balance:,.2f}")
            return portfolio_balance

        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.warning(f"⚠️ Error fetching balance (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
            if i == retries - 1:
                send_telegram(f"⛔️ API Error: ไม่สามารถดึงยอดคงเหลือได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
            time.sleep(15)
        except Exception as e:
            logger.error(f"❌ Unexpected error in get_portfolio_balance: {e}", exc_info=True)
            send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึงยอดคงเหลือได้\nรายละเอียด: {e}")
            return 0.0
    logger.error(f"❌ Failed to fetch balance after {retries} attempts.")
    send_telegram(f"⛔️ API Error: ล้มเหลวในการดึงยอดคงเหลือหลังจาก {retries} ครั้ง.")
    return 0.0

def get_current_position() -> dict | None:
    retries = 5
    for i in range(retries):
        try:
            logger.info(f"🔍 กำลังดึงโพซิชันปัจจุบันจาก Exchange (Attempt {i+1}/{retries})...")
            time.sleep(1)
            positions = exchange.fetch_positions([SYMBOL])
            logger.info(f"INFO: Raw positions fetched from Exchange (Attempt {i+1}): {positions}")
            time.sleep(0.5)

            for pos in positions:
                if 'symbol' in pos and 'contracts' in pos:
                    pos_amount = float(pos.get('contracts', 0) or pos.get('positionAmt', 0))
                    pos_side = pos.get('side') # 'long' or 'short'
                    
                    entry_price_val = float(pos.get('entryPrice', 0))
                    unrealized_pnl_val = float(pos.get('unrealizedPnl', 0))
                    liquidation_price_val = float(pos.get('liquidationPrice', 0)) if pos.get('liquidationPrice') else None

                    if pos['symbol'] == SYMBOL and pos_amount != 0 and pos_side:
                        logger.info(f"✅ พบโพซิชัน {pos['symbol']}: Side={pos_side}, Contracts={abs(pos_amount):,.8f}, Entry={entry_price_val:,.2f}")
                        return {
                            'symbol': pos['symbol'],
                            'side': pos_side,
                            'contracts': abs(pos_amount),
                            'entry_price': entry_price_val,
                            'unrealized_pnl': unrealized_pnl_val,
                            'liquidation_price': liquidation_price_val,
                            'info': pos
                        }
                else:
                    logger.debug(f"DEBUG: Skipping position entry due to missing 'symbol' or 'contracts': {pos}")

            logger.info(f"ℹ️ ไม่พบโพซิชันที่เปิดอยู่บน Exchange สำหรับ SYMBOL นี้หลังจากตรวจสอบ {len(positions)} รายการ.")
            return None
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.warning(f"⚠️ Error fetching positions (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
            if i == retries - 1:
                send_telegram(f"⛔️ API Error: ไม่สามารถดึงโพซิชันได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
            time.sleep(15)
        except Exception as e:
            logger.error(f"❌ Unexpected error in get_current_position: {e}", exc_info=True)
            send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึงโพซิชันได้\nรายละเอียด: {e}")
            return None
    logger.error(f"❌ Failed to fetch positions after {retries} attempts.")
    send_telegram(f"⛔️ API Error: ล้มเหลวในการดึงโพซิชันหลังจาก {retries} ครั้ง.")
    return None

# ==============================================================================
# 9. ฟังก์ชันคำนวณ Indicators (INDICATOR CALCULATION FUNCTIONS)
# ==============================================================================

def calculate_ema(prices: list[float], period: int) -> float | None:
    if len(prices) < period:
        return None

    sma = sum(prices[:period]) / period
    ema = sma
    multiplier = 2 / (period + 1)

    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))

    return ema

# แก้ไขฟังก์ชัน check_ema_cross() ใหม่
def check_ema_cross() -> str | None:
    global last_ema_position_status
    global last_cross_bar_time
    
    try:
        retries = 3
        ohlcv = None
        
        # คำนวณจำนวนแท่งเทียนที่ต้องการตาม EMA periods
        min_required_candles = max(EMA_FAST_PERIOD, EMA_SLOW_PERIOD) + 50
        required_limit = max(EMA_FAST_PERIOD, EMA_SLOW_PERIOD) * 4 # ใช้ 4 เท่าเพื่อความปลอดภัย
        
        for i in range(retries):
            logger.debug(f"กำลังดึงข้อมูล OHLCV สำหรับ EMA{EMA_FAST_PERIOD}/{EMA_SLOW_PERIOD} ({i+1}/{retries})...")
            try:
                ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=required_limit)
                time.sleep(0.2) # ลดเวลา sleep
                break
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                logger.warning(f"⚠️ Error fetching OHLCV (Attempt {i+1}/{retries}): {e}. Retrying in 10 seconds...")
                if i == retries - 1:
                    send_telegram(f"⚠️ API Error: ไม่สามารถดึง OHLCV ได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
                time.sleep(10) # ลดเวลา sleep
            except Exception as e:
                logger.error(f"❌ Unexpected error fetching OHLCV: {e}", exc_info=True)
                send_telegram(f"❌ Unexpected Error: ไม่สามารถดึง OHLCV ได้\nรายละเอียด: {e}")
                return None
        
        if not ohlcv or len(ohlcv) < min_required_candles:
            logger.warning(f"ข้อมูล OHLCV ไม่เพียงพอ. ต้องการอย่างน้อย {min_required_candles} แท่ง ได้{len(ohlcv)}")
            send_telegram(f"⚠️ ข้อมูล OHLCV ไม่เพียงพอ ({len(ohlcv)} แท่ง) สำหรับ EMA{EMA_FAST_PERIOD}/{EMA_SLOW_PERIOD}")
            return None
        
        closes = [candle[4] for candle in ohlcv]
        prev_bar_time_ms = ohlcv[-2][0]
        
        # คำนวณ EMA ปัจจุบันและก่อนหน้า
        ema_fast_current = calculate_ema(closes, EMA_FAST_PERIOD)
        ema_slow_current = calculate_ema(closes, EMA_SLOW_PERIOD)
        
        # คำนวณ EMA ก่อนหน้า 1 แท่ง (สำหรับตรวจสอบการตัดกัน)
        ema_fast_previous = calculate_ema(closes[:-1], EMA_FAST_PERIOD)
        ema_slow_previous = calculate_ema(closes[:-1], EMA_SLOW_PERIOD)
        
        logger.info(f"📊 EMA Values: Current EMA{EMA_FAST_PERIOD}={ema_fast_current:,.2f}, EMA{EMA_SLOW_PERIOD}={ema_slow_current:,.2f}")
        logger.info(f"📊 EMA Previous: EMA{EMA_FAST_PERIOD}={ema_fast_previous:,.2f}, EMA{EMA_SLOW_PERIOD}={ema_slow_previous:,.2f}")
        
        if None in [ema_fast_current, ema_slow_current, ema_fast_previous, ema_slow_previous]:
            logger.warning("❌ ค่า EMA ไม่สามารถคำนวณได้ (เป็น None).")
            return None
        
        # กำหนดสถานะ EMA ปัจจุบันและก่อนหน้า
        current_ema_position = 'above' if ema_fast_current > ema_slow_current else 'below'
        previous_ema_position = 'above' if ema_fast_previous > ema_slow_previous else 'below'
        
        # === กรณีที่บอทเพิ่งเริ่มรัน หรือ reset หลังปิดโพซิชัน ===
        if last_ema_position_status is None:
            last_ema_position_status = current_ema_position
            save_monthly_stats()
            logger.info(f"🔄 บอทเพิ่งเริ่มรัน/รีเซ็ต. บันทึกสถานะ EMA{EMA_FAST_PERIOD}/{EMA_SLOW_PERIOD} เริ่มต้นเป็น: {current_ema_position.upper()}. จะรอสัญญาณการตัดกันครั้งถัดไป.")
            return None
        
        # === ตรวจสอบการตัดกันของ EMA ===
        cross_signal = None
        
        # ตรวจสอบ Golden Cross (EMA Fast ตัดขึ้นข้าม EMA Slow)
        if (previous_ema_position == 'below' and current_ema_position == 'above' and 
            ema_fast_current > (ema_slow_current + CROSS_THRESHOLD_POINTS)):
            cross_signal = 'long'
            last_cross_bar_time = prev_bar_time_ms
            logger.info(f"🟢 Golden Cross Detected: EMA{EMA_FAST_PERIOD} ({ema_fast_current:,.2f}) ตัดขึ้นข้าม EMA{EMA_SLOW_PERIOD} ({ema_slow_current:,.2f}) + {CROSS_THRESHOLD_POINTS} points threshold")
        
        # ตรวจสอบ Death Cross (EMA Fast ตัดลงข้าม EMA Slow)
        elif (previous_ema_position == 'above' and current_ema_position == 'below' and 
              ema_fast_current < (ema_slow_current - CROSS_THRESHOLD_POINTS)):
            cross_signal = 'short'
            last_cross_bar_time = prev_bar_time_ms
            logger.info(f"🔴 Death Cross Detected: EMA{EMA_FAST_PERIOD} ({ema_fast_current:,.2f}) ตัดลงข้าม EMA{EMA_SLOW_PERIOD} ({ema_slow_current:,.2f}) - {CROSS_THRESHOLD_POINTS} points threshold")
        
        # อัปเดตสถานะ EMA ปัจจุบัน
        if current_ema_position != last_ema_position_status:
            logger.info(f"🔄 EMA position changed from {last_ema_position_status.upper()} to {current_ema_position.upper()}")
            last_ema_position_status = current_ema_position
            save_monthly_stats()
        
        # แสดงผลสัญญาณ
        if cross_signal is not None:
            logger.info(f"🎯 สัญญาณ EMA{EMA_FAST_PERIOD}/{EMA_SLOW_PERIOD} Cross ที่ตรวจพบ: {cross_signal.upper()}")
            return cross_signal
        else:
            logger.info(f"⏳ ไม่พบสัญญาณ EMA{EMA_FAST_PERIOD}/{EMA_SLOW_PERIOD} Cross ที่ชัดเจน. สถานะปัจจุบัน: {current_ema_position.upper()}")
            return None
            
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการคำนวณ EMA: {e}", exc_info=True)
        send_telegram(f"❌ Error: ไม่สามารถคำนวณ EMA ได้\nรายละเอียด: {e}")
        return None
        
# ฟังก์ชันเสริมสำหรับการปรับเปลี่ยนค่า EMA ระหว่างการทำงาน (ถ้าต้องการ)
def update_ema_parameters(fast_period: int, slow_period: int):
    """
    ปรับเปลี่ยนค่า EMA periods ระหว่างการทำงาน
    fast_period: ค่า EMA ที่เร็วกว่า (เช่น 10, 20, 50)
    slow_period: ค่า EMA ที่ช้ากว่า (เช่น 50, 100, 200)
    """
    global EMA_FAST_PERIOD, EMA_SLOW_PERIOD, last_ema_position_status
    
    if fast_period >= slow_period:
        logger.error("Fast EMA period ต้องน้อยกว่า Slow EMA period")
        return False
    
    if fast_period < 1 or slow_period < 1:
        logger.error("EMA periods ต้องมากกว่า 0")
        return False
    
    old_fast = EMA_FAST_PERIOD
    old_slow = EMA_SLOW_PERIOD
    
    EMA_FAST_PERIOD = fast_period
    EMA_SLOW_PERIOD = slow_period
    last_ema_position_status = None  # รีเซ็ตสถานะเมื่อเปลี่ยนค่า EMA
    
    logger.info(f"🔄 อัปเดตค่า EMA จาก {old_fast}/{old_slow} เป็น {EMA_FAST_PERIOD}/{EMA_SLOW_PERIOD}")
    send_telegram(f"🔄 เปลี่ยนค่า EMA เป็น {EMA_FAST_PERIOD}/{EMA_SLOW_PERIOD}\nรีเซ็ตสถานะ EMA แล้ว")
    return True

# ==============================================================================
# 10. ฟังก์ชันช่วยสำหรับการคำนวณและตรวจสอบออเดอร์
# ==============================================================================

def round_to_precision(value: float, precision_type: str) -> float:
    """ปัดค่าให้เป็นไปตาม Precision ที่ Exchange กำหนด"""
    if market_info and 'precision' in market_info and precision_type in market_info['precision']:
        return float(exchange.decimal_to_precision(value, ccxt.ROUND, market_info['precision'][precision_type]))
    else:
        logger.warning(f"⚠️ ไม่พบ Precision สำหรับ '{precision_type}'. ใช้ round() ปกติ.")
        return round(value, 8)

def set_sl_only_for_position(direction: str, amount: float, sl_price: float) -> bool:
    """
    ตั้งแค่ Stop Loss อย่างเดียว (ไม่ตั้ง TP)
    จะยกเลิก TP/SL ที่เปิดอยู่ก่อนเสมอแล้วตั้งใหม่
    """
    if not amount or amount <= 0:
        logger.error("ไม่สามารถตั้ง SL ได้: ขนาดโพซิชันเป็น 0 หรือไม่ถูกต้อง")
        return False
    
    # ยกเลิก TP/SL เก่าก่อน
    cancel_all_open_tp_sl_orders()
    time.sleep(2)  # เพิ่ม delay ให้ยกเลิกเสร็จสิ้น
    
    # ตรวจสอบ market_info
    if not market_info or 'precision' not in market_info:
        logger.error("ไม่พบข้อมูล market_info หรือ precision. ไม่สามารถตั้ง SL ได้")
        return False
    
    try:
        # ปัดเศษราคาให้ถูกต้องตาม Exchange
        market_info_precision_price = 'price'
        sl_price_formatted = round_to_precision(sl_price, market_info_precision_price)
        
        logger.info(f"กำลังตั้ง SL: {sl_price_formatted:,.2f} สำหรับ {direction.upper()}")
        
        # ตรวจสอบความถูกต้องของราคา SL
        current_price = exchange.fetch_ticker(SYMBOL)['last']
        
        if direction == 'long':
            if sl_price_formatted >= current_price:
                logger.error(f"❌ SL price ({sl_price_formatted}) ต้องต่ำกว่าราคาปัจจุบัน ({current_price}) สำหรับ Long")
                return False
        else:  # short
            if sl_price_formatted <= current_price:
                logger.error(f"❌ SL price ({sl_price_formatted}) ต้องสูงกว่าราคาปัจจุบัน ({current_price}) สำหรับ Short")
                return False
        
        sl_side = 'sell' if direction == 'long' else 'buy'
        
        # ตั้ง Stop Loss Order
        try:
            sl_order = exchange.create_order(
                symbol=SYMBOL,
                type='STOP_MARKET',
                side=sl_side,
                amount=amount,
                price=None,
                params={
                    'stopPrice': sl_price_formatted,
                    'reduceOnly': True,
                }
            )
            logger.info(f"🛑 Stop Loss order placed: ID → {sl_order.get('id', 'N/A')}, Price: {sl_price_formatted}")
            
        except Exception as sl_error:
            logger.error(f"Error setting Stop Loss: {str(sl_error)}")
            send_telegram(f"❌ SL Error: ไม่สามารถตั้ง Stop Loss ได้\n{str(sl_error)}")
            return False
        
        # ส่งการแจ้งเตือนสำเร็จ
        send_telegram(
            f"✅ <b>ตั้ง SL สำเร็จ!</b>\n"
            f"📌 <b>SL:</b> <code>{sl_price_formatted:,.2f}</code>\n"
            #f"📊 <b>Direction:</b> {direction.upper()}\n"
            #f"📏 <b>Size:</b> {amount:,.8f}"
        )
        
        return True
        
    except ccxt.BaseError as e:
        logger.error(f"Error setting SL: {str(e)}", exc_info=True)
        send_telegram(f"❌ API Error (SL): {e.args[0] if e.args else str(e)}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error setting SL: {e}", exc_info=True)
        send_telegram(f"❌ Unexpected Error (SL): {e}")
        return False

def calculate_order_details(available_usdt: float, price: float) -> tuple[float, float]:
    """
    คำนวณจำนวนสัญญาที่จะเปิดและ Margin ที่ต้องใช้ โดยพิจารณาจาก Exchange Limits
    และเปอร์เซ็นต์ของเงินทุนในพอร์ตที่ต้องการใช้
    """
    if price <= 0 or LEVERAGE <= 0 or TARGET_POSITION_SIZE_FACTOR <= 0:
        logger.error("Error: Price, leverage, and target_position_size_factor must be positive.")
        return (0, 0)

    if not market_info:
        logger.error(f"❌ Could not retrieve market info for {SYMBOL}. Please ensure setup_exchange ran successfully.")
        return (0, 0)

    try:
        amount_step = market_info['limits']['amount']['step']
        min_exchange_amount = market_info['limits']['amount']['min']
        max_exchange_amount = market_info['limits']['amount']['max']
        min_notional_exchange = market_info['limits']['cost']['min']

    except KeyError as e:
        logger.critical(f"❌ Error accessing market limits for {SYMBOL}: Missing key {e}. Exiting.", exc_info=True)
        send_telegram(f"⛔️ Critical Error: Cannot parse market limits for {SYMBOL}.\nDetails: {e}")
        return (0, 0)
    except (TypeError, ValueError) as e:
        logger.critical(f"❌ Error parsing market limits for {SYMBOL}: Invalid value {e}. Exiting.", exc_info=True)
        send_telegram(f"⛔️ Critical Error: Cannot parse market limits for {SYMBOL}.\nDetails: {e}")
        return (0, 0)

    # *** ส่วนที่แก้ไขเพื่อใช้เปอร์เซ็นต์ของทุนทั้งหมด (80%) ***
    investable_capital = available_usdt - MARGIN_BUFFER_USDT
    if investable_capital <= 0:
        logger.warning(f"❌ Available capital ({available_usdt:,.2f}) is not enough after deducting margin buffer ({MARGIN_BUFFER_USDT:,.2f}) for investment.")
        return (0, 0)

    target_notional_for_order_raw = investable_capital * TARGET_POSITION_SIZE_FACTOR * LEVERAGE

    min_notional_from_min_amount = min_exchange_amount * price
    target_notional_for_order = max(target_notional_for_order_raw, min_notional_exchange, min_notional_from_min_amount)

    if target_notional_for_order_raw < min_notional_exchange:
        logger.info(f"ℹ️ Calculated notional from percentage ({target_notional_for_order_raw:,.2f}) is below exchange minimum ({min_notional_exchange:,.2f}). Will attempt to open at exchange minimum ({target_notional_for_order:,.2f}).")

    # *** สิ้นสุดส่วนแก้ไข ***

    contracts_raw = target_notional_for_order / price
    contracts_to_open = float(exchange.amount_to_precision(SYMBOL, contracts_raw))

    contracts_to_open = max(contracts_to_open, min_exchange_amount)
    contracts_to_open = min(contracts_to_open, max_exchange_amount)

    actual_notional_after_precision = contracts_to_open * price
    required_margin = actual_notional_after_precision / LEVERAGE

    if contracts_to_open == 0:
        logger.warning(f"⚠️ Calculated contracts to open is 0 after all adjustments. (Target notional: {target_notional_for_order:,.2f} USDT, Current price: {price:,.2f}, Min exchange amount: {min_exchange_amount:.8f}). This means calculated size is too small or rounded to zero.")
        return (0, 0)

    if available_usdt < required_margin + MARGIN_BUFFER_USDT:
        logger.error(f"❌ Margin not sufficient. Available: {available_usdt:,.2f}, Required: {required_margin:,.2f} (for trade) + {MARGIN_BUFFER_USDT} (Buffer) = {required_margin + MARGIN_BUFFER_USDT:,.2f} USDT.")
        return (0, 0)

    logger.info(f"💡 Order Calculation Result: Contracts: {contracts_to_open:,.8f}, Target Notional: {target_notional_for_order:,.2f}, Actual Notional: {actual_notional_after_precision:,.2f}, Req Margin: {required_margin:,.2f} USDT")
    return (contracts_to_open, required_margin)


def confirm_position_entry(expected_direction: str, expected_contracts: float) -> tuple[bool, float | None]:
    """ยืนยันการเปิดโพซิชัน"""
    global current_position_details

    if not market_info:
        logger.critical("❌ Critical Error: market_info is not loaded. Cannot confirm position.")
        send_telegram("⛔️ Critical Error: Market info not loaded. Cannot confirm position.")
        return False, None

    try:
        step_size = market_info['limits']['amount']['step']
    except KeyError:
        logger.critical("❌ Critical Error: market_info['limits']['amount']['step'] is invalid. Cannot confirm position.")
        send_telegram("⛔️ Critical Error: Market info step size invalid. Cannot confirm position.")
        return False, None

    size_tolerance = max(step_size * 2, expected_contracts * 0.001)

    logger.info(f"ℹ️ Initial 5-second sleep before starting position confirmation attempts for {expected_direction.upper()} {expected_contracts:,.8f} contracts.")
    time.sleep(5)

    for attempt in range(CONFIRMATION_RETRIES):
        logger.info(f"⏳ ยืนยันโพซิชัน (Attempt {attempt + 1}/{CONFIRMATION_RETRIES})...")
        time.sleep(CONFIRMATION_SLEEP)

        try:
            position_info = get_current_position()
            logger.info(f"INFO: Position info retrieved for confirmation attempt {attempt+1}: {position_info}")

            if position_info and position_info.get('side') == expected_direction:
                actual_size = position_info.get('contracts', 0.0)
                confirmed_entry_price = position_info.get('entry_price') or position_info.get('entryPrice')

                if math.isclose(actual_size, expected_contracts, rel_tol=size_tolerance):
                    logger.info(f"✅ ยืนยันโพซิชัน {expected_direction.upper()} สำเร็จ:")

                    if confirmed_entry_price is not None:
                        logger.info(f"   - Entry Price: {confirmed_entry_price:,.2f}")
                    else:
                        logger.warning("⚠️ Entry Price ยังเป็น None ใน confirm_position_entry()")

                    logger.info(f"   - Size: {actual_size:,.8f} Contracts")
                    logger.info(f"   - Direction: {expected_direction.upper()}")

                    current_position_details = {
                        'symbol': SYMBOL,
                        'side': expected_direction,
                        'contracts': actual_size,
                        'entry_price': confirmed_entry_price,
                        'unrealized_pnl': position_info.get('unrealizedPnl', 0.0),
                        'liquidation_price': position_info.get('liquidationPrice', None),
                        'sl_step': 0,  # เริ่มต้นที่ Step 0
                        'sl_price': None,  # จะถูกตั้งใน monitor_position
                        'tp_price': None,  # จะถูกตั้งใน monitor_position
                        'initial_sl_price': None  # จะถูกบันทึกเมื่อตั้ง SL ครั้งแรก
                    }
                    logger.info(f"INFO: current_position_details set: {current_position_details}")

                    profit_loss = position_info.get('unrealizedPnl', 0)
                    send_telegram(
                        f"🎯 เปิดโพซิชัน {expected_direction.upper()} สำเร็จ!\n"
                        f"📊 ขนาด: {actual_size:,.8f} Contracts\n"
                        f"💰 Entry: {confirmed_entry_price if confirmed_entry_price is not None else 'N/A'}\n"
                        f"📈 P&L: {profit_loss:,.2f} USDT"
                    )
                    return True, confirmed_entry_price
                else:
                    logger.warning(f"⚠️ ขนาดโพซิชันไม่ตรงกัน (คาดหวัง: {expected_contracts:,.8f}, ได้: {actual_size:,.8f}). Tolerance: {size_tolerance:,.8f}. Retrying...")
            else:
                logger.warning(f"⚠️ ไม่พบโพซิชันที่ตรงกัน (คาดหวัง: {expected_direction.upper()}) หรือไม่พบโพซิชันเลย. Retrying...")

        except Exception as e:
            logger.warning(f"⚠️ Error ในการยืนยันโพซิชัน (Attempt {attempt+1}): {e}", exc_info=True)

    logger.error(f"❌ ไม่สามารถยืนยันโพซิชันได้หลังจาก {CONFIRMATION_RETRIES} ครั้ง")
    send_telegram(
        f"⛔️ Position Confirmation Failed\n"
        f"🔍 กรุณาตรวจสอบโพซิชันใน Exchange ด่วน!\n"
        f"📊 คาดหวัง: {expected_direction.upper()} {expected_contracts:,.8f} Contracts"
    )
    return False, None

# ==============================================================================
# 11. ฟังก์ชันจัดการคำสั่งซื้อขาย
# ==============================================================================
def open_market_order(direction: str, current_price: float) -> tuple[bool, float | None]:
    global current_position_details, last_trade_closed_time, waiting_for_cooldown

    # *** การแก้ไขหลัก: ตรวจสอบ cooldown อย่างเข้มงวด ***
    if last_trade_closed_time and last_trade_closed_time != datetime.min:
        seconds_since_close = (datetime.now() - last_trade_closed_time).total_seconds()
        if seconds_since_close < TRADE_COOLDOWN_SECONDS:
            time_left = TRADE_COOLDOWN_SECONDS - seconds_since_close
            logger.warning(f"🚫 COOLDOWN ACTIVE → เหลืออีก {time_left:.0f} วินาที")
            send_telegram(f"🚫 บอทยังไม่พ้นช่วง cooldown หลังปิดโพซิชัน\nจะไม่เปิดออเดอร์ใหม่จนกว่าจะครบ {TRADE_COOLDOWN_SECONDS // 60} นาที")
            return False, None

    try:
        balance = get_portfolio_balance()
        if balance <= MARGIN_BUFFER_USDT:
            error_msg = f"ยอดคงเหลือ ({balance:,.2f} USDT) ต่ำเกินไป ไม่เพียงพอสำหรับ Margin Buffer ({MARGIN_BUFFER_USDT} USDT)."
            send_telegram(f" Balance Error: {error_msg}")
            logger.error(f" {error_msg}")
            return False, None

        order_amount, estimated_used_margin = calculate_order_details(balance, current_price)
        if order_amount <= 0:
            error_msg = " Calculated order amount is zero or insufficient. Cannot open position."
            send_telegram(f" Order Calculation Error: {error_msg}")
            logger.error(f" {error_msg}")
            return False, None

        decimal_places = 0
        if market_info and 'limits' in market_info and 'amount' in market_info['limits'] and 'step' in market_info['limits']['amount'] and market_info['limits']['amount']['step'] is not None:
            step_size = market_info['limits']['amount']['step']
            if step_size < 1:
                decimal_places = int(round(-math.log10(step_size)))

        logger.info(f" Trading Summary before opening order:")
        logger.info(f" - Balance: {balance:,.2f} USDT")
        logger.info(f" - Contracts: {order_amount:,.{decimal_places}f}")
        logger.info(f" - Required Margin (incl. buffer): {estimated_used_margin + MARGIN_BUFFER_USDT:,.2f} USDT")
        logger.info(f" - Direction: {direction.upper()}")

        side = 'buy' if direction == 'long' else 'sell'
        params = {}
        order = None

        for attempt in range(3):
            logger.info(f"ส่งคำสั่ง Market Order (Attempt {attempt + 1}/3) - {order_amount:,.{decimal_places}f} Contracts, Direction: {direction.upper()}")
            try:
                order = exchange.create_market_order(
                    symbol=SYMBOL,
                    side=side,
                    amount=order_amount,
                    params=params
                )
                if order and order.get('id'):
                    logger.info(f" Market Order ส่งสำเร็จ: ID → {order.get('id')}, Status: {order.get('status', 'N/A')}")
                    time.sleep(2)
                    break
                else:
                    logger.warning(f" Order response ไม่สมบูรณ์ (Attempt {attempt + 1}/3)")
            except ccxt.NetworkError as e:
                logger.warning(f" Network Error (Attempt {attempt + 1}/3): {e}")
                if attempt == 2:
                    send_telegram(f" Network Error: ไม่สามารถส่งออเดอร์ได้\n{str(e)[:200]}...")
                time.sleep(10)
            except ccxt.ExchangeError as e:
                logger.warning(f" Exchange Error (Attempt {attempt + 1}/3): {e}")
                if attempt == 2:
                    send_telegram(f" Exchange Error: ไม่สามารถส่งออเดอร์ได้\n{str(e)[:200]}...")
                time.sleep(10)
            except Exception as e:
                logger.error(f" Unexpected error (Attempt {attempt + 1}/3): {e}", exc_info=True)
                send_telegram(f" Unexpected Error: ไม่สามารถส่งออเดอร์ได้\n{str(e)[:200]}...")
                return False, None

        if not order:
            logger.error("ล้มเหลวในการส่งออเดอร์หลังจาก 3 ครั้ง")
            send_telegram(" Order Failed: ล้มเหลวในการส่งออเดอร์หลังจาก 3 ครั้ง")
            return False, None

        logger.info(f"INFO: Calling confirm_position_entry for direction: {direction}")
        return confirm_position_entry(direction, order_amount)

    except Exception as e:
        logger.error(f" Critical Error in open_market_order: {e}", exc_info=True)
        send_telegram(f" Critical Error: ไม่สามารถเปิดออเดอร์ได้\n{str(e)[:200]}...")
        return False, None

# ========================================================================
# 12. ฟังก์ชันตั้งค่า TP/SL/กันทุน (ปรับปรุงสำหรับ Trailing SL) - แก้ไขแล้ว
# ========================================================================

def cancel_all_open_tp_sl_orders():
    """ยกเลิกคำสั่ง TP/SL ที่ค้างอยู่สำหรับ Symbol ปัจจุบันบน Binance Futures."""
    logger.info(f"🔄 Checking for and canceling existing TP/SL orders for {SYMBOL}...")
    
    try:
        open_orders = exchange.fetch_open_orders(SYMBOL)
        canceled_count = 0
        
        for order in open_orders:
            if (order['status'] == 'open' or order['status'] == 'pending') and \
               (order.get('reduceOnly', False) == True or \
                order['type'] in ['TAKE_PROFIT_MARKET', 'STOP_MARKET', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT']):
                try:
                    exchange.cancel_order(order['id'], SYMBOL)
                    logger.info(f"✅ Canceled old TP/SL order: ID {order['id']}, Type: {order['type']}, Side: {order['side']}, Price: {order.get('stopPrice') or order.get('price')}")
                    canceled_count += 1
                    time.sleep(0.5)  # เพิ่ม delay เล็กน้อยระหว่างการยกเลิก
                except ccxt.OrderNotFound:
                    logger.info(f"ℹ️ Order {order['id']} not found or already canceled/filled. No action needed.")
                except ccxt.BaseError as e:
                    logger.warning(f"⚠️ Failed to cancel order {order['id']}: {str(e)}")
        
        if canceled_count == 0:
            logger.info("ℹ️ No old TP/SL orders found to cancel.")
        else:
            logger.info(f"✅ Successfully canceled {canceled_count} old TP/SL orders.")
            
    except ccxt.NetworkError as e:
        logger.error(f"❌ Network error while fetching/canceling open orders: {e}")
        send_telegram(f"❌ API Error: ไม่สามารถยกเลิก TP/SL เก่าได้ (Network)\nรายละเอียด: {e}")
    except ccxt.ExchangeError as e:
        logger.error(f"❌ Exchange error while fetching/canceling open orders: {e}")
        send_telegram(f"❌ API Error: ไม่สามารถยกเลิก TP/SL เก่าได้ (Exchange)\nรายละเอียด: {e}")
    except Exception as e:
        logger.error(f"❌ An unexpected error occurred while canceling orders: {e}", exc_info=True)
        send_telegram(f"❌ Unexpected Error: ไม่สามารถยกเลิก TP/SL เก่าได้\nรายละเอียด: {e}")

def set_tpsl_for_position(direction: str, amount: float, current_sl_price: float, current_tp_price: float) -> bool:
    """
    ตั้ง Take Profit และ Stop Loss สำหรับโพซิชัน.
    จะยกเลิก TP/SL ที่เปิดอยู่ก่อนเสมอแล้วตั้งใหม่
    """
    if not amount or amount <= 0:
        logger.error("❌ ไม่สามารถตั้ง TP/SL ได้: ขนาดโพซิชันเป็น 0 หรือไม่ถูกต้อง.")
        return False
    
    # ยกเลิก TP/SL เก่าก่อน
    cancel_all_open_tp_sl_orders()
    time.sleep(1)  # เพิ่ม delay ให้ยกเลิกเสร็จสิ้น
    
    # ตรวจสอบ market_info
    if not market_info or 'precision' not in market_info:
        logger.error("❌ ไม่พบข้อมูล market_info หรือ precision. ไม่สามารถตั้ง TP/SL ได้.")
        return False
    
    try:
        # ปัดเศษราคาให้ถูกต้องตาม Exchange
        market_info_precision_price = 'price'
        tp_price_formatted = round_to_precision(current_tp_price, market_info_precision_price)
        sl_price_formatted = round_to_precision(current_sl_price, market_info_precision_price)
        
        logger.info(f"🎯 กำลังตั้ง TP: {tp_price_formatted:,.2f} | 🛡️ กำลังตั้ง SL: {sl_price_formatted:,.2f} สำหรับ {direction.upper()}")
        
        # ตรวจสอบความถูกต้องของราคา TP/SL
        current_price = exchange.fetch_ticker(SYMBOL)['last']
        
        if direction == 'long':
            if tp_price_formatted <= current_price:
                logger.error(f"❌ TP price ({tp_price_formatted}) ต้องสูงกว่าราคาปัจจุบัน ({current_price}) สำหรับ Long")
                return False
            if sl_price_formatted >= current_price:
                logger.error(f"❌ SL price ({sl_price_formatted}) ต้องต่ำกว่าราคาปัจจุบัน ({current_price}) สำหรับ Long")
                return False
        else:  # short
            if tp_price_formatted >= current_price:
                logger.error(f"❌ TP price ({tp_price_formatted}) ต้องต่ำกว่าราคาปัจจุบัน ({current_price}) สำหรับ Short")
                return False
            if sl_price_formatted <= current_price:
                logger.error(f"❌ SL price ({sl_price_formatted}) ต้องสูงกว่าราคาปัจจุบัน ({current_price}) สำหรับ Short")
                return False
        
        tp_sl_side = 'sell' if direction == 'long' else 'buy'
        
        # ตั้ง Take Profit Order
        try:
            tp_order = exchange.create_order(
                symbol=SYMBOL,
                type='TAKE_PROFIT_MARKET',
                side=tp_sl_side,
                amount=amount,
                price=None,
                params={
                    'stopPrice': tp_price_formatted,
                    'reduceOnly': True,
                }
            )
            logger.info(f"✅ Take Profit order placed: ID → {tp_order.get('id', 'N/A')}, Price: {tp_price_formatted}")
            time.sleep(1)  # หน่วงเวลาระหว่างการตั้ง TP และ SL
            
        except Exception as tp_error:
            logger.error(f"❌ Error setting Take Profit: {str(tp_error)}")
            send_telegram(f"❌ TP Error: ไม่สามารถตั้ง Take Profit ได้\n{str(tp_error)}")
            return False
        
        # ตั้ง Stop Loss Order
        try:
            sl_order = exchange.create_order(
                symbol=SYMBOL,
                type='STOP_MARKET',
                side=tp_sl_side,
                amount=amount,
                price=None,
                params={
                    'stopPrice': sl_price_formatted,
                    'reduceOnly': True,
                }
            )
            logger.info(f"🛡️ Stop Loss order placed: ID → {sl_order.get('id', 'N/A')}, Price: {sl_price_formatted}")
            
        except Exception as sl_error:
            logger.error(f"❌ Error setting Stop Loss: {str(sl_error)}")
            send_telegram(f"❌ SL Error: ไม่สามารถตั้ง Stop Loss ได้\n{str(sl_error)}")
            
            # ถ้าตั้ง SL ไม่ได้ ให้ยกเลิก TP ที่ตั้งไปแล้วด้วย
            try:
                exchange.cancel_order(tp_order['id'], SYMBOL)
                logger.info("🔄 Canceled TP order due to SL failure")
            except:
                pass
            return False
        
        # ส่งการแจ้งเตือนสำเร็จ
        send_telegram(
            f"✅ <b>ตั้ง TP/SL สำเร็จ!</b>\n"
            f"🎯 <b>TP:</b> <code>{tp_price_formatted:,.2f}</code>\n"
            f"🛡️ <b>SL:</b> <code>{sl_price_formatted:,.2f}</code>\n"
            f"📊 <b>Direction:</b> {direction.upper()}\n"
            #f"📦 <b>Size:</b> {amount:,.8f}"
        )
        
        return True
        
    except ccxt.BaseError as e:
        logger.error(f"❌ Error setting TP/SL: {str(e)}", exc_info=True)
        send_telegram(f"❌ API Error (TP/SL): {e.args[0] if e.args else str(e)}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error setting TP/SL: {e}", exc_info=True)
        send_telegram(f"❌ Unexpected Error (TP/SL): {e}")
        return False

# ========================================================================
# 13. ฟังก์ชันตรวจสอบสถานะและบริหารโพซิชัน (MONITORING FUNCTIONS) - แก้ไขแล้ว
# ========================================================================
def monitor_position(current_market_price: float):
    global current_position_details, last_ema_position_status, monthly_stats, last_trade_closed_time
    global waiting_for_cooldown
    global last_manual_tp_alert_time
    global must_wait_new_cross
    
    logger.info(f"กำลังตรวจสอบสถานะโพซิชัน (Current Price: {current_market_price:,.2f})")
    pos_info_from_exchange = get_current_position()
    
    # A. มีโพซิชันทั้งในระบบและใน exchange → อัปเดตข้อมูล
    if pos_info_from_exchange and current_position_details:
        current_position_details.update({
            'entry_price': pos_info_from_exchange['entry_price'],
            'contracts': pos_info_from_exchange['contracts'],
            'side': pos_info_from_exchange['side'],
            'unrealized_pnl': pos_info_from_exchange['unrealized_pnl'],
            'liquidation_price': pos_info_from_exchange['liquidation_price']
        })
        
        # ตั้งค่าใช้งาน
        side = current_position_details['side']
        entry_price = current_position_details['entry_price']
        contracts = current_position_details['contracts']
        sl_step = current_position_details.get('sl_step', 0)
        sl_price = current_position_details.get('sl_price')
        
        logger.info(f"{side.upper()} | Entry: {entry_price:.2f} | Price: {current_market_price:.2f} | PnL: {current_position_details['unrealized_pnl']:.2f}")
        
        # ตั้ง SL เริ่มต้น ถ้ายังไม่มี
        if sl_price is None:
            sl = entry_price - SL_DISTANCE_POINTS if side == 'long' else entry_price + SL_DISTANCE_POINTS
            current_position_details['sl_price'] = sl
            current_position_details['initial_sl_price'] = sl
            current_position_details['sl_step'] = 0
            current_position_details['tp_price'] = 0
            
            logger.info(f"ตั้ง SL เริ่มต้น → SL: {sl:.2f} (ไม่ตั้ง TP)")
            success = set_sl_only_for_position(side, contracts, sl)
            if not success:
                logger.error("ไม่สามารถตั้ง SL เริ่มต้นได้")
                return
        
        # คำนวณกำไร
        pnl_points = (current_market_price - entry_price) if side == 'long' else (entry_price - current_market_price)
        
        # Step trigger
        trail_trigger_1 = TRAIL_SL_STEP1_TRIGGER_LONG_POINTS if side == 'long' else TRAIL_SL_STEP1_TRIGGER_SHORT_POINTS
        trail_trigger_2 = TRAIL_SL_STEP2_TRIGGER_LONG_POINTS if side == 'long' else TRAIL_SL_STEP2_TRIGGER_SHORT_POINTS
        trail_trigger_3 = TRAIL_SL_STEP3_TRIGGER_LONG_POINTS if side == 'long' else TRAIL_SL_STEP3_TRIGGER_SHORT_POINTS
        
        # SL เป้าหมายแต่ละ step
        trail_sl_1 = entry_price + TRAIL_SL_STEP1_NEW_SL_POINTS_LONG if side == 'long' else entry_price + TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT
        trail_sl_2 = entry_price + TRAIL_SL_STEP2_NEW_SL_POINTS_LONG if side == 'long' else entry_price + TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT
        trail_sl_3 = entry_price + TRAIL_SL_STEP3_NEW_SL_POINTS_LONG if side == 'long' else entry_price + TRAIL_SL_STEP3_NEW_SL_POINTS_SHORT
        
        # SL Step 1
        if sl_step == 0 and pnl_points >= trail_trigger_1:
            current_position_details['sl_step'] = 1
            current_position_details['sl_price'] = trail_sl_1
            logger.info(f"🚀 SL Step 1 triggered → ย้าย SL จาก {sl_price:.2f} เป็น {trail_sl_1:.2f}")
            success = set_sl_only_for_position(side, contracts, trail_sl_1)
            if not success:
                logger.error("ไม่สามารถอัปเดต SL Step 1 ได้")
        # 🆕 Step 2 (Breakeven/กันทุน)
        elif sl_step == 1 and pnl_points >= trail_trigger_2:
            current_position_details['sl_step'] = 2
            current_position_details['sl_price'] = trail_sl_2
            logger.info(f"🟰 SL Step 2 (BE) → ตั้ง SL กันทุนที่ {trail_sl_2:.2f}")
            success = set_sl_only_for_position(side, contracts, trail_sl_2)
            if not success:
                logger.error("ไม่สามารถอัปเดต SL Step 2 (BE) ได้")
        
        # SL Step 3 (TP จำลอง) แทน Step 2
        elif sl_step == 2 and pnl_points >= trail_trigger_3:
            current_position_details['sl_step'] = 3
            trail_sl_3 = round_to_precision(trail_sl_3, 'price')
            current_position_details['sl_price'] = trail_sl_3
            logger.info(f"🎯 SL Step 3 (TP จำลอง) triggered → ตั้ง SL ที่ {trail_sl_3:.2f}")
            success = set_sl_only_for_position(side, contracts, trail_sl_3)
            if not success:
                logger.error("ไม่สามารถอัปเดต SL Step 3 (TP จำลอง) ได้")
        
        # Manual TP Alert
        elif sl_step == 3 and pnl_points > MANUAL_TP_ALERT_THRESHOLD:
            current_time = datetime.now()
            time_since_last_alert = (current_time - last_manual_tp_alert_time).total_seconds()
            if time_since_last_alert >= MANUAL_TP_ALERT_INTERVAL:
                last_manual_tp_alert_time = current_time
                logger.info(f"🔔 Manual TP Alert: กำไรเกิน {MANUAL_TP_ALERT_THRESHOLD} points → แจ้งเตือนเรียบร้อย")
                send_telegram(
                    f"🔔 <b>Manual TP Alert!</b>\n"
                    f"💰 กำไรปัจจุบัน: <b>{pnl_points:+,.0f} points</b>\n"
                    f"📈 Entry: {entry_price:,.2f} → Current: {current_market_price:,.2f}\n"
                    f"💡 <b>กำไรทะลุเป้าจะแตกแล้ว🩲 รีบกด cancel all ด่วนๆ🔥</b>"
                )
        
        return
    
    # B. ตรวจพบว่าโพซิชัน "หายจาก exchange" แต่ยังจำในบอท
    elif not pos_info_from_exchange and current_position_details:
        logger.warning(f"โพซิชันหายไปจาก Exchange แต่บอทยังจำอยู่: {current_position_details}")
        
        entry = current_position_details.get('entry_price')
        contracts = current_position_details.get('contracts')
        side = current_position_details.get('side')
        sl_step = current_position_details.get('sl_step', 0)
        
        if not entry or not contracts or not side:
            logger.warning("ข้อมูล current_position_details ไม่ครบ → ข้ามการคำนวณ PnL")
            current_position_details = None
            # *** รีเซ็ต EMA status เมื่อปิดโพซิชัน ***
            last_ema_position_status = None
            must_wait_new_cross = True
            logger.info("🔄 รีเซ็ต EMA status หลังปิดโพซิชัน - จะตรวจสอบสถานะ EMA ใหม่ในรอบถัดไป")
            save_monthly_stats()
            return
        
        closed_price = current_market_price
        pnl = (closed_price - entry) * contracts if side == 'long' else (entry - closed_price) * contracts
        reason = "TP" if sl_step in (2, 3) else "SL"
        
        # เพิ่มสถิติ
        add_trade_result(reason, pnl)
        
        # เช็กซ้ำว่าโพซิชันกลับมาไหม
        time.sleep(1)
        confirm_pos = get_current_position()
        if confirm_pos:
            logger.warning("ตรวจพบโพซิชันกลับเข้ามาหลัง close → ข้ามการรีเซ็ตสถานะ")
            current_position_details = {
                'symbol': confirm_pos['symbol'],
                'side': confirm_pos['side'],
                'contracts': confirm_pos['contracts'],
                'entry_price': confirm_pos['entry_price'],
                'unrealized_pnl': confirm_pos['unrealized_pnl'],
                'liquidation_price': confirm_pos['liquidation_price'],
                'sl_step': current_position_details.get('sl_step', 0),
                'sl_price': current_position_details.get('sl_price'),
                'tp_price': current_position_details.get('tp_price'),
                'initial_sl_price': current_position_details.get('initial_sl_price')
            }
            return
        
        # เคลียร์คำสั่ง TP/SL ทันที
        try:
            cancel_all_open_tp_sl_orders()
            logger.info("เคลียร์คำสั่ง TP/SL เดิมเรียบร้อยหลังปิดโพซิชัน")
        except Exception as e:
            logger.warning(f"ไม่สามารถล้างคำสั่ง TP/SL: {e}")
        
        current_position_details = None
        last_trade_closed_time = datetime.now()
        waiting_for_cooldown = True
        
        # *** รีเซ็ต EMA status เมื่อปิดโพซิชัน ***
        last_ema_position_status = None
        logger.info("🔄 รีเซ็ต EMA status หลังปิดโพซิชัน - จะตรวจสอบสถานะ EMA ใหม่ในรอบถัดไป")
        
        save_monthly_stats()
        logger.info(f"เริ่ม COOLDOWN PERIOD: {TRADE_COOLDOWN_SECONDS} วินาที")
        
        # ยกเลิกคำสั่งทั้งหมด (เช่น market/limit ที่ยังค้าง)
        try:
            time.sleep(1)
            exchange.cancel_all_orders(SYMBOL)
            logger.info("ยกเลิกคำสั่งทั้งหมดหลังปิดโพซิชันแล้ว")
        except Exception as e:
            logger.warning(f"ยกเลิกคำสั่งไม่สำเร็จหลังปิดโพซิชัน: {e}")
        
        # ส่งการแจ้งเตือน
        send_telegram(
            f"📊 ปิดโพซิชัน {side.upper()} แล้ว!\n"
            f"💰 P&L: <b>{pnl:+,.2f} USDT</b>\n"
            f"📈 Entry: <code>{entry:,.2f}</code> → Exit: <code>{closed_price:,.2f}</code>\n"
            f"🌈 สาเหตุ: <b>{reason}</b>\n"
            #f"🔄 บอทจะรีเซ็ต EMA และรอสัญญาณใหม่\n"
            f"⏳ Cooldown: <b>{TRADE_COOLDOWN_SECONDS // 60} นาที</b>"
        )
        
        return
    
    # C. ไม่มีโพซิชันทั้งใน exchange และใน bot
    else:
        if current_position_details:
            logger.warning("บอทยังมีสถานะ แต่ exchange ไม่มีโพซิชัน → รีเซ็ต")
            try:
                exchange.cancel_all_orders(SYMBOL)
            except Exception as e:
                logger.warning(f"ยกเลิกคำสั่งค้างไม่สำเร็จ: {e}")
            current_position_details = None
            # *** รีเซ็ต EMA status ***
            last_ema_position_status = None
            must_wait_new_cross = True  
            logger.info("🔄 รีเซ็ต EMA status - จะตรวจสอบสถานะ EMA ใหม่ในรอบถัดไป")
            save_monthly_stats()
        else:
            logger.info("ไม่มีโพซิชันเปิดอยู่")
            
# ========================================================================
# เพิ่มฟังก์ชันช่วยเหลือสำหรับการ round precision
# ========================================================================

def round_to_precision(value: float, precision_type: str) -> float:
    """ปัดค่าให้เป็นไปตาม Precision ที่ Exchange กำหนด"""
    if market_info and 'precision' in market_info and precision_type in market_info['precision']:
        return float(exchange.decimal_to_precision(value, ccxt.ROUND, market_info['precision'][precision_type]))
    else:
        logger.warning(f"⚠️ ไม่พบ Precision สำหรับ '{precision_type}'. ใช้ round() ปกติ.")
        return round(value, 2)

# ==============================================================================
# 14. ฟังก์ชันรายงานประจำเดือน (MONTHLY REPORT FUNCTIONS)
# ==============================================================================
def monthly_report():
    global last_monthly_report_date, monthly_stats, initial_balance

    now = datetime.now()
    current_month_year = now.strftime('%Y-%m')

    if last_monthly_report_date and \
       last_monthly_report_date.year == now.year and \
       last_monthly_report_date.month == now.month:
        logger.debug(f"ℹ️ รายงานประจำเดือนสำหรับ {current_month_year} ถูกส่งไปแล้ว.")
        return

    report_day_of_month = min(MONTHLY_REPORT_DAY, calendar.monthrange(now.year, now.month)[1])
    if not (now.day == report_day_of_month and now.hour == MONTHLY_REPORT_HOUR and now.minute == MONTHLY_REPORT_MINUTE):
        logger.debug(f"ℹ️ ยังไม่ถึงเวลาส่งรายงานประจำเดือน ({report_day_of_month} {MONTHLY_REPORT_HOUR:02d}:{MONTHLY_REPORT_MINUTE:02d}).")
        return

    try:
        balance = get_portfolio_balance()

        if monthly_stats['month_year'] != current_month_year:
            logger.info(f"🆕 สถิติประจำเดือนที่ใช้ไม่ตรงกับเดือนนี้ ({monthly_stats['month_year']} vs {current_month_year}). กำลังรีเซ็ตสถิติเพื่อรายงานเดือนใหม่.")
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
    global last_monthly_report_date

    logger.info("⏰ เริ่ม Monthly Report Scheduler.")
    while True:
        now = datetime.now()

        report_day = min(MONTHLY_REPORT_DAY, calendar.monthrange(now.year, now.month)[1])
        next_report_time_this_month = now.replace(day=report_day, hour=MONTHLY_REPORT_HOUR, minute=MONTHLY_REPORT_MINUTE, second=0, microsecond=0)

        if now >= next_report_time_this_month and \
           (last_monthly_report_date is None or \
            last_monthly_report_date.year != now.year or \
            last_monthly_report_date.month != now.month):
            logger.info(f"⏰ ตรวจพบว่าถึงเวลาส่งรายงานประจำเดือน ({now.strftime('%H:%M')}) และยังไม่ได้ส่งสำหรับเดือนนี้. กำลังส่ง...")
            monthly_report()

        if now >= next_report_time_this_month:
            next_month = now.month + 1
            next_year = now.year
            if next_month > 12:
                next_month = 1
                next_year += 1
            max_day_in_next_month = calendar.monthrange(next_year, next_month)[1]
            report_day_for_next_month = min(MONTHLY_REPORT_DAY, max_day_in_next_month)
            next_report_time = datetime(next_year, next_month, report_day_for_next_month, MONTHLY_REPORT_HOUR, MONTHLY_REPORT_MINUTE, 0, 0)
        else:
            next_report_time = next_report_time_this_month

        time_to_wait = (next_report_time - datetime.now()).total_seconds()
        if time_to_wait > 0:
            logger.info(f"⏰ กำหนดส่งรายงานประจำเดือนถัดไปในอีก {int(time_to_wait / 86400)} วัน {int((time_to_wait % 86400) / 3600)} ชั่วโมง {int((time_to_wait % 3600) / 60)} นาที.")
            time.sleep(max(time_to_wait, 60))
        else:
            time.sleep(60)

# =================================================================
# 15. ฟังก์ชันเริ่มต้นบอท (BOT STARTUP FUNCTIONS)
# =================================================================
def send_startup_message():
    global initial_balance
    try:
        initial_balance = get_portfolio_balance()
        startup_time = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
        
        message = f"""🤖 <b>บอทเริ่มทำงาน</b> 💰
💰 <b>ยอดเริ่มต้น:</b> <code>{initial_balance:,.2f} USDT</code>
📊 <b>TF:</b> <code>{TIMEFRAME}</code> | <b>Leverage:</b> <code>{LEVERAGE}x</code>
📈 • <b>EMA Fast:</b> <code>{EMA_FAST_PERIOD}</code>
📉 • <b>EMA Slow:</b> <code>{EMA_SLOW_PERIOD}</code>
❎ <b>SL เริ่มต้น:</b> <code>{SL_DISTANCE_POINTS} points</code>
🚀 • <b>Step 1:</b> <code>{TRAIL_SL_STEP1_TRIGGER_LONG_POINTS}pts</code> → SL <code>{TRAIL_SL_STEP1_NEW_SL_POINTS_LONG:+}pts</code>
🔥 • <b>Step 2:(TP1)</b> <code>{TRAIL_SL_STEP2_TRIGGER_LONG_POINTS}pts</code> → SL <code>{TRAIL_SL_STEP2_NEW_SL_POINTS_LONG:+}pts</code>
🎉 • <b>Step 3 (TP2):</b> <code>{TRAIL_SL_STEP3_TRIGGER_LONG_POINTS}pts</code> → SL <code>{TRAIL_SL_STEP3_NEW_SL_POINTS_LONG}pts</code> 
⏰ <b>Manual TP Alert:</b> <code>{MANUAL_TP_ALERT_THRESHOLD} points</code> (จะมีการแจ้งเตือนปิดกำไร🤑)
🔍 <b>กำลังรอเปิดออเดอร์...</b>"""

        send_telegram(message)
        logger.info("ส่งข้อความแจ้งเตือนเมื่อบอทเริ่มทำงาน.")
        
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการส่งข้อความเริ่มต้น: {e}", exc_info=True)

# =================================================================
# 16. ฟังก์ชันหลักของบอท (MAIN BOT LOGIC)
# =================================================================
def main():
    global must_wait_new_cross, last_cross_bar_time, last_entry_cross_bar_time
    global current_position_details, last_ema_position_status, last_ema_calc_time, last_trade_closed_time
    global waiting_for_cooldown
    
    try:
        setup_exchange()
        load_monthly_stats()
        send_startup_message()
        
        monthly_thread = threading.Thread(target=monthly_report_scheduler, daemon=True)
        monthly_thread.start()
        logger.info("Monthly Report Scheduler Thread Started.")
        
    except Exception as e:
        error_msg = f"Error: ไม่สามารถเริ่มต้นบอทได้\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
        send_telegram(error_msg)
        logger.critical(f"Startup error: {e}", exc_info=True)
        time.sleep(ERROR_RETRY_SLEEP_SECONDS)
        sys.exit(1)
    
    logger.info("บอทเข้าสู่ Main Loop แล้วและพร้อมทำงาน...")
    force_open_initial_order = False  # ตั้งเป็น True สำหรับการทดสอบเปิด Long ทันที/False เพื่อใช้ema คำนวณ
    
    last_ema_calc_time = datetime.min  # กำหนดเวลาเริ่มต้นสำหรับ EMA Calculation
    last_trade_closed_time = datetime.min  # กำหนดเวลาเริ่มต้นสำหรับ Cooldown
    
    while True:
        try:
            current_time = datetime.now()
            logger.info(f"เริ่มรอบ Main Loop ({current_time.strftime('%Y-%m-%d %H:%M:%S')}) - กำลังดึงข้อมูลและตรวจสอบ.")
            
            # --- 1. ตรวจสอบ COOLDOWN ก่อนทำอะไรทั้งหมด (การแก้ไขหลัก) ---
            if last_trade_closed_time != datetime.min:
                seconds_since_close = (current_time - last_trade_closed_time).total_seconds()
                if seconds_since_close < TRADE_COOLDOWN_SECONDS:
                    time_left = TRADE_COOLDOWN_SECONDS - seconds_since_close
                    logger.info(f"🚫 COOLDOWN ACTIVE: เหลืออีก {time_left:.0f} วินาที ({time_left/60:.1f} นาที)")
                    time.sleep(FAST_LOOP_INTERVAL_SECONDS)
                    continue
                else:
                    # ออกจาก cooldown period
                    if waiting_for_cooldown:
                        waiting_for_cooldown = False
                        logger.info("✅ COOLDOWN PERIOD สิ้นสุด - บอทพร้อมเปิดออเดอร์ใหม่")
                        #send_telegram("✅ สิ้นสุดช่วง Cooldown - บอทพร้อมเทรดใหม่")
            
            # --- 2. ดึงราคาปัจจุบัน ---
            current_price = None
            try:
                logger.info("กำลังดึงราคาล่าสุด (Ticker)...")
                ticker = exchange.fetch_ticker(SYMBOL)
                time.sleep(0.2)  # ลดจาก 0.5 เป็น 0.2
            except Exception as e:
                logger.warning(f"Error fetching ticker: {e}. Retrying in {ERROR_RETRY_SLEEP_SECONDS} วินาที...")
                send_telegram(f"🚨 API Error: ไม่สามารถดึงราคาล่าสุดได้. รายละเอียด: {e.args[0] if e.args else str(e)}")
                time.sleep(ERROR_RETRY_SLEEP_SECONDS)
                continue
            
            if not ticker or 'last' not in ticker:
                logger.error("Failed to fetch valid ticker. Skipping loop and retrying.")
                send_telegram("❌ Error: ไม่สามารถดึงราคาล่าสุดได้ถูกต้อง. Skipping.")
                time.sleep(ERROR_RETRY_SLEEP_SECONDS)
                continue
            
            current_price = float(ticker['last'])
            logger.info(f"ราคาปัจจุบันของ {SYMBOL}: {current_price:,.2f}")
            
            # --- 3. ตรวจสอบสถานะและบริหารจัดการโพซิชันที่มีอยู่ (TP/SL, Trailing SL) ---
            monitor_position(current_price)
            
            # --- 4. ตรวจสอบสัญญาณและเปิดโพซิชันใหม่ (เฉพาะเมื่อไม่มีโพซิชันและไม่อยู่ใน cooldown) ---
            if current_position_details is None and not waiting_for_cooldown:
                # ตรวจสอบ cooldown อีกครั้งเพื่อความแน่ใจ (Double-check mechanism)
                if last_trade_closed_time != datetime.min:
                    seconds_since_close = (current_time - last_trade_closed_time).total_seconds()
                    if seconds_since_close < TRADE_COOLDOWN_SECONDS:
                        logger.info(f"🚫 Double-check: ยังอยู่ใน cooldown ({TRADE_COOLDOWN_SECONDS - seconds_since_close:.0f} วินาที)")
                        time.sleep(FAST_LOOP_INTERVAL_SECONDS)
                        continue
                
                # Force open mode (สำหรับทดสอบ)
                if force_open_initial_order:
                    logger.info("ไม่มีโพซิชันเปิดอยู่ และตั้งค่าให้บังคับเปิด Long ออเดอร์ครั้งแรก.")
                    send_telegram("🧪 <b>ทดสอบ:</b> กำลังบังคับเปิด Long ออเดอร์เพื่อทดสอบ TP/SL.")
                    market_order_success, confirmed_entry_price = open_market_order('long', current_price)
                    if market_order_success and confirmed_entry_price:
                        logger.info(f"บังคับเปิด Long ออเดอร์สำเร็จ. บอทจะดูแล TP/SL ในรอบถัดไป.")
                        force_open_initial_order = False
                    else:
                        logger.warning(f"ไม่สามารถบังคับเปิด Long ออเดอร์ได้. โปรดตรวจสอบ Log.")
                
                # โหมด EMA Cross ปกติ
                else:
                    # ตรวจสอบว่าถึงเวลาคำนวณ EMA หรือยัง
                    if (current_time - last_ema_calc_time).total_seconds() >= EMA_CALC_INTERVAL_SECONDS:
                        logger.info("ไม่มีโพซิชันเปิดอยู่. ถึงเวลาตรวจสอบสัญญาณ EMA Cross เพื่อเปิดโพซิชัน...")
                        if must_wait_new_cross:
                            _ = check_ema_cross()  # Priming
                            must_wait_new_cross = False
                            time.sleep(FAST_LOOP_INTERVAL_SECONDS)
                            continue
                        signal = check_ema_cross()
                        last_ema_calc_time = current_time  # อัปเดตเวลาที่คำนวณ EMA ล่าสุด
                        
                        if signal:
                            logger.info(f"ตรวจพบสัญญาณ EMA Cross: {signal.upper()}. กำลังพยายามเปิดออเดอร์.")
                            send_telegram(f"🎯 <b>SIGNAL:</b> ตรวจพบสัญญาณ EMA Cross: <b>{signal.upper()}</b>")
                            
                            market_order_success, confirmed_entry_price = open_market_order(signal, current_price)
                            if market_order_success and confirmed_entry_price:
                                logger.info(f"เปิดออเดอร์ {signal.upper()} สำเร็จ. บอทจะดูแล TP/SL ในรอบถัดไป.")
                                # 🆕 บันทึกว่าใช้ cross แท่งนี้ไปแล้ว
                                global last_entry_cross_bar_time, last_cross_bar_time
                                last_entry_cross_bar_time = last_cross_bar_time
        
                            else:
                                logger.warning(f"ไม่สามารถเปิด Market Order {signal.upper()} ได้.")
                        else:
                            logger.info("ไม่พบสัญญาณ EMA Cross ที่ชัดเจนในรอบนี้.")
                    else:
                        time_until_next_ema = EMA_CALC_INTERVAL_SECONDS - (current_time - last_ema_calc_time).total_seconds()
                        logger.info(f"ไม่มีโพซิชันเปิดอยู่. รอคำนวณ EMA Cross อีก {time_until_next_ema:,.0f} วินาที.")
            
            elif current_position_details is not None:  # กรณีมีโพซิชันเปิดอยู่แล้ว
                logger.info(f"Current Position: {current_position_details['side'].upper()}, SL Step: {current_position_details['sl_step']}. บอทจะดูแลการปิดหรือเลื่อน SL เพิ่มเติม.")
            
            elif waiting_for_cooldown:  # กรณีอยู่ใน cooldown period
                seconds_left = TRADE_COOLDOWN_SECONDS - (current_time - last_trade_closed_time).total_seconds()
                logger.info(f"อยู่ในช่วง Cooldown - เหลืออีก {seconds_left:.0f} วินาที")
            
            # --- 5. หน่วงเวลาสำหรับรอบ Main Loop (ลูปเร็ว) ---
            logger.info(f"จบรอบ Main Loop. รอ {FAST_LOOP_INTERVAL_SECONDS} วินาทีสำหรับรอบถัดไป.")
            time.sleep(FAST_LOOP_INTERVAL_SECONDS)
            
        except KeyboardInterrupt:
            logger.info("บอทหยุดทำงานโดยผู้ใช้ (KeyboardInterrupt).")
            send_telegram("🛑 Bot หยุดทำงานโดยผู้ใช้.")
            break
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            error_msg = f"🚨 API Error ใน Main Loop\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
            logger.error(error_msg, exc_info=True)
            send_telegram(error_msg)
            time.sleep(ERROR_RETRY_SLEEP_SECONDS)
        except Exception as e:
            error_msg = f"❌ Error: เกิดข้อผิดพลาดที่ไม่คาดคิดใน Main Loop\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
            logger.error(error_msg, exc_info=True)
            send_telegram(error_msg)
            time.sleep(ERROR_RETRY_SLEEP_SECONDS)

# =================================================================
# 17. จุดเริ่มต้นการทำงานของโปรแกรม (ENTRY POINT)
# =================================================================
if __name__ == '__main__':
    main()


