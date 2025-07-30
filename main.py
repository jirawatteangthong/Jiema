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
SYMBOL = 'BTC/USDT:USDT'  # ใช้ 'BTC/USDT:USDT' ตามที่ Exchange คืนมาใน get_current_position()
TIMEFRAME = '1m'
LEVERAGE = 20
TP_DISTANCE_POINTS = 201
SL_DISTANCE_POINTS = 1111

# --- Trailing Stop Loss Parameters (2 Steps) ---
# สำหรับ Long Position: (ราคาวิ่งขึ้น)
TRAIL_SL_STEP1_TRIGGER_LONG_POINTS = 100
TRAIL_SL_STEP1_NEW_SL_POINTS_LONG = -700
TRAIL_SL_STEP2_TRIGGER_LONG_POINTS = 150
TRAIL_SL_STEP2_NEW_SL_POINTS_LONG = 10

# สำหรับ Short Position: (ราคาวิ่งลง)
TRAIL_SL_STEP1_TRIGGER_SHORT_POINTS = 100
TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT = 700
TRAIL_SL_STEP2_TRIGGER_SHORT_POINTS = 150
TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT = -10

CROSS_THRESHOLD_POINTS = 1

# --- Risk Management ---
MARGIN_BUFFER_USDT = 5
TARGET_POSITION_SIZE_FACTOR = 0.8  # ใช้ 0.8 (80%) ของ Equity ที่ใช้ได้ทั้งหมด

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
FAST_LOOP_INTERVAL_SECONDS = 3  # สำหรับการจัดการออเดอร์ TP/SL (เร็วขึ้น)
EMA_CALC_INTERVAL_SECONDS = 60  # ปรับให้ตรวจจับ EMA บ่อยขึ้น (เดิม 180)
TRADE_COOLDOWN_SECONDS = 60  
ERROR_RETRY_SLEEP_SECONDS = 60
MONTHLY_REPORT_DAY = 20
MONTHLY_REPORT_HOUR = 0
MONTHLY_REPORT_MINUTE = 5

# ==============================================================================
# 2. การตั้งค่า Logging
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s-%(levelname)s-%(message)s',
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
just_closed_by_tp_sl = False
just_closed_by_tp_sl_lock = threading.Lock()
just_closed_by_tp_sl_time = datetime.min # เก็บเวลาที่ปิดล่าสุดเพื่อใช้ cooldown เสริม
last_trade_closed_time = datetime.min  # *** เพิ่ม: ตัวแปรสำหรับเวลาที่ปิดเทรดล่าสุด ***
last_trade_side = None  # ตัวแปรสำหรับจดจำฝั่งล่าสุดที่เทรด

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
        logger.info(" เชื่อมต่อกับ Binance Futures Exchange สำเร็จ และโหลด Markets แล้ว.")
        market_info = exchange.market(SYMBOL)
        if not market_info:
            raise ValueError(f"ไม่พบข้อมูลตลาดสำหรับสัญลักษณ์ {SYMBOL}")

        if 'limits' not in market_info:
            market_info['limits'] = {}
        if 'amount' not in market_info['limits']:
            market_info['limits']['amount'] = {}
        if 'cost' not in market_info['limits']:
            market_info['limits']['cost'] = {}

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

        logger.info(f" Market info limits for {SYMBOL}: Amount step={market_info['limits']['amount']['step']}, "
                    f"min={market_info['limits']['amount']['min']}; Cost min={market_info['limits']['cost']['min']}")

        try:
            result = exchange.set_leverage(LEVERAGE, SYMBOL)
            logger.info(f" ตั้งค่า Leverage เป็น {LEVERAGE}x สำหรับ {SYMBOL}: {result}")
        except ccxt.ExchangeError as e:
            if "leverage is not valid" in str(e) or "not valid for this symbol" in str(e):
                logger.critical(f" Error: Leverage {LEVERAGE}x ไม่ถูกต้องสำหรับ {SYMBOL} บน Binance. โปรดตรวจสอบ Max Allowed Leverage.")
            else:
                logger.critical(f" Error ในการตั้งค่า Leverage: {e}", exc_info=True)
                send_telegram(f" Critical Error: ไม่สามารถตั้งค่า Leverage ได้.\nรายละเอียด: {e}")
            exit()
    except ValueError as ve:
        logger.critical(f" Configuration Error: {ve}", exc_info=True)
        send_telegram(f" Critical Error: การตั้งค่าเริ่มต้นผิดพลาด.\nรายละเอียด: {ve}")
        exit()
    except Exception as e:
        logger.critical(f" ไม่สามารถเชื่อมต่อหรือโหลดข้อมูล Exchange เบื้องต้นได้: {e}", exc_info=True)
        send_telegram(f" Critical Error: ไม่สามารถเชื่อมต่อ Exchange ได้\nรายละเอียด: {e}")
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
        logger.debug(f" บันทึกสถิติการเทรดลงไฟล์ {STATS_FILE} สำเร็จ")
    except Exception as e:
        logger.error(f" เกิดข้อผิดพลาดในการบันทึกสถิติ: {e}")


def reset_monthly_stats():
    global monthly_stats, last_ema_position_status
    monthly_stats['month_year'] = datetime.now().strftime('%Y-%m')
    monthly_stats['tp_count'] = 0
    monthly_stats['sl_count'] = 0
    monthly_stats['total_pnl'] = 0.0
    monthly_stats['trades'] = []
    save_monthly_stats()
    logger.info(f" รีเซ็ตสถิติประจำเดือนสำหรับเดือน {monthly_stats['month_year']}")


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

            logger.info(f" โหลดสถิติการเทรดจากไฟล์ {STATS_FILE} สำเร็จ")

            if monthly_stats['last_report_month_year']:
                try:
                    year, month = map(int, monthly_stats['last_report_month_year'].split('-'))
                    last_monthly_report_date = datetime(year, month, 1).date()
                except ValueError:
                    logger.warning(
                        " รูปแบบวันที่ last_report_report_month_year ในไฟล์ไม่ถูกต้อง. จะถือว่ายังไม่มีการส่งรายงาน.")
                    last_monthly_report_date = None
            else:
                last_monthly_report_date = None

            current_month_year_str = datetime.now().strftime('%Y-%m')
            if monthly_stats['month_year'] != current_month_year_str:
                logger.info(
                    f" สถิติที่โหลดมาเป็นของเดือน {monthly_stats['month_year']} ไม่ตรงกับเดือนนี้ {current_month_year_str}. จะรีเซ็ตสถิติสำหรับเดือนใหม่.")
                reset_monthly_stats()
        else:
            logger.info(f" ไม่พบไฟล์สถิติ {STATS_FILE} สร้างไฟล์ใหม่")
            reset_monthly_stats()
    except Exception as e:
        logger.error(f" เกิดข้อผิดพลาดในการโหลดสถิติ: {e}", exc_info=True)
        if not os.access(os.path.dirname(stats_file_path) or '.', os.W_OK):
            logger.critical(
                f" ข้อผิดพลาด: ไม่มีสิทธิ์เขียนไฟล์ในไดเรกทอรี: {os.path.dirname(stats_file_path) or '.'}. โปรดตรวจสอบสิทธิ์การเข้าถึงหรือเปลี่ยน STATS_FILE.")

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
        logger.info(
            f" เดือนเปลี่ยนใน add_trade_result: {monthly_stats['month_year']}-> {current_month_year_str}. กำลังรีเซ็ตสถิติประจำเดือน.")
        reset_monthly_stats()

    if reason.upper() == 'TP':
        monthly_stats['tp_count'] += 1
    elif reason.upper() == 'SL' or reason.upper() == 'SL(ขาดทุน)':
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
        logger.warning(" TELEGRAM_TOKEN หรือ TELEGRAM_CHAT_ID ไม่ได้ถูกตั้งค่า. ไม่สามารถส่งข้อความ Telegram ได้.")
        return

    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        params = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        logger.info(f" Telegram: {msg.splitlines()[0]}...")
    except requests.exceptions.Timeout:
        logger.error(" Error: ไม่สามารถส่งข้อความ Telegram ได้ (Timeout)")
    except requests.exceptions.HTTPError as e:
        telegram_error_msg = e.response.json().get('description', e.response.text)
        logger.error(f" Error: ไม่สามารถส่งข้อความ Telegram ได้ (HTTP Error) - รายละเอียด: {telegram_error_msg}")
    except requests.exceptions.RequestException as e:
        logger.error(f" Error: ไม่สามารถส่งข้อความ Telegram ได้ (Request Error) - {e}")
    except Exception as e:
        logger.error(f" Unexpected Telegram error: {e}")

# ==============================================================================
# 8. ฟังก์ชันดึงข้อมูล Exchange (EXCHANGE DATA RETRIEVAL FUNCTIONS)
# ==============================================================================
def get_portfolio_balance() -> float:
    global portfolio_balance
    retries = 3
    for i in range(retries):
        try:
            logger.debug(f" กำลังดึงยอดคงเหลือ (Attempt {i + 1}/{retries})...")
            balance = exchange.fetch_balance()
            time.sleep(0.5)

            free_usdt = balance.get('USDT', {}).get('free', 0)
            if free_usdt == 0:
                for asset_info in balance.get('info', {}).get('assets', []):
                    if asset_info.get('asset') == 'USDT':
                        free_usdt = float(asset_info.get('availableBalance', 0))
                        break

            portfolio_balance = float(free_usdt)
            logger.info(f" ยอดคงเหลือ USDT: {portfolio_balance:,.2f}")
            return portfolio_balance
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.warning(f" Error fetching balance (Attempt {i + 1}/{retries}): {e}. Retrying in 15 seconds...")
            if i == retries - 1:
                send_telegram(
                    f" API Error: ไม่สามารถดึงยอดคงเหลือได้ (Attempt {i + 1}/{retries})\nรายละเอียด: {e}")
            time.sleep(15)
        except Exception as e:
            logger.error(f" Unexpected error in get_portfolio_balance: {e}", exc_info=True)
            send_telegram(f" Unexpected Error: ไม่สามารถดึงยอดคงเหลือได้\nรายละเอียด: {e}")
            return 0.0
    logger.error(f" Failed to fetch balance after {retries} attempts.")
    send_telegram(f" API Error: ล้มเหลวในการดึงยอดคงเหลือหลังจาก {retries} ครั้ง.")
    return 0.0


def get_current_position() -> dict | None:
    retries = 5
    for i in range(retries):
        try:
            logger.info(f" กำลังดึงโพซิชันปัจจุบันจาก Exchange (Attempt {i + 1}/{retries})...")
            time.sleep(1)
            positions = exchange.fetch_positions([SYMBOL])
            logger.debug(f"DEBUG: Raw positions fetched from Exchange (Attempt {i + 1}): {positions}")
            time.sleep(0.5)

            for pos in positions:
                if 'symbol' in pos and 'contracts' in pos:
                    pos_amount = float(pos.get('contracts', 0) or pos.get('positionAmt', 0))
                    pos_side = pos.get('side')  # 'long' or 'short'

                    entry_price_val = float(pos.get('entryPrice', 0))
                    unrealized_pnl_val = float(pos.get('unrealizedPnl', 0))
                    liquidation_price_val = float(pos.get('liquidationPrice', 0)) if pos.get('liquidationPrice') else None

                    if pos['symbol'] == SYMBOL and pos_amount != 0 and pos_side:
                        logger.info(f" พบโพซิชัน {pos['symbol']}: Side={pos_side}, Contracts={abs(pos_amount):,.8f}, Entry={entry_price_val:,.2f}")
                        return {
                            'symbol': pos['symbol'],
                            'side': pos_side,
                            'contracts': abs(pos_amount),
                            'entryPrice': entry_price_val,
                            'unrealizedPnl': unrealized_pnl_val,
                            'liquidationPrice': liquidation_price_val,
                            'info': pos
                        }
                else:
                    logger.debug(f"DEBUG: Skipping position entry due to missing 'symbol' or 'contracts': {pos}")

            logger.info(
                f" ไม่พบโพซิชันที่เปิดอยู่บน Exchange สำหรับ SYMBOL นี้หลังจากตรวจสอบ {len(positions)} รายการ.")
            return None
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.warning(f" Error fetching positions (Attempt {i + 1}/{retries}): {e}. Retrying in 15 seconds...")
            if i == retries - 1:
                send_telegram(
                    f" API Error: ไม่สามารถดึงโพซิชันได้ (Attempt {i + 1}/{retries})\nรายละเอียด: {e}")
            time.sleep(15)
        except Exception as e:
            logger.error(f" Unexpected error in get_current_position: {e}", exc_info=True)
            send_telegram(f" Unexpected Error: ไม่สามารถดึงโพซิชันได้\nรายละเอียด: {e}")
            return None
    logger.error(f" Failed to fetch positions after {retries} attempts.")
    send_telegram(f" API Error: ล้มเหลวในการดึงโพซิชันหลังจาก {retries} ครั้ง.")
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


def check_ema_cross() -> str | None:
    global last_ema_position_status

    try:
        retries = 3
        ohlcv = None
        for i in range(retries):
            logger.debug(f" กำลังดึงข้อมูล OHLCV สำหรับ EMA ({i + 1}/{retries})...")
            try:
                ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=500)
                time.sleep(0.5)
                break
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                logger.warning(f" Error fetching OHLCV (Attempt {i + 1}/{retries}): {e}. Retrying in 15 seconds...")
                if i == retries - 1:
                    send_telegram(
                        f" API Error: ไม่สามารถดึง OHLCV ได้ (Attempt {i + 1}/{retries})\nรายละเอียด: {e}")
                time.sleep(15)
            except Exception as e:
                logger.error(f" Unexpected error fetching OHLCV: {e}", exc_info=True)
                send_telegram(f" Unexpected Error: ไม่สามารถดึง OHLCV ได้\nรายละเอียด: {e}")
                return None

        if not ohlcv or len(ohlcv) < 201:
            logger.warning(f"ข้อมูล OHLCV ไม่เพียงพอ. ต้องการอย่างน้อย 201 แท่ง ได้ {len(ohlcv)}")
            send_telegram(f" ข้อมูล OHLCV ไม่เพียงพอ ({len(ohlcv)} แท่ง).")
            return None

        closes = [candle[4] for candle in ohlcv]

        ema50_current = calculate_ema(closes, 50)
        ema200_current = calculate_ema(closes, 200)

        logger.info(f" EMA Values: Current EMA50={ema50_current:,.2f}, EMA200={ema200_current:,.2f}")

        if None in [ema50_current, ema200_current]:
            logger.warning("ค่า EMA ไม่สามารถคำนวณได้ (เป็น None).")
            return None

        current_ema_position = None
        if ema50_current > ema200_current:
            current_ema_position = 'above'
        elif ema50_current < ema200_current:
            current_ema_position = 'below'

        if last_ema_position_status is None:
            if current_ema_position:
                last_ema_position_status = current_ema_position
                save_monthly_stats()
                logger.info(
                    f" บอทเพิ่งเริ่มรัน. บันทึกสถานะ EMA ปัจจุบันเป็น: {current_ema_position.upper()}. จะรอสัญญาณการตัดกันถัดไป.")
            return None

        cross_signal = None

        if last_ema_position_status == 'below' and current_ema_position == 'above' and \
                ema50_current > (ema200_current + CROSS_THRESHOLD_POINTS):
            cross_signal = 'long'
            logger.info(
                f" Threshold Golden Cross: EMA50({ema50_current:,.2f}) is {CROSS_THRESHOLD_POINTS} points above EMA200({ema200_current:,.2f})")
        elif last_ema_position_status == 'above' and current_ema_position == 'below' and \
                ema50_current < (ema200_current - CROSS_THRESHOLD_POINTS):
            cross_signal = 'short'
            logger.info(
                f" Threshold Death Cross: EMA50({ema50_current:,.2f}) is {CROSS_THRESHOLD_POINTS} points below EMA200({ema200_current:,.2f})")

        if cross_signal is not None:
            logger.info(f" สัญญาณ EMA Cross ที่ตรวจพบ: {cross_signal.upper()}")
        if current_ema_position != last_ema_position_status:
            logger.info(
                f" EMA position changed from {last_ema_position_status.upper()} to {current_ema_position.upper()}. Updating last_ema_position_status.")
            last_ema_position_status = current_ema_position
            save_monthly_stats()
        else:
            logger.info(" ไม่พบสัญญาณ EMA Cross ที่ชัดเจน.")
        return cross_signal
    except Exception as e:
        logger.error(f" เกิดข้อผิดพลาดในการคำนวณ EMA: {e}", exc_info=True)
        send_telegram(f" Error: ไม่สามารถคำนวณ EMA ได้\nรายละเอียด: {e}")
        return None

# ==============================================================================
# 9.1 ฟังก์ชันวิเคราะห์สัญญาณ EMA และเปิดออเดอร์ (พร้อม Cooldown)
# ==============================================================================
def check_ema_signal_and_trade(current_price: float):
    global last_trade_closed_time, current_position_details, last_trade_side # เพิ่ม global ที่จำเป็น
    # เพิ่ม global สำหรับ Flag ใหม่
    global just_closed_by_tp_sl, just_closed_by_tp_sl_lock, just_closed_by_tp_sl_time

    # 1. เช็ก cooldown ก่อนเปิด order (cooldown หลังปิดด้วย TP/SL หรือด้วยตนเอง)
    cooldown_remaining = TRADE_COOLDOWN_SECONDS - (datetime.now() - last_trade_closed_time).total_seconds()
    if cooldown_remaining > 0:
        logger.info(f" ยังอยู่ในช่วง Cooldown อีก {cooldown_remaining:.0f} วินาที")
        return

    # *** 2. เช็ก Flag just_closed_by_tp_sl และเวลาเพิ่มเติม ***
    # เพิ่ม Cooldown เล็กน้อย (เช่น 10 วินาที) หลังจากปิดด้วย TP/SL เพื่อป้องกันการเปิดสวนทันที
    with just_closed_by_tp_sl_lock:
        if just_closed_by_tp_sl:
             # Cooldown 10 วินาทีหลังจากปิดด้วย TP/SL
             tp_sl_cooldown_remaining = 10 - (datetime.now() - just_closed_by_tp_sl_time).total_seconds()
             if tp_sl_cooldown_remaining > 0:
                 logger.info(f" *** ข้ามการตรวจ EMA: เพิ่งปิดด้วย TP/SL (Flag ยังเป็น True) รออีก {tp_sl_cooldown_remaining:.1f} วินาที *** ")
                 return # ข้ามการตรวจ EMA ไปก่อน
             else:
                 # ถ้าเกิน 10 วินาทีแล้ว ให้รีเซ็ต Flag เพื่อความปลอดภัย
                 logger.debug(" *** เกิน Cooldown 10 วินาที รีเซ็ต Flag just_closed_by_tp_sl = False (Auto-reset) *** ")
                 just_closed_by_tp_sl = False
    # *** สิ้นสุดการเช็ก Flag ***

    # 3. ห้ามเปิดถ้ามีโพซิชันอยู่
    if current_position_details is not None:
        logger.info(" มีโพซิชันเปิดอยู่แล้ว บอทจะไม่เปิดโพซิชันใหม่.")
        return

    # 4. ตรวจสัญญาณ EMA cross
    signal = check_ema_cross()
    if not signal:
        logger.info(" ไม่พบสัญญาณ EMA Cross.")
        return

    # ไม่ให้เปิดฝั่งเดิมซ้ำ
    if signal == last_trade_side:
        logger.info(f" ข้ามการเปิดออเดอร์: สัญญาณ {signal.upper()} ซ้ำกับฝั่งล่าสุดที่เพิ่งเปิด")
        return

    # 5. สั่งเปิดออเดอร์ตามสัญญาณ
    logger.info(f" พบสัญญาณ EMA Cross: {signal.upper()} → สั่งเปิดออเดอร์")
    open_market_order(signal, current_price)

# ==============================================================================
# 10. ฟังก์ชันช่วยสำหรับการคำนวณและตรวจสอบออเดอร์
# ==============================================================================
def round_to_precision(value: float, precision_type: str) -> float:
    """ปัดค่าให้เป็นไปตาม Precision ที่ Exchange กำหนด"""
    if market_info and 'precision' in market_info and precision_type in market_info['precision']:
        return float(exchange.decimal_to_precision(value, ccxt.ROUND, market_info['precision'][precision_type]))
    else:
        logger.warning(f" ไม่พบ Precision สำหรับ '{precision_type}'. ใช้ round() ปกติ.")
        return round(value, 8)


def calculate_order_details(available_usdt: float, price: float) -> tuple[float, float]:
    """ คำนวณจำนวนสัญญาที่จะเปิดและ Margin ที่ต้องใช้ โดยพิจารณาจาก Exchange Limits
    และเปอร์เซ็นต์ของเงินทุนในพอร์ต์ที่ต้องการใช้
    """
    if price <= 0 or LEVERAGE <= 0 or TARGET_POSITION_SIZE_FACTOR <= 0:
        logger.error("Error: Price, leverage, and target_position_size_factor must be positive.")
        return (0, 0)

    if not market_info:
        logger.error(f" Could not retrieve market info for {SYMBOL}. Please ensure setup_exchange ran successfully.")
        return (0, 0)

    try:
        amount_step = market_info['limits']['amount']['step']
        min_exchange_amount = market_info['limits']['amount']['min']
        max_exchange_amount = market_info['limits']['amount']['max']
        min_notional_exchange = market_info['limits']['cost']['min']

    except KeyError as e:
        logger.critical(f" Error accessing market limits for {SYMBOL}: Missing key {e}. Exiting.", exc_info=True)
        send_telegram(f" Critical Error: Cannot parse market limits for {SYMBOL}.\nDetails: {e}")
        return (0, 0)
    except (TypeError, ValueError) as e:
        logger.critical(f" Error parsing market limits for {SYMBOL}: Invalid value {e}. Exiting.", exc_info=True)
        send_telegram(f" Critical Error: Cannot parse market limits for {SYMBOL}.\nDetails: {e}")
        return (0, 0)

    # *** ส่วนที่แก้ไขเพื่อใช้เปอร์เซ็นต์ของทุนทั้งหมด (80%) ***
    investable_capital = available_usdt - MARGIN_BUFFER_USDT
    if investable_capital <= 0:
        logger.warning(
            f" Available capital ({available_usdt:,.2f}) is not enough after deducting margin buffer ({MARGIN_BUFFER_USDT:,.2f}) for investment.")
        return (0, 0)

    target_notional_for_order_raw = investable_capital * TARGET_POSITION_SIZE_FACTOR * LEVERAGE

    min_notional_from_min_amount = min_exchange_amount * price
    target_notional_for_order = max(target_notional_for_order_raw, min_notional_exchange, min_notional_from_min_amount)

    if target_notional_for_order_raw < min_notional_exchange:
        logger.info(
            f" Calculated notional from percentage ({target_notional_for_order_raw:,.2f}) is below exchange minimum ({min_notional_exchange:,.2f}). Will attempt to open at exchange minimum ({target_notional_for_order:,.2f}).")
    # *** สิ้นสุดส่วนแก้ไข ***

    contracts_raw = target_notional_for_order / price
    contracts_to_open = float(exchange.amount_to_precision(SYMBOL, contracts_raw))

    contracts_to_open = max(contracts_to_open, min_exchange_amount)
    contracts_to_open = min(contracts_to_open, max_exchange_amount)

    actual_notional_after_precision = contracts_to_open * price
    required_margin = actual_notional_after_precision / LEVERAGE

    if contracts_to_open == 0:
        logger.warning(
            f" Calculated contracts to open is 0 after all adjustments. (Target notional: {target_notional_for_order:,.2f} USDT, Current price: {price:,.2f}, Min exchange amount: {min_exchange_amount:.8f}). This means calculated size is too small or rounded to zero.")
        return (0, 0)

    if available_usdt < required_margin + MARGIN_BUFFER_USDT:
        logger.error(
            f" Margin not sufficient. Available: {available_usdt:,.2f}, Required: {required_margin:,.2f} (for trade) + {MARGIN_BUFFER_USDT} (Buffer) = {required_margin + MARGIN_BUFFER_USDT:,.2f} USDT.")
        return (0, 0)

    logger.info(
        f" Order Calculation Result: Contracts: {contracts_to_open:,.8f}, Target Notional: {target_notional_for_order:,.2f}, Actual Notional: {actual_notional_after_precision:,.2f}, Req Margin: {required_margin:,.2f} USDT")
    return (contracts_to_open, required_margin)


def confirm_position_entry(expected_direction: str, expected_contracts: float) -> tuple[bool, float | None]:
    """ยืนยันการเปิดโพซิชัน"""
    global current_position_details

    if not market_info:
        logger.critical(" Critical Error: market_info is not loaded. Cannot confirm position.")
        send_telegram(" Critical Error: Market info not loaded. Cannot confirm position.")
        return False, None

    try:
        step_size = market_info['limits']['amount']['step']
    except KeyError:
        logger.critical(
            " Critical Error: market_info['limits']['amount']['step'] is invalid. Cannot confirm position.")
        send_telegram(" Critical Error: Market info step size invalid. Cannot confirm position.")
        return False, None

    size_tolerance = max(step_size * 2, expected_contracts * 0.001)

    logger.info(
        f" Initial 5-second sleep before starting position confirmation attempts for {expected_direction.upper()} {expected_contracts:,.8f} contracts.")
    time.sleep(5)

    for attempt in range(CONFIRMATION_RETRIES):
        logger.info(f" ยืนยันโพซิชัน (Attempt {attempt + 1}/{CONFIRMATION_RETRIES})...")
        time.sleep(CONFIRMATION_SLEEP)

        try:
            position_info = get_current_position()
            logger.debug(f"DEBUG: Position info retrieved for confirmation attempt {attempt + 1}: {position_info}")

            if position_info and position_info.get('side') == expected_direction:
                actual_size = position_info.get('contracts', 0.0)

                confirmed_entry_price = position_info.get('entryPrice')

                if math.isclose(actual_size, expected_contracts, rel_tol=size_tolerance):
                    logger.info(f" ยืนยันโพซิชัน {expected_direction.upper()} สำเร็จ:")
                    logger.info(f" - Entry Price: {confirmed_entry_price:,.2f}")
                    logger.info(f" - Size: {actual_size:,.8f} Contracts")
                    logger.info(f" - Direction: {expected_direction.upper()}")

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
                    logger.debug(f"DEBUG: current_position_details set: {current_position_details}")

                    profit_loss = position_info.get('unrealizedPnl', 0)
                    send_telegram(
                        f" เปิดโพซิชัน {expected_direction.upper()} สำเร็จ!\n"
                        f" ขนาด: {actual_size:,.8f} Contracts\n"
                        f" Entry: {confirmed_entry_price:,.2f}\n"
                        f" P&L: {profit_loss:,.2f} USDT"
                    )
                    return True, confirmed_entry_price
                else:
                    logger.warning(
                        f" ขนาดโพซิชันไม่ตรงกัน (คาดหวัง: {expected_contracts:,.8f}, ได้: {actual_size:,.8f}). Tolerance: {size_tolerance:,.8f}. Retrying...")
            else:
                logger.warning(
                    f" ไม่พบโพซิชันที่ตรงกัน (คาดหวัง: {expected_direction.upper()}) หรือไม่พบโพซิชันเลย. Retrying...")
        except Exception as e:
            logger.warning(f" Error ในการยืนยันโพซิชัน (Attempt {attempt + 1}): {e}", exc_info=True)

    logger.error(f" ไม่สามารถยืนยันโพซิชันได้หลังจาก {CONFIRMATION_RETRIES} ครั้ง")
    send_telegram(
        f" Position Confirmation Failed\n"
        f" กรุณาตรวจสอบโพซิชันใน Exchange ด่วน!\n"
        f" คาดหวัง: {expected_direction.upper()} {expected_contracts:,.8f} Contracts"
    )
    return False, None

# ==============================================================================
# 11. ฟังก์ชันจัดการคำสั่งซื้อขาย
# ==============================================================================
def open_market_order(direction: str, current_price: float) -> tuple[bool, float | None]:
    global current_position_details, last_trade_closed_time, last_trade_side
    # [1] ตรวจสอบ cooldown 15 นาที
    if last_trade_closed_time:
        seconds_since_close = (datetime.now() - last_trade_closed_time).total_seconds()
        if seconds_since_close < TRADE_COOLDOWN_SECONDS:
            time_left = TRADE_COOLDOWN_SECONDS - seconds_since_close
            logger.warning(f" ยังไม่พ้นช่วง cooldown → เหลืออีก {time_left:.0f} วินาที")
            send_telegram(
                f" บอทยังไม่พ้นช่วง cooldown หลังปิดโพซิชัน\nจะไม่เปิดออเดอร์ใหม่จนกว่าจะครบ {TRADE_COOLDOWN_SECONDS // 60} นาที")
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
        if market_info and 'limits' in market_info and 'amount' in market_info['limits'] and 'step' in \
                market_info['limits']['amount'] and market_info['limits']['amount']['step'] is not None:
            step_size = market_info['limits']['amount']['step']
            if step_size < 1:
                decimal_places = int(round(-math.log10(step_size)))

        logger.info(f" Trading Summary before opening order:")
        logger.info(f" - Balance: {balance:,.2f} USDT")
        logger.info(f" - Contracts: {order_amount:,.{decimal_places}f}")
        logger.info(f" - Required Margin(incl. buffer): {estimated_used_margin + MARGIN_BUFFER_USDT:,.2f} USDT")
        logger.info(f" - Direction: {direction.upper()}")

        side = 'buy' if direction == 'long' else 'sell'
        params = {}

        order = None
        for attempt in range(3):
            logger.info(
                f" ส่งคำสั่ง Market Order (Attempt {attempt + 1}/3) - {order_amount:,.{decimal_places}f} Contracts, Direction: {direction.upper()}")
            try:
                order = exchange.create_market_order(
                    symbol=SYMBOL,
                    side=side,
                    amount=order_amount,
                    params=params
                )
                if order and order.get('id'):
                    logger.info(
                        f" Market Order ส่งสำเร็จ: ID → {order.get('id')}, Status: {order.get('status', 'N/A')}")
                    time.sleep(5)
                    break
                else:
                    logger.warning(f" Order response ไม่สมบูรณ์ (Attempt {attempt + 1}/3)")
            except ccxt.NetworkError as e:
                logger.warning(f" Network Error (Attempt {attempt + 1}/3): {e}")
                if attempt == 2:
                    send_telegram(f" Network Error: ไม่สามารถส่งออเดอร์ได้\n{str(e)[:200]}...")
                time.sleep(15)
            except ccxt.ExchangeError as e:
                logger.warning(f" Exchange Error (Attempt {attempt + 1}/3): {e}")
                if attempt == 2:
                    send_telegram(f" Exchange Error: ไม่สามารถส่งออเดอร์ได้\n{str(e)[:200]}...")
                time.sleep(15)
            except Exception as e:
                logger.error(f" Unexpected error (Attempt {attempt + 1}/3): {e}", exc_info=True)
                send_telegram(f" Unexpected Error: ไม่สามารถส่งออเดอร์ได้\n{str(e)[:200]}...")
                return False, None

        if not order:
            logger.error(" ล้มเหลวในการส่งออเดอร์หลังจาก 3 ครั้ง")
            send_telegram(" Order Failed: ล้มเหลวในการส่งออเดอร์หลังจาก 3 ครั้ง")
            return False, None

        logger.debug(f"DEBUG: Calling confirm_position_entry for direction: {direction}")
        last_trade_side = direction  # จดจำฝั่งล่าสุด
        return confirm_position_entry(direction, order_amount)
    except Exception as e:
        logger.error(f" Critical Error in open_market_order: {e}", exc_info=True)
        send_telegram(f" Critical Error: ไม่สามารถเปิดออเดอร์ได้\n{str(e)[:200]}...")
        return False, None

# ==============================================================================
# 12. ฟังก์ชันตั้งค่า TP/SL/ขาดทุน (ปรับปรุงสำหรับ Trailing SL)
# ==============================================================================
def cancel_all_open_tp_sl_orders():
    """ยกเลิกคำสั่ง TP/SL ที่ค้างอยู่สำหรับ Symbol ปัจจุบันบน Binance Futures."""
    logger.info(f" Checking for and canceling existing TP/SL orders for {SYMBOL}...")
    try:
        open_orders = exchange.fetch_open_orders(SYMBOL)

        canceled_count = 0
        for order in open_orders:
            if (order['status'] == 'open' or order['status'] == 'pending') and \
                    (order.get('reduceOnly', False) == True or \
                     order['type'] in ['TAKE_PROFIT_MARKET', 'STOP_MARKET', 'STOP_LOSS_LIMIT',
                                       'TAKE_PROFIT_LIMIT']):
                try:
                    exchange.cancel_order(order['id'], SYMBOL)
                    logger.info(
                        f" Canceled old TP/SL order: ID {order['id']}, Type: {order['type']}, Side: {order['side']}, Price: {order.get('stopPrice') or order.get('price')}")
                    canceled_count += 1
                except ccxt.OrderNotFound:
                    logger.info(f" Order {order['id']} not found or already canceled/filled. No action needed.")
                except ccxt.BaseError as e:
                    logger.warning(f" Failed to cancel order {order['id']}: {str(e)}")

        if canceled_count == 0:
            logger.info("No old TP/SL orders found to cancel.")
        else:
            logger.info(f"✓ Successfully canceled {canceled_count} old TP/SL orders.")
    except ccxt.NetworkError as e:
        logger.error(f" Network error while fetching/canceling open orders: {e}")
        send_telegram(f" API Error: ไม่สามารถยกเลิก TP/SL เก่าได้ (Network)\nรายละเอียด: {e}")
    except ccxt.ExchangeError as e:
        logger.error(f" Exchange error while fetching/canceling open orders: {e}")
        send_telegram(f" API Error: ไม่สามารถยกเลิก TP/SL เก่าได้ (Exchange)\nรายละเอียด: {e}")
    except Exception as e:
        logger.error(f" An unexpected error occurred while canceling orders: {e}", exc_info=True)
        send_telegram(f" Unexpected Error: ไม่สามารถยกเลิก TP/SL เก่าได้\nรายละเอียด: {e}")


def set_tpsl_for_position(direction: str, amount: float, current_sl_price: float,
                          current_tp_price: float) -> bool:
    """ ตั้ง Take Profit และ Stop Loss สำหรับโพซิชัน.
    จะยกเลิก TP/SL ที่เปิดอยู่ก่อนเสมอแล้วตั้งใหม่
    """
    if not amount or amount <= 0:
        logger.error(" ไม่สามารถตั้ง TP/SL ได้: ขนาดโพซิชันเป็น 0 หรือไม่ถูกต้อง.")
        return False

    cancel_all_open_tp_sl_orders()
    time.sleep(1)

    market_info_precision_price = 'price'
    tp_price_formatted = round_to_precision(current_tp_price, market_info_precision_price)
    sl_price_formatted = round_to_precision(current_sl_price, market_info_precision_price)

    logger.info(
        f" กำลังตั้ง TP: {tp_price_formatted:,.2f} | กำลังตั้ง SL: {sl_price_formatted:,.2f} สำหรับ {direction.upper()}")

    try:
        tp_sl_side = 'sell' if direction == 'long' else 'buy'

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
        logger.info(f" Take Profit order placed: ID → {tp_order.get('id', 'N/A')}")

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
        logger.info(f" Stop Loss order placed: ID → {sl_order.get('id', 'N/A')}")

        return True
    except ccxt.BaseError as e:
        logger.error(f" Error setting TP/SL: {str(e)}", exc_info=True)
        send_telegram(f" API Error(TP/SL): {e.args[0] if e.args else str(e)}")
        return False
    except Exception as e:
        logger.error(f" Unexpected error setting TP/SL: {e}", exc_info=True)
        send_telegram(f" Unexpected Error(TP/SL): {e}")
        return False

# ==============================================================================
# 13. ฟังก์ชันตรวจสอบสถานะและบริหารโพซิชัน (MONITORING FUNCTIONS)
# ==============================================================================
def monitor_position(current_market_price: float):
    """
    ตรวจสอบสถานะโพซิชันปัจจุบันจาก Exchange และจัดการ TP/SL, Trailing SL
    รวมถึงการตรวจจับการปิดโพซิชันด้วย TP/SL และ Reversal
    """
    global current_position_details, last_ema_position_status, monthly_stats, last_trade_closed_time, last_trade_side
    global just_closed_by_tp_sl, just_closed_by_tp_sl_lock, just_closed_by_tp_sl_time
    global just_reversed_position, just_reversed_position_time, just_reversed_position_lock

    logger.info(f" กำลังตรวจสอบสถานะโพซิชัน (Current Price: {current_market_price:,.2f})")
    pos_info_from_exchange = get_current_position()

    # A. ตรวจสอบว่า "โพซิชันใหม่เข้ามาแทน" (Reversal: entry/ side เปลี่ยน)
    if pos_info_from_exchange and current_position_details:
        new_entry = pos_info_from_exchange['entryPrice']
        new_side = pos_info_from_exchange['side']
        old_entry = current_position_details['entry_price']
        old_side = current_position_details['side']

        # *** เพิ่มเงื่อนไขตรวจสอบว่าเป็นการเปลี่ยนฝั่ง (Reversal) หรือไม่ ***
        if abs(new_entry - old_entry) > 1e-6 or new_side != old_side:
            logger.warning(" ตรวจพบโพซิชันใหม่เข้ามาแทน (Reversal หรือ entry/ side เปลี่ยน)")
            send_telegram(
                f" <b>ตรวจพบโพซิชันใหม่ (Reversal)</b>\n"
                f" Side เดิม: {old_side.upper()}, Entry: {old_entry:.2f}\n"
                f" Side ใหม่: {new_side.upper()}, Entry: {new_entry:.2f}\n"
                f"ระบบจะรีเซ็ตสถานะและยกเลิก TP/SL เดิม")

            # ยกเลิกออเดอร์ที่ค้างอยู่
            try:
                exchange.cancel_all_orders(SYMBOL)
            except Exception as e:
                logger.warning(f" ไม่สามารถยกเลิกคำสั่งค้าง: {e}")

            # อัปเดตสถานะใหม่
            new_position_details = {
                'symbol': pos_info_from_exchange['symbol'],
                'side': new_side,
                'contracts': pos_info_from_exchange['contracts'],
                'entry_price': new_entry,
                'unrealized_pnl': pos_info_from_exchange['unrealizedPnl'],
                'liquidation_price': pos_info_from_exchange['liquidationPrice'],
                'sl_step': 0,
                'sl_price': None,
                'tp_price': None,
                'initial_sl_price': None
            }
            current_position_details = new_position_details

            # รีเซ็ตสถานะเก่า
            last_ema_position_status = None
            last_trade_side = None # รีเซ็ตเพื่อป้องกันการเปิดซ้ำ

            # *** อัปเดต Flag สำหรับ Reversal และเวลา ***
            with just_reversed_position_lock:
                just_reversed_position = True
                just_reversed_position_time = datetime.now()

            # รีเซ็ต Flag just_closed_by_tp_sl ด้วย (ถ้ามี)
            with just_closed_by_tp_sl_lock:
                 just_closed_by_tp_sl = False

            logger.info(" *** ตั้ง Flag just_reversed_position = True และรีเซ็ต last_trade_side *** ")
            time.sleep(5) # หน่วงเวลาเล็กน้อย
            return # ออกจากฟังก์ชันเพื่อประมวลผลใหม่ในรอบถัดไป

    # B. โพซิชันปิด (ไม่มีใน exchange แล้ว แต่บอทยังมีข้อมูล) - กรณี TP/SL ปกติ
    elif not pos_info_from_exchange and current_position_details:
        # ตรวจสอบ open orders เพื่อดูว่า TP/SL ยังอยู่ไหม
        tp_sl_orders_exist = False
        try:
            open_orders = exchange.fetch_open_orders(SYMBOL)
            tp_sl_orders = [o for o in open_orders if o.get('reduceOnly') and o.get('type') in ['TAKE_PROFIT_MARKET', 'STOP_MARKET']]
            tp_sl_orders_exist = len(tp_sl_orders) > 0
        except Exception as e:
            logger.warning(f" ไม่สามารถดึง open orders ได้เพื่อตรวจสอบ TP/SL: {e}")
            # สมมุติว่าไม่มีเพื่อให้รีเซ็ตสถานะได้
            tp_sl_orders_exist = False

        # ถ้าไม่มีข้อมูลจาก exchange และไม่มี TP/SL ค้างอยู่ แสดงว่าโพซิชันถูกปิดแล้ว (อาจด้วย TP/SL)
        if not tp_sl_orders_exist:
            closed_price = current_market_price
            pnl = 0.0
            entry = current_position_details['entry_price']
            contracts = current_position_details['contracts']
            side = current_position_details['side']

            if side == 'long':
                pnl = (closed_price - entry) * contracts
            else:
                pnl = (entry - closed_price) * contracts

            send_telegram(
                f" <b>ปิดโพซิชัน {side.upper()} สำเร็จ</b>\n"
                f"ราคาออก: <code>{closed_price:.2f}</code>\n"
                f"PnL(ประมาณ): <code>{pnl:.2f} USDT</code>")

            try:
                exchange.cancel_all_orders(SYMBOL)
            except Exception as e:
                logger.warning(f" ไม่สามารถยกเลิกคำสั่งทั้งหมดหลังปิดโพซิชัน: {e}")
                send_telegram(f" ยกเลิกคำสั่งไม่สำเร็จหลังปิดโพซิชัน: {e}")

            # *** อัปเดตสถานะและ Flag พร้อมกัน ***
            current_position_details = None
            last_trade_side = None # รีเซ็ตเพื่อให้สามารถเทรดฝั่งเดียวกันได้หลัง cooldown
            last_ema_position_status = None
            last_trade_closed_time = datetime.now() # อัปเดต cooldown

            # *** ตั้ง Flag ว่าเพิ่งปิดด้วย TP/SL ***
            with just_closed_by_tp_sl_lock:
                just_closed_by_tp_sl = True
                just_closed_by_tp_sl_time = datetime.now()

            # รีเซ็ต Flag Reversal ด้วย
            with just_reversed_position_lock:
                just_reversed_position = False

            save_monthly_stats()
            add_trade_result("TP/SL", pnl)
            logger.info(" *** ตั้ง Flag just_closed_by_tp_sl = True และรีเซ็ต Flag Reversal *** ")
            return

    # C. มีโพซิชันเปิดอยู่ → ดำเนินการจัดการ TP/SL
    elif pos_info_from_exchange and current_position_details:
        # รีเซ็ต Flag หากยังมีโพซิชันอยู่ (กรณี Reversal แล้วผ่านไปหนึ่งรอบ)
        with just_reversed_position_lock:
            if just_reversed_position and (datetime.now() - just_reversed_position_time).total_seconds() > 30:
                 logger.info(" *** รีเซ็ต Flag just_reversed_position = False (หลัง Reversal ผ่านไปหนึ่งรอบ) *** ")
                 just_reversed_position = False
        # รีเซ็ต Flag หากยังมีโพซิชันอยู่ (กรณี just_closed_by_tp_sl)
        with just_closed_by_tp_sl_lock:
            if just_closed_by_tp_sl:
                logger.info(" *** พบว่ายังมีโพซิชันอยู่ รีเซ็ต Flag just_closed_by_tp_sl = False *** ")
                just_closed_by_tp_sl = False

        current_position_details['unrealized_pnl'] = pos_info_from_exchange['unrealizedPnl']
        current_position_details['liquidation_price'] = pos_info_from_exchange['liquidationPrice']

        side = current_position_details['side']
        entry_price = current_position_details['entry_price']
        contracts = current_position_details['contracts']
        sl_step = current_position_details['sl_step']
        tp_price = current_position_details['tp_price']
        sl_price = current_position_details['sl_price']

        logger.info(
            f" {side.upper()} | Entry: {entry_price:.2f} | Price: {current_market_price:.2f} | PnL: {current_position_details['unrealized_pnl']:.2f}")

        # ตั้ง TP/SL ครั้งแรก
        if tp_price is None or sl_price is None:
            tp = entry_price + TP_DISTANCE_POINTS if side == 'long' else entry_price - TP_DISTANCE_POINTS
            sl = entry_price - SL_DISTANCE_POINTS if side == 'long' else entry_price + SL_DISTANCE_POINTS

            current_position_details['tp_price'] = tp
            current_position_details['sl_price'] = sl
            current_position_details['initial_sl_price'] = sl

            logger.info(f" ตั้ง TP/SL เริ่มต้น → TP: {tp:.2f} | SL: {sl:.2f}")
            set_tpsl_for_position(side, contracts, sl, tp)

        # Trailing SL 2-step
        pnl_points = (current_market_price - entry_price) if side == 'long' else (entry_price - current_market_price)
        trail_trigger_1 = TRAIL_SL_STEP1_TRIGGER_LONG_POINTS if side == 'long' else TRAIL_SL_STEP1_TRIGGER_SHORT_POINTS
        trail_trigger_2 = TRAIL_SL_STEP2_TRIGGER_LONG_POINTS if side == 'long' else TRAIL_SL_STEP2_TRIGGER_SHORT_POINTS
        trail_sl_1 = entry_price + TRAIL_SL_STEP1_NEW_SL_POINTS_LONG if side == 'long' else entry_price + TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT
        trail_sl_2 = entry_price + TRAIL_SL_STEP2_NEW_SL_POINTS_LONG if side == 'long' else entry_price + TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT

        if sl_step == 0 and pnl_points >= trail_trigger_1:
            current_position_details['sl_step'] = 1
            current_position_details['sl_price'] = trail_sl_1
            logger.info(" SL Step 1 triggered → ย้าย SL")
            send_telegram(f" <b>SL Step 1</b> | SL ใหม่: <code>{trail_sl_1:.2f}</code>")
            set_tpsl_for_position(side, contracts, trail_sl_1, tp_price)
        elif sl_step == 1 and pnl_points >= trail_trigger_2:
            current_position_details['sl_step'] = 2
            current_position_details['sl_price'] = trail_sl_2
            logger.info(" SL Step 2 triggered → ย้าย SL อีกครั้ง")
            send_telegram(f" <b>SL Step 2</b> | SL ใหม่: <code>{trail_sl_2:.2f}</code>")
            set_tpsl_for_position(side, contracts, trail_sl_2, tp_price)

    # D. ไม่มีโพซิชันทั้งใน exchange และใน bot
    else:
        if current_position_details:
            logger.warning(" บอทยังมีสถานะ แต่ exchange ไม่มีโพซิชัน → รีเซ็ต")
            try:
                exchange.cancel_all_orders(SYMBOL)
            except Exception as e:
                logger.warning(f" ยกเลิกคำสั่งค้างไม่สำเร็จ: {e}")
            current_position_details = None
            last_ema_position_status = None
            last_trade_side = None # รีเซ็ต
            # รีเซ็ต Flag ทั้งหมด
            with just_closed_by_tp_sl_lock:
                if just_closed_by_tp_sl:
                    logger.info(" *** ไม่มีอะไรเลย รีเซ็ต Flag just_closed_by_tp_sl = False *** ")
                    just_closed_by_tp_sl = False
            with just_reversed_position_lock:
                if just_reversed_position:
                    logger.info(" *** ไม่มีอะไรเลย รีเซ็ต Flag just_reversed_position = False *** ")
                    just_reversed_position = False
            save_monthly_stats()
        else:
            logger.info(" ไม่มีโพซิชันเปิดอยู่")
            # รีเซ็ต Flag หากไม่มีอะไรเลย
            with just_closed_by_tp_sl_lock:
                if just_closed_by_tp_sl:
                     logger.debug(" *** ไม่มีโพซิชัน รีเซ็ต Flag just_closed_by_tp_sl = False (Debug) *** ")
                     just_closed_by_tp_sl = False
            with just_reversed_position_lock:
                if just_reversed_position:
                     logger.debug(" *** ไม่มีโพซิชัน รีเซ็ต Flag just_reversed_position = False (Debug) *** ")
                     just_reversed_position = False

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
        logger.debug(f" รายงานประจำเดือนสำหรับ {current_month_year} ถูกส่งไปแล้ว.")
        return

    report_day_of_month = min(MONTHLY_REPORT_DAY, calendar.monthrange(now.year, now.month)[1])
    if not (now.day == report_day_of_month and now.hour == MONTHLY_REPORT_HOUR and now.minute == MONTHLY_REPORT_MINUTE):
        logger.debug(
            f" ยังไม่ถึงเวลาส่งรายงานประจำเดือน ({report_day_of_month} {MONTHLY_REPORT_HOUR:02d}:{MONTHLY_REPORT_MINUTE:02d}).")
        return

    try:
        balance = get_portfolio_balance()

        if monthly_stats['month_year'] != current_month_year:
            logger.info(
                f" สถิติประจำเดือนที่ใช้ไม่ตรงกับเดือนนี้ ({monthly_stats['month_year']} vs {current_month_year}). กำลังรีเซ็ตสถิติเพื่อรายงานเดือนใหม่.")
            reset_monthly_stats()

        tp_count = monthly_stats['tp_count']
        sl_count = monthly_stats['sl_count']
        total_pnl = monthly_stats['total_pnl']
        pnl_from_start = balance - initial_balance if initial_balance > 0 else 0.0

        message = f""" <b>รายงานสรุปผลประจำเดือน - {now.strftime('%B %Y')}</b>
<b> กำไรสุทธิเดือนนี้:</b> <code>{total_pnl:+,.2f} USDT</code>
<b> SL:</b> <code>{sl_count} ครั้ง</code>
<b> TP:</b> <code>{tp_count} ครั้ง</code>
<b> คงเหลือปัจจุบัน:</b> <code>{balance:,.2f} USDT</code>
<b> กำไร/ขาดทุนรวมจากยอดเริ่มต้น:</b> <code>{pnl_from_start:+,.2f} USDT</code>
<b> บอทยังทำงานปกติ</b>
<b>เวลา:</b> <code>{now.strftime('%H:%M')}</code>"""
        send_telegram(message)
        last_monthly_report_date = now.date()
        monthly_stats['last_report_month_year'] = current_month_year
        save_monthly_stats()
        logger.info(" ส่งรายงานประจำเดือนแล้ว.")
    except Exception as e:
        logger.error(f" เกิดข้อผิดพลาดในการส่งรายงานประจำเดือน: {e}", exc_info=True)
        send_telegram(f" Error: ไม่สามารถส่งรายงานประจำเดือนได้\nรายละเอียด: {e}")


def monthly_report_scheduler():
    global last_monthly_report_date

    logger.info(" เริ่ม Monthly Report Scheduler.")
    while True:
        now = datetime.now()

        report_day = min(MONTHLY_REPORT_DAY, calendar.monthrange(now.year, now.month)[1])
        next_report_time_this_month = now.replace(day=report_day, hour=MONTHLY_REPORT_HOUR,
                                                  minute=MONTHLY_REPORT_MINUTE, second=0, microsecond=0)

        if now >= next_report_time_this_month and \
                (last_monthly_report_date is None or \
                 last_monthly_report_date.year != now.year or \
                 last_monthly_report_date.month != now.month):
            logger.info(
                f" ตรวจพบว่าถึงเวลาส่งรายงานประจำเดือน ({now.strftime('%H:%M')}) และยังไม่ได้ส่งสำหรับเดือนนี้. กำลังส่ง...")
            monthly_report()

        if now >= next_report_time_this_month:
            next_month = now.month + 1
            next_year = now.year
            if next_month > 12:
                next_month = 1
                next_year += 1
            max_day_in_next_month = calendar.monthrange(next_year, next_month)[1]
            report_day_for_next_month = min(MONTHLY_REPORT_DAY, max_day_in_next_month)
            next_report_time = datetime(next_year, next_month, report_day_for_next_month,
                                        MONTHLY_REPORT_HOUR, MONTHLY_REPORT_MINUTE, 0, 0)
        else:
            next_report_time = next_report_time_this_month

        time_to_wait = (next_report_time - datetime.now()).total_seconds()
        if time_to_wait > 0:
            logger.info(
                f" กำหนดส่งรายงานประจำเดือนถัดไปในอีก {int(time_to_wait / 86400)} วัน {int((time_to_wait % 86400) / 3600)} ชั่วโมง {int((time_to_wait % 3600) / 60)} นาที.")
            time.sleep(max(time_to_wait, 60))
        else:
            time.sleep(60)

# ==============================================================================
# 15. ฟังก์ชันเริ่มต้นบอท (BOT STARTUP FUNCTIONS)
# ==============================================================================
def send_startup_message():
    global initial_balance

    try:
        initial_balance = get_portfolio_balance()
        startup_time = datetime.now().strftime('%d/%m/%Y %H:%M:%S')

        message = f""" <b>บอทเริ่มทำงาน </b>
<b> EMA Cross Trading Bot</b>
<b> ยอดเริ่มต้น:</b> <code>{initial_balance:,.2f} USDT</code>
<b> เวลาเริ่ม:</b> <code>{startup_time}</code>
<b> เฟรม:</b> <code>{TIMEFRAME}</code> | <b>Leverage:</b> <code>{LEVERAGE}x</code>
<b> TP:</b> <code>{TP_DISTANCE_POINTS}</code> | <b>SL(เริ่มต้น):</b> <code>{SL_DISTANCE_POINTS}</code>
<b> Trailing SL(Long):</b> Step1:{TRAIL_SL_STEP1_TRIGGER_LONG_POINTS}pts->SL({TRAIL_SL_STEP1_NEW_SL_POINTS_LONG:+,}pts), Step2: {TRAIL_SL_STEP2_TRIGGER_LONG_POINTS}pts->SL({TRAIL_SL_STEP2_NEW_SL_POINTS_LONG:+,}pts)
<b> Trailing SL(Short):</b> Step1:{TRAIL_SL_STEP1_TRIGGER_SHORT_POINTS}pts->SL({TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT:+,}pts), Step2: {TRAIL_SL_STEP2_TRIGGER_SHORT_POINTS}pts->SL({TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT:+,}pts)
<b> Margin Buffer:</b> <code>{MARGIN_BUFFER_USDT:,.0f} USDT</code>
<b> Railway Region:</b> <code>{os.getenv('RAILWAY_REGION', 'Unknown')}</code>
<b> กำลังรอเปิดออเดอร์...</b>"""
        send_telegram(message)
        logger.info(" ส่งข้อความแจ้งเตือนเมื่อบอทเริ่มทำงาน.")
    except Exception as e:
        logger.error(f" เกิดข้อผิดพลาดในการส่งข้อความเริ่มต้น: {e}", exc_info=True)

# ==============================================================================
# 16. ฟังก์ชันหลักของบอท (MAIN BOT LOGIC)
# ==============================================================================
def main():
    global current_position_details, last_ema_position_status, last_ema_calc_time, last_trade_closed_time, last_trade_side

    try:
        setup_exchange()
        load_monthly_stats()
        send_startup_message()

        monthly_thread = threading.Thread(target=monthly_report_scheduler, daemon=True)
        monthly_thread.start()

        logger.info(" Monthly Report Scheduler Thread Started.")
    except Exception as e:
        error_msg = f" Error: ไม่สามารถเริ่มต้นบอทได้\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
        send_telegram(error_msg)
        logger.critical(f" Startup error: {e}", exc_info=True)
        time.sleep(ERROR_RETRY_SLEEP_SECONDS)
        sys.exit(1)

    logger.info(" บอทเข้าสู่ Main Loop แล้วและพร้อมทำงาน...")
    force_open_initial_order = False  # ตั้งเป็น True สำหรับการทดสอบเปิด Long ทันที/False เพื่อ ใช้emaคำนวณ
    last_ema_calc_time = datetime.min  # กำหนดเวลาเริ่มต้นสำหรับ EMA Calculation
    last_trade_closed_time = datetime.min  # กำหนดเวลาเริ่มต้นสำหรับ Cooldown

    while True:
        try:
            current_time = datetime.now()
            logger.info(
                f" เริ่มรอบ Main Loop ({current_time.strftime('%Y-%m-%d %H:%M:%S')}) - กำลังดึงข้อมูลและตรวจสอบ.")

            # --- 1. ดึงราคาปัจจุบัน ---
            current_price = None
            try:
                logger.info(" กำลังดึงราคาล่าสุด (Ticker)...")
                ticker = exchange.fetch_ticker(SYMBOL)
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f" Error fetching ticker: {e}. Retrying in {ERROR_RETRY_SLEEP_SECONDS} วินาที...")
                send_telegram(
                    f" API Error: ไม่สามารถดึงราคาล่าสุดได้. รายละเอียด: {e.args[0] if e.args else str(e)}")
                time.sleep(ERROR_RETRY_SLEEP_SECONDS)
                continue

            if not ticker or 'last' not in ticker:
                logger.error(" Failed to fetch valid ticker. Skipping loop and retrying.")
                send_telegram(" Error: ไม่สามารถดึงราคาล่าสุดได้ถูกต้อง. Skipping.")
                time.sleep(ERROR_RETRY_SLEEP_SECONDS)
                continue
            current_price = float(ticker['last'])
            logger.info(f" ราคาปัจจุบันของ {SYMBOL}: {current_price:,.2f}")

            # --- 2. ตรวจสอบสถานะและบริหารจัดการโพซิชันที่มีอยู่ (TP/SL, Trailing SL) ---
            monitor_position(current_price)

            # --- 3. วิเคราะห์สัญญาณ EMA แล้วเปิด order ถ้ามีสัญญาณใหม่ (มี cooldown ด้วย) ---
            check_ema_signal_and_trade(current_price)

            # --- 4. ตรวจสอบสัญญาณและเปิดโพซิชันใหม่ (ถ้าไม่มีโพซิชันเปิดอยู่) ---
            if current_position_details is None:
                # ตรวจสอบ Cooldown ก่อน
                if (current_time - last_trade_closed_time).total_seconds() < TRADE_COOLDOWN_SECONDS:
                    time_left_cooldown = TRADE_COOLDOWN_SECONDS - (
                                current_time - last_trade_closed_time).total_seconds()
                    logger.info(
                        f" อยู่ในช่วง Cooldown หลังปิดเทรด. จะเปิดเทรดใหม่ได้ในอีก {time_left_cooldown:,.0f} วินาที.")
                    # continue # เทสบอทให้ลบส่วนนี้ออก
                # โหมด EMA Cross ปกติ (เมื่อ force_open_initial_order เป็น False และไม่อยู่ในช่วง Cooldown)
                else:
                    # ตรวจสอบว่าถึงเวลาคำนวณ EMA หรือยัง
                    if (current_time - last_ema_calc_time).total_seconds() >= EMA_CALC_INTERVAL_SECONDS:
                        logger.info(
                            " ไม่มีโพซิชันเปิดอยู่. ถึงเวลาตรวจสอบสัญญาณ EMA Cross เพื่อเปิดโพซิชัน...")
                        signal = check_ema_cross()
                        last_ema_calc_time = current_time  # อัปเดตเวลาที่คำนวณ EMA ล่าสุด

                        if signal:
                            logger.info(f" ตรวจพบสัญญาณ EMA Cross: {signal.upper()}. กำลังพยายามเปิดออเดอร์")
                            send_telegram(
                                f" <b>SIGNAL:</b> ตรวจพบสัญญาณ EMA Cross: <b>{signal.upper()}</b>")

                            # เพิ่มการตรวจสอบสถานะอีกครั้งก่อนเปิดออเดอร์
                            if get_current_position() is not None:
                                logger.info(" พบว่ามีโพซิชันเปิดอยู่แล้ว (อาจเปิดจาก TP/SL) ข้ามการเปิดใหม่.")
                                continue

                            market_order_success, confirmed_entry_price = open_market_order(signal, current_price)
                            if market_order_success and confirmed_entry_price:
                                logger.info(
                                    f" เปิดออเดอร์ {signal.upper()} สำเร็จ. บอทจะดูแล TP/SL ในรอบถัดไป.")
                            else:
                                logger.warning(f" ไม่สามารถเปิด Market Order {signal.upper()} ได้.")
                        else:
                            logger.info(" ไม่พบสัญญาณ EMA Cross ที่ชัดเจนในรอบนี้.")
                    else:
                        time_until_next_ema = EMA_CALC_INTERVAL_SECONDS - (
                                    current_time - last_ema_calc_time).total_seconds()
                        logger.info(f" ไม่มีโพซิชันเปิดอยู่. รอคำนวณ EMA Cross อีก {time_until_next_ema:,.0f} วินาที.")
            else:  # กรณีมีโพซิชันเปิดอยู่แล้ว (ไม่ว่าจะเปิดโดยบังคับหรือโดย EMA)
                logger.info(
                    f"Current Position: {current_position_details['side'].upper()}, SL Step: {current_position_details['sl_step']}. บอทจะดูแลการปิดหรือเลื่อน SL เพิ่มเติม.")

            # --- 4. หน่วงเวลาสำหรับรอบ Main Loop (ลูปเร็ว) ---
            logger.info(f" จบรอบ Main Loop. รอ {FAST_LOOP_INTERVAL_SECONDS} วินาทีสำหรับรอบถัดไป.")
            time.sleep(FAST_LOOP_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info(" บอทหยุดทำงานโดยผู้ใช้ (KeyboardInterrupt).")
            send_telegram(" Bot หยุดทำงานโดยผู้ใช้.")
            break
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            error_msg = f" API Error ใน Main Loop\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
            logger.error(error_msg, exc_info=True)
            send_telegram(error_msg)
            time.sleep(ERROR_RETRY_SLEEP_SECONDS)
        except Exception as e:
            error_msg = f" Error: เกิดข้อผิดพลาดที่ไม่คาดคิดใน Main Loop\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
            logger.error(error_msg, exc_info=True)
            send_telegram(error_msg)
            time.sleep(ERROR_RETRY_SLEEP_SECONDS)

# ==============================================================================
# 17. จุดเริ่มต้นการทำงานของโปรแกรม (ENTRY POINT)
# ==============================================================================
if __name__ == '__main__':
    main()
