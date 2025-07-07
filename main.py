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
import math # เพิ่ม math

# ==============================================================================
# 1. ตั้งค่าพื้นฐาน (CONFIGURATION)
# ==============================================================================

# --- API Keys & Credentials (ดึงจาก Environment Variables เพื่อความปลอดภัย) ---
# ตรวจสอบให้แน่ใจว่าได้ตั้งค่าใน Environment Variables: BINANCE_API_KEY, BINANCE_SECRET
API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('BINANCE_SECRET', 'YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING')

# --- Trade Parameters ---
SYMBOL = 'BTC/USDT' # ใช้รูปแบบ Binance Futures
TIMEFRAME = '15m'
LEVERAGE = 30
TP_DISTANCE_POINTS = 501  # ระยะ TP เป็นจุด (จากราคาเข้า)
SL_DISTANCE_POINTS = 999  # ระยะ SL เป็นจุด (จากราคาเข้า)
BE_PROFIT_TRIGGER_POINTS = 350 # กำไรกี่จุดถึงจะเลื่อน SL ไปกันทุน
BE_SL_BUFFER_POINTS = 80   # เลื่อน SL ไปเหนือ/ใต้ราคาเข้ากี่จุด (สำหรับกันทุน)
CROSS_THRESHOLD_POINTS = 5 # จำนวนจุดที่ EMA ต้องห่างกันเพื่อยืนยันสัญญาณ

# เพิ่มค่าตั้งค่าใหม่สำหรับการบริหารความเสี่ยงและออเดอร์
MARGIN_BUFFER_USDT = 5 # buffer สำหรับ margin (USDT) เพื่อเผื่อค่าธรรมเนียม
TARGET_POSITION_SIZE_FACTOR = 0.8 # ใช้ 95% ของขนาดสูงสุดที่เปิดได้จาก margin

# ค่าสำหรับยืนยันโพซิชันหลังเปิดออเดอร์ (ใช้ใน confirm_position_entry)
CONFIRMATION_RETRIES = 15  # จำนวนครั้งที่ลองยืนยันโพซิชัน
CONFIRMATION_SLEEP = 3  # วินาทีที่รอระหว่างการยืนยัน

# --- Telegram Notification Settings ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# --- Files & Paths ---
# ควรเปลี่ยนเป็น '/data/trading_stats.json' หากใช้ Railway Volume ใน production
STATS_FILE = 'trading_stats.json' 

# --- Bot Timing ---
MAIN_LOOP_SLEEP_SECONDS = 300 # 3 นาที
ERROR_RETRY_SLEEP_SECONDS = 60
MONTHLY_REPORT_DAY = 20
MONTHLY_REPORT_HOUR = 0
MONTHLY_REPORT_MINUTE = 5

# --- Tolerance สำหรับการระบุสาเหตุการปิดออเดอร์ ---
TP_SL_BE_PRICE_TOLERANCE_PERCENT = 0.005 # 0.5% ของราคา entry เพื่อระบุว่าปิดด้วย TP/SL/BE

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
current_position_details = None # เปลี่ยนชื่อเป็น current_position_details เพื่อเก็บ dict
entry_price = None
sl_moved = False
portfolio_balance = 0.0
last_monthly_report_date = None
initial_balance = 0.0
current_position_size = 0.0 # ขนาดโพซิชันในหน่วย Contracts
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
    'last_ema_cross_signal': None, 
    'last_ema_position_status': None # ใช้สำหรับเก็บสถานะ EMA เพื่อการตรวจจับ Cross
}

# ==============================================================================
# 5. การตั้งค่า Exchange (CCXT EXCHANGE SETUP)
# ==============================================================================
exchange = None # กำหนดให้เป็น None ก่อน
market_info = None # เก็บข้อมูลตลาดของ symbol นี้

def setup_exchange():
    global exchange, market_info
    try:
        if not API_KEY or API_KEY == 'YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING' or \
           not SECRET or SECRET == 'YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING':
            raise ValueError("API_KEY หรือ SECRET ไม่ถูกตั้งค่าใน Environment Variables.")

        exchange = ccxt.binance({ # เปลี่ยนเป็น Binance
            'apiKey': API_KEY,
            'secret': SECRET,
            'sandbox': False,  # ⚠️ ตั้งค่าเป็น False สำหรับบัญชีจริง
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future', # ใช้ 'future' สำหรับ Binance Futures
                'marginMode': 'cross',   # ใช้ Cross Margin
            },
            'verbose': False, # ตั้งค่าเป็น True ถ้าต้องการเห็น Request/Response
            'timeout': 30000,
        })
        
        # โหลด markets ก่อนเสมอ
        exchange.load_markets()
        logger.info("✅ เชื่อมต่อกับ Binance Futures Exchange สำเร็จ และโหลด Markets แล้ว.")
        
        # ดึงข้อมูล market ของ SYMBOL ทันที
        market_info = exchange.market(SYMBOL)
        if not market_info:
            raise ValueError(f"ไม่พบข้อมูลตลาดสำหรับสัญลักษณ์ {SYMBOL}")

        # ตั้งค่า Leverage
        try:
            result = exchange.set_leverage(LEVERAGE, SYMBOL)
            logger.info(f"✅ ตั้งค่า Leverage เป็น {LEVERAGE}x สำหรับ {SYMBOL}: {result}")
        except ccxt.ExchangeError as e:
            if "leverage is not valid" in str(e) or "not valid for this symbol" in str(e):
                logger.critical(f"❌ Error: Leverage {LEVERAGE}x ไม่ถูกต้องสำหรับ {SYMBOL} บน Binance. โปรดตรวจสอบ Max Allowed Leverage.")
            else:
                logger.critical(f"❌ Error ในการตั้งค่า Leverage: {e}", exc_info=True)
            exit() # ออกจากโปรแกรมถ้าตั้งค่า Leverage ไม่ได้
        
    except ValueError as ve:
        logger.critical(f"❌ Configuration Error: {ve}", exc_info=True)
        exit()
    except Exception as e:
        logger.critical(f"❌ ไม่สามารถเชื่อมต่อหรือโหลดข้อมูล Exchange เบื้องต้นได้: {e}", exc_info=True)
        exit()

# ==============================================================================
# 6. ฟังก์ชันจัดการสถิติ (STATISTICS MANAGEMENT FUNCTIONS)
# ==============================================================================
# (ส่วนนี้ไม่เปลี่ยนแปลงจากโค้ดเดิม เพราะเป็น Logic ภายใน)

def save_monthly_stats():
    """บันทึกสถิติการเทรดประจำเดือนลงในไฟล์ JSON."""
    global monthly_stats, last_ema_position_status
    try:
        monthly_stats['last_ema_position_status'] = last_ema_position_status
        with open(STATS_FILE, 'w') as f:
            json.dump(monthly_stats, f, indent=4)
        logger.debug(f"💾 บันทึกสถิติการเทรดลงไฟล์ {STATS_FILE} สำเร็จ")
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการบันทิติสถิติ: {e}")

def reset_monthly_stats():
    """รีเซ็ตสถิติประจำเดือนสำหรับเดือนใหม่."""
    global monthly_stats, last_ema_position_status
    monthly_stats['month_year'] = datetime.now().strftime('%Y-%m')
    monthly_stats['tp_count'] = 0
    monthly_stats['sl_count'] = 0
    monthly_stats['total_pnl'] = 0.0
    monthly_stats['trades'] = []
    last_ema_position_status = None 
    save_monthly_stats() 
    logger.info(f"🔄 รีเซ็ตสถิติประจำเดือนสำหรับเดือน {monthly_stats['month_year']}")

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
        monthly_stats = {
            'month_year': None, 'tp_count': 0, 'sl_count': 0, 'total_pnl': 0.0, 'trades': [],
            'last_report_month_year': None, 'last_ema_cross_signal': None, 'last_ema_position_status': None
        }
        last_monthly_report_date = None
        last_ema_position_status = None
        reset_monthly_stats()

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
# (ส่วนนี้ไม่เปลี่ยนแปลงจากโค้ดเดิม เพราะเป็น Logic ภายใน)
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
    """ดึงยอดคงเหลือ USDT ในพอร์ต (เฉพาะ USDT free balance)."""
    global portfolio_balance
    retries = 3
    for i in range(retries):
        try:
            logger.debug(f"🔍 กำลังดึงยอดคงเหลือ (Attempt {i+1}/{retries})...")
            balance = exchange.fetch_balance()
            time.sleep(1) # เพิ่ม delay เพื่อไม่ให้ติด rate limit
            
            free_usdt = balance.get('USDT', {}).get('free', 0)
            if free_usdt == 0:
                # Binance balance structure can vary, try to get from info if 'free' is 0
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
    """ตรวจสอบและดึงข้อมูลโพซิชัน BTC/USDT ปัจจุบัน."""
    retries = 3
    for i in range(retries):
        try:
            logger.debug(f"🔍 กำลังดึงโพซิชันปัจจุบัน (Attempt {i+1}/{retries})...")
            # Binance Futures positions
            positions = exchange.fetch_positions([SYMBOL]) 
            time.sleep(1) # เพิ่ม delay
            
            for pos in positions:
                # Ensure the position is for the correct symbol and has non-zero contracts
                if pos['symbol'] == SYMBOL and float(pos['contracts']) != 0:
                    pos_amount = float(pos['contracts'])
                    # Binance futures 'side' can be 'long' or 'short' directly
                    return {
                        'side': 'long' if pos_amount > 0 else 'short',
                        'size': abs(pos_amount), # จะเป็นจำนวน Contracts
                        'entry_price': float(pos['entryPrice']),
                        'unrealized_pnl': float(pos['unrealizedPnl']),
                        'pos_id': pos.get('id', 'N/A') # Binance usually doesn't have a 'pos_id' like OKX
                    }
            return None # No active position for this symbol
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.warning(f"⚠️ Error fetching positions (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
            if i == retries - 1:
                send_telegram(f"⛔️ API Error: ไม่สามารถดึงโพซิชันได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
            time.sleep(15)
        except Exception as e:
            logger.error(f"❌ Unexpected error in get_current_position: {e}", exc_info=True)
            send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึงโพซิชันได้\nรายละเอียด: {e}")
            return None # Return None on unexpected errors too
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
                # Binance might return fewer candles than requested if not enough data
                ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=250) 
                time.sleep(1) # เพิ่ม delay
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

        # ต้องการอย่างน้อย 200 แท่ง + 1 สำหรับการคำนวณ EMA200 ล่าสุด
        if len(ohlcv) < 201: 
            logger.warning(f"ข้อมูล OHLCV ไม่เพียงพอ. ต้องการอย่างน้อย 201 แท่ง ได้ {len(ohlcv)}")
            send_telegram(f"⚠️ ข้อมูล OHLCV ไม่เพียงพอ ({len(ohlcv)} แท่ง).")
            return None

        closes = [candle[4] for candle in ohlcv]

        # คำนวณ EMA จากข้อมูล 'closes' ทั้งหมด
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
                save_monthly_stats()
                logger.info(f"ℹ️ บอทเพิ่งเริ่มรัน. บันทึกสถานะ EMA ปัจจุบันเป็น: {current_ema_position.upper()}. จะรอสัญญาณการตัดกันครั้งถัดไป.")
            return None

        cross_signal = None

        # Golden Cross (Long)
        if last_ema_position_status == 'below' and current_ema_position == 'above' and \
           ema50_current > (ema200_current + CROSS_THRESHOLD_POINTS):
            cross_signal = 'long'
            logger.info(f"🚀 Threshold Golden Cross: EMA50({ema50_current:.2f}) is {CROSS_THRESHOLD_POINTS} points above EMA200({ema200_current:.2f})")

        # Death Cross (Short)
        elif last_ema_position_status == 'above' and current_ema_position == 'below' and \
             ema50_current < (ema200_current - CROSS_THRESHOLD_POINTS):
            cross_signal = 'short'
            logger.info(f"🔻 Threshold Death Cross: EMA50({ema50_current:.2f}) is {CROSS_THRESHOLD_POINTS} points below EMA200({ema200_current:.2f})")

        # อัปเดตสถานะ EMA ล่าสุดเสมอ
        # อัปเดตเมื่อมีสัญญาณ หรือเมื่อไม่มีสัญญาณแต่สถานะ EMA เปลี่ยนแปลง
        if current_ema_position and (cross_signal is not None or current_ema_position != last_ema_position_status):
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
# 10. ฟังก์ชันช่วยสำหรับการคำนวณและตรวจสอบออเดอร์ (ปรับปรุงตามโค้ดแรก)
# ==============================================================================

def calculate_order_details(available_usdt: float, price: float) -> tuple[float, float]:
    """
    คำนวณจำนวนสัญญาที่จะเปิดและ Margin ที่ต้องใช้ โดยพิจารณาจาก Exchange Limits
    และ Margin Buffer จากโค้ดแรกที่ทำงานได้ดี.
    """
    if price <= 0 or LEVERAGE <= 0 or TARGET_POSITION_SIZE_FACTOR <= 0: 
        logger.error("Error: Price, leverage, and target_position_size_factor must be positive.")
        return (0, 0)

    if not market_info:
        logger.error(f"❌ Could not retrieve market info for {SYMBOL}.")
        return (0, 0)
    
    # Binance Futures uses 'amount' limits for contract quantity
    exchange_amount_step = market_info['limits']['amount']['step'] if 'amount' in market_info['limits'] and 'step' in market_info['limits']['amount'] and market_info['limits']['amount']['step'] is not None else 0.001
    
    # Binance futures typically has 'min' and 'max' for amount
    min_exchange_amount = market_info['limits']['amount']['min'] if 'amount' in market_info['limits'] and 'min' in market_info['limits']['amount'] and market_info['limits']['amount']['min'] is not None else 0.001
    max_exchange_amount = market_info['limits']['amount']['max'] if 'amount' in market_info['limits'] and 'max' in market_info['limits']['amount'] and market_info['limits']['amount']['max'] is not None else float('inf')

    # Binance futures also has 'min' and 'max' for notional (cost)
    min_notional_exchange = market_info['limits']['cost']['min'] if 'cost' in market_info['limits'] and 'min' in market_info['limits']['cost'] and market_info['limits']['cost']['min'] is not None else 5.0 # Default min notional for BTC/USDT is often 5 USDT
    max_notional_exchange = market_info['limits']['cost']['max'] if 'cost' in market_info['limits'] and 'max' in market_info['limits']['cost'] and market_info['limits']['cost']['max'] is not None else float('inf')


    # Calculate max notional based on available margin and leverage
    max_notional_from_available_margin = (available_usdt - MARGIN_BUFFER_USDT) * LEVERAGE
    if max_notional_from_available_margin <= 0:
        logger.warning(f"❌ Available margin ({available_usdt:.2f}) too low after buffer ({MARGIN_BUFFER_USDT}) for any notional value.")
        return (0, 0)

    target_notional_for_order = max_notional_from_available_margin * TARGET_POSITION_SIZE_FACTOR
    
    # Ensure target notional meets exchange's minimum notional
    min_notional_from_min_amount = min_exchange_amount * price
    target_notional_for_order = max(target_notional_for_order, min_notional_exchange, min_notional_from_min_amount)
    target_notional_for_order = min(target_notional_for_order, max_notional_exchange) # Cap at max notional

    contracts_raw = target_notional_for_order / price
    
    # Adjust contracts to step size and ensure within exchange amount limits
    contracts_to_open = round(contracts_raw / exchange_amount_step) * exchange_amount_step
    contracts_to_open = float(f"{contracts_to_open:.10f}") # Format to avoid floating point issues

    # Apply min/max amount limits
    contracts_to_open = max(contracts_to_open, min_exchange_amount)
    contracts_to_open = min(contracts_to_open, max_exchange_amount)

    actual_notional_after_precision = contracts_to_open * price
    required_margin = actual_notional_after_precision / LEVERAGE

    if contracts_to_open < min_exchange_amount:
        logger.error(f"❌ Calculated amount {contracts_to_open:.8f} is less than exchange's minimum amount {min_exchange_amount:.8f}. Cannot open (after final precision adjust).") 
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

    # Binance uses a 'precision' for amount, so we use a small tolerance
    size_tolerance = max(market_info['limits']['amount']['step'] * 2, expected_contracts * 0.001) 

    for attempt in range(CONFIRMATION_RETRIES):
        logger.info(f"⏳ ยืนยันโพซิชัน ({attempt + 1}/{CONFIRMATION_RETRIES})...")
        time.sleep(CONFIRMATION_SLEEP)
        
        try:
            position_info = get_current_position() # ดึงโพซิชันใหม่
            
            if position_info and position_info.get('side') == expected_direction:
                actual_size = position_info.get('size', 0.0)
                confirmed_entry_price = position_info.get('entry_price')
                
                # Check if actual_size is close to expected_contracts
                if abs(actual_size - expected_contracts) <= size_tolerance:
                    logger.info(f"✅ ยืนยันโพซิชันสำเร็จ:")
                    logger.info(f"   - Entry Price: {confirmed_entry_price:.2f}")
                    logger.info(f"   - Size: {actual_size:,.8f} Contracts") # แสดงทศนิยม
                    logger.info(f"   - Direction: {expected_direction.upper()}")
                    
                    current_position_size = actual_size
                    entry_price = confirmed_entry_price
                    current_position_details = position_info # เก็บรายละเอียดโพซิชัน
                    
                    # ส่งการแจ้งเตือน
                    profit_loss = position_info.get('unrealized_pnl', 0)
                    send_telegram(
                        f"🎯 เปิดโพซิชัน {expected_direction.upper()} สำเร็จ\n"
                        f"📊 ขนาด: {actual_size:,.8f} Contracts\n" # แสดงทศนิยม
                        f"💰 Entry: {confirmed_entry_price:.2f}\n"
                        f"📈 P&L: {profit_loss:,.2f} USDT"
                    )
                    
                    return True, confirmed_entry_price
                else:
                    logger.warning(f"⚠️ ขนาดโพซิชันไม่ตรงกัน (คาดหวัง: {expected_contracts:,.8f}, ได้: {actual_size:,.8f})")
            else:
                logger.warning(f"⚠️ ไม่พบโพซิชันที่ตรงกัน (คาดหวัง: {expected_direction})")
                
        except Exception as e:
            logger.warning(f"⚠️ Error ในการยืนยันโพซิชัน: {e}", exc_info=True)
            
    # ล้มเหลวในการยืนยัน
    logger.error(f"❌ ไม่สามารถยืนยันโพซิชันได้หลังจาก {CONFIRMATION_RETRIES} ครั้ง")
    send_telegram(
        f"⛔️ Position Confirmation Failed\n"
        f"🔍 กรุณาตรวจสอบโพซิชันใน Exchange ด่วน!\n"
        f"📊 คาดหวัง: {expected_direction.upper()} {expected_contracts:,.8f} Contracts" # แสดงทศนิยม
    )

    return False, None


# ==============================================================================
# 10. ฟังก์ชันจัดการคำสั่งซื้อขาย (ORDER MANAGEMENT FUNCTIONS)
# ==============================================================================
def open_market_order(direction: str, current_price: float) -> tuple[bool, float | None]:
    """
    เปิดออเดอร์ Market ด้วยจำนวนสัญญาที่คำนวณจาก calculate_order_details
    และคืนราคา Entry Price.
    """
    global current_position_size

    try:
        # 1. ดึงยอดคงเหลือ
        balance = get_portfolio_balance()
        if balance <= MARGIN_BUFFER_USDT:
            error_msg = f"ยอดคงเหลือ ({balance:,.2f} USDT) ต่ำเกินไป ไม่เพียงพอสำหรับ Margin Buffer ({MARGIN_BUFFER_USDT} USDT)."
            send_telegram(f"⛔️ Balance Error: {error_msg}")
            logger.error(f"❌ {error_msg}")
            return False, None

        # 2. คำนวณขนาดโพซิชันและ Margin ที่ต้องการ
        order_amount, estimated_used_margin = calculate_order_details(balance, current_price)
        
        if order_amount <= 0:
            error_msg = "❌ Calculated order amount is zero or insufficient. Cannot open position."
            send_telegram(f"⛔️ Order Calculation Error: {error_msg}")
            logger.error(f"❌ {error_msg}")
            return False, None
        
        # Determine number of decimal places for logging/display
        decimal_places = 0
        if market_info and 'limits' in market_info and 'amount' in market_info['limits'] and 'step' in market_info['limits']['amount']:
            step_size = market_info['limits']['amount']['step']
            if step_size < 1:
                decimal_places = int(round(-math.log10(step_size)))
            
        logger.info(f"ℹ️ Trading Summary:")
        logger.info(f"   - Balance: {balance:,.2f} USDT")
        logger.info(f"   - Contracts: {order_amount:,.{decimal_places}f}")
        logger.info(f"   - Required Margin (incl. buffer): {estimated_used_margin + MARGIN_BUFFER_USDT:,.2f} USDT")
        logger.info(f"   - Direction: {direction.upper()}")
        
        # 3. ส่งออเดอร์ Market
        side = 'buy' if direction == 'long' else 'sell'
        params = {} # Binance Futures doesn't need tdMode, mgnCcy in create_order

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
                    time.sleep(2) 
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
        
        # 4. ยืนยันโพซิชัน
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
        # Binance Futures TP/SL orders are usually 'STOP_MARKET', 'TAKE_PROFIT_MARKET' and have 'reduceOnly'
        open_orders = exchange.fetch_open_orders(SYMBOL)
        
        canceled_count = 0
        for order in open_orders:
            if order['status'] == 'open' or order['status'] == 'pending': 
                # Check for types typically used for TP/SL and reduceOnly
                if order['type'] in ['TAKE_PROFIT_MARKET', 'STOP_MARKET', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT'] or \
                   order.get('reduceOnly', False) == True: 
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


def set_tpsl_for_position(direction: str, entry_price: float) -> bool:
    """ตั้งค่า Take Profit และ Stop Loss สำหรับโพซิชันที่เปิดอยู่บน Binance Futures."""
    global current_position_size

    if not current_position_size:
        logger.error("❌ ไม่สามารถตั้ง TP/SL ได้: ขนาดโพซิชันเป็น 0.")
        send_telegram("⛔️ Error: ไม่สามารถตั้ง TP/SL ได้ (ขนาดโพซิชันเป็น 0).")
        return False

    # ยกเลิกคำสั่งเก่าก่อนเสมอ
    cancel_all_open_tp_sl_orders()
    time.sleep(1) # ให้เวลา Exchange ประมวลผล

    tp_price = 0.0
    sl_price = 0.0

    if direction == 'long':
        tp_price = entry_price + TP_DISTANCE_POINTS
        sl_price = entry_price - SL_DISTANCE_POINTS
    elif direction == 'short':
        tp_price = entry_price - TP_DISTANCE_POINTS
        sl_price = entry_price + SL_DISTANCE_POINTS
    
    # ปรับราคาให้ตรงกับ precision ของ Exchange
    tp_price = exchange.price_to_precision(SYMBOL, tp_price)
    sl_price = exchange.price_to_precision(SYMBOL, sl_price)

    logger.info(f"🎯 Calculated TP: {tp_price:.2f} | 🛑 Calculated SL: {sl_price:.2f}")

    try:
        tp_sl_side = 'sell' if direction == 'long' else 'buy'
        
        # --- Setting Take Profit (TAKE_PROFIT_MARKET on Binance Futures) ---
        logger.info(f"⏳ Setting Take Profit order at {tp_price}...")
        tp_order = exchange.create_order(
            symbol=SYMBOL,
            type='TAKE_PROFIT_MARKET', 
            side=tp_sl_side,
            amount=current_position_size, # Contracts quantity
            price=None, # Market order, no limit price
            params={
                'stopPrice': float(tp_price), # For Binance, use 'stopPrice' for trigger
                'reduceOnly': True, 
            }
        )
        logger.info(f"✅ Take Profit order placed: ID → {tp_order.get('id', 'N/A')}")

        # --- Setting Stop Loss (STOP_MARKET on Binance Futures) ---
        logger.info(f"⏳ Setting Stop Loss order at {sl_price}...")
        sl_order = exchange.create_order(
            symbol=SYMBOL,
            type='STOP_MARKET', 
            side=tp_sl_side,         
            amount=current_position_size, # Contracts quantity
            price=None,         
            params={
                'stopPrice': float(sl_price), # For Binance, use 'stopPrice' for trigger
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


def move_sl_to_breakeven(direction: str, entry_price: float) -> bool:
    """เลื่อน Stop Loss ไปที่จุด Breakeven (หรือ +BE_SL_BUFFER_POINTS) บน Binance Futures."""
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
        logger.info("⏳ กำลังยกเลิกคำสั่ง Stop Loss เก่า...")
        # ยกเลิกเฉพาะ STOP_MARKET ที่เป็น reduceOnly
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

        time.sleep(1) # ให้เวลา Exchange ประมวลผล

        new_sl_side = 'sell' if direction == 'long' else 'buy'
        
        logger.info(f"⏳ Setting new Stop Loss (Breakeven) order at {breakeven_sl_price}...")
        new_sl_order = exchange.create_order(
            symbol=SYMBOL,
            type='STOP_MARKET',
            side=new_sl_side,
            amount=current_position_size, # Contracts quantity
            price=None,
            params={
                'stopPrice': float(breakeven_sl_price),
                'reduceOnly': True,
            }
        )
        logger.info(f"✅ เลื่อน SL ไปที่กันทุนสำเร็จ: Trigger Price: {breakeven_sl_price:.2f}, ID: {new_sl_order.get('id', 'N/A')}")
        sl_moved = True
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
    """ตรวจสอบสถานะโพซิชันปัจจุบันและจัดการ Stop Loss."""
    global current_position_details, sl_moved, entry_price, current_position_size
    global monthly_stats, last_ema_position_status

    logger.debug(f"🔄 กำลังตรวจสอบสถานะโพซิชัน: Pos_Info={pos_info}, Current_Price={current_price}")
    
    # ถ้าไม่มีโพซิชันใน Exchange แต่บอทเคยบันทึกว่ามี
    if not pos_info and current_position_details:
        logger.info(f"ℹ️ โพซิชัน {current_position_details['side'].upper()} ถูกปิดแล้วใน Exchange.")

        closed_price = current_price
        pnl_usdt_actual = 0.0

        # PnL สำหรับ Futures คือ (ราคาปิด - ราคาเข้า) * จำนวนสัญญา
        if entry_price and current_position_size:
            if current_position_details['side'] == 'long':
                pnl_usdt_actual = (closed_price - entry_price) * current_position_size
            else: # short
                pnl_usdt_actual = (entry_price - closed_price) * current_position_size

        close_reason = "ปิดโดยไม่ทราบสาเหตุ"
        emoji = "❓"

        tp_sl_be_tolerance_points = entry_price * TP_SL_BE_PRICE_TOLERANCE_PERCENT if entry_price else 0

        # Determine close reason
        if current_position_details['side'] == 'long' and entry_price:
            if closed_price >= (entry_price + TP_DISTANCE_POINTS) - tp_sl_be_tolerance_points:
                close_reason = "TP"
                emoji = "✅"
            elif sl_moved and abs(closed_price - (entry_price + BE_SL_BUFFER_POINTS)) <= tp_sl_be_tolerance_points:
                 close_reason = "SL (กันทุน)"
                 emoji = "🛡️"
            elif closed_price <= (entry_price - SL_DISTANCE_POINTS) + tp_sl_be_tolerance_points:
                close_reason = "SL"
                emoji = "❌"
        elif current_position_details['side'] == 'short' and entry_price:
            if closed_price <= (entry_price - TP_DISTANCE_POINTS) + tp_sl_be_tolerance_points:
                close_reason = "TP"
                emoji = "✅"
            elif sl_moved and abs(closed_price - (entry_price - BE_SL_BUFFER_POINTS)) <= tp_sl_be_tolerance_points:
                 close_reason = "SL (กันทุน)"
                 emoji = "🛡️"
            elif closed_price >= (entry_price + SL_DISTANCE_POINTS) - tp_sl_be_tolerance_points:
                close_reason = "SL"
                emoji = "❌"
        
        # Log and send telegram for position close
        send_telegram(f"{emoji} <b>ปิดออเดอร์ด้วย {close_reason}</b>\n<b>PnL (ประมาณ):</b> <code>{pnl_usdt_actual:,.2f} USDT</code>")
        logger.info(f"✅ โพซิชันปิด: {close_reason}, PnL (ประมาณ): {pnl_usdt_actual:.2f}")
        add_trade_result(close_reason, pnl_usdt_actual) # Use 'close_reason' for consistency

        # Reset global state variables
        current_position_details = None
        entry_price = None
        current_position_size = 0.0
        sl_moved = False
        last_ema_position_status = None # Reset EMA status to re-evaluate on next signal
        save_monthly_stats()

        # Cancel any remaining TP/SL orders for this symbol, just in case
        cancel_all_open_tp_sl_orders() 

        return

    # ถ้ามีโพซิชันเปิดอยู่
    if pos_info:
        current_position_details = pos_info # อัปเดตรายละเอียดโพซิชัน
        entry_price = pos_info['entry_price']
        unrealized_pnl = pos_info['unrealized_pnl']
        current_position_size = pos_info['size']

        logger.info(f"📊 สถานะปัจจุบัน: {current_position_details['side'].upper()}, PnL: {unrealized_pnl:,.2f} USDT, ราคา: {current_price:,.1f}, เข้า: {entry_price:,.1f}, Size: {current_position_size:,.8f} Contracts")

        pnl_in_points = 0
        if current_position_details['side'] == 'long':
            pnl_in_points = current_price - entry_price
        elif current_position_details['side'] == 'short':
            pnl_in_points = entry_price - current_price

        # ตรวจสอบและเลื่อน SL ไปกันทุน
        if not sl_moved and pnl_in_points >= BE_PROFIT_TRIGGER_POINTS:
            logger.info(f"ℹ️ กำไรถึงจุดเลื่อน SL: {pnl_in_points:,.0f} จุด (PnL: {unrealized_pnl:,.2f} USDT)")
            move_sl_to_breakeven(current_position_details['side'], entry_price)

# ==============================================================================
# 13. ฟังก์ชันรายงานประจำเดือน (MONTHLY REPORT FUNCTIONS)
# ==============================================================================
# (ส่วนนี้ไม่เปลี่ยนแปลงจากโค้ดเดิม เพราะเป็น Logic ภายใน)
def monthly_report():
    """ส่งรายงานสถิติการเทรดประจำเดือนไปยัง Telegram."""
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
            
            # Calculate next report time for the next month
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
            time.sleep(max(time_to_wait, 60)) # Sleep at least 60 seconds
        else:
            time.sleep(60) # If somehow time_to_wait is negative or zero, wait 60 seconds to prevent tight loop


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
    """ฟังก์ชันหลักที่รัน Bot."""
    global current_position_details, last_ema_position_status

    try:
        setup_exchange() # เรียกใช้เพื่อเชื่อมต่อและตั้งค่า Binance
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
            
            # ดึงข้อมูลตลาดล่าสุดเพื่อให้แน่ใจว่าได้ราคาปัจจุบัน
            ticker = None
            try:
                logger.info("📊 กำลังดึงราคาล่าสุด (Ticker)...")
                ticker = exchange.fetch_ticker(SYMBOL)
                time.sleep(1) # เพิ่ม delay
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

            # ดึงสถานะโพซิชันปัจจุบัน
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
            
            # มอนิเตอร์โพซิชัน (รวมถึงการเลื่อน SL ไปกันทุนและจัดการการปิดโพซิชัน)
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
                            # last_ema_position_status = None # ไม่ต้องรีเซ็ตตรงนี้ เพราะ check_ema_cross จะอัปเดตเอง
                            # save_monthly_stats() # ไม่ต้อง save ตรงนี้ เพราะ check_ema_cross และ monitor_position จะ save เอง
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

