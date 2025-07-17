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
# Removed: import pandas as pd (reverted to manual EMA calculation)

# ==============================================================================
# 1. ตั้งค่าพื้นฐาน (CONFIGURATION)
# ==============================================================================

# --- API Keys & Credentials (ดึงจาก Environment Variables เพื่อความปลอดภัย) ---
API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('BINANCE_SECRET', 'YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING')

# --- Trade Parameters ---
SYMBOL = 'BTC/USDT:USDT'
TIMEFRAME = '15m'
LEVERAGE = 34
TP_DISTANCE_POINTS = 501
SL_DISTANCE_POINTS = 999

# Breakeven & Multi-Step SL Settings (BE_PROFIT_TRIGGER_POINTS และ BE_SL_BUFFER_POINTS จะไม่ถูกใช้โดยตรงแล้ว)
BE_PROFIT_TRIGGER_POINTS = 350 # ยังคงไว้แต่ไม่ได้ถูกเรียกใช้โดยตรงใน manage_stop_loss
BE_SL_BUFFER_POINTS = 100    # ยังคงไว้แต่ไม่ได้ถูกเรียกใช้โดยตรงใน manage_stop_loss

# New: Multi-step SL settings
SL_MOVE_STEP1_PROFIT_TRIGGER_POINTS = 300  # เมื่อราคา + 300 จุดจากทุน
SL_MOVE_STEP1_SL_POINTS = 400            # SL ใหม่ที่ - 400 จุดจากทุน (ระยะจาก entry_price)
SL_MOVE_STEP2_PROFIT_TRIGGER_POINTS = 400  # เมื่อราคา + 400 จุดจากทุน
SL_MOVE_STEP2_SL_POINTS = -100            # SL ใหม่ที่ + 100 จุดจากทุน (ระยะจาก entry_price, คือกำไร 100 จุด)

CROSS_THRESHOLD_POINTS = 100 # หมายถึง EMA ต้องห่างกันอย่างน้อย 1 จุดเพื่อเป็นสัญญาณที่ชัดเจน

# เพิ่มค่าตั้งค่าใหม่สำหรับการบริหารความเสี่ยงและออเดอร์
MARGIN_BUFFER_USDT = 5
TARGET_POSITION_SIZE_FACTOR = 0.8

# ค่าสำหรับยืนยันโพซิชันหลังเปิดออเดอร์ (ใช้ใน confirm_position_entry)
CONFIRMATION_RETRIES = 15
CONFIRMATION_SLEEP = 5

# --- Fee Settings ---
TAKER_FEE_RATE = 0.0004 # 0.04% สำหรับ Taker Fee. โปรดตรวจสอบอัตราค่าธรรมเนียมจริงของบัญชีคุณบน Binance

# --- Telegram Notification Settings ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# --- Files & Paths ---
STATS_FILE = 'trading_stats.json' # ควรเปลี่ยนเป็น '/data/trading_stats.json' หากใช้ Railway Volume

# --- Bot Timing ---
MAIN_LOOP_SLEEP_SECONDS = 180 # 180 วินาที (3 นาที)
ERROR_RETRY_SLEEP_SECONDS = 60
MONTHLY_REPORT_DAY = 20
MONTHLY_REPORT_HOUR = 0
MONTHLY_REPORT_MINUTE = 5

# --- Tolerance สำหรับการระบุสาเหตุการปิดออเดอร์ ---
TP_SL_BE_PRICE_TOLERANCE_PERCENT = 0.005

# ==============================================================================
# 2. การตั้งค่า Logging
# ==============================================================================
logging.basicConfig(
    level=logging.DEBUG, # ยังคงตั้งเป็น DEBUG เพื่อดีบักปัญหา EMA Cross และการทำงานของบอท
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
for handler in logging.root.handlers:
    if hasattr(handler, 'flush'):
        handler.flush = lambda: sys.stdout.flush() if isinstance(handler, logging.StreamHandler) else handler.stream.flush()

logger = logging.getLogger(__name__)


# ==============================================================================
# 3. ตัวแปรสถานะการเทรด (GLOBAL TRADE STATE VARIABLES)
# ==============================================================================
current_position_details = None
entry_price = None
sl_moved = False # ใช้บ่งชี้ว่าเคยมีการเลื่อน SL ไปที่กันทุนแล้วหรือไม่ (สำหรับ BE_PROFIT_TRIGGER_POINTS) - **ตัวแปรนี้จะไม่ได้ถูกใช้โดยตรงใน manage_stop_loss แล้ว**
sl_moved_to_step1 = False # New: สถานะการเลื่อน SL Step 1
sl_moved_to_step2 = False # New: สถานะการเลื่อน SL Step 2

portfolio_balance = 0.0
last_monthly_report_date = None
initial_balance = 0.0
current_position_size = 0.0 # ขนาดโพซิชันในหน่วย Contracts
last_ema_position_status = None

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
    'last_ema_cross_signal': None, # Keep this for signal tracking logic
    'last_ema_position_status': None,
    'sl_moved_to_step1_status': False, # New: บันทึกสถานะ SL Step 1
    'sl_moved_to_step2_status': False  # New: บันทึกสถานะ SL Step 2
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
            raise ValueError("API_KEY หรือ SECRET ไม่ถูกตั้งค่าใน Environment Variables.")

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

        # --- ตรวจสอบและกำหนดค่าเริ่มต้นที่เหมาะสมสำหรับ limits ---
        if 'limits' not in market_info:
            market_info['limits'] = {}
        if 'amount' not in market_info['limits']:
            market_info['limits']['amount'] = {}
        if 'cost' not in market_info['limits']:
            market_info['limits']['cost'] = {}

        amount_step = market_info['limits']['amount'].get('step')
        market_info['limits']['amount']['step'] = float(amount_step) if amount_step is not None else 0.001

        amount_min = market_info['limits']['amount'].get('min')
        market_info['limits']['amount']['min'] = float(amount_min) if amount_min is not None else 0.001

        amount_max = market_info['limits']['amount'].get('max')
        market_info['limits']['amount']['max'] = float(amount_max) if amount_max is not None else sys.float_info.max # ใช้ sys.float_info.max

        cost_min = market_info['limits']['cost'].get('min')
        market_info['limits']['cost']['min'] = float(cost_min) if cost_min is not None else 5.0

        cost_max = market_info['limits']['cost'].get('max')
        market_info['limits']['cost']['max'] = float(cost_max) if cost_max is not None else sys.float_info.max

        logger.debug(f"DEBUG: Market info limits for {SYMBOL}:")
        logger.debug(f"   Amount: step={market_info['limits']['amount']['step']}, min={market_info['limits']['amount']['min']}, max={market_info['limits']['amount']['max']}")
        logger.debug(f"   Cost: min={market_info['limits']['cost']['min']}, max={market_info['limits']['cost']['max']}")

        try:
            result = exchange.set_leverage(LEVERAGE, SYMBOL)
            logger.info(f"✅ ตั้งค่า Leverage เป็น {LEVERAGE}x สำหรับ {SYMBOL}: {result}")
        except ccxt.ExchangeError as e:
            if "leverage is not valid" in str(e) or "not valid for this symbol" in str(e):
                logger.critical(f"❌ Error: Leverage {LEVERAGE}x ไม่ถูกต้องสำหรับ {SYMBOL} บน Binance. โปรดตรวจสอบ Max Allowed Leverage.")
            else:
                logger.critical(f"❌ Error ในการตั้งค่า Leverage: {e}", exc_info=True)
            exit()

    except ValueError as ve:
        logger.critical(f"❌ Configuration Error: {ve}", exc_info=True)
        exit()
    except Exception as e:
        logger.critical(f"❌ ไม่สามารถเชื่อมต่อหรือโหลดข้อมูล Exchange เบื้องต้นได้: {e}", exc_info=True)
        exit()

# ==============================================================================
# 6. ฟังก์ชันจัดการสถิติ (STATISTICS MANAGEMENT FUNCTIONS)
# ==============================================================================

def save_monthly_stats():
    global monthly_stats, last_ema_position_status, sl_moved_to_step1, sl_moved_to_step2
    try:
        monthly_stats['last_ema_position_status'] = last_ema_position_status
        monthly_stats['sl_moved_to_step1_status'] = sl_moved_to_step1 # Save new status
        monthly_stats['sl_moved_to_step2_status'] = sl_moved_to_step2 # Save new status
        with open(os.path.join(os.getcwd(), STATS_FILE), 'w') as f:
            json.dump(monthly_stats, f, indent=4)
        logger.debug(f"💾 บันทึกสถิติการเทรดลงไฟล์ {STATS_FILE} สำเร็จ")
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการบันทิติสถิติ: {e}")

def reset_monthly_stats():
    global monthly_stats, last_ema_position_status, sl_moved, sl_moved_to_step1, sl_moved_to_step2
    monthly_stats['month_year'] = datetime.now().strftime('%Y-%m')
    monthly_stats['tp_count'] = 0
    monthly_stats['sl_count'] = 0
    monthly_stats['total_pnl'] = 0.0
    monthly_stats['trades'] = []
    last_ema_position_status = None
    sl_moved = False # Reset on new month (legacy flag, still kept for safety but not primary)
    sl_moved_to_step1 = False # Reset on new month
    sl_moved_to_step2 = False # Reset on new month
    save_monthly_stats()
    logger.info(f"🔄 รีเซ็ตสถิติประจำเดือนสำหรับเดือน {monthly_stats['month_year']}")

def load_monthly_stats():
    global monthly_stats, last_monthly_report_date, last_ema_position_status, sl_moved_to_step1, sl_moved_to_step2
    try:
        stats_file_path = os.path.join(os.getcwd(), STATS_FILE)
        if os.path.exists(stats_file_path):
            with open(stats_file_path, 'r') as f:
                loaded_stats = json.load(f)

                monthly_stats['month_year'] = loaded_stats.get('month_year', None)
                monthly_stats['tp_count'] = loaded_stats.get('tp_count', 0)
                monthly_stats['sl_count'] = loaded_stats.get('sl_count', 0)
                monthly_stats['total_pnl'] = loaded_stats.get('total_pnl', 0.0)
                monthly_stats['trades'] = loaded_stats.get('trades', [])
                monthly_stats['last_report_month_year'] = loaded_stats.get('last_report_month_year', None)
                monthly_stats['last_ema_cross_signal'] = loaded_stats.get('last_ema_cross_signal', None)
                last_ema_position_status = loaded_stats.get('last_ema_position_status', None)
                sl_moved_to_step1 = loaded_stats.get('sl_moved_to_step1_status', False) # Load new status
                sl_moved_to_step2 = loaded_stats.get('sl_moved_to_step2_status', False) # Load new status

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
            'last_report_month_year': None, 'last_ema_cross_signal': None, 'last_ema_position_status': None,
            'sl_moved_to_step1_status': False, 'sl_moved_to_step2_status': False
        }
        last_monthly_report_date = None
        last_ema_position_status = None
        sl_moved_to_step1 = False
        sl_moved_to_step2 = False
        reset_monthly_stats()

def add_trade_result(reason: str, pnl: float):
    global monthly_stats, sl_moved, sl_moved_to_step1, sl_moved_to_step2
    current_month_year_str = datetime.now().strftime('%Y-%m')

    if monthly_stats['month_year'] != current_month_year_str:
        logger.info(f"🆕 เดือนเปลี่ยนใน add_trade_result: {monthly_stats['month_year']} -> {current_month_year_str}. กำลังรีเซ็ตสถิติประจำเดือน.")
        reset_monthly_stats()

    if reason.upper() == 'TP':
        monthly_stats['tp_count'] += 1
    elif reason.upper() == 'SL' or reason.upper() == 'SL (กันทุน)' or reason.upper() == 'SL (STEP 1)' or reason.upper() == 'SL (STEP 2)' or reason.upper() == 'SL (ORIGINAL)':
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
            time.sleep(1)

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
    retries = 3
    for i in range(retries):
        try:
            logger.debug(f"🔍 กำลังดึงโพซิชันปัจจุบัน (Attempt {i+1}/{retries})...")
            positions = exchange.fetch_positions([SYMBOL])
            logger.debug(f"DEBUG: Raw positions fetched: {positions}")
            time.sleep(1)

            for pos in positions:
                if pos['symbol'] == SYMBOL and float(pos['contracts']) != 0:
                    pos_amount = float(pos['contracts'])
                    return {
                        'side': 'long' if pos_amount > 0 else 'short',
                        'size': abs(pos_amount),
                        'entry_price': float(pos['entryPrice']),
                        'unrealized_pnl': float(pos['unrealizedPnl']),
                        'pos_id': pos.get('id', 'N/A')
                    }
            return None
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.warning(f"⚠️ Error fetching positions (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
            if i == retries - 1:
                send_telegram(f"⛔️ API Error: ไม่สามารถดึงโพซิชันได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
            time.sleep(15)
        except Exception as e:
            logger.error(f"❌ Unexpected error in get_current_position: {e}", exc_info=True)
            send_telegram(f"⛔️️ Unexpected Error: ไม่สามารถดึงโพซิชันได้\nรายละเอียด: {e}")
            return None
    logger.error(f"❌ Failed to fetch positions after {retries} attempts.")
    send_telegram(f"⛔️ API Error: ล้มเหลวในการดึงโพซิชันหลังจาก {retries} ครั้ง.")
    return None

# ==============================================================================
# 9. ฟังก์ชันคำนวณ Indicators (INDICATOR CALCULATION FUNCTIONS)
# ==============================================================================

# This is the manual EMA calculation function that you preferred.
def calculate_ema(prices: list[float], period: int) -> float | None:
    if len(prices) < period:
        return None

    # Calculate initial SMA for the first 'period' values
    sma = sum(prices[:period]) / period
    ema = sma # Initialize EMA with SMA

    multiplier = 2 / (period + 1)

    # Calculate EMA for the rest of the prices
    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))

    return ema

def check_ema_cross() -> str | None:
    global last_ema_position_status, monthly_stats # Added monthly_stats for last_ema_cross_signal tracking

    try:
        retries = 3
        ohlcv = None
        for i in range(retries):
            logger.debug(f"🔍 กำลังดึงข้อมูล OHLCV สำหรับ EMA ({i+1}/{retries})...")
            try:
                # Ensure enough data for EMA200 (need at least 200 candles + 1 for current price = 201)
                ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=201)
                time.sleep(1)
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

        if not ohlcv:
            logger.error(f"❌ Failed to fetch OHLCV after {retries} attempts.")
            send_telegram(f"⛔️ API Error: ล้มเหลวในการดึง OHLCV หลังจาก {retries} ครั้ง.")
            return None

        if len(ohlcv) < 201:
            logger.warning(f"ข้อมูล OHLCV ไม่เพียงพอ. ต้องการอย่างน้อย 201 แท่ง ได้ {len(ohlcv)}")
            send_telegram(f"⚠️ ข้อมูล OHLCV ไม่เพียงพอ ({len(ohlcv)} แท่ง).")
            return None

        closes = [candle[4] for candle in ohlcv]

        ema50_current = calculate_ema(closes, 50)
        ema200_current = calculate_ema(closes, 200)

        logger.info(f"💡 EMA Values: Current EMA50={ema50_current:,.2f}, EMA200={ema200_current:,.2f}")

        if ema50_current is None or ema200_current is None: # Check for None from manual calculate_ema
            logger.warning("ค่า EMA ไม่สามารถคำนวณได้ (เป็น None).")
            return None

        current_ema_position = None
        if ema50_current > ema200_current:
            current_ema_position = 'above'
        elif ema50_current < ema200_current:
            current_ema_position = 'below'

        # ตรวจสอบสถานะเริ่มต้นของบอท หรือเมื่อมีการรีเซ็ตสถิติ
        if last_ema_position_status is None:
            if current_ema_position:
                last_ema_position_status = current_ema_position
                monthly_stats['last_ema_cross_signal'] = None # Reset previous cross signal
                save_monthly_stats()
                logger.info(f"ℹ️ บอทเพิ่งเริ่มรัน/รีเซ็ต. บันทึกสถานะ EMA ปัจจุบันเป็น: {current_ema_position.upper()}. จะรอสัญญาณการตัดกันครั้งถัดไป.")
            return None # ไม่ส่งสัญญาณในรอบแรกของการรัน/รีเซ็ต (เพื่อกำหนดสถานะเริ่มต้น)

        cross_signal = None

        # Logic ใหม่: ตรวจสอบว่าปัจจุบัน EMA อยู่ในสถานะที่ควรจะเป็นสำหรับสัญญาณนั้นๆ และห่างพอ
        # และยังไม่เคยส่งสัญญาณนี้ไปแล้วตั้งแต่ครั้งล่าสุด (หรือสถานะ EMA เพิ่งเปลี่ยน)

        # ตรวจสอบ Golden Cross (Long Signal)
        # EMA50 ต้องอยู่เหนือ EMA200 และห่างกันเกิน CROSS_THRESHOLD_POINTS
        # และ (สถานะเดิมต้องเป็น 'below' หรือยังไม่เคยส่งสัญญาณ 'long' ตั้งแต่ปิดโพซิชันล่าสุด)
        if current_ema_position == 'above' and \
           ema50_current > (ema200_current + CROSS_THRESHOLD_POINTS):
            if last_ema_position_status == 'below' or monthly_stats.get('last_ema_cross_signal') != 'long':
                cross_signal = 'long'
                logger.info(f"🚀 Threshold Golden Cross: EMA50({ema50_current:,.2f}) is {CROSS_THRESHOLD_POINTS} points above EMA200({ema200_current:,.2f})")

        # ตรวจสอบ Death Cross (Short Signal)
        # EMA50 ต้องอยู่ใต้ EMA200 และห่างกันเกิน CROSS_THRESHOLD_POINTS
        # และ (สถานะเดิมต้องเป็น 'above' หรือยังไม่เคยส่งสัญญาณ 'short' ตั้งแต่ปิดโพซิชันล่าสุด)
        elif current_ema_position == 'below' and \
             ema50_current < (ema200_current - CROSS_THRESHOLD_POINTS):
            if last_ema_position_status == 'above' or monthly_stats.get('last_ema_cross_signal') != 'short':
                cross_signal = 'short'
                logger.info(f"🔻 Threshold Death Cross: EMA50({ema50_current:,.2f}) is {CROSS_THRESHOLD_POINTS} points below EMA200({ema200_current:,.2f})")


        # อัปเดตสถานะ EMA ล่าสุดเสมอหลังจากการประเมินสัญญาณ
        # มีการปรับปรุง Log เพื่อให้เห็นชัดเจนขึ้น
        if cross_signal is not None:
            logger.info(f"✨ สัญญาณ EMA Cross ที่ตรวจพบ: {cross_signal.upper()}")
            # อัปเดตสถานะ EMA ล่าสุดเมื่อมีสัญญาณ
            last_ema_position_status = current_ema_position
            monthly_stats['last_ema_cross_signal'] = cross_signal # บันทึกสัญญาณที่เพิ่งเกิดไป
            save_monthly_stats()

        elif current_ema_position != last_ema_position_status: # ถ้าไม่มี cross_signal แต่สถานะ EMA เปลี่ยนแปลง
            logger.info(f"ℹ️ EMA position changed from {last_ema_position_status.upper()} to {current_ema_position.upper()}. Updating last_ema_position_status (no cross signal detected).")
            last_ema_position_status = current_ema_position
            monthly_stats['last_ema_cross_signal'] = None # Clear previous signal if position just changed
            save_monthly_stats()

        else: # ไม่มีสัญญาณ และสถานะไม่เปลี่ยนแปลง
            logger.info("🔎 ไม่พบสัญญาณ EMA Cross ที่ชัดเจน.")

        return cross_signal

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการคำนวณ EMA: {e}", exc_info=True)
        send_telegram(f"⛔️ Error: ไม่สามารถคำนวณ EMA ได้\nรายละเอียด: {e}")
        return None

# ==============================================================================
# 10. ฟังก์ชันช่วยสำหรับการคำนวณและตรวจสอบออเดอร์ (ปรับปรุงแก้ไข KeyError และ Contracts=0)
# ==============================================================================

def calculate_order_details(available_usdt: float, price: float) -> tuple[float, float]:
    """
    คำนวณจำนวนสัญญาที่จะเปิดและ Margin ที่ต้องใช้ โดยพิจารณาจาก Exchange Limits
    และ Margin Buffer.
    """
    if price <= 0 or LEVERAGE <= 0 or TARGET_POSITION_SIZE_FACTOR <= 0:
        logger.error("Error: Price, leverage, and target_position_size_factor must be positive.")
        return (0, 0)

    if not market_info:
        logger.error(f"❌ Could not retrieve market info for {SYMBOL}. Please ensure setup_exchange ran successfully.")
        return (0, 0)

    try:
        exchange_amount_step = float(market_info['limits']['amount'].get('step', '0.001'))
        min_exchange_amount = float(market_info['limits']['amount'].get('min', '0.001'))
        max_exchange_amount = float(market_info['limits']['amount'].get('max', str(sys.float_info.max)))
        min_notional_exchange = float(market_info['limits']['cost'].get('min', '5.0'))
        max_notional_exchange = float(market_info['limits']['cost'].get('max', str(sys.float_info.max)))
    except (TypeError, ValueError) as e:
        logger.critical(f"❌ Error parsing market limits for {SYMBOL}: {e}. Check API response structure. Exiting.", exc_info=True)
        send_telegram(f"⛔️ Critical Error: Cannot parse market limits for {SYMBOL}.\nDetails: {e}")
        return (0, 0)


    max_notional_from_available_margin = (available_usdt - MARGIN_BUFFER_USDT) * LEVERAGE
    if max_notional_from_available_margin <= 0:
        logger.warning(f"❌ Available margin ({available_usdt:.2f}) too low after buffer ({MARGIN_BUFFER_USDT}) for any notional value.")
        return (0, 0)

    target_notional_for_order = max_notional_from_available_margin * TARGET_POSITION_SIZE_FACTOR

    min_notional_from_min_amount = min_exchange_amount * price
    target_notional_for_order = max(target_notional_for_order, min_notional_exchange, min_notional_from_min_amount)
    target_notional_for_order = min(target_notional_for_order, max_notional_exchange)

    contracts_raw = target_notional_for_order / price

    if exchange_amount_step == 0:
        logger.error(f"❌ Exchange amount step is 0 for {SYMBOL}. Cannot calculate precision. Defaulting to raw amount.")
        contracts_to_open = contracts_raw
    else:
        contracts_to_open = float(exchange.amount_to_precision(SYMBOL, contracts_raw))

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

    logger.debug(f"💡 DEBUG (calculate_order_details): Max Notional from Available Margin: {max_notional_from_available_margin:.2f}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Target Position Size Factor: {TARGET_POSITION_SIZE_FACTOR}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Final Target Notional for Order: {target_notional_for_order:.2f}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Raw contracts: {contracts_raw:.8f}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Exchange Amount Step: {exchange_amount_step}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Contracts after step size adjustment: {contracts_to_open:.8f}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Actual Notional after step size: {actual_notional_after_precision:.2f}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Calculated Required Margin: {required_margin:.2f} USDT")
    logger.debug(f"💡 DEBUG (calculate_order_details): Min Exchange Amount: {min_exchange_amount:.8f}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Min Notional Exchange: {min_notional_exchange:.2f}")
    logger.debug(f"💡 DEBUG (calculate_order_details): Min Notional from Min Amount: {min_notional_from_min_amount:.2f}")


    return (contracts_to_open, required_margin)


def confirm_position_entry(expected_direction: str, expected_contracts: float) -> tuple[bool, float | None]:
    """ยืนยันการเปิดโพซิชัน"""
    global current_position_size, entry_price, current_position_details

    try:
        step_size = float(market_info['limits']['amount'].get('step', '0.001'))
    except (TypeError, ValueError):
        logger.critical("❌ Critical Error: market_info['limits']['amount']['step'] is invalid. Cannot confirm position. Re-running setup_exchange might help.")
        send_telegram("⛔️ Critical Error: Market info step size invalid. Cannot confirm position.")
        return False, None

    size_tolerance = max(step_size * 2, expected_contracts * 0.001)

    time.sleep(10) # เพิ่มเป็น 10 วินาที
    logger.info("ℹ️ Initial 10-second sleep before starting position confirmation attempts.")

    for attempt in range(CONFIRMATION_RETRIES):
        logger.info(f"⏳ ยืนยันโพซิชัน (Attempt {attempt + 1}/{CONFIRMATION_RETRIES})...")
        time.sleep(CONFIRMATION_SLEEP)

        try:
            position_info = get_current_position()

            if position_info and position_info.get('side') == expected_direction:
                actual_size = position_info.get('size', 0.0)
                confirmed_entry_price = position_info.get('entry_price')

                if math.isclose(actual_size, expected_contracts, rel_tol=size_tolerance):
                    logger.info(f"✅ ยืนยันโพซิชันสำเร็จ:")
                    logger.info(f"    - Entry Price: {confirmed_entry_price:.2f}")
                    logger.info(f"    - Size: {actual_size:,.8f} Contracts")
                    logger.info(f"    - Direction: {expected_direction.upper()}")

                    current_position_size = actual_size
                    entry_price = confirmed_entry_price
                    current_position_details = position_info

                    profit_loss = position_info.get('unrealized_pnl', 0)
                    send_telegram(
                        f"🎯 เปิดโพซิชัน {expected_direction.upper()} สำเร็จ\n"
                        f"📊 ขนาด: {actual_size:,.8f} Contracts\n"
                        f"💰 Entry: {confirmed_entry_price:.2f}\n"
                        f"📈 P&L: {profit_loss:,.2f} USDT"
                    )

                    return True, confirmed_entry_price
                else:
                    logger.warning(f"⚠️ ขนาดโพซิชันไม่ตรงกัน (คาดหวัง: {expected_contracts:,.8f}, ได้: {actual_size:,.8f}). Tolerance: {size_tolerance:,.8f}")
            else:
                logger.warning(f"⚠️ ไม่พบโพซิชันที่ตรงกัน (คาดหวัง: {expected_direction}) หรือไม่พบโพซิชันเลย.")

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
# 10. ฟังก์ชันจัดการคำสั่งซื้อขาย (ORDER MANAGEMENT FUNCTIONS)
# ==============================================================================
def open_market_order(direction: str, current_price: float) -> tuple[bool, float | None]:
    global current_position_size, sl_moved_to_step1, sl_moved_to_step2 # reset SL states on new order

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

        decimal_places = 0
        if market_info and 'limits' in market_info and 'amount' in market_info['limits'] and 'step' in market_info['limits']['amount'] and market_info['limits']['amount']['step'] is not None:
            step_size = market_info['limits']['amount']['step']
            if step_size < 1:
                decimal_places = int(round(-math.log10(step_size)))

        logger.info(f"ℹ️ Trading Summary:")
        logger.info(f"    - Balance: {balance:,.2f} USDT")
        logger.info(f"    - Contracts: {order_amount:,.{decimal_places}f}")
        logger.info(f"    - Required Margin (incl. buffer): {estimated_used_margin + MARGIN_BUFFER_USDT:,.2f} USDT")
        logger.info(f"    - Direction: {direction.upper()}")

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

        # Reset SL movement flags when opening a new position
        sl_moved_to_step1 = False
        sl_moved_to_step2 = False
        save_monthly_stats() # Save the reset flags

        return confirm_position_entry(direction, order_amount)

    except Exception as e:
        logger.error(f"❌ Critical Error in open_market_order: {e}", exc_info=True)
        send_telegram(f"⛔️ Critical Error: ไม่สามารถเปิดออเดอร์ได้\n{str(e)[:200]}...")
        return False, None

# ==============================================================================
# 11. ฟังก์ชันตั้งค่า TP/SL/กันทุน (TP/SL/BREAKEVER FUNCTIONS - ปรับใช้สำหรับ Binance)
# ==============================================================================

def cancel_all_open_tp_sl_orders():
    """ยกเลิกคำสั่ง TP/SL ที่ค้างอยู่สำหรับ Symbol ปัจจุบันบน Binance Futures."""
    logger.info(f"⏳ Checking for and canceling open TP/SL orders for {SYMBOL}...")
    try:
        open_orders = exchange.fetch_open_orders(SYMBOL)

        canceled_count = 0
        for order in open_orders:
            if order['status'] == 'open' or order['status'] == 'pending':
                # Binance Futures TP/SL orders usually have 'reduceOnly' set to True
                # and types like 'TAKE_PROFIT_MARKET', 'STOP_MARKET'
                if order.get('reduceOnly', False) == True or \
                   order['type'] in ['TAKE_PROFIT_MARKET', 'STOP_MARKET', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT']:
                    try:
                        exchange.cancel_order(order['id'], SYMBOL)
                        logger.info(f"✅ Canceled old TP/SL order: ID {order['id']}, Type: {order['type']}, Side: {order['side']}")
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

# ในฟังก์ชัน set_tpsl_for_position
def set_tpsl_for_position(direction: str, entry_price: float) -> bool:
    global current_position_size

    if not current_position_size:
        logger.error("❌ ไม่สามารถตั้ง TP/SL ได้: ขนาดโพซิชันเป็น 0.")
        send_telegram("⛔️ Error: ไม่สามารถตั้ง TP/SL ได้ (ขนาดโพซิชันเป็น 0).")
        return False

    cancel_all_open_tp_sl_orders()
    time.sleep(1)

    tp_price_raw = 0.0 # ใช้ชื่อใหม่เพื่อความชัดเจน
    sl_price_raw = 0.0 # ใช้ชื่อใหม่เพื่อความชัดเจน

    if direction == 'long':
        tp_price_raw = entry_price + TP_DISTANCE_POINTS
        sl_price_raw = entry_price - SL_DISTANCE_POINTS
    elif direction == 'short':
        tp_price_raw = entry_price - TP_DISTANCE_POINTS
        sl_price_raw = entry_price + SL_DISTANCE_POINTS

    # แปลงเป็น precision string
    tp_price_str = exchange.price_to_precision(SYMBOL, tp_price_raw)
    sl_price_str = exchange.price_to_precision(SYMBOL, sl_price_raw)

    # แปลงกลับเป็น float สำหรับการแสดงผลและการใช้งานต่อ
    # สำคัญ: ต้องแปลงเป็น float เพื่อให้ .2f format code ทำงานได้ และเพื่อให้ CCXT รับค่า float ใน params
    tp_price = float(tp_price_str)
    sl_price = float(sl_price_str)

    logger.info(f"🎯 Calculated TP: {tp_price:.2f} | 🛑 Calculated SL: {sl_price:.2f}")

    try:
        tp_sl_side = 'sell' if direction == 'long' else 'buy'

        logger.info(f"⏳ Setting Take Profit order at {tp_price:.2f}...") # เพิ่ม .2f
        tp_order = exchange.create_order(
            symbol=SYMBOL,
            type='TAKE_PROFIT_MARKET',
            side=tp_sl_side,
            amount=current_position_size,
            price=None,
            params={
                'stopPrice': tp_price,
                'reduceOnly': True,
            }
        )
        logger.info(f"✅ Take Profit order placed: ID → {tp_order.get('id', 'N/A')}")

        logger.info(f"⏳ Setting Stop Loss order at {sl_price:.2f}...") # เพิ่ม .2f
        sl_order = exchange.create_order(
            symbol=SYMBOL,
            type='STOP_MARKET',
            side=tp_sl_side,
            amount=current_position_size,
            price=None,
            params={
                'stopPrice': sl_price,
                'reduceOnly': True,
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

# ในฟังก์ชัน move_sl_to_breakeven
def move_sl_to_breakeven(direction: str, entry_price: float) -> bool:
    global sl_moved, current_position_size

    if sl_moved:
        logger.info("ℹ️ SL ถูกเลื่อนไปที่กันทุนแล้ว ไม่จำเป็นต้องเลื่อนอีก.")
        return True

    if not current_position_size:
        logger.error("❌ ไม่สามารถเลื่อน SL ได้: ขนาดโพซิชันเป็น 0.")
        return False

    breakeven_sl_price_raw = 0.0
    if direction == 'long':
        breakeven_sl_price_raw = entry_price + BE_SL_BUFFER_POINTS
    elif direction == 'short':
        breakeven_sl_price_raw = entry_price - BE_SL_BUFFER_POINTS

    breakeven_sl_price_str = exchange.price_to_precision(SYMBOL, breakeven_sl_price_raw)
    breakeven_sl_price = float(breakeven_sl_price_str) # แปลงเป็น float

    try:
        logger.info("⏳ กำลังยกเลิกคำสั่ง Stop Loss เก่า...")
        open_orders = exchange.fetch_open_orders(SYMBOL)

        sl_order_ids_to_cancel = []
        for order in open_orders:
            if order['type'] == 'STOP_MARKET' and order.get('reduceOnly', False) and \
               (order['status'] == 'open' or order['status'] == 'pending'):
                sl_order_ids_to_cancel.append(order['id'])

        if sl_order_ids_to_cancel:
            for sl_id in sl_order_ids_to_cancel:
                try:
                    exchange.cancel_order(sl_id, SYMBOL)
                    logger.info(f"✅ ยกเลิก SL Order ID {sl_id} สำเร็จ.")
                except ccxt.OrderNotFound:
                    logger.info(f"💡 Order {sl_id} not found or already canceled/filled. No action needed.")
                except Exception as cancel_e:
                    logger.warning(f"⚠️ ไม่สามารถยกเลิก SL Order ID {sl_id} ได้: {cancel_e}")
        else:
            logger.info("ℹ️ ไม่พบคำสั่ง Stop Loss เก่าที่ต้องยกเลิก.")

        time.sleep(1)

        new_sl_side = 'sell' if direction == 'long' else 'buy'

        logger.info(f"⏳ Setting new Stop Loss (Breakeven) order at {breakeven_sl_price:.2f}...") # เพิ่ม .2f
        new_sl_order = exchange.create_order(
            symbol=SYMBOL,
            type='STOP_MARKET',
            side=new_sl_side,
            amount=current_position_size,
            price=None,
            params={
                'stopPrice': breakeven_sl_price,
                'reduceOnly': True,
            }
        )
        logger.info(f"✅ เลื่อน SL ไปที่กันทุนสำเร็จ: Trigger Price: {breakeven_sl_price:.2f}, ID: {new_sl_order.get('id', 'N/A')}")
        sl_moved = True

        send_telegram(f"🛡️ <b>SL ถูกเลื่อนไปกันทุนแล้ว!</b>\nโพซิชัน {current_position_details['side'].upper()}\nราคาเข้า: {entry_price:.2f}\nSL ใหม่ที่: {breakeven_sl_price:.2f}")

        return True

    except ccxt.BaseError as e:
        logger.error(f"❌ Error moving SL to breakeven: {str(e)}", exc_info=True)
        send_telegram(f"⛔️ API Error (Move SL): {e.args[0] if e.args else str(e)}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error moving SL to breakeven: {e}", exc_info=True)
        send_telegram(f"⛔️ Unexpected Error (Move SL): {e}")
        return False

# ==============================================================================
# 12. ฟังก์ชันตรวจสอบสถานะ (MONITORING FUNCTIONS)
# ==============================================================================

def monitor_position(pos_info: dict | None, current_price: float):
    global current_position_details, sl_moved, entry_price, current_position_size
    global monthly_stats, last_ema_position_status, sl_moved_to_step1, sl_moved_to_step2

    logger.debug(f"🔄 กำลังตรวจสอบสถานะโพซิชัน: Pos_Info={pos_info}, Current_Price={current_price}")

    if not pos_info and current_position_details:
        logger.info(f"ℹ️ โพซิชัน {current_position_details['side'].upper()} ถูกปิดแล้วใน Exchange.")

        closed_price = current_price
        pnl_usdt_gross = 0.0 # กำไร/ขาดทุนก่อนหักค่าธรรมเนียม

        if entry_price and current_position_size:
            if current_position_details['side'] == 'long':
                pnl_usdt_gross = (closed_price - entry_price) * current_position_size
            else:
                pnl_usdt_gross = (entry_price - closed_price) * current_position_size

            # --- คำนวณค่าธรรมเนียมโดยประมาณ ---
            # ค่าธรรมเนียมคิดจาก Notional Value (ขนาดสัญญา * ราคา) ทั้งตอนเปิดและปิด
            notional_value_entry = entry_price * current_position_size
            notional_value_close = closed_price * current_position_size

            # ค่าธรรมเนียมรวมสำหรับทั้งเปิดและปิดโพซิชัน
            estimated_fees = (notional_value_entry + notional_value_close) * TAKER_FEE_RATE

            pnl_usdt_actual = pnl_usdt_gross - estimated_fees
            logger.debug(f"DEBUG: Gross PnL: {pnl_usdt_gross:,.2f} USDT, Estimated Fees: {estimated_fees:,.2f} USDT")
        else:
            pnl_usdt_actual = 0.0 # ถ้าไม่มี entry_price หรือ size ก็ไม่มี PnL

        close_reason = "ปิดโดยไม่ทราบสาเหตุ"
        emoji = "❓"

        tp_sl_be_tolerance_points = entry_price * TP_SL_BE_PRICE_TOLERANCE_PERCENT if entry_price else 0

        # Determine close reason based on entry price and current price
        # Using the specific SL levels (SL_DISTANCE_POINTS, SL_MOVE_STEP1_SL_POINTS, SL_MOVE_STEP2_SL_POINTS)
        if current_position_details['side'] == 'long' and entry_price:
            if closed_price >= (entry_price + TP_DISTANCE_POINTS) - tp_sl_be_tolerance_points:
                close_reason = "TP"
                emoji = "✅"
            elif sl_moved_to_step2 and abs(closed_price - (entry_price + SL_MOVE_STEP2_SL_POINTS)) <= tp_sl_be_tolerance_points:
                close_reason = "SL (Step 2)"
                emoji = "🛡️"
            elif sl_moved_to_step1 and abs(closed_price - (entry_price - SL_MOVE_STEP1_SL_POINTS)) <= tp_sl_be_tolerance_points:
                close_reason = "SL (Step 1)"
                emoji = "⚠️"
            elif closed_price <= (entry_price - SL_DISTANCE_POINTS) + tp_sl_be_tolerance_points:
                close_reason = "SL (Original)"
                emoji = "❌"
        elif current_position_details['side'] == 'short' and entry_price:
            if closed_price <= (entry_price - TP_DISTANCE_POINTS) + tp_sl_be_tolerance_points:
                close_reason = "TP"
                emoji = "✅"
            elif sl_moved_to_step2 and abs(closed_price - (entry_price - SL_MOVE_STEP2_SL_POINTS)) <= tp_sl_be_tolerance_points:
                close_reason = "SL (Step 2)"
                emoji = "🛡️"
            elif sl_moved_to_step1 and abs(closed_price - (entry_price + SL_MOVE_STEP1_SL_POINTS)) <= tp_sl_be_tolerance_points:
                close_reason = "SL (Step 1)"
                emoji = "⚠️"
            elif closed_price >= (entry_price + SL_DISTANCE_POINTS) - tp_sl_be_tolerance_points:
                close_reason = "SL (Original)"
                emoji = "❌"

        send_telegram(f"{emoji} <b>ปิดออเดอร์ด้วย {close_reason}</b>\n<b>PnL (ประมาณ):</b> <code>{pnl_usdt_actual:,.2f} USDT</code>")
        logger.info(f"✅ โพซิชันปิด: {close_reason}, PnL (ประมาณ): {pnl_usdt_actual:.2f}")
        add_trade_result(close_reason, pnl_usdt_actual)

        # Reset all position-related global variables after closing
        current_position_details = None
        entry_price = None
        current_position_size = 0.0
        sl_moved = False # Ensure this legacy flag is reset too
        sl_moved_to_step1 = False # Reset for next trade
        sl_moved_to_step2 = False # Reset for next trade
        save_monthly_stats()

        # THIS IS THE CRUCIAL PART FOR REQUIREMENT 2: Cancel all open TP/SL orders
        cancel_all_open_tp_sl_orders()

        return

    if pos_info:
        current_position_details = pos_info
        entry_price = pos_info['entry_price']
        unrealized_pnl = pos_info['unrealizedPnl']
        current_position_size = pos_info['size']

        logger.info(f"📊 สถานะปัจจุบัน: {current_position_details['side'].upper()}, PnL: {unrealized_pnl:,.2f} USDT, ราคา: {current_price:,.1f}, เข้า: {entry_price:,.1f}, Size: {current_position_size:,.8f} Contracts")

        # Call the new multi-step SL management function
        manage_stop_loss(current_position_details['side'], entry_price, current_price)

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

            next_month = next_report_time.month + 1
            next_year = next_report_time.year
            if next_month > 12:
                next_month = 1
                next_year += 1

            max_day_in_next_month = calendar.monthrange(next_year, next_month)[1]
            report_day_for_next_month = min(MONTHLY_REPORT_DAY, max_day_in_next_month)
            next_report_time = next_report_time.replace(year=next_year, month=next_month, day=report_day_for_next_month)


        time_to_wait = (next_report_time - datetime.now()).total_seconds()
        if time_to_wait > 0:
            logger.info(f"⏰ กำหนดส่งรายงานประจำเดือนถัดไปในอีก {int(time_to_wait / 86400)} วัน {int((time_to_wait % 86400) / 3600)} ชั่วโมง {int((time_to_wait % 3600) / 60)} นาที.")
            time.sleep(max(time_to_wait, 60))
        else:
            time.sleep(60)


# ==============================================================================
# 14. ฟังก์ชันเริ่มต้นบอท (BOT STARTUP FUNCTIONS)
# ==============================================================================
def send_startup_message():
    global initial_balance

    try:
        initial_balance = get_portfolio_balance()
        startup_time = datetime.now().strftime('%d/%m/%Y %H:%M:%S')

        message = f"""🔄 <b>บอทเริ่มทำงาน</b>
<b>🤖 EMA Cross Trading Bot</b>
<b>💰 ยอดเริ่มต้น:</b> <code>{initial_balance:,.2f} USDT</code>
<b>⏰ เวลาเริ่ม:</b> <code>{startup_time}</code>
<b>📊 เฟรม:</b> <code>{TIMEFRAME}</code> | <b>Leverage:</b> <code>{LEVERAGE}x</code>
<b>🎯 TP:</b> <code>{TP_DISTANCE_POINTS}</code> | <b>SL:</b> <code>{SL_DISTANCE_POINTS}</code>
<b>🔧 Margin Buffer:</b> <code>{MARGIN_BUFFER_USDT:,.0f} USDT</code>
<b>📈 รอสัญญาณ EMA Cross...</b>"""

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
        setup_exchange()
        load_monthly_stats()
        send_startup_message()

        monthly_thread = threading.Thread(target=monthly_report_scheduler, daemon=True)
        monthly_thread.start()
        logger.info("✅ Monthly Report Scheduler Thread Started.")

    except Exception as e:
        error_msg = f"⛔️ Error: ไม่สามารถเริ่มต้นบอทได้\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
        send_telegram(error_msg)
        logger.critical(f"❌ Startup error: {e}", exc_info=True)
        time.sleep(ERROR_RETRY_SLEEP_SECONDS)
        return

    logger.info("🚀 บอทเข้าสู่ Main Loop แล้วและพร้อมทำงาน...")
    while True:
        try:
            logger.info(f"🔄 เริ่มรอบ Main Loop ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) - กำลังดึงข้อมูลและตรวจสอบ.")

            ticker = None
            try:
                logger.info("📊 กำลังดึงราคาล่าสุด (Ticker)...")
                ticker = exchange.fetch_ticker(SYMBOL)
                time.sleep(1)
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
            logger.info(f"💲 ราคาปัจจุบันของ {SYMBOL}: {current_price:,.1f}")

            current_pos_info = None
            try:
                logger.info("🔎 กำลังดึงสถานะโพซิชันปัจจุบัน...")
                current_pos_info = get_current_position()
                logger.info(f"☑️ ดึงสถานะโพซิชันปัจจุบันสำเร็จ: {'มีโพซิชัน' if current_pos_info else 'ไม่มีโพซิชัน'}.")
            except Exception as e:
                logger.error(f"❌ Error ในการดึงสถานะโพซิชัน: {e}", exc_info=True)
                send_telegram(f"⛔️ API Error: ไม่สามารถดึงสถานะโพซิชันได้. รายละเอียด: {e.args[0] if e.args else str(e)}")
                time.sleep(ERROR_RETRY_SLEEP_SECONDS)
                continue

            monitor_position(current_pos_info, current_price)

            if not current_pos_info:
                logger.info("🔍 ไม่มีโพซิชันเปิดอยู่. กำลังตรวจสอบสัญญาณ EMA Cross...")
                signal = check_ema_cross()

                if signal:
                    logger.info(f"🌟 ตรวจพบสัญญาณ EMA Cross: {signal.upper()}")
                    logger.info(f"✨ สัญญาณ {signal.upper()} ที่เข้าเงื่อนไข. กำลังพยายามเปิดออเดอร์.")

                    market_order_success, confirmed_entry_price = open_market_order(signal, current_price)

                    if market_order_success and confirmed_entry_price:
                        set_tpsl_success = set_tpsl_for_position(signal, confirmed_entry_price)

                        if set_tpsl_success:
                            logger.info(f"✅ เปิดออเดอร์ {signal.upper()} และตั้ง TP/SL สำเร็จ.")
                        else:
                            logger.error(f"❌ เปิดออเดอร์ {signal.upper()} ได้ แต่ตั้ง TP/SL ไม่สำเร็จ. กรุณาตรวจสอบและปิดออเดอร์ด้วยตนเอง!")
                            send_telegram(f"⛔️ <b>ข้อผิดพลาดร้ายแรง:</b> เปิดออเดอร์ {signal.upper()} ได้ แต่ตั้ง TP/SL ไม่สำเร็จ. โพซิชันไม่มี SL/TP! โปรดจัดการด้วยตนเอง!")
                    else:
                        logger.warning(f"⚠️ ไม่สามารถเปิด Market Order {signal.upper()} ได้.")
                else:
                    logger.info("🔎 ไม่พบสัญญาณ EMA Cross ที่ชัดเจน.")
            else:
                logger.info(f"Current Position: {current_pos_info['side'].upper()}. รอการปิดหรือเลื่อน SL.")

            logger.info(f"😴 จบรอบ Main Loop. รอ {MAIN_LOOP_SLEEP_SECONDS} วินาทีสำหรับรอบถัดไป.")
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
            error_msg = f"⛔️ Error: เกิดข้อผิดพลาดที่ไม่คาดคิดใน Main Loop\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
            logger.error(error_msg, exc_info=True)
            send_telegram(error_msg)
            time.sleep(ERROR_RETRY_SLEEP_SECONDS)

# ==============================================================================
# 16. จุดเริ่มต้นการทำงานของโปรแกรม (ENTRY POINT)
# ==============================================================================
if __name__ == '__main__':
    main()
