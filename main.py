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

# ==============================================================================
# 1. ตั้งค่าพื้นฐาน (CONFIGURATION)
# ==============================================================================

# --- API Keys & Credentials (ดึงจาก Environment Variables เพื่อความปลอดภัย) ---
API_KEY = os.getenv('RAILWAY_API_KEY', 'YOUR_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('RAILWAY_SECRET', 'YOUR_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('RAILWAY_PASSWORD', 'YOUR_PASSWORD_HERE_FOR_LOCAL_TESTING')

# --- Trade Parameters ---
SYMBOL = 'BTC/USDT:USDT'
TIMEFRAME = '5m'
LEVERAGE = 30
TP_VALUE_POINTS = 501
SL_VALUE_POINTS = 999
BE_PROFIT_TRIGGER_POINTS = 350
BE_SL_BUFFER_POINTS = 100
PORTFOLIO_PERCENT_TRADE = 1.0 # ใช้ 90% ของพอร์ต ("All in") เผื่อค่าธรรมเนียม
CROSS_THRESHOLD_POINTS = 5 # จำนวนจุดที่ EMA ต้องห่างกันเพื่อยืนยันสัญญาณ

# --- Telegram Notification Settings ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# --- Files & Paths ---
STATS_FILE = 'trading_stats.json' # ตรวจสอบให้แน่ใจว่าได้เปลี่ยนเป็น /data/trading_stats.json หากใช้ Railway Volume

# --- Bot Timing ---
MAIN_LOOP_SLEEP_SECONDS = 300 # 6 นาที
ERROR_RETRY_SLEEP_SECONDS = 60
MONTHLY_REPORT_DAY = 20
MONTHLY_REPORT_HOUR = 0
MONTHLY_REPORT_MINUTE = 5

# --- Tolerance สำหรับการระบุสาเหตุการปิดออเดอร์ (ใช้สำหรับ Soft TP/SL Detection ถ้าไม่ได้ตั้งใน Exchange) ---
TP_SL_BE_PRICE_TOLERANCE_PERCENT = 0.005 

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
current_position_size = 0.0
last_ema_position_status = None # 'above', 'below', หรือ None (เมื่อเริ่มต้น)

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
    'last_ema_cross_signal': None, # ยังคงเก็บไว้เผื่อ Logic ในอนาคต
    'last_ema_position_status': None # ใช้สำหรับเก็บสถานะ EMA เพื่อการตรวจจับ Cross
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
        'options': {
            'defaultType': 'swap',
            'warnOnFetchOHLCVLimitArgument': False,
            'adjustForTimeDifference': True,
            'loadMarkets': False, 
            'loadInstruments': False,
            'loadCurrencies': False,
        },
        'verbose': False, 
        'timeout': 30000,
    })
    exchange.set_sandbox_mode(False) # ⚠️ ตั้งค่าให้เป็น False สำหรับบัญชีจริง
    logger.info("✅ เชื่อมต่อกับ OKX Exchange สำเร็จ")

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
    """บันทึกสถิติการเทรดประจำเดือนลงในไฟล์ JSON."""
    global monthly_stats, last_ema_position_status
    try:
        monthly_stats['last_ema_position_status'] = last_ema_position_status # อัปเดตสถานะ EMA ก่อนบันทึก
        with open(STATS_FILE, 'w') as f:
            json.dump(monthly_stats, f, indent=4)
        logger.debug(f"💾 บันทึกสถิติการเทรดลงไฟล์ {STATS_FILE} สำเร็จ")
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการบันทึกสถิติ: {e}")

def load_monthly_stats():
    """โหลดสถิติการเทรดประจำเดือนจากไฟล์ JSON."""
    global monthly_stats, last_monthly_report_date, last_ema_position_status
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
                last_ema_position_status = loaded_stats.get('last_ema_position_status', None) # โหลดสถานะ EMA

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
        logger.error(f"❌ เกิดข้อผิดพลาดในการโหลดสถิติ: {e}")
        # ตั้งค่าเริ่มต้นหากเกิดข้อผิดพลาดในการโหลด
        monthly_stats = {
            'month_year': None, 'tp_count': 0, 'sl_count': 0, 'total_pnl': 0.0, 'trades': [],
            'last_report_month_year': None, 'last_ema_cross_signal': None, 'last_ema_position_status': None
        }
        last_monthly_report_date = None
        last_ema_position_status = None
        reset_monthly_stats()

def reset_monthly_stats():
    """รีเซ็ตสถิติประจำเดือนสำหรับเดือนใหม่."""
    global monthly_stats, last_ema_position_status
    monthly_stats['month_year'] = datetime.now().strftime('%Y-%m')
    monthly_stats['tp_count'] = 0
    monthly_stats['sl_count'] = 0
    monthly_stats['total_pnl'] = 0.0
    monthly_stats['trades'] = []
    last_ema_position_status = None # รีเซ็ตสถานะ EMA เมื่อเริ่มเดือนใหม่
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
            logger.debug(f"🔍 กำลังดึงยอดคงเหลือ (Attempt {i+1}/{retries})...")
            balance_data = exchange.fetch_balance(params={'type': 'trading'})
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
            logger.error(f"❌ Unexpected error in get_portfolio_balance: {e}", exc_info=True)
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
            logger.debug(f"🔍 กำลังดึงโพซิชันปัจจุบัน (Attempt {i+1}/{retries})...")
            positions = exchange.fetch_positions([SYMBOL])
            logger.debug(f"DEBUG: Fetched positions raw: {positions}") # <--- เพิ่มบรรทัดนี้เพื่อ debug
            time.sleep(2)
            for pos in positions:
                if float(pos.get('info', {}).get('posAmt', 0)) != 0:
                    pos_amount = float(pos['info']['posAmt'])
                    return {
                        'side': 'long' if pos_amount > 0 else 'short',
                        'size': abs(pos_amount),
                        'entry_price': float(pos['entryPrice']),
                        'unrealized_pnl': float(pos['unrealizedPnl']),
                        'pos_id': pos.get('id', 'N/A') # ใช้ .get() เพื่อความปลอดภัย
                    }
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
    """
    ตรวจสอบการตัดกันของ EMA50 และ EMA200 โดยใช้ Threshold เพื่อยืนยัน.
    จะเปิดออเดอร์เมื่อ EMA50 "เปลี่ยนสถานะ" จากเหนือ/ใต้ เป็นใต้/เหนือ และห่างเกิน Threshold.
    ถ้าบอทเพิ่งเริ่มรัน (last_ema_position_status เป็น None) จะบันทึกสถานะปัจจุบันไว้ก่อน ไม่ส่งสัญญาณ.
    """
    global last_ema_position_status 
    
    try:
        retries = 3
        ohlcv = None
        for i in range(retries):
            logger.debug(f"🔍 กำลังดึงข้อมูล OHLCV สำหรับ EMA ({i+1}/{retries})...")
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
                logger.error(f"❌ Unexpected error fetching OHLCV: {e}", exc_info=True)
                send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึง OHLCV ได้\nรายละเอียด: {e}")
                return None

        if not ohlcv:
            logger.error(f"❌ Failed to fetch OHLCV after {retries} attempts.")
            send_telegram(f"⛔️ API Error: ล้มเหลวในการดึง OHLCV หลังจาก {retries} ครั้ง.")
            return None

        if len(ohlcv) < 202: 
            logger.warning(f"ข้อมูล OHLCV ไม่เพียงพอ. ต้องการอย่างน้อย 202 แท่ง ได้ {len(ohlcv)}")
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
        
        # ถ้ายังไม่เคยบันทึกสถานะ EMA เลย (บอทเพิ่งเริ่มรัน)
        if last_ema_position_status is None:
            if current_ema_position:
                last_ema_position_status = current_ema_position
                save_monthly_stats() # บันทึกสถานะเริ่มต้น
                logger.info(f"ℹ️ บอทเพิ่งเริ่มรัน. บันทึกสถานะ EMA ปัจจุบันเป็น: {current_ema_position.upper()}. จะรอสัญญาณการตัดกันครั้งถัดไป.")
            return None # ไม่ส่งสัญญาณเพื่อเปิดออเดอร์ในรอบแรก

        cross_signal = None

        # Golden Cross (Long) - EMA50 เปลี่ยนจากอยู่ล่าง เป็นอยู่บน และห่างเกิน Threshold
        if last_ema_position_status == 'below' and current_ema_position == 'above' and \
           ema50_current > (ema200_current + CROSS_THRESHOLD_POINTS):
            cross_signal = 'long'
            logger.info(f"🚀 Threshold Golden Cross: EMA50({ema50_current:.2f}) is {CROSS_THRESHOLD_POINTS} points above EMA200({ema200_current:.2f})")

        # Death Cross (Short) - EMA50 เปลี่ยนจากอยู่บน เป็นอยู่ล่าง และห่างเกิน Threshold
        elif last_ema_position_status == 'above' and current_ema_position == 'below' and \
             ema50_current < (ema200_current - CROSS_THRESHOLD_POINTS):
            cross_signal = 'short'
            logger.info(f"🔻 Threshold Death Cross: EMA50({ema50_current:.2f}) is {CROSS_THRESHOLD_POINTS} points below EMA200({ema200_current:.2f})")

        # อัปเดตสถานะ EMA ล่าสุดเสมอ (ไม่ว่าจะมีสัญญาณหรือไม่)
        # ยกเว้นเมื่อมีสัญญาณที่จะถูกส่งออกไป (เพราะหลังเปิดออเดอร์ เราจะรีเซ็ต last_ema_position_status เป็น None ใน main loop)
        if current_ema_position and cross_signal is None: # ถ้าไม่มีสัญญาณครอส ให้บันทึกสถานะปัจจุบัน
            last_ema_position_status = current_ema_position
            save_monthly_stats() 

        if cross_signal:
            logger.info(f"✨ สัญญาณ EMA Cross ที่ตรวจพบ: {cross_signal.upper()}")
        else:
            logger.info("🔎 ไม่พบสัญญาณ EMA Cross ที่ชัดเจน.") 
            
        return cross_signal

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการคำนวณ EMA: {e}", exc_info=True)
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
        
        # ดึงค่า min amount และ min notional, และจัดการกรณีที่อาจเป็น None
        # ใช้ .get() เพื่อป้องกัน KeyError และใช้ None แทนค่าเริ่มต้นเพื่อแยกแยะระหว่าง 0 กับ ไม่มีค่า
        min_amount_btc_from_exchange_val = market.get('limits', {}).get('amount', {}).get('min')
        min_notional_usdt_from_exchange_val = market.get('limits', {}).get('cost', {}).get('min')
        
        # เตรียมตัวแปรสำหรับแสดงผลใน Log ให้ปลอดภัยจาก None
        min_amount_btc_display = min_amount_btc_from_exchange_val if min_amount_btc_from_exchange_val is not None else 0.0
        min_notional_usdt_display = min_notional_usdt_from_exchange_val if min_notional_usdt_from_exchange_val is not None else 0.0

        logger.info(f"ℹ️ Exchange Minimums for {SYMBOL}: Min_Amount_BTC={min_amount_btc_display:.6f}, Min_Notional_USDT={min_notional_usdt_display:.2f}")


        order_size_in_btc_calculated = (use_balance_for_trade * LEVERAGE) / current_price
        logger.info(f"ℹ️ Calculated Order Size (raw): {order_size_in_btc_calculated:.6f} BTC (จาก {use_balance_for_trade:,.2f} USDT * {LEVERAGE}x)")

        order_size_in_btc = order_size_in_btc_calculated # เริ่มต้นด้วยขนาดที่คำนวณได้

        # ตรวจสอบและบังคับใช้ขั้นต่ำของ Amount
        if min_amount_btc_from_exchange_val is not None and min_amount_btc_from_exchange_val > 0:
            # ถ้าขนาดที่คำนวณได้น้อยกว่าขั้นต่ำ ให้ใช้ขั้นต่ำ (หรือใช้ขนาดที่คำนวณได้ถ้ามันมากกว่า)
            order_size_in_btc = max(order_size_in_btc, min_amount_btc_from_exchange_val)
            logger.info(f"ℹ️ Adjusted for Min_Amount_BTC: Current order_size_in_btc={order_size_in_btc:.6f} (Min={min_amount_btc_from_exchange_val:.6f})")

        # ตรวจสอบและบังคับใช้ขั้นต่ำของ Notional Value
        current_notional_value = order_size_in_btc * current_price
        if min_notional_usdt_from_exchange_val is not None and min_notional_usdt_from_exchange_val > 0:
            if current_notional_value < min_notional_usdt_from_exchange_val:
                required_btc_for_min_notional = min_notional_usdt_from_exchange_val / current_price
                # ต้องมั่นใจว่าขนาดที่ปรับแล้วยังมากกว่าขั้นต่ำของ Amount ด้วย
                order_size_in_btc = max(order_size_in_btc, required_btc_for_min_notional) 
                logger.warning(f"⚠️ Adjusted for Min_Notional_USDT: Current order_size_in_btc={order_size_in_btc:.6f} (Min Notional={min_notional_usdt_from_exchange_val:.2f})")
        
        # บรรทัดนี้ต้องอยู่หลังการปรับขั้นต่ำทั้งหมด
        order_size_in_btc = float(exchange.amount_to_precision(SYMBOL, order_size_in_btc))
        logger.info(f"ℹ️ ขนาดออเดอร์สุดท้ายหลังจากปรับขั้นต่ำและ Precision: {order_size_in_btc:.6f} BTC")

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
            logger.info(f"⚡️ กำลังส่งคำสั่ง Market Order (Attempt {i+1}/3)...")
            try:
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
                logger.error(f"❌ Unexpected error creating market order: {e}", exc_info=True)
                send_telegram(f"⛔️ Unexpected Error: ไม่สามารถสร้างออเดอร์ตลาดได้\nรายละเอียด: {e}")
                return False, None

        if not order:
            logger.error("❌ Failed to create market order after 3 attempts.")
            send_telegram("⛔️ API Error: ล้มเหลวในการสร้างออเดอร์ตลาดหลังจาก 3 ครั้ง.")
            return False, None

        confirmed_pos_info = None
        confirmation_retries = 15
        confirmation_sleep = 3 # <<-- ปรับเพิ่มเวลา sleep เพื่อช่วยการยืนยันโพซิชัน

        for i in range(confirmation_retries):
            logger.info(f"⏳ รอการยืนยันโพซิชัน ({i+1}/{confirmation_retries})...")
            time.sleep(confirmation_sleep)
            confirmed_pos_info = get_current_position()
            # ตรวจสอบว่ายืนยันโพซิชันได้จริงหรือไม่
            # ขนาดโพซิชันที่ยืนยันได้ควรจะใกล้เคียงกับขนาดที่ส่งคำสั่ง
            # ใช้ tolerance เล็กน้อยเนื่องจากความคลาดเคลื่อนของ Exchange
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
        logger.error(f"❌ Error ใน open_market_order (General Error): {e}", exc_info=True)
        send_telegram(f"⛔️ Error: ไม่สามารถเปิดออเดอร์ตลาดได้ (General Error)\nรายละเอียด: {e}")
        return False, None

# ==============================================================================
# 11. ฟังก์ชันตั้งค่า TP/SL/กันทุน (TP/SL/BREAKEVER FUNCTIONS)
# ==============================================================================

def set_tpsl_for_position(direction: str, entry_price: float) -> bool:
    """ตั้งค่า Take Profit และ Stop Loss สำหรับโพซิชันที่เปิดอยู่"""
    global current_position_size

    if not current_position_size:
        logger.error("❌ ไม่สามารถตั้ง TP/SL ได้: ขนาดโพซิชันเป็น 0.")
        send_telegram("⛔️ Error: ไม่สามารถตั้ง TP/SL ได้ (ขนาดโพซิชันเป็น 0).")
        return False

    tp_price = 0.0
    sl_price = 0.0

    if direction == 'long':
        tp_price = entry_price + TP_VALUE_POINTS
        sl_price = entry_price - SL_VALUE_POINTS
    elif direction == 'short':
        tp_price = entry_price - TP_VALUE_POINTS
        sl_price = entry_price + SL_VALUE_POINTS
    
    tp_price = exchange.price_to_precision(SYMBOL, tp_price)
    sl_price = exchange.price_to_precision(SYMBOL, sl_price)

    try:
        # ถ้า direction เป็น long, ต้องการขาย (sell) ที่ TP และ SL
        # ถ้า direction เป็น short, ต้องการซื้อ (buy) ที่ TP และ SL
        tp_sl_side = 'sell' if direction == 'long' else 'buy'
        
        # --- Setting Take Profit ---
        tp_order = exchange.create_order(
            symbol=SYMBOL,
            type='take_profit_market', 
            side=tp_sl_side,
            amount=current_position_size,
            price=None, # Market order after trigger
            params={
                'triggerPrice': float(tp_price), 
                'reduceOnly': True,
                'tdMode': 'cross' # Cross-margin mode
            }
        )
        logger.info(f"✅ ส่งคำสั่ง Take Profit สำเร็จ: ID {tp_order.get('id', 'N/A')}, Trigger Price: {tp_price:.2f}")

        # --- Setting Stop Loss ---
        sl_order = exchange.create_order(
            symbol=SYMBOL,
            type='stop_loss_market', 
            side=tp_sl_side,
            amount=current_position_size,
            price=None, # Market order after trigger
            params={
                'triggerPrice': float(sl_price), 
                'reduceOnly': True,
                'tdMode': 'cross'
            }
        )
        logger.info(f"✅ ส่งคำสั่ง Stop Loss สำเร็จ: ID {sl_order.get('id', 'N/A')}, Trigger Price: {sl_price:.2f}")

        return True

    except ccxt.ArgumentsRequired as e:
        logger.error(f"❌ Error setting TP/SL: Arguments missing or incorrect for OKX. {e}", exc_info=True)
        send_telegram(f"⛔️ API Error (TP/SL Arguments): {e.args[0] if e.args else str(e)}")
        return False
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        logger.error(f"❌ Error setting TP/SL: API/Network issue. {e}", exc_info=True)
        send_telegram(f"⛔️ API Error (TP/SL): {e.args[0] if e.args else str(e)}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error setting TP/SL: {e}", exc_info=True)
        send_telegram(f"⛔️ Unexpected Error (TP/SL): {e}")
        return False


def move_sl_to_breakeven(direction: str, entry_price: float) -> bool:
    """เลื่อน Stop Loss ไปที่จุด Breakeven (หรือ +BE_SL_BUFFER_POINTS)"""
    global sl_moved, current_position_size

    if sl_moved:
        logger.info("ℹ️ SL ถูกเลื่อนไปที่กันทุนแล้ว ไม่จำเป็นต้องเลื่อนอีก.")
        return True

    if not current_position_size:
        logger.error("❌ ไม่สามารถเลื่อน SL ได้: ขนาดโพซิชันเป็น 0.")
        return False

    breakeven_sl_price = 0.0
    if direction == 'long':
        breakeven_sl_price = entry_price + BE_SL_BUFFER_POINTS
    elif direction == 'short':
        breakeven_sl_price = entry_price - BE_SL_BUFFER_POINTS
    
    breakeven_sl_price = exchange.price_to_precision(SYMBOL, breakeven_sl_price)

    try:
        # ขั้นตอนที่ 1: ยกเลิกคำสั่ง Stop Loss เก่าทั้งหมดสำหรับ Symbol นี้ (Algo Orders)
        logger.info("⏳ กำลังยกเลิกคำสั่ง Stop Loss เก่า...")
        
        # Fetch only 'conditional' (algo) orders which include TP/SL on OKX
        # Docs: https://www.okx.com/docs-v5/en/#trading-account-rest-api-get-algo-order-list
        open_algo_orders = exchange.fetch_open_orders(SYMBOL, params={'ordType': 'conditional'})
        
        sl_order_ids_to_cancel = []
        for order in open_algo_orders:
            # OKX 'algoOrderType' can be 'sl', 'tp', 'ts', 'conditional'
            # We are looking for the previous stop loss order
            if order.get('info', {}).get('algoOrdType') == 'sl' and order.get('status') == 'live':
                sl_order_ids_to_cancel.append(order['id'])
        
        if sl_order_ids_to_cancel:
            for sl_id in sl_order_ids_to_cancel:
                try:
                    exchange.cancel_order(sl_id, SYMBOL) # OKX cancel_order can take algo order ID
                    logger.info(f"✅ ยกเลิก SL Order ID {sl_id} สำเร็จ.")
                except Exception as cancel_e:
                    logger.warning(f"⚠️ ไม่สามารถยกเลิก SL Order ID {sl_id} ได้: {cancel_e}")
        else:
            logger.info("ℹ️ ไม่พบคำสั่ง Stop Loss เก่าที่ต้องยกเลิก.")

        time.sleep(1) # รอให้คำสั่งยกเลิกได้รับการประมวลผล

        # ขั้นตอนที่ 2: ตั้ง Stop Loss ใหม่ที่ราคา Breakeven
        new_sl_side = 'sell' if direction == 'long' else 'buy'
        new_sl_order = exchange.create_order(
            symbol=SYMBOL,
            type='stop_loss_market',
            side=new_sl_side,
            amount=current_position_size,
            price=None, # Market order after trigger
            params={
                'triggerPrice': float(breakeven_sl_price),
                'reduceOnly': True,
                'tdMode': 'cross'
            }
        )
        logger.info(f"✅ เลื่อน SL ไปที่กันทุนสำเร็จ: Trigger Price: {breakeven_sl_price:.2f}, ID: {new_sl_order.get('id', 'N/A')}")
        sl_moved = True # อัปเดตสถานะว่า SL ถูกเลื่อนแล้ว
        return True

    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        logger.error(f"❌ Error moving SL to breakeven: API/Network issue. {e}", exc_info=True)
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
    """ตรวจสอบสถานะโพซิชันปัจจุบันและจัดการ Stop Loss."""
    global current_position, sl_moved, entry_price, current_position_size
    global monthly_stats, last_ema_position_status

    logger.debug(f"🔄 กำลังตรวจสอบสถานะโพซิชัน: Pos_Info={pos_info}, Current_Price={current_price}")
    if not pos_info:
        if current_position: # หากก่อนหน้านี้มีโพซิชัน แต่ตอนนี้ไม่มีแล้ว แสดงว่าถูกปิด
            logger.info(f"ℹ️ โพซิชัน {current_position.upper()} ถูกปิดแล้ว.")

            closed_price = current_price
            pnl_usdt_actual = 0.0

            # พยายามคำนวณ PnL จากข้อมูล Entry Price ที่บันทึกไว้ในบอท
            if entry_price and current_position_size:
                if current_position == 'long':
                    pnl_usdt_actual = (closed_price - entry_price) * current_position_size
                else:
                    pnl_usdt_actual = (entry_price - closed_price) * current_position_size

            close_reason = "ปิดโดยไม่ทราบสาเหตุ"
            emoji = "❓"

            # การระบุสาเหตุการปิดออเดอร์จากราคา (ใช้ได้ดีเมื่อ TP/SL ถูกตั้งในบอท)
            # ถ้า TP/SL ถูกตั้งใน Exchange แล้ว Exchange อาจแจ้งเหตุผลใน Log ของตัวเอง
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
            sl_moved = False # <<-- สำคัญ: รีเซ็ตสถานะ SL
            last_ema_position_status = None # <<-- สำคัญ: รีเซ็ตสถานะ EMA เพื่อรอการครอสครั้งใหม่
            save_monthly_stats() # บันทึกสถานะที่รีเซ็ตแล้ว

        return

    current_position = pos_info['side']
    entry_price = pos_info['entry_price']
    unrealized_pnl = pos_info['unrealizedPnl']
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
# 13. ฟังก์ชันรายงานประจำเดือน (MONTHLY REPORT FUNCTIONS)
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
            
            # คำนวณเวลาสำหรับรอบถัดไป (ในเดือนถัดไป)
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
            time.sleep(60) # ถ้าเวลาติดลบหรือ 0 ให้รอ 1 นาทีเพื่อเลี่ยง loop ถี่เกินไป

# ==============================================================================
# 14. ฟังก์ชันเริ่มต้นบอท (BOT STARTUP FUNCTIONS)
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
# 15. ฟังก์ชันหลักของบอท (MAIN BOT LOGIC)
# ==============================================================================
def main():
    """ฟังก์ชันหลักที่รัน Bot."""
    global monthly_stats, last_ema_position_status

    try:
        load_monthly_stats()
        send_startup_message()

        monthly_thread = threading.Thread(target=monthly_report_scheduler, daemon=True)
        monthly_thread.start()
        logger.info("✅ Monthly Report Scheduler Thread Started.")

    except Exception as e:
        error_msg = f"⛔️ Error: ไม่สามารถเริ่มต้นบอทได้\nรายละเอียด: {e} | บอทจะลองเริ่มต้นใหม่ใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
        send_telegram(error_msg)
        logger.critical(f"❌ Startup error: {e}", exc_info=True)
        time.sleep(ERROR_RETRY_SLEEP_SECONDS)
        return

    logger.info("🚀 บอทเข้าสู่ Main Loop แล้วและพร้อมทำงาน...")
    while True:
        try:
            logger.info(f"🔄 เริ่มรอบ Main Loop ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) - กำลังดึงข้อมูลและตรวจสอบ.")
            
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

            ticker = None
            try:
                logger.info("📊 กำลังดึงราคาล่าสุด (Ticker)...")
                ticker = exchange.fetch_ticker(SYMBOL)
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

            # มอนิเตอร์โพซิชัน (รวมถึงการเลื่อน SL ไปกันทุน)
            monitor_position(current_pos_info, current_price)

            if not current_pos_info: # ถ้าไม่มีโพซิชันเปิดอยู่
                logger.info("🔍 ไม่มีโพซิชันเปิดอยู่. กำลังตรวจสอบสัญญาณ EMA Cross...")
                signal = check_ema_cross() # ตรวจสอบสัญญาณ EMA Cross

                if signal: # ถ้าพบสัญญาณ
                    logger.info(f"🌟 ตรวจพบสัญญาณ EMA Cross: {signal.upper()}")
                    logger.info(f"✨ สัญญาณ {signal.upper()} ที่เข้าเงื่อนไข. กำลังพยายามเปิดออเดอร์.")

                    market_order_success, confirmed_entry_price = open_market_order(signal, current_price)

                    if market_order_success and confirmed_entry_price:
                        set_tpsl_success = set_tpsl_for_position(signal, confirmed_entry_price)

                        if set_tpsl_success:
                            # รีเซ็ตสถานะ EMA เพื่อรอสัญญาณใหม่หลังเปิดออเดอร์
                            last_ema_position_status = None 
                            save_monthly_stats() # บันทึกสถานะที่รีเซ็ตแล้ว
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

