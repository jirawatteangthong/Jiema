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
# **สำคัญมาก: หากใช้ Railway หรือ Production ควรตั้งค่าเหล่านี้ใน Environment Variables**
# **ไม่ควรใส่ API Key/Secret ตรงๆ ในโค้ดเมื่อ Deploy จริง**
API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('BINANCE_SECRET', 'YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING')

# --- Trade Parameters ---
SYMBOL = 'BTC/USDT' # ใช้ 'BTC/USDT' สำหรับ Binance Futures
TIMEFRAME = '15m'
LEVERAGE = 34
TP_DISTANCE_POINTS = 501 # Take Profit ระยะ 501 จุด
SL_DISTANCE_POINTS = 999 # Stop Loss เริ่มต้น ระยะ 999 จุด

# --- Trailing Stop Loss Parameters (2 Steps) ---
# สำหรับ Long Position:
# Trigger 1: เมื่อราคาขึ้นไปถึง (ราคาเข้า + TRAIL_SL_STEP1_TRIGGER_LONG_POINTS)
# SL ใหม่ 1: SL จะไปอยู่ที่ (ราคาเข้า + TRAIL_SL_STEP1_NEW_SL_POINTS_LONG)
TRAIL_SL_STEP1_TRIGGER_LONG_POINTS = 300 # ราคากำไร 300 จุด จากราคาเข้า
TRAIL_SL_STEP1_NEW_SL_POINTS_LONG = -400 # SL ใหม่ที่ ราคาเข้า - 400 จุด

# Trigger 2: เมื่อราคาขึ้นไปถึง (ราคาเข้า + TRAIL_SL_STEP2_TRIGGER_LONG_POINTS)
# SL ใหม่ 2: SL จะไปอยู่ที่ (ราคาเข้า + TRAIL_SL_STEP2_NEW_SL_POINTS_LONG)
TRAIL_SL_STEP2_TRIGGER_LONG_POINTS = 400 # ราคากำไร 400 จุด จากราคาเข้า
TRAIL_SL_STEP2_NEW_SL_POINTS_LONG = 100 # SL ใหม่ที่ ราคาเข้า + 100 จุด (กันทุน+กำไร)

# สำหรับ Short Position: (ยังไม่ได้กำหนดในโค้ด ต้องกลับด้าน logic และค่า)
# TRAIL_SL_STEP1_TRIGGER_SHORT_POINTS = -300 # ราคากำไร 300 จุด (ลง)
# TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT = 400  # SL ใหม่ที่ ราคาเข้า + 400 จุด
# TRAIL_SL_STEP2_TRIGGER_SHORT_POINTS = -400 # ราคากำไร 400 จุด (ลง)
# TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT = -100 # SL ใหม่ที่ ราคาเข้า - 100 จุด (กันทุน+กำไร)

CROSS_THRESHOLD_POINTS = 1 # EMA Cross Threshold

# --- Risk Management ---
MARGIN_BUFFER_USDT = 5 # เงินสำรองใน USDT
TARGET_POSITION_SIZE_FACTOR = 0.8 # ขนาดโพซิชัน (80% ของ Equity ที่ใช้ได้)

# --- Order Confirmation & Stability ---
CONFIRMATION_RETRIES = 15 # จำนวนครั้งที่พยายามยืนยันโพซิชัน
CONFIRMATION_SLEEP = 5 # หน่วงเวลาระหว่างการยืนยัน (วินาที)
TP_SL_BE_PRICE_TOLERANCE_PERCENT = 0.005 # เปอร์เซ็นต์ความคลาดเคลื่อนในการระบุสาเหตุการปิดออเดอร์

# --- Telegram Notification Settings ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# --- Files & Paths ---
# หากใช้ Railway Volume ให้เปลี่ยนเป็น '/data/trading_stats.json'
STATS_FILE = 'trading_stats.json'

# --- Bot Timing ---
MAIN_LOOP_SLEEP_SECONDS = 180 # รอบการทำงานหลัก (3 นาที)
ERROR_RETRY_SLEEP_SECONDS = 60 # หน่วงเวลาเมื่อเกิดข้อผิดพลาด
MONTHLY_REPORT_DAY = 20 # วันที่ส่งรายงานประจำเดือน
MONTHLY_REPORT_HOUR = 0 # เวลาที่ส่งรายงานประจำเดือน (เที่ยงคืน)
MONTHLY_REPORT_MINUTE = 5 # เวลาที่ส่งรายงานประจำเดือน (00:05)

# ==============================================================================
# 2. การตั้งค่า Logging
# ==============================================================================
logging.basicConfig(
    level=logging.INFO, # ตั้งค่าเป็น INFO สำหรับการใช้งานปกติ, เปลี่ยนเป็น DEBUG เพื่อดูรายละเอียดเพิ่มเติม
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
# Force flush for StreamHandler to see logs immediately in console
for handler in logging.root.handlers:
    if isinstance(handler, logging.StreamHandler):
        handler.flush = lambda: sys.stdout.flush()

logger = logging.getLogger(__name__)


# ==============================================================================
# 3. ตัวแปรสถานะการเทรด (GLOBAL TRADE STATE VARIABLES)
# ==============================================================================
current_position_details = None  # เก็บข้อมูลโพซิชันปัจจุบัน (side, size, entry_price, sl_step, etc.)
portfolio_balance = 0.0
last_monthly_report_date = None
initial_balance = 0.0
last_ema_position_status = None # 'above' (EMA50 > EMA200) หรือ 'below' (EMA50 < EMA200)

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
    'last_ema_cross_signal': None, # อาจจะไม่ได้ใช้โดยตรง แต่เก็บไว้เพื่อ debug ได้
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
        # ตรวจสอบ API Key และ Secret
        if not API_KEY or API_KEY == 'YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING' or \
           not SECRET or SECRET == 'YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING':
            raise ValueError("API_KEY หรือ SECRET ไม่ถูกตั้งค่าใน Environment Variables. โปรดแก้ไข.")

        # ตั้งค่า Exchange instance
        exchange = ccxt.binance({
            'apiKey': API_KEY,
            'secret': SECRET,
            'sandbox': False, # ตั้งเป็น True หากใช้ Testnet ของ Binance
            'enableRateLimit': True, # เปิดใช้งาน Rate Limit ของ CCXT เพื่อป้องกันการส่งคำขอเร็วเกินไป
            'options': {
                'defaultType': 'future', # สำคัญมาก: กำหนดให้เป็น Futures
                'marginMode': 'cross',   # ใช้ Cross Margin (หรือ 'isolated' ถ้าต้องการ)
            },
            'verbose': False, # ตั้งเป็น True หากต้องการเห็น Raw HTTP requests/responses (สำหรับ Debugging ขั้นสูง)
            'timeout': 30000, # กำหนด Timeout 30 วินาทีสำหรับการเชื่อมต่อ
        })

        # โหลดข้อมูลตลาด (Symbols, Precision, Limits)
        exchange.load_markets()
        logger.info("✅ เชื่อมต่อกับ Binance Futures Exchange สำเร็จ และโหลด Markets แล้ว.")

        # ดึงข้อมูลตลาดสำหรับคู่เทรดที่กำหนด (SYMBOL)
        market_info = exchange.market(SYMBOL)
        if not market_info:
            raise ValueError(f"ไม่พบข้อมูลตลาดสำหรับสัญลักษณ์ {SYMBOL}")

        # --- ตรวจสอบและกำหนดค่าเริ่มต้นที่เหมาะสมสำหรับ limits ---
        # เพื่อป้องกัน KeyError หรือ TypeError หาก API response ไม่สมบูรณ์
        if 'limits' not in market_info:
            market_info['limits'] = {}
        if 'amount' not in market_info['limits']:
            market_info['limits']['amount'] = {}
        if 'cost' not in market_info['limits']:
            market_info['limits']['cost'] = {}

        # ดึงค่า limits ออกมาตรวจสอบก่อนแปลงเป็น float
        # การใช้ .get() และตรวจสอบ `is not None` จะช่วยให้โค้ดแข็งแกร่งขึ้น
        amount_step_val = market_info['limits']['amount'].get('step')
        amount_min_val = market_info['limits']['amount'].get('min')
        amount_max_val = market_info['limits']['amount'].get('max')
        cost_min_val = market_info['limits']['cost'].get('min')
        cost_max_val = market_info['limits']['cost'].get('max')

        market_info['limits']['amount']['step'] = float(amount_step_val) if amount_step_val is not None else 0.001
        market_info['limits']['amount']['min'] = float(amount_min_val) if amount_min_val is not None else 0.001
        market_info['limits']['amount']['max'] = float(amount_max_val) if amount_max_val is not None else sys.float_info.max
        market_info['limits']['cost']['min'] = float(cost_min_val) if cost_min_val is not None else 5.0
        market_info['limits']['cost']['max'] = float(cost_max_val) if cost_max_val is not None else sys.float_info.max # แก้ไขตรงนี้

        logger.debug(f"DEBUG: Market info limits for {SYMBOL}:")
        logger.debug(f"  Amount: step={market_info['limits']['amount']['step']}, min={market_info['limits']['amount']['min']}, max={market_info['limits']['amount']['max']}")
        logger.debug(f"  Cost: min={market_info['limits']['cost']['min']}, max={market_info['limits']['cost']['max']}")

        # ตั้งค่า Leverage
        try:
            result = exchange.set_leverage(LEVERAGE, SYMBOL)
            logger.info(f"✅ ตั้งค่า Leverage เป็น {LEVERAGE}x สำหรับ {SYMBOL}: {result}")
        except ccxt.ExchangeError as e:
            # ดักจับ Error เฉพาะที่เกี่ยวกับ Leverage ไม่ถูกต้อง
            if "leverage is not valid" in str(e) or "not valid for this symbol" in str(e):
                logger.critical(f"❌ Error: Leverage {LEVERAGE}x ไม่ถูกต้องสำหรับ {SYMBOL} บน Binance. โปรดตรวจสอบ Max Allowed Leverage.")
            else:
                logger.critical(f"❌ Error ในการตั้งค่า Leverage: {e}", exc_info=True)
            send_telegram(f"⛔️ Critical Error: ไม่สามารถตั้งค่า Leverage ได้.\nรายละเอียด: {e}")
            exit() # ออกจากโปรแกรมทันทีหากตั้ง Leverage ไม่ได้

    except ValueError as ve:
        logger.critical(f"❌ Configuration Error: {ve}", exc_info=True)
        send_telegram(f"⛔️ Critical Error: การตั้งค่าเริ่มต้นผิดพลาด.\nรายละเอียด: {ve}")
        exit() # ออกจากโปรแกรมหากมีปัญหาจากการตั้งค่าพื้นฐาน
    except Exception as e:
        logger.critical(f"❌ ไม่สามารถเชื่อมต่อหรือโหลดข้อมูล Exchange เบื้องต้นได้: {e}", exc_info=True)
        send_telegram(f"⛔️ Critical Error: ไม่สามารถเชื่อมต่อ Exchange ได้\nรายละเอียด: {e}")
        exit() # ออกจากโปรแกรมหากการเชื่อมต่อ Exchange ล้มเหลว

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
    # last_ema_position_status ไม่ต้องรีเซ็ตที่นี่ เพราะมันควรจะต่อเนื่องไป
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

        # ตั้งค่าเริ่มต้นหากเกิดข้อผิดพลาดในการโหลด
        monthly_stats = {
            'month_year': None, 'tp_count': 0, 'sl_count': 0, 'total_pnl': 0.0, 'trades': [],
            'last_report_month_year': None, 'last_ema_cross_signal': None, 'last_ema_position_status': None
        }
        last_monthly_report_date = None
        last_ema_position_status = None
        reset_monthly_stats() # เพื่อสร้างไฟล์เปล่า

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
            time.sleep(0.5) # เพิ่มหน่วงเวลาเล็กน้อย

            free_usdt = balance.get('USDT', {}).get('free', 0)
            if free_usdt == 0: # บาง Exchange อาจอยู่ใน 'info'
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
    retries = 3
    for i in range(retries):
        try:
            logger.debug(f"🔍 กำลังดึงโพซิชันปัจจุบัน (Attempt {i+1}/{retries})...")
            positions = exchange.fetch_positions([SYMBOL])
            logger.debug(f"DEBUG: Raw positions fetched: {positions}")
            time.sleep(0.5) # เพิ่มหน่วงเวลาเล็กน้อย

            for pos in positions:
                if pos['symbol'] == SYMBOL and float(pos['contracts']) != 0:
                    pos_amount = float(pos['contracts'])
                    return {
                        'symbol': pos['symbol'],
                        'side': 'long' if pos_amount > 0 else 'short',
                        'contracts': abs(pos_amount), # ใช้ 'contracts' แทน 'size'
                        'entryPrice': float(pos['entryPrice']), # ใช้ 'entryPrice'
                        'unrealizedPnl': float(pos['unrealizedPnl']),
                        'liquidationPrice': float(pos['liquidationPrice']) if pos['liquidationPrice'] else None,
                        'info': pos # เก็บ info ไว้เผื่อใช้
                    }
            return None # ไม่มีโพซิชันเปิด
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

    # Simple Moving Average (SMA) for initial EMA
    sma = sum(prices[:period]) / period
    ema = sma
    multiplier = 2 / (period + 1)

    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))

    return ema

def check_ema_cross() -> str | None:
    global last_ema_position_status # ต้องเป็น global เพื่ออัปเดตสถานะ

    try:
        retries = 3
        ohlcv = None
        for i in range(retries):
            logger.debug(f"🔍 กำลังดึงข้อมูล OHLCV สำหรับ EMA ({i+1}/{retries})...")
            try:
                ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=500) # ต้องการ 200 แท่ง + เผื่อ
                time.sleep(0.5)
                break
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                logger.warning(f"⚠️ Error fetching OHLCV (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
                if i == retries - 1:
                    send_telegram(f"⛔️ API Error: ไม่สามารถดึง OHLCV ได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
                time.sleep(15)
            except Exception as e:
                logger.error(f"❌ Unexpected error fetching OHLCV: {e}", exc_info=True)
                send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึง OHLCV ได้\nรายละเอียด: {e}")
                return None

        if not ohlcv or len(ohlcv) < 201: # 200 สำหรับ EMA200 + 1 แท่งปัจจุบัน
            logger.warning(f"ข้อมูล OHLCV ไม่เพียงพอ. ต้องการอย่างน้อย 201 แท่ง ได้ {len(ohlcv)}")
            send_telegram(f"⚠️ ข้อมูล OHLCV ไม่เพียงพอ ({len(ohlcv)} แท่ง).")
            return None

        closes = [candle[4] for candle in ohlcv]

        ema50_current = calculate_ema(closes, 50)
        ema200_current = calculate_ema(closes, 200)

        logger.info(f"💡 EMA Values: Current EMA50={ema50_current:.2f}, EMA200={ema200_current:.2f}")

        if None in [ema50_current, ema200_current]:
            logger.warning("ค่า EMA ไม่สามารถคำนวณได้ (เป็น None).")
            return None

        current_ema_position = None
        if ema50_current > ema200_current:
            current_ema_position = 'above'
        elif ema50_current < ema200_current:
            current_ema_position = 'below'

        # ตรวจสอบสถานะเริ่มต้นของบอท
        if last_ema_position_status is None:
            if current_ema_position:
                last_ema_position_status = current_ema_position
                save_monthly_stats()
                logger.info(f"ℹ️ บอทเพิ่งเริ่มรัน. บันทึกสถานะ EMA ปัจจุบันเป็น: {current_ema_position.upper()}. จะรอสัญญาณการตัดกันครั้งถัดไป.")
            return None # ไม่ส่งสัญญาณในรอบแรกของการรัน (เพื่อกำหนดสถานะเริ่มต้น)

        cross_signal = None

        if last_ema_position_status == 'below' and current_ema_position == 'above' and \
           ema50_current > (ema200_current + CROSS_THRESHOLD_POINTS):
            cross_signal = 'long'
            logger.info(f"🚀 Threshold Golden Cross: EMA50({ema50_current:.2f}) is {CROSS_THRESHOLD_POINTS} points above EMA200({ema200_current:.2f})")

        elif last_ema_position_status == 'above' and current_ema_position == 'below' and \
             ema50_current < (ema200_current - CROSS_THRESHOLD_POINTS):
            cross_signal = 'short'
            logger.info(f"🔻 Threshold Death Cross: EMA50({ema50_current:.2f}) is {CROSS_THRESHOLD_POINTS} points below EMA200({ema200_current:.2f})")

        # อัปเดตสถานะ EMA ล่าสุดเสมอหลังจากการประเมินสัญญาณ
        if cross_signal is not None:
            logger.info(f"✨ สัญญาณ EMA Cross ที่ตรวจพบ: {cross_signal.upper()}")
            if current_ema_position != last_ema_position_status:
                logger.info(f"ℹ️ EMA position changed from {last_ema_position_status.upper()} to {current_ema_position.upper()} during a cross signal. Updating last_ema_position_status.")
                last_ema_position_status = current_ema_position
                save_monthly_stats()
        elif current_ema_position != last_ema_position_status: # ถ้าไม่มี cross_signal แต่สถานะ EMA เปลี่ยนแปลง
            logger.info(f"ℹ️ EMA position changed from {last_ema_position_status.upper()} to {current_ema_position.upper()}. Updating last_ema_position_status (no cross signal detected).")
            last_ema_position_status = current_ema_position
            save_monthly_stats()
        else: # ไม่มีสัญญาณ และสถานะไม่เปลี่ยนแปลง
            logger.info("🔎 ไม่พบสัญญาณ EMA Cross ที่ชัดเจน.")

        return cross_signal

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการคำนวณ EMA: {e}", exc_info=True)
        send_telegram(f"⛔️ Error: ไม่สามารถคำนวณ EMA ได้\nรายละเอียด: {e}")
        return None

# ==============================================================================
# 10. ฟังก์ชันช่วยสำหรับการคำนวณและตรวจสอบออเดอร์
# ==============================================================================

def round_to_precision(value: float, precision: float) -> float:
    """ปัดค่าให้เป็นไปตาม Precision ที่ Exchange กำหนด"""
    if market_info and 'precision' in market_info and precision in market_info['precision']:
        return float(exchange.decimal_to_precision(value, exchange.ROUND, market_info['precision'][precision]))
    else:
        logger.warning(f"⚠️ ไม่พบ Precision สำหรับ '{precision}'. ใช้ round() ปกติ.")
        return round(value, 8) # Fallback to a common high precision

def calculate_order_details(available_usdt: float, price: float) -> tuple[float, float]:
    """
    คำนวณจำนวนสัญญาที่จะเปิดและ Margin ที่ต้องใช้ โดยพิจารณาจาก Exchange Limits
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
        # max_notional_exchange = market_info['limits']['cost']['max'] # Not strictly needed if limited by available margin

    except KeyError as e:
        logger.critical(f"❌ Error accessing market limits for {SYMBOL}: Missing key {e}. Check API response structure or setup_exchange defaults. Exiting.", exc_info=True)
        send_telegram(f"⛔️ Critical Error: Cannot parse market limits for {SYMBOL}.\nDetails: {e}")
        return (0, 0)
    except (TypeError, ValueError) as e:
        logger.critical(f"❌ Error parsing market limits for {SYMBOL}: Invalid value {e}. Check API response structure. Exiting.", exc_info=True)
        send_telegram(f"⛔️ Critical Error: Cannot parse market limits for {SYMBOL}.\nDetails: {e}")
        return (0, 0)

    max_notional_from_available_margin = (available_usdt - MARGIN_BUFFER_USDT) * LEVERAGE
    if max_notional_from_available_margin <= 0:
        logger.warning(f"❌ Available margin ({available_usdt:.2f}) too low after buffer ({MARGIN_BUFFER_USDT}) for any notional value.")
        return (0, 0)

    target_notional_for_order = max_notional_from_available_margin * TARGET_POSITION_SIZE_FACTOR

    min_notional_from_min_amount = min_exchange_amount * price
    # ต้องไม่ต่ำกว่า min_notional_exchange และ min_notional_from_min_amount
    target_notional_for_order = max(target_notional_for_order, min_notional_exchange, min_notional_from_min_amount)
    # target_notional_for_order = min(target_notional_for_order, max_notional_exchange) # ไม่จำเป็นต้องจำกัดด้วย max notional ถ้าถูกจำกัดด้วย available margin แล้ว

    contracts_raw = target_notional_for_order / price

    # ปัดจำนวนสัญญาให้เป็นไปตาม step size
    # ใช้ amount_to_precision ของ CCXT โดยตรงจะดีกว่า
    contracts_to_open = float(exchange.amount_to_precision(SYMBOL, contracts_raw))

    # ตรวจสอบขั้นต่ำ/สูงสุดอีกครั้งหลังปัด
    contracts_to_open = max(contracts_to_open, min_exchange_amount)
    contracts_to_open = min(contracts_to_open, max_exchange_amount)

    actual_notional_after_precision = contracts_to_open * price
    required_margin = actual_notional_after_precision / LEVERAGE

    if contracts_to_open == 0:
        logger.warning(f"⚠️ Calculated contracts to open is 0 after all adjustments. (Target notional: {target_notional_for_order:.2f} USDT, Current price: {price:.2f}, Min exchange amount: {min_exchange_amount:.8f}). This means calculated size is too small or rounded to zero.")
        return (0, 0)

    if available_usdt < required_margin + MARGIN_BUFFER_USDT:
        logger.error(f"❌ Margin not sufficient. Available: {available_usdt:.2f}, Required: {required_margin:.2f} + {MARGIN_BUFFER_USDT} (Buffer) = {required_margin + MARGIN_BUFFER_USDT:.2f} USDT.")
        return (0, 0)

    logger.debug(f"💡 DEBUG (calculate_order_details): Final Contracts to Open: {contracts_to_open:.8f}, Required Margin: {required_margin:.2f} USDT")
    return (contracts_to_open, required_margin)


def confirm_position_entry(expected_direction: str, expected_contracts: float) -> tuple[bool, float | None]:
    """ยืนยันการเปิดโพซิชัน"""
    global current_position_details

    if not market_info:
        logger.critical("❌ Critical Error: market_info is not loaded. Cannot confirm position. Re-running setup_exchange might help.")
        send_telegram("⛔️ Critical Error: Market info not loaded. Cannot confirm position.")
        return False, None

    try:
        step_size = market_info['limits']['amount']['step']
    except KeyError:
        logger.critical("❌ Critical Error: market_info['limits']['amount']['step'] is invalid. Cannot confirm position. Re-running setup_exchange might help.")
        send_telegram("⛔️ Critical Error: Market info step size invalid. Cannot confirm position.")
        return False, None

    size_tolerance = max(step_size * 2, expected_contracts * 0.001) # 2 steps or 0.1% of expected

    time.sleep(5) # ให้เวลา Exchange ประมวลผลคำสั่งก่อนเริ่มยืนยัน
    logger.info("ℹ️ Initial 5-second sleep before starting position confirmation attempts.")

    for attempt in range(CONFIRMATION_RETRIES):
        logger.info(f"⏳ ยืนยันโพซิชัน (Attempt {attempt + 1}/{CONFIRMATION_RETRIES})...")
        time.sleep(CONFIRMATION_SLEEP) # หน่วงเวลาระหว่างการยืนยัน

        try:
            position_info = get_current_position() # ใช้ get_current_position() ที่เราปรับปรุงแล้ว
            logger.debug(f"DEBUG: Position info in confirmation: {position_info}")

            if position_info and position_info.get('side') == expected_direction:
                actual_size = position_info.get('contracts', 0.0)
                confirmed_entry_price = position_info.get('entryPrice')

                if math.isclose(actual_size, expected_contracts, rel_tol=size_tolerance):
                    logger.info(f"✅ ยืนยันโพซิชันสำเร็จ:")
                    logger.info(f"   - Entry Price: {confirmed_entry_price:.2f}")
                    logger.info(f"   - Size: {actual_size:,.8f} Contracts")
                    logger.info(f"   - Direction: {expected_direction.upper()}")

                    current_position_details = {
                        'symbol': SYMBOL,
                        'side': expected_direction,
                        'contracts': actual_size,
                        'entry_price': confirmed_entry_price,
                        'unrealized_pnl': position_info.get('unrealizedPnl', 0.0),
                        'liquidation_price': position_info.get('liquidationPrice', None),
                        'sl_step': 0, # เริ่มต้นที่ Step 0
                        'sl_price': None, # จะถูกตั้งใน monitor_position
                        'tp_price': None, # จะถูกตั้งใน monitor_position
                        'initial_sl_price': None # จะถูกบันทึกเมื่อตั้ง SL ครั้งแรก
                    }

                    profit_loss = position_info.get('unrealizedPnl', 0)
                    send_telegram(
                        f"🎯 เปิดโพซิชัน {expected_direction.upper()} สำเร็จ!\n"
                        f"📊 ขนาด: {actual_size:,.8f} Contracts\n"
                        f"💰 Entry: {confirmed_entry_price:.2f}\n"
                        f"📈 P&L: {profit_loss:,.2f} USDT"
                    )

                    return True, confirmed_entry_price
                else:
                    logger.warning(f"⚠️ ขนาดโพซิชันไม่ตรงกัน (คาดหวัง: {expected_contracts:,.8f}, ได้: {actual_size:,.8f}). Tolerance: {size_tolerance:.8f}. Retrying...")
            else:
                logger.warning(f"⚠️ ไม่พบโพซิชันที่ตรงกัน (คาดหวัง: {expected_direction}) หรือไม่พบโพซิชันเลย. Retrying...")

        except Exception as e:
            logger.warning(f"⚠️ Error ในการยืนยันโพซิชัน: {e}", exc_info=True)

    logger.error(f"❌ ไม่สามารถยืนยันโพซิชันได้หลังจาก {CONFIRMATION_RETRIES} ครั้ง")
    send_telegram(
        f"⛔️ Position Confirmation Failed\n"
        f"🔍 กรุณาตรวจสอบโพซิชันใน Exchange ด่วน!\n"
        f"📊 คาดหวัง: {expected_direction.upper()} {expected_contracts:,.8f} Contracts"
    )

    return False, None


# ==============================================================================
# 10. ฟังก์ชันจัดการคำสั่งซื้อขาย
# ==============================================================================
def open_market_order(direction: str, current_price: float) -> tuple[bool, float | None]:
    global current_position_details

    try:
        balance = get_portfolio_balance()
        if balance <= MARGIN_BUFFER_USDT:
            error_msg = f"ยอดคงเหลือ ({balance:,.2f} USDT) ต่ำเกินไป ไม่เพียงพอสำหรับ Margin Buffer ({MARGIN_BUFFER_USDT} USDT)."
            send_telegram(f"⛔️ Balance Error: {error_msg}")
            logger.error(f"❌ {error_msg}")
            return False, None

        order_amount, estimated_used_margin = calculate_order_details(balance, current_price)

        if order_amount <= 0:
            error_msg = "❌ Calculated order amount is zero or insufficient. Cannot open position."
            send_telegram(f"⛔️ Order Calculation Error: {error_msg}")
            logger.error(f"❌ {error_msg}")
            return False, None

        # Determine display precision for order amount
        decimal_places = 0
        if market_info and 'limits' in market_info and 'amount' in market_info['limits'] and 'step' in market_info['limits']['amount'] and market_info['limits']['amount']['step'] is not None:
            step_size = market_info['limits']['amount']['step']
            if step_size < 1:
                decimal_places = int(round(-math.log10(step_size)))

        logger.info(f"ℹ️ Trading Summary:")
        logger.info(f"   - Balance: {balance:,.2f} USDT")
        logger.info(f"   - Contracts: {order_amount:,.{decimal_places}f}")
        logger.info(f"   - Required Margin (incl. buffer): {estimated_used_margin + MARGIN_BUFFER_USDT:,.2f} USDT")
        logger.info(f"   - Direction: {direction.upper()}")

        side = 'buy' if direction == 'long' else 'sell'
        params = {}

        order = None
        for attempt in range(3):
            logger.info(f"⚡️ ส่งคำสั่ง Market Order (Attempt {attempt + 1}/3) - {order_amount:,.{decimal_places}f} Contracts")
            try:
                order = exchange.create_market_order(
                    symbol=SYMBOL,
                    side=side,
                    amount=order_amount,
                    params=params
                )

                if order and order.get('id'):
                    logger.info(f"✅ Market Order ส่งสำเร็จ: ID → {order.get('id')}")
                    time.sleep(2) # ให้เวลา Exchange ประมวลผลคำสั่งก่อนยืนยัน
                    break
                else:
                    logger.warning(f"⚠️ Order response ไม่สมบูรณ์ (Attempt {attempt + 1}/3)")

            except ccxt.NetworkError as e:
                logger.warning(f"⚠️ Network Error (Attempt {attempt + 1}/3): {e}")
                if attempt == 2:
                    send_telegram(f"⛔️ Network Error: ไม่สามารถส่งออเดอร์ได้\n{str(e)[:200]}...")
                time.sleep(15)

            except ccxt.ExchangeError as e:
                logger.warning(f"⚠️ Exchange Error (Attempt {attempt + 1}/3): {e}")
                if attempt == 2:
                    send_telegram(f"⛔️ Exchange Error: ไม่สามารถส่งออเดอร์ได้\n{str(e)[:200]}...")
                time.sleep(15)

            except Exception as e:
                logger.error(f"❌ Unexpected error (Attempt {attempt + 1}/3): {e}", exc_info=True)
                send_telegram(f"⛔️ Unexpected Error: ไม่สามารถส่งออเดอร์ได้\n{str(e)[:200]}...")
                return False, None

        if not order:
            logger.error("❌ ล้มเหลวในการส่งออเดอร์หลังจาก 3 ครั้ง")
            send_telegram("⛔️ Order Failed: ล้มเหลวในการส่งออเดอร์หลังจาก 3 ครั้ง")
            return False, None

        return confirm_position_entry(direction, order_amount)

    except Exception as e:
        logger.error(f"❌ Critical Error in open_market_order: {e}", exc_info=True)
        send_telegram(f"⛔️ Critical Error: ไม่สามารถเปิดออเดอร์ได้\n{str(e)[:200]}...")
        return False, None

# ==============================================================================
# 11. ฟังก์ชันตั้งค่า TP/SL/กันทุน (ปรับปรุงสำหรับ Trailing SL)
# ==============================================================================

def cancel_all_open_tp_sl_orders():
    """ยกเลิกคำสั่ง TP/SL ที่ค้างอยู่สำหรับ Symbol ปัจจุบันบน Binance Futures."""
    logger.info(f"⏳ Checking for and canceling existing TP/SL orders for {SYMBOL}...")
    try:
        # Fetch only 'limit' and 'stop' types if possible for efficiency,
        # but fetch_open_orders usually gets all
        open_orders = exchange.fetch_open_orders(SYMBOL)

        canceled_count = 0
        for order in open_orders:
            # Check if it's an open/pending order that is intended to reduce position
            # or is a known TP/SL type.
            # Binance's TAKE_PROFIT_MARKET and STOP_MARKET are reduceOnly by default if opening position in opposite direction
            if (order['status'] == 'open' or order['status'] == 'pending') and \
               (order.get('reduceOnly', False) == True or \
                order['type'] in ['TAKE_PROFIT_MARKET', 'STOP_MARKET', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT']):
                try:
                    exchange.cancel_order(order['id'], SYMBOL)
                    logger.info(f"✅ Canceled old TP/SL order: ID {order['id']}, Type: {order['type']}, Side: {order['side']}, Price: {order.get('stopPrice') or order.get('price')}")
                    canceled_count += 1
                except ccxt.OrderNotFound:
                    logger.info(f"💡 Order {order['id']} not found or already canceled/filled. No action needed.")
                except ccxt.BaseError as e:
                    logger.warning(f"❌ Failed to cancel order {order['id']}: {str(e)}")

        if canceled_count == 0:
            logger.info("No old TP/SL orders found to cancel.")
        else:
            logger.info(f"✓ Successfully canceled {canceled_count} old TP/SL orders.")

    except ccxt.NetworkError as e:
        logger.error(f"❌ Network error while fetching/canceling open orders: {e}")
        send_telegram(f"⛔️ API Error: ไม่สามารถยกเลิก TP/SL เก่าได้ (Network)\nรายละเอียด: {e}")
    except ccxt.ExchangeError as e:
        logger.error(f"❌ Exchange error while fetching/canceling open orders: {e}")
        send_telegram(f"⛔️ API Error: ไม่สามารถยกเลิก TP/SL เก่าได้ (Exchange)\nรายละเอียด: {e}")
    except Exception as e:
        logger.error(f"❌ An unexpected error occurred while canceling orders: {e}", exc_info=True)
        send_telegram(f"⛔️ Unexpected Error: ไม่สามารถยกเลิก TP/SL เก่าได้\nรายละเอียด: {e}")


def set_tpsl_for_position(direction: str, amount: float, current_sl_price: float, current_tp_price: float) -> bool:
    """
    ตั้ง Take Profit และ Stop Loss สำหรับโพซิชัน.
    จะยกเลิก TP/SL ที่เปิดอยู่ก่อนเสมอแล้วตั้งใหม่
    """
    if not amount or amount <= 0:
        logger.error("❌ ไม่สามารถตั้ง TP/SL ได้: ขนาดโพซิชันเป็น 0 หรือไม่ถูกต้อง.")
        # ไม่ต้องส่ง Telegram เพราะอาจเกิดบ่อยหากมีปัญหาเล็กน้อย
        return False

    cancel_all_open_tp_sl_orders()
    time.sleep(1) # ให้เวลา Exchange ประมวลผลการยกเลิก

    market_info_precision_price = market_info['precision']['price']

    # ปัดราคา TP/SL ให้แม่นยำตาม Exchange
    tp_price_formatted = round_to_precision(current_tp_price, market_info_precision_price)
    sl_price_formatted = round_to_precision(current_sl_price, market_info_precision_price)

    logger.info(f"🎯 กำลังตั้ง TP: {tp_price_formatted:.2f} | 🛑 กำลังตั้ง SL: {sl_price_formatted:.2f}")

    try:
        tp_sl_side = 'sell' if direction == 'long' else 'buy' # Sell for Long, Buy for Short

        # ตั้ง Take Profit order
        # ใช้ TAKE_PROFIT_MARKET เพื่อให้แน่ใจว่าปิดที่ราคาตลาดเมื่อ Trigger
        tp_order = exchange.create_order(
            symbol=SYMBOL,
            type='TAKE_PROFIT_MARKET',
            side=tp_sl_side,
            amount=amount,
            price=None, # สำหรับ Market Order price เป็น None
            params={
                'stopPrice': tp_price_formatted, # ราคา Trigger
                'reduceOnly': True, # สำคัญ: เพื่อให้เป็นคำสั่งปิดโพซิชันเท่านั้น
            }
        )
        logger.info(f"✅ Take Profit order placed: ID → {tp_order.get('id', 'N/A')}")

        # ตั้ง Stop Loss order
        # ใช้ STOP_MARKET เพื่อให้แน่ใจว่าปิดที่ราคาตลาดเมื่อ Trigger
        sl_order = exchange.create_order(
            symbol=SYMBOL,
            type='STOP_MARKET',
            side=tp_sl_side,
            amount=amount,
            price=None, # สำหรับ Market Order price เป็น None
            params={
                'stopPrice': sl_price_formatted, # ราคา Trigger
                'reduceOnly': True, # สำคัญ: เพื่อให้เป็นคำสั่งปิดโพซิชันเท่านั้น
            }
        )
        logger.info(f"✅ Stop Loss order placed: ID → {sl_order.get('id', 'N/A')}")

        return True

    except ccxt.BaseError as e:
        logger.error(f"❌ Error setting TP/SL: {str(e)}", exc_info=True)
        send_telegram(f"⛔️ API Error (TP/SL): {e.args[0] if e.args else str(e)}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error setting TP/SL: {e}", exc_info=True)
        send_telegram(f"⛔️ Unexpected Error (TP/SL): {e}")
        return False


# ==============================================================================
# 12. ฟังก์ชันตรวจสอบสถานะและบริหารโพซิชัน (MONITORING FUNCTIONS)
# ==============================================================================

def monitor_position(current_market_price: float):
    global current_position_details, last_ema_position_status, monthly_stats

    logger.debug(f"🔄 กำลังตรวจสอบสถานะโพซิชัน: Current_Price={current_market_price}")

    # ดึงข้อมูลโพซิชันปัจจุบันจาก Exchange
    pos_info_from_exchange = get_current_position()

    # 1. ตรวจสอบว่าโพซิชันถูกปิดแล้ว (Exchange ไม่มี แต่บอทยังมีข้อมูล)
    if not pos_info_from_exchange and current_position_details:
        logger.info(f"ℹ️ โพซิชัน {current_position_details['side'].upper()} ถูกปิดแล้วใน Exchange.")

        closed_price = current_market_price
        pnl_usdt_actual = 0.0

        if current_position_details['entry_price'] and current_position_details['contracts']:
            if current_position_details['side'] == 'long':
                pnl_usdt_actual = (closed_price - current_position_details['entry_price']) * current_position_details['contracts']
            else:
                pnl_usdt_actual = (current_position_details['entry_price'] - closed_price) * current_position_details['contracts']

        close_reason = "ปิดโดยไม่ทราบสาเหตุ"
        emoji = "❓"

        # ใช้ TP_SL_BE_PRICE_TOLERANCE_PERCENT สำหรับการระบุสาเหตุการปิด
        # ควรใช้ tolerance เป็นค่าสัมบูรณ์จากราคาเข้า หรือใช้ Percentage จากราคา TP/SL
        # เพื่อความถูกต้อง ใช้เป็น 'จุด' เทียบกับราคาเข้า
        tolerance_points = current_position_details['entry_price'] * TP_SL_BE_PRICE_TOLERANCE_PERCENT

        # ตรวจสอบว่าปิดโดย TP, SL, หรือ SL (กันทุน)
        if current_position_details['side'] == 'long':
            # ปิดโดย TP
            if closed_price >= (current_position_details['entry_price'] + TP_DISTANCE_POINTS) - tolerance_points:
                close_reason = "TP"
                emoji = "✅"
            # ปิดโดย SL (กันทุน Step 2)
            elif current_position_details['sl_step'] >= 2 and \
                 abs(closed_price - (current_position_details['entry_price'] + TRAIL_SL_STEP2_NEW_SL_POINTS_LONG)) <= tolerance_points:
                close_reason = "SL (กันทุน Step 2)"
                emoji = "🛡️"
            # ปิดโดย SL (กันทุน Step 1)
            elif current_position_details['sl_step'] >= 1 and \
                 abs(closed_price - (current_position_details['entry_price'] + TRAIL_SL_STEP1_NEW_SL_POINTS_LONG)) <= tolerance_points:
                close_reason = "SL (กันทุน Step 1)"
                emoji = "🛡️"
            # ปิดโดย SL เริ่มต้น
            elif closed_price <= (current_position_details['initial_sl_price']) + tolerance_points:
                close_reason = "SL (เริ่มต้น)"
                emoji = "❌"
        elif current_position_details['side'] == 'short':
            # ปิดโดย TP
            if closed_price <= (current_position_details['entry_price'] - TP_DISTANCE_POINTS) + tolerance_points:
                close_reason = "TP"
                emoji = "✅"
            # ปิดโดย SL (กันทุน Step 2)
            elif current_position_details['sl_step'] >= 2 and \
                 abs(closed_price - (current_position_details['entry_price'] + TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT)) <= tolerance_points:
                close_reason = "SL (กันทุน Step 2)"
                emoji = "🛡️"
            # ปิดโดย SL (กันทุน Step 1)
            elif current_position_details['sl_step'] >= 1 and \
                 abs(closed_price - (current_position_details['entry_price'] + TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT)) <= tolerance_points:
                close_reason = "SL (กันทุน Step 1)"
                emoji = "🛡️"
            # ปิดโดย SL เริ่มต้น
            elif closed_price >= (current_position_details['initial_sl_price']) - tolerance_points:
                close_reason = "SL (เริ่มต้น)"
                emoji = "❌"

        send_telegram(f"{emoji} <b>ปิดออเดอร์ด้วย {close_reason}</b>\n<b>PnL (ประมาณ):</b> <code>{pnl_usdt_actual:,.2f} USDT</code>")
        logger.info(f"✅ โพซิชันปิด: {close_reason}, PnL (ประมาณ): {pnl_usdt_actual:.2f} USDT")
        add_trade_result(close_reason, pnl_usdt_actual)

        # **สำคัญ:** ยกเลิกคำสั่งทั้งหมดสำหรับ SYMBOL นี้ เมื่อโพซิชันถูกปิด
        try:
            exchange.cancel_all_orders(SYMBOL)
            logger.info(f"✅ ยกเลิกคำสั่งที่ค้างอยู่ทั้งหมดสำหรับ {SYMBOL} หลังจากปิดโพซิชันแล้ว.")
        except Exception as e:
            logger.warning(f"⚠️ ไม่สามารถยกเลิกคำสั่งทั้งหมดสำหรับ {SYMBOL} ได้หลังปิดโพซิชัน: {e}")
            send_telegram(f"⚠️ คำเตือน: ไม่สามารถยกเลิกคำสั่งทั้งหมดสำหรับ {SYMBOL} ได้หลังปิดโพซิชัน\nรายละเอียด: {e}")


        # รีเซ็ตตัวแปรสถานะ
        current_position_details = None
        # last_ema_position_status = None # ไม่ต้องรีเซ็ตตรงนี้ เพราะสถานะ EMA ควรต่อเนื่อง
        save_monthly_stats() # บันทึกสถานะล่าสุด (รวมถึง last_ema_position_status)
        return

    # 2. ถ้ามีโพซิชันเปิดอยู่ (ทั้งใน Exchange และในบอท)
    elif pos_info_from_exchange and current_position_details:
        # อัปเดต PnL ล่าสุดใน current_position_details
        current_position_details['unrealized_pnl'] = pos_info_from_exchange['unrealizedPnl']
        current_position_details['liquidation_price'] = pos_info_from_exchange['liquidationPrice']

        side = current_position_details['side']
        entry_price = current_position_details['entry_price']
        current_contracts = current_position_details['contracts']
        current_sl_step = current_position_details['sl_step']

        logger.info(f"📊 สถานะปัจจุบัน: {side.upper()}, PnL: {current_position_details['unrealized_pnl']:,.2f} USDT, ราคา: {current_market_price:,.1f}, เข้า: {entry_price:,.1f}, Size: {current_contracts:,.8f} Contracts")

        # --- การตั้ง TP/SL ครั้งแรก (หลังจากเปิดโพซิชันและยืนยันแล้ว) ---
        if current_position_details['tp_price'] is None or current_position_details['sl_price'] is None:
            tp_price_calc = entry_price + TP_DISTANCE_POINTS if side == 'long' else entry_price - TP_DISTANCE_POINTS
            sl_price_calc = entry_price - SL_DISTANCE_POINTS if side == 'long' else entry_price + SL_DISTANCE_POINTS

            # บันทึก SL เริ่มต้นเพื่อใช้ในการระบุสาเหตุการปิด
            current_position_details['initial_sl_price'] = sl_price_calc

            logger.info(f"ℹ️ ตั้ง TP/SL เริ่มต้นสำหรับโพซิชัน {side.upper()} Entry: {entry_price:.2f}. TP: {tp_price_calc:.2f}, SL: {sl_price_calc:.2f}")
            set_tpsl_for_position(side, current_contracts, sl_price_calc, tp_price_calc)
            current_position_details['tp_price'] = tp_price_calc
            current_position_details['sl_price'] = sl_price_calc
            # ไม่ต้อง return เพราะอาจจะเข้าเงื่อนไขเลื่อน SL ได้ทันทีในลูปถัดไป

        # --- Logic สำหรับ SL กันทุน 2 Step ---
        pnl_in_points = 0
        if side == 'long':
            pnl_in_points = current_market_price - entry_price
        elif side == 'short':
            pnl_in_points = entry_price - current_market_price

        current_sl_to_use = current_position_details['sl_price'] # SL ปัจจุบันในระบบ

        if side == 'long':
            # Step 1: ราคากำไรถึงจุด Trigger (TRAIL_SL_STEP1_TRIGGER_LONG_POINTS)
            if current_sl_step == 0 and pnl_in_points >= TRAIL_SL_STEP1_TRIGGER_LONG_POINTS:
                new_sl_price = entry_price + TRAIL_SL_STEP1_NEW_SL_POINTS_LONG
                current_position_details['sl_step'] = 1
                logger.info(f"⬆️ Long: ราคาถึง Step 1 ({pnl_in_points:,.0f} จุดกำไร). เลื่อน SL จาก {current_sl_to_use:,.2f} ไปที่ {new_sl_price:,.2f}")
                send_telegram(f"⬆️ <b>Long Position - SL เลื่อน! (Step 1)</b>\n"
                              f"ราคาเข้า: {entry_price:,.2f}\n"
                              f"ราคาปัจจุบัน: {current_market_price:,.2f}\n"
                              f"SL ใหม่: <code>{new_sl_price:,.2f}</code> ({TRAIL_SL_STEP1_NEW_SL_POINTS_LONG:+,} จุดจากราคาเข้า)")
                set_tpsl_for_position(
                    side,
                    current_contracts,
                    new_sl_price, # SL ใหม่
                    current_position_details['tp_price'] # TP เดิม
                )
                current_position_details['sl_price'] = new_sl_price # อัปเดต SL ในรายละเอียดโพซิชัน

            # Step 2: ราคากำไรถึงจุด Trigger (TRAIL_SL_STEP2_TRIGGER_LONG_POINTS)
            elif current_sl_step == 1 and pnl_in_points >= TRAIL_SL_STEP2_TRIGGER_LONG_POINTS:
                new_sl_price = entry_price + TRAIL_SL_STEP2_NEW_SL_POINTS_LONG
                current_position_details['sl_step'] = 2 # ตั้งเป็น Step 2 หรือสูงกว่า ไม่ให้เลื่อนอีก
                logger.info(f"💰 Long: ราคาถึง Step 2 ({pnl_in_points:,.0f} จุดกำไร). เลื่อน SL จาก {current_sl_to_use:,.2f} ไปที่ {new_sl_price:,.2f} (กันทุน+กำไร)")
                send_telegram(f"💰 <b>Long Position - SL เลื่อน! (Step 2)</b>\n"
                              f"ราคาเข้า: {entry_price:,.2f}\n"
                              f"ราคาปัจจุบัน: {current_market_price:,.2f}\n"
                              f"SL ใหม่: <code>{new_sl_price:,.2f}</code> ({TRAIL_SL_STEP2_NEW_SL_POINTS_LONG:+,} จุดจากราคาเข้า - กันทุน)")
                set_tpsl_for_position(
                    side,
                    current_contracts,
                    new_sl_price, # SL ใหม่
                    current_position_details['tp_price'] # TP เดิม
                )
                current_position_details['sl_price'] = new_sl_price # อัปเดต SL ในรายละเอียดโพซิชัน

        elif side == 'short':
            # --- ต้องเพิ่ม Logic สำหรับ Short Position ที่นี่ ---
            # Step 1 (Short): ราคากำไรถึงจุด Trigger (ลงไป -300 จุด)
            # if current_sl_step == 0 and pnl_in_points >= abs(TRAIL_SL_STEP1_TRIGGER_SHORT_POINTS): # Note: pnl_in_points for short is positive for profit
            #     new_sl_price = entry_price + TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT
            #     current_position_details['sl_step'] = 1
            #     logger.info(f"⬇️ Short: ราคาถึง Step 1 ({pnl_in_points:,.0f} จุดกำไร). เลื่อน SL จาก {current_sl_to_use:,.2f} ไปที่ {new_sl_price:,.2f}")
            #     send_telegram(f"⬇️ <b>Short Position - SL เลื่อน! (Step 1)</b>\n"
            #                   f"ราคาเข้า: {entry_price:,.2f}\n"
            #                   f"ราคาปัจจุบัน: {current_market_price:,.2f}\n"
            #                   f"SL ใหม่: <code>{new_sl_price:,.2f}</code> ({TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT:+,} จุดจากราคาเข้า)")
            #     set_tpsl_for_position(side, current_contracts, new_sl_price, current_position_details['tp_price'])
            #     current_position_details['sl_price'] = new_sl_price
            #
            # # Step 2 (Short): ราคากำไรถึงจุด Trigger (ลงไป -400 จุด)
            # elif current_sl_step == 1 and pnl_in_points >= abs(TRAIL_SL_STEP2_TRIGGER_SHORT_POINTS):
            #     new_sl_price = entry_price + TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT
            #     current_position_details['sl_step'] = 2
            #     logger.info(f"💰 Short: ราคาถึง Step 2 ({pnl_in_points:,.0f} จุดกำไร). เลื่อน SL จาก {current_sl_to_use:,.2f} ไปที่ {new_sl_price:,.2f} (กันทุน+กำไร)")
            #     send_telegram(f"💰 <b>Short Position - SL เลื่อน! (Step 2)</b>\n"
            #                   f"ราคาเข้า: {entry_price:,.2f}\n"
            #                   f"ราคาปัจจุบัน: {current_market_price:,.2f}\n"
            #                   f"SL ใหม่: <code>{new_sl_price:,.2f}</code> ({TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT:+,} จุดจากราคาเข้า - กันทุน)")
            #     set_tpsl_for_position(side, current_contracts, new_sl_price, current_position_details['tp_price'])
            #     current_position_details['sl_price'] = new_sl_price
            pass # ลบบรรทัดนี้เมื่อคุณเพิ่มโค้ดสำหรับ Short
    # 3. ถ้าไม่มีโพซิชันเปิดอยู่ (ทั้งใน Exchange และในบอท)
    else:
        # หากบอทคิดว่ามีโพซิชัน แต่ Exchange บอกว่าไม่มี (อาจเกิดจากบอท restart หรือ sync หลุด)
        # ให้เคลียร์สถานะในบอท และยกเลิกคำสั่งที่อาจค้างอยู่
        if current_position_details:
            logger.warning("⚠️ พบว่าบอทมีข้อมูลโพซิชันเก่า แต่ Exchange แจ้งว่าไม่มีโพซิชันเปิดอยู่. กำลังเคลียร์สถานะบอท...")
            send_telegram(f"⚠️ คำเตือน: สถานะโพซิชันในบอทไม่ตรงกับ Exchange. กำลังรีเซ็ตและยกเลิกคำสั่งที่ค้างอยู่.")
            try:
                exchange.cancel_all_orders(SYMBOL)
                logger.info(f"✅ ยกเลิกคำสั่งที่ค้างอยู่ทั้งหมดสำหรับ {SYMBOL} เรียบร้อยแล้ว.")
            except Exception as e:
                logger.warning(f"❌ ไม่สามารถยกเลิกคำสั่งทั้งหมดสำหรับ {SYMBOL} ได้: {e}")
            current_position_details = None
            # last_ema_position_status = None # ไม่ต้องรีเซ็ตตรงนี้ เพราะสถานะ EMA ควรต่อเนื่อง
            save_monthly_stats() # บันทึกสถานะล่าสุด
        else:
            logger.info("🔎 ไม่มีโพซิชันเปิดอยู่.")


# ==============================================================================
# 13. ฟังก์ชันรายงานประจำเดือน (MONTHLY REPORT FUNCTIONS)
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

    # Check if it's the specific day/time for the report
    report_day_of_month = min(MONTHLY_REPORT_DAY, calendar.monthrange(now.year, now.month)[1])
    if not (now.day == report_day_of_month and now.hour == MONTHLY_REPORT_HOUR and now.minute == MONTHLY_REPORT_MINUTE):
        logger.debug(f"ℹ️ ยังไม่ถึงเวลาส่งรายงานประจำเดือน ({report_day_of_month} {MONTHLY_REPORT_HOUR:02d}:{MONTHLY_REPORT_MINUTE:02d}).")
        return

    try:
        balance = get_portfolio_balance()

        # If stats loaded are for a different month, reset them before generating report for current month
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

        # ถ้าถึงเวลาส่งรายงานสำหรับเดือนนี้แล้ว และยังไม่ได้ส่ง
        if now >= next_report_time_this_month and \
           (last_monthly_report_date is None or \
            last_monthly_report_date.year != now.year or \
            last_monthly_report_date.month != now.month):
            logger.info(f"⏰ ตรวจพบว่าถึงเวลาส่งรายงานประจำเดือน ({now.strftime('%H:%M')}) และยังไม่ได้ส่งสำหรับเดือนนี้. กำลังส่ง...")
            monthly_report()
            # หลังจากส่งแล้ว คำนวณเวลาสำหรับเดือนถัดไป
            # (ไม่ต้องเปลี่ยน next_report_time_this_month ตรงนี้ เพราะมันจะถูกคำนวณใหม่ในลูปถัดไป)

        # คำนวณเวลาที่จะรอจนกว่าจะถึงเวลาส่งรายงานรอบถัดไป
        # ต้องคำนวณสำหรับเดือนถัดไปเสมอ หากรอบของเดือนนี้ผ่านไปแล้ว
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
            time.sleep(max(time_to_wait, 60)) # อย่างน้อย 1 นาที
        else:
            time.sleep(60) # ถ้าเวลาติดลบหรือ 0 (เช่น รันเลยเวลาไปแล้ว) ให้รอ 1 นาทีแล้วเช็คใหม่

# ==============================================================================
# 14. ฟังก์ชันเริ่มต้นบอท (BOT STARTUP FUNCTIONS)
# ==============================================================================
def send_startup_message():
    global initial_balance

    try:
        initial_balance = get_portfolio_balance() # ดึงยอดคงเหลือ ณ ตอนเริ่มต้น
        startup_time = datetime.now().strftime('%d/%m/%Y %H:%M:%S')

        message = f"""🔄 <b>บอทเริ่มทำงาน</b>
<b>🤖 EMA Cross Trading Bot</b>
<b>💰 ยอดเริ่มต้น:</b> <code>{initial_balance:,.2f} USDT</code>
<b>⏰ เวลาเริ่ม:</b> <code>{startup_time}</code>
<b>📊 เฟรม:</b> <code>{TIMEFRAME}</code> | <b>Leverage:</b> <code>{LEVERAGE}x</code>
<b>🎯 TP:</b> <code>{TP_DISTANCE_POINTS}</code> | <b>SL (เริ่มต้น):</b> <code>{SL_DISTANCE_POINTS}</code>
<b>📈 Trailing SL (Long):</b> Step1:{TRAIL_SL_STEP1_TRIGGER_LONG_POINTS}pts->SL({TRAIL_SL_STEP1_NEW_SL_POINTS_LONG:+,}pts), Step2:{TRAIL_SL_STEP2_TRIGGER_LONG_POINTS}pts->SL({TRAIL_SL_STEP2_NEW_SL_POINTS_LONG:+,}pts)
<b>🔧 Margin Buffer:</b> <code>{MARGIN_BUFFER_USDT:,.0f} USDT</code>
<b>🌐 Railway Region:</b> <code>{os.getenv('RAILWAY_REGION', 'Unknown')}</code>
<b>🔍 กำลังรอสัญญาณ EMA Cross...</b>"""

        send_telegram(message)
        logger.info("✅ ส่งข้อความแจ้งเตือนเมื่อบอทเริ่มทำงาน.")

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการส่งข้อความเริ่มต้น: {e}", exc_info=True)

# ==============================================================================
# 15. ฟังก์ชันหลักของบอท (MAIN BOT LOGIC)
# ==============================================================================
def main():
    global current_position_details, last_ema_position_status

    try:
        setup_exchange() # เชื่อมต่อ Exchange
        load_monthly_stats() # โหลดสถิติเก่า
        send_startup_message() # ส่งข้อความเริ่มบอท

        # เริ่ม Monthly Report Scheduler ใน Thread แยก
        monthly_thread = threading.Thread(target=monthly_report_scheduler, daemon=True)
        monthly_thread.start()
        logger.info("✅ Monthly Report Scheduler Thread Started.")

    except Exception as e:
        error_msg = f"⛔️ Error: ไม่สามารถเริ่มต้นบอทได้\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
        send_telegram(error_msg)
        logger.critical(f"❌ Startup error: {e}", exc_info=True)
        time.sleep(ERROR_RETRY_SLEEP_SECONDS)
        sys.exit(1) # ออกจากโปรแกรมหาก Startup ล้มเหลว

    logger.info("🚀 บอทเข้าสู่ Main Loop แล้วและพร้อมทำงาน...")
    while True:
        try:
            logger.info(f"🔄 เริ่มรอบ Main Loop ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) - กำลังดึงข้อมูลและตรวจสอบ.")

            # 1. ดึงราคาปัจจุบัน
            current_price = None
            try:
                logger.info("📊 กำลังดึงราคาล่าสุด (Ticker)...")
                ticker = exchange.fetch_ticker(SYMBOL)
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"⚠️ Error fetching ticker: {e}. Retrying in {ERROR_RETRY_SLEEP_SECONDS} วินาที...")
                send_telegram(f"⛔️ API Error: ไม่สามารถดึงราคาล่าสุดได้. รายละเอียด: {e.args[0] if e.args else str(e)}")
                time.sleep(ERROR_RETRY_SLEEP_SECONDS)
                continue

            if not ticker or 'last' not in ticker:
                logger.error("❌ Failed to fetch valid ticker. Skipping loop and retrying.")
                send_telegram("⛔️ Error: ไม่สามารถดึงราคาล่าสุดได้ถูกต้อง. Skipping.")
                time.sleep(ERROR_RETRY_SLEEP_SECONDS)
                continue
            current_price = float(ticker['last'])
            logger.info(f"💲 ราคาปัจจุบันของ {SYMBOL}: {current_price:,.2f}")


            # 2. ตรวจสอบสถานะและบริหารจัดการโพซิชันที่มีอยู่ (รวมถึง Trailing SL)
            monitor_position(current_price)


            # 3. ตรวจสอบสัญญาณและเปิดโพซิชันใหม่ (ถ้าไม่มีโพซิชันเปิดอยู่)
            if current_position_details is None:
                logger.info("🔍 ไม่มีโพซิชันเปิดอยู่. กำลังตรวจสอบสัญญาณ EMA Cross เพื่อเปิดโพซิชัน...")
                signal = check_ema_cross()

                if signal:
                    logger.info(f"🌟 ตรวจพบสัญญาณ EMA Cross: {signal.upper()}. กำลังพยายามเปิดออเดอร์.")
                    send_telegram(f"✨ <b>SIGNAL:</b> ตรวจพบสัญญาณ EMA Cross: <b>{signal.upper()}</b>")

                    market_order_success, confirmed_entry_price = open_market_order(signal, current_price)

                    if market_order_success and confirmed_entry_price:
                        # TP/SL จะถูกตั้งใน monitor_position() ทันทีหลังจาก position_details ถูกอัปเดต
                        logger.info(f"✅ เปิดออเดอร์ {signal.upper()} สำเร็จ. รอการตั้ง TP/SL ในรอบถัดไป.")
                    else:
                        logger.warning(f"⚠️ ไม่สามารถเปิด Market Order {signal.upper()} ได้.")
                else:
                    logger.info("🔎 ไม่พบสัญญาณ EMA Cross ที่ชัดเจน.")
            else:
                logger.info(f"Current Position: {current_position_details['side'].upper()}, SL Step: {current_position_details['sl_step']}. รอการปิดหรือเลื่อน SL เพิ่มเติม.")


            logger.info(f"😴 จบรอบ Main Loop. รอ {MAIN_LOOP_SLEEP_SECONDS} วินาทีสำหรับรอบถัดไป.")
            time.sleep(MAIN_LOOP_SLEEP_SECONDS)

        except KeyboardInterrupt:
            logger.info("🛑 บอทหยุดทำงานโดยผู้ใช้ (KeyboardInterrupt).")
            send_telegram("🛑 Bot หยุดทำงานโดยผู้ใช้.")
            break # ออกจากลูปหลัก
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            error_msg = f"⛔️ API Error ใน Main Loop\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
            logger.error(error_msg, exc_info=True)
            send_telegram(error_msg)
            time.sleep(ERROR_RETRY_SLEEP_SECONDS)
        except Exception as e:
            error_msg = f"⛔️ Error: เกิดข้อผิดพลาดที่ไม่คาดคิดใน Main Loop\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
            logger.error(error_msg, exc_info=True)
            send_telegram(error_msg)
            time.sleep(ERROR_RETRY_SLEEP_SECONDS)

# ==============================================================================
# 16. จุดเริ่มต้นการทำงานของโปรแกรม (ENTRY POINT)
# ==============================================================================
if __name__ == '__main__':
    main()
