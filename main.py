import ccxt
import time
import requests
from datetime import datetime, timedelta
import logging
import threading
import json
import os

# ==============================================================================
# 1. ตั้งค่าพื้นฐาน (CONFIGURATION)
#    ⚠️ สำคัญมาก: โปรดตั้งค่า Environment Variables เหล่านี้บน GitHub/Railway
#    เช่น RAILWAY_API_KEY, RAILWAY_SECRET, RAILWAY_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
# ==============================================================================

# --- API Keys & Credentials (ดึงจาก Environment Variables เพื่อความปลอดภัย) ---
# ตั้งค่าค่าเริ่มต้นเป็น None หรือสตริงว่าง เพื่อให้บอทไม่รันหากไม่ได้ตั้งค่า ENV
API_KEY = os.getenv('RAILWAY_API_KEY', 'YOUR_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('RAILWAY_SECRET', 'YOUR_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('RAILWAY_PASSWORD', 'YOUR_PASSWORD_HERE_FOR_LOCAL_TESTING')

# --- Trade Parameters ---
SYMBOL = 'BTC/USDT:USDT'  # คู่เทรดที่ต้องการ (เช่น BTC/USDT:USDT สำหรับ Perpetual Swap)
TIMEFRAME = '15m'         # Timeframe ของแท่งเทียน (เช่น '1m', '5m', '15m', '1h')
LEVERAGE = 30             # อัตราทด (Leverage) ที่ต้องการใช้
TP_VALUE_POINTS = 501     # ระยะ TP (Take Profit) เป็นจุด (เช่น 500 จุดสำหรับ BTC)
SL_VALUE_POINTS = 999     # ระยะ SL (Stop Loss) เป็นจุด
BE_PROFIT_TRIGGER_POINTS = 350   # กำไรที่ต้องถึงก่อนเลื่อน SL เป็นกันทุน (เป็นจุด)
BE_SL_BUFFER_POINTS = 100        # Buffer สำหรับ SL กันทุน (เป็นจุด) เช่น เลื่อน SL ไปที่ Entry + 100 จุด
PORTFOLIO_PERCENT_TRADE = 0.8 # เปอร์เซ็นต์ของพอร์ตที่ใช้ในการเปิดออเดอร์ (0.8 = 80%)

# --- Telegram Notification Settings ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# --- Files & Paths ---
STATS_FILE = 'trading_stats.json' # ไฟล์สำหรับบันทึกสถิติการเทรด

# --- Bot Timing ---
MAIN_LOOP_SLEEP_SECONDS = 360 # 🚀 เวลาหน่วงระหว่างรอบการทำงานหลัก (6 นาที)
ERROR_RETRY_SLEEP_SECONDS = 60 # หน่วงเวลานานขึ้นเมื่อเกิดข้อผิดพลาดร้ายแรง/API Error
DAILY_REPORT_HOUR = 0        # ชั่วโมงที่ต้องการส่งรายงานประจำวัน (0-23)
DAILY_REPORT_MINUTE = 5      # นาทีที่ต้องการส่งรายงานประจำวัน (0-59)

# --- Tolerance สำหรับการระบุสาเหตุการปิดออเดอร์ ---
# หากราคาปิดอยู่ภายในช่วง +/- X% ของราคา TP/SL/BE ก็จะถือว่าเป็นสาเหตุนั้นๆ
TP_SL_BE_PRICE_TOLERANCE_PERCENT = 0.005 # 0.5% (เช่น 100 จุดของราคา 20,000 คือ 0.5%)

# ==============================================================================
# 2. การตั้งค่า Logging
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'), # บันทึก Log ลงไฟล์
        logging.StreamHandler()       # แสดง Log บน Console ด้วย
    ]
)
logger = logging.getLogger(__name__)

# ==============================================================================
# 3. ตัวแปรสถานะการเทรด (GLOBAL TRADE STATE VARIABLES)
#    (ไม่ควรเปลี่ยนค่าโดยตรง ยกเว้นผ่านฟังก์ชันที่กำหนด)
# ==============================================================================
current_position = None  # สถานะของโพซิชันปัจจุบัน: None, 'long', 'short'
entry_price = None       # ราคาเข้าของโพซิชันปัจจุบัน
sl_moved = False         # True หากมีการเลื่อน SL ไปแล้ว
# last_ema_cross_signal จะถูกเก็บใน daily_stats เพื่อการ Persistence
portfolio_balance = 0.0    # ยอดคงเหลือในพอร์ตปัจจุบัน
last_daily_report_date = None # วันที่ของรายงานประจำวันล่าสุดที่ส่งไป (เป็น datetime.date object)
initial_balance = 0.0      # ยอดเงินเริ่มต้นเมื่อบอทเริ่มทำงาน
current_position_size = 0.0 # ขนาดของโพซิชันปัจจุบันที่เปิดอยู่ (สำหรับคำนวณ PnL เมื่อปิด)

# ==============================================================================
# 4. โครงสร้างข้อมูลสถิติ (STATISTICS DATA STRUCTURE)
# ==============================================================================
daily_stats = {
    'date': None,       # วันที่ของสถิติ (YYYY-MM-DD) ที่กำลังรวบรวม
    'tp_count': 0,      # จำนวนครั้งที่ปิดด้วย TP
    'sl_count': 0,      # จำนวนครั้งที่ปิดด้วย SL
    'total_pnl': 0.0,   ### <<< แก้ไข: เปลี่ยนจาก 0 เป็น 0.0
    'trades': [],       # รายละเอียดการเทรดแต่ละครั้ง
    'last_report_date': None, # วันที่ (YYYY-MM-DD) ที่ส่งรายงานประจำวันล่าสุดไปแล้ว
    'last_ema_cross_signal': None # เก็บสัญญาณ EMA cross ล่าสุดที่ใช้เปิดออเดอร์ เพื่อป้องกันเปิดซ้ำ
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
        'enableRateLimit': True, # เปิดใช้งาน Rate Limit Protection
        'rateLimit': 1000,       # 🛡️ ตั้งค่าให้หน่วงอย่างน้อย 1 วินาทีต่อ Request (1000ms)
        'options': {'defaultType': 'swap'} # ตั้งค่าประเภทตลาดเริ่มต้นเป็น Swap (Futures)
    })
    # ตั้งค่าโหมด Sandbox (True สำหรับ Testnet, False สำหรับ Real)
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

def save_daily_stats():
    """บันทึกสถิติการเทรดประจำวันลงในไฟล์ JSON."""
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump(daily_stats, f, indent=4)
        logger.debug(f"💾 บันทึกสถิติการเทรดลงไฟล์ {STATS_FILE} สำเร็จ")
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการบันทึกสถิติ: {e}")

def load_daily_stats():
    """โหลดสถิติการเทรดประจำวันจากไฟล์ JSON."""
    global daily_stats, last_daily_report_date
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, 'r') as f:
                loaded_stats = json.load(f)

                # ตรวจสอบและอัปเดตโครงสร้างหากมีการเปลี่ยนแปลงในอนาคต
                daily_stats['date'] = loaded_stats.get('date', None)
                daily_stats['tp_count'] = loaded_stats.get('tp_count', 0)
                daily_stats['sl_count'] = loaded_stats.get('sl_count', 0)
                daily_stats['total_pnl'] = loaded_stats.get('total_pnl', 0.0) ### <<< แก้ไข
                daily_stats['trades'] = loaded_stats.get('trades', [])
                daily_stats['last_report_date'] = loaded_stats.get('last_report_date', None)
                daily_stats['last_ema_cross_signal'] = loaded_stats.get('last_ema_cross_signal', None)

            logger.info(f"💾 โหลดสถิติการเทรดจากไฟล์ {STATS_FILE} สำเร็จ")

            # อัปเดต global variable last_daily_report_date
            if daily_stats['last_report_date']:
                try:
                    last_daily_report_date = datetime.strptime(daily_stats['last_report_date'], '%Y-%m-%d').date()
                except ValueError:
                    logger.warning("⚠️ รูปแบบวันที่ last_report_date ในไฟล์ไม่ถูกต้อง. จะถือว่ายังไม่มีการส่งรายงาน.")
                    last_daily_report_date = None
            else:
                last_daily_report_date = None

            # ตรวจสอบว่าสถิติที่โหลดมาเป็นของวันปัจจุบันหรือไม่
            current_date_str = datetime.now().strftime('%Y-%m-%d')
            if daily_stats['date'] != current_date_str:
                logger.info(f"ℹ️ สถิติที่โหลดมาเป็นของวันที่ {daily_stats['date']} ไม่ตรงกับวันนี้ {current_date_str}. จะรีเซ็ตสถิติสำหรับวันใหม่.")
                reset_daily_stats() # รีเซ็ตหากเป็นวันใหม่

        else:
            logger.info(f"🆕 ไม่พบไฟล์สถิติ {STATS_FILE} สร้างไฟล์ใหม่")
            reset_daily_stats() # สร้างไฟล์ใหม่พร้อมรีเซ็ตสถิติสำหรับวันนี้

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการโหลดสถิติ: {e}")
        # หากโหลดไม่ได้ ให้เริ่มต้นด้วยค่าว่างและรีเซ็ต
        daily_stats = {
            'date': None, 'tp_count': 0, 'sl_count': 0, 'total_pnl': 0.0, 'trades': [], ### <<< แก้ไข
            'last_report_date': None, 'last_ema_cross_signal': None
        }
        last_daily_report_date = None
        reset_daily_stats() # รีเซ็ตเพื่อให้แน่ใจว่าเริ่มต้นใหม่

def reset_daily_stats():
    """รีเซ็ตสถิติประจำวันสำหรับวันใหม่."""
    global daily_stats
    daily_stats['date'] = datetime.now().strftime('%Y-%m-%d')
    daily_stats['tp_count'] = 0
    daily_stats['sl_count'] = 0
    daily_stats['total_pnl'] = 0.0 ### <<< แก้ไข: เปลี่ยนจาก 0 เป็น 0.0
    daily_stats['trades'] = []
    # 'last_report_date' และ 'last_ema_cross_signal' ไม่ควรรีเซ็ตที่นี่
    save_daily_stats()
    logger.info(f"🔄 รีเซ็ตสถิติประจำวันสำหรับวันที่ {daily_stats['date']}")

def add_trade_result(reason: str, pnl: float):
    """เพิ่มผลการเทรดลงในสถิติประจำวัน."""
    global daily_stats
    current_date_str = datetime.now().strftime('%Y-%m-%d')

    if daily_stats['date'] != current_date_str:
        logger.info(f"🆕 วันที่เปลี่ยนใน add_trade_result: {daily_stats['date']} -> {current_date_str}. กำลังรีเซ็ตสถิติประจำวัน.")
        reset_daily_stats()

    if reason.upper() == 'TP':
        daily_stats['tp_count'] += 1
    elif reason.upper() == 'SL' or reason.upper() == 'SL (กันทุน)':
        daily_stats['sl_count'] += 1

    daily_stats['total_pnl'] += pnl

    daily_stats['trades'].append({
        'time': datetime.now().strftime('%H:%M:%S'),
        'reason': reason,
        'pnl': pnl
    })
    save_daily_stats()

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
    """ตรวจสอบการตัดกันของ EMA50 และ EMA200 โดยรอให้แท่งเทียนปิดก่อน."""
    try:
        retries = 3
        ohlcv = None
        for i in range(retries):
            try:
                # เราดึงข้อมูลมา 251 แท่ง เพื่อให้แน่ใจว่าเมื่อตัดแท่งล่าสุดออกไปแล้ว ยังมีข้อมูลพอ
                ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=251)
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

        # =================================================================
        # === จุดสำคัญของการแก้ไขคือตรงนี้ ===
        # เราจะตัดแท่งเทียนล่าสุด (ที่ยังไม่ปิด) ทิ้งไป ใช้เฉพาะแท่งที่ปิดแล้ว
        closed_ohlcv = ohlcv[:-1]
        logger.debug(f"Using {len(closed_ohlcv)} closed candles for EMA calculation.")
        # =================================================================

        if len(closed_ohlcv) < 202:
            logger.warning(f"ข้อมูล OHLCV ที่ปิดแล้วไม่เพียงพอ. ต้องการอย่างน้อย 202 แท่ง ได้ {len(closed_ohlcv)}")
            return None

        # ใช้ราคาปิดของแท่งที่ปิดแล้วเท่านั้นในการคำนวณ
        closes = [candle[4] for candle in closed_ohlcv]

        # EMA ของ "แท่งล่าสุดที่เพิ่งปิดไป"
        ema50_current = calculate_ema(closes, 50)
        ema200_current = calculate_ema(closes, 200)

        # EMA ของ "แท่งก่อนหน้าแท่งล่าสุด"
        ema50_prev = calculate_ema(closes[:-1], 50)
        ema200_prev = calculate_ema(closes[:-1], 200)

        if None in [ema50_prev, ema200_prev, ema50_current, ema200_current]:
            logger.warning("ค่า EMA ไม่สามารถคำนวณได้ (เป็น None).")
            return None

        cross_signal = None

        # เงื่อนไขการตัดกันจะเหมือนเดิม แต่ข้อมูลที่ใช้คำนวณจะมาจากแท่งที่ปิดแล้วเท่านั้น
        if ema50_prev <= ema200_prev and ema50_current > ema200_current:
            cross_signal = 'long'
            logger.info(f"🚀 Confirmed Golden Cross: EMA50({ema50_current:.2f}) > EMA200({ema200_current:.2f}) on closed candle.")

        elif ema50_prev >= ema200_prev and ema50_current < ema200_current:
            cross_signal = 'short'
            logger.info(f"🔻 Confirmed Death Cross: EMA50({ema50_current:.2f}) < EMA200({ema200_current:.2f}) on closed candle.")

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
            return False, None

        use_balance = balance * PORTFOLIO_PERCENT_TRADE

        market = exchange.market(SYMBOL)

        order_size_in_btc = (use_balance * LEVERAGE) / current_price

        min_amount = market.get('limits', {}).get('amount', {}).get('min', 0)
        if min_amount and order_size_in_btc < min_amount:
            logger.warning(f"⚠️ ขนาดออเดอร์ที่คำนวณได้ ({order_size_in_btc:.6f} BTC) ต่ำกว่าขั้นต่ำ ({min_amount:.6f} BTC). ปรับขนาดเป็นขั้นต่ำ.")
            order_size_in_btc = min_amount

        order_size_in_btc = float(exchange.amount_to_precision(SYMBOL, order_size_in_btc))

        if order_size_in_btc <= 0:
            send_telegram("⛔️ Error: ขนาดออเดอร์คำนวณได้เป็นศูนย์หรือติดลบหลังปรับ precision.")
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
        confirmation_retries = 10
        confirmation_sleep = 3

        for i in range(confirmation_retries):
            logger.info(f"⏳ รอการยืนยันโพซิชัน ({i+1}/{confirmation_retries})...")
            time.sleep(confirmation_sleep)
            confirmed_pos_info = get_current_position()
            if confirmed_pos_info and confirmed_pos_info['side'] == direction:
                logger.info(f"✅ ยืนยันโพซิชัน Entry Price: {confirmed_pos_info['entry_price']:.2f}")
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

def set_tpsl_for_position(pos_direction: str, entry_price_val: float) -> bool:
    """ตั้งค่า Take Profit และ Stop Loss สำหรับโพซิชันที่เปิดอยู่."""
    try:
        if pos_direction == 'long':
            tp_price = entry_price_val + TP_VALUE_POINTS
            sl_price = entry_price_val - SL_VALUE_POINTS
        else: # short
            tp_price = entry_price_val - TP_VALUE_POINTS
            sl_price = entry_price_val + SL_VALUE_POINTS

        tp_price = float(exchange.price_to_precision(SYMBOL, tp_price))
        sl_price = float(exchange.price_to_precision(SYMBOL, sl_price))

        params = {
            'instId': exchange.market(SYMBOL)['id'],
            'posSide': 'net',
            'mgnMode': 'cross',
            'tpTriggerPx': str(tp_price),
            'tpOrdPx': '-1',
            'slTriggerPx': str(sl_price),
            'slOrdPx': '-1',
        }

        set_tpsl_success = False
        for i in range(3):
            try:
                logger.info(f"⚙️ กำลังตั้งค่า TP/SL ({pos_direction.upper()}) SL:{sl_price:,.1f} TP:{tp_price:,.1f}... (Attempt {i+1}/3)")
                response = exchange.private_post_trade_order_tpsl(params)
                time.sleep(2)
                if response and response.get('code') == '0':
                    set_tpsl_success = True
                    logger.info("✅ ตั้งค่า TP/SL สำเร็จ.")
                    break
                else:
                    error_detail = response.get('msg', 'No message')
                    logger.warning(f"⚠️ OKX response for set_tpsl_order was not successful: {error_detail}. Retrying...")
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                logger.warning(f"⚠️ Error setting TP/SL (Attempt {i+1}/3): {e}. Retrying in 15 seconds...")
                if i == 2:
                    send_telegram(f"⛔️ API Error: ไม่สามารถตั้งค่า TP/SL ได้ (Attempt {i+1}/3)\nรายละเอียด: {e}")
                time.sleep(15)
            except Exception as e:
                logger.error(f"❌ Unexpected error setting TP/SL: {e}", exc_info=True)
                send_telegram(f"⛔️ Unexpected Error: ไม่สามารถตั้งค่า TP/SL ได้\nรายละเอียด: {e}")
                return False

        if not set_tpsl_success:
            logger.error("❌ Failed to set TP/SL after 3 attempts.")
            send_telegram("⛔️ API Error: ล้มเหลวในการตั้งค่า TP/SL หลังจาก 3 ครั้ง.")
            return False

        message = f"""{('📈' if pos_direction == 'long' else '📉')} ✅ <b>เปิด {pos_direction.upper()} & ตั้ง TP/SL</b>
<b>Entry:</b> <code>{entry_price_val:,.1f}</code>
<b>TP:</b> <code>{tp_price:,.1f}</code>
<b>SL:</b> <code>{sl_price:,.1f}</code>
"""
        send_telegram(message)
        return True

    except Exception as e:
        logger.error(f"❌ Error in set_tpsl_for_position: {e}", exc_info=True)
        send_telegram(f"⛔️ Error: เกิดข้อผิดพลาดในการตั้ง TP/SL\nรายละเอียด: {e}")
        return False

def move_sl_to_breakeven(pos_direction: str, entry_price_val: float):
    """เลื่อน Stop Loss ของโพซิชันที่เปิดอยู่ไปที่ราคาเข้า (Break-even)."""
    global sl_moved

    if sl_moved:
        return

    try:
        if pos_direction == 'long':
            new_sl_price = entry_price_val + BE_SL_BUFFER_POINTS
        else: # short
            new_sl_price = entry_price_val - BE_SL_BUFFER_POINTS

        new_sl_price = float(exchange.price_to_precision(SYMBOL, new_sl_price))

        params = {
            'instId': exchange.market(SYMBOL)['id'],
            'posSide': 'net',
            'mgnMode': 'cross',
            'slTriggerPx': str(new_sl_price),
            'slOrdPx': '-1',
        }

        set_sl_success = False
        for i in range(3):
            try:
                logger.info(f"⚙️ กำลังเลื่อน SL ({pos_direction.upper()}) ไปที่กันทุน: {new_sl_price:,.1f}... (Attempt {i+1}/3)")
                response = exchange.private_post_trade_order_tpsl(params)
                time.sleep(2)
                if response and response.get('code') == '0':
                    set_sl_success = True
                    logger.info("✅ เลื่อน SL ไปที่กันทุนสำเร็จ.")
                    break
                else:
                    error_detail = response.get('msg', 'No message')
                    logger.warning(f"⚠️ OKX response for move_sl_to_breakeven was not successful: {error_detail}. Retrying...")
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                logger.warning(f"⚠️ Error moving SL (Attempt {i+1}/3): {e}. Retrying in 15 seconds...")
                if i == 2:
                    send_telegram(f"⛔️ API Error: ไม่สามารถเลื่อน SL ได้ (Attempt {i+1}/3)\nรายละเอียด: {e}")
                time.sleep(15)
            except Exception as e:
                logger.error(f"❌ Unexpected error moving SL: {e}", exc_info=True)
                send_telegram(f"⛔️ Unexpected Error: ไม่สามารถเลื่อน SL ได้\nรายละเอียด: {e}")
                return

        if not set_sl_success:
            logger.error("❌ Failed to move SL after 3 attempts.")
            send_telegram("⛔️ API Error: ล้มเหลวในการเลื่อน SL หลังจาก 3 ครั้ง.")
            return

        sl_moved = True
        message = f"""🔄 <b>ราคาวิ่ง +{BE_PROFIT_TRIGGER_POINTS} จุดแล้ว → เลื่อน SL ไปที่ราคาเข้า (Break-even)</b>
<b>📍 Entry:</b> <code>{entry_price_val:,.1f}</code>
<b>🛡️ SL ใหม่:</b> <code>{new_sl_price:,.1f}</code>"""

        send_telegram(message)
        logger.info(f"✅ SL เลื่อนไปที่กันทุน: {new_sl_price:.2f}")

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการเลื่อน SL: {e}", exc_info=True)
        send_telegram(f"⛔️ Error: ไม่สามารถเลื่อน SL ได้\nรายละเอียด: {e}")

# ==============================================================================
# 11. ฟังก์ชันตรวจสอบสถานะ (MONITORING FUNCTIONS)
# ==============================================================================

def monitor_position(pos_info: dict | None, current_price: float):
    """ตรวจสอบสถานะโพซิชันปัจจุบันและจัดการ Stop Loss."""
    global current_position, sl_moved, entry_price, current_position_size
    global daily_stats

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
            daily_stats['last_ema_cross_signal'] = None
            save_daily_stats()

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
# 12. ฟังก์ชันรายงานประจำวัน (DAILY REPORT FUNCTIONS)
# ==============================================================================
def daily_report():
    """ส่งรายงานสถิติการเทรดประจำวันไปยัง Telegram."""
    global last_daily_report_date, daily_stats

    now = datetime.now()
    today_date = now.date()

    if last_daily_report_date == today_date:
        logger.debug(f"ℹ️ รายงานประจำวันสำหรับ {today_date} ถูกส่งไปแล้ว.")
        return

    try:
        balance = get_portfolio_balance()

        if daily_stats['date'] != today_date.strftime('%Y-%m-%d'):
            logger.info(f"🆕 สถิติประจำวันที่ใช้ไม่ตรงกับวันนี้ ({daily_stats['date']} vs {today_date.strftime('%Y-%m-%d')}). กำลังรีเซ็ตสถิติเพื่อรายงานวันนี้.")
            reset_daily_stats()

        tp_count = daily_stats['tp_count']
        sl_count = daily_stats['sl_count']
        total_pnl = daily_stats['total_pnl']
        pnl_from_start = balance - initial_balance if initial_balance > 0 else 0.0

        ### <<< แก้ไข: ปรับปรุง format string ให้ถูกต้อง
        message = f"""📊 <b>รายงานประจำวัน - {now.strftime('%d/%m/%Y')}</b>
<b>🔹 กำไรสุทธิวันนี้:</b> <code>{total_pnl:+,.2f} USDT</code>
<b>🔹 SL:</b> <code>{sl_count} ครั้ง</code>
<b>🔹 TP:</b> <code>{tp_count} ครั้ง</code>
<b>🔹 คงเหลือปัจจุบัน:</b> <code>{balance:,.2f} USDT</code>
<b>🔹 กำไร/ขาดทุนรวมจากยอดเริ่มต้น:</b> <code>{pnl_from_start:+,.2f} USDT</code>
<b>⏱ บอทยังทำงานปกติ</b> ✅
<b>เวลา:</b> <code>{now.strftime('%H:%M')}</code>"""

        send_telegram(message)
        last_daily_report_date = today_date
        daily_stats['last_report_date'] = today_date.strftime('%Y-%m-%d')
        save_daily_stats()
        logger.info("✅ ส่งรายงานประจำวันแล้ว.")

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการส่งรายงานประจำวัน: {e}", exc_info=True)
        send_telegram(f"⛔️ Error: ไม่สามารถส่งรายงานประจำวันได้\nรายละเอียด: {e}")

def daily_report_scheduler():
    """ตั้งเวลาสำหรับส่งรายงานประจำวัน."""
    logger.info("⏰ เริ่ม Daily Report Scheduler.")
    while True:
        now = datetime.now()
        next_report_time = now.replace(hour=DAILY_REPORT_HOUR, minute=DAILY_REPORT_MINUTE, second=0, microsecond=0)

        if now >= next_report_time:
            if last_daily_report_date is None or last_daily_report_date < now.date():
                 logger.info(f"⏰ ตรวจพบว่าถึงเวลาส่งรายงานประจำวัน ({now.strftime('%H:%M')}) และยังไม่ได้ส่งสำหรับวันนี้. กำลังส่ง...")
                 daily_report()
            next_report_time += timedelta(days=1)

        time_to_wait = (next_report_time - datetime.now()).total_seconds()
        if time_to_wait > 0:
            logger.info(f"⏰ กำหนดส่งรายงานประจำวันถัดไปในอีก {int(time_to_wait / 3600)} ชั่วโมง {int((time_to_wait % 3600) / 60)} นาที.")
            time.sleep(max(time_to_wait, 60))
        else:
            # กรณีที่คำนวณแล้วเวลาติดลบ (ไม่ควรเกิด แต่นี่เป็น safety net)
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
    global daily_stats

    try:
        load_daily_stats()
        send_startup_message()

        daily_thread = threading.Thread(target=daily_report_scheduler, daemon=True)
        daily_thread.start()

    except Exception as e:
        error_msg = f"⛔️ Error: ไม่สามารถเริ่มต้นบอทได้\nรายละเอียด: {e} | บอทจะลองเริ่มต้นใหม่ใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
        send_telegram(error_msg)
        logger.critical(f"❌ Startup error: {e}", exc_info=True)
        time.sleep(ERROR_RETRY_SLEEP_SECONDS)
        return

    logger.info("🚀 บอทเข้าสู่ Main Loop แล้ว...")
    while True:
        try:
            current_pos_info = get_current_position()

            ticker = None
            try:
                ticker = exchange.fetch_ticker(SYMBOL)
                time.sleep(2)
            except Exception as e:
                logger.warning(f"⚠️ Error fetching ticker: {e}. Retrying...")
                time.sleep(15)
                continue

            if not ticker or 'last' not in ticker:
                logger.error("❌ Failed to fetch valid ticker. Skipping loop.")
                time.sleep(MAIN_LOOP_SLEEP_SECONDS)
                continue

            current_price = float(ticker['last'])

            monitor_position(current_pos_info, current_price)

            if not current_pos_info:
                signal = check_ema_cross()

                if signal:
                    logger.info(f"🌟 ตรวจพบสัญญาณ EMA Cross: {signal.upper()}")

                    if signal != daily_stats.get('last_ema_cross_signal'):
                        logger.info(f"✨ สัญญาณ {signal.upper()} ใหม่ที่ถูกต้อง. กำลังพยายามเปิดออเดอร์.")

                        market_order_success, confirmed_entry_price = open_market_order(signal, current_price)

                        if market_order_success and confirmed_entry_price:
                            set_tpsl_success = set_tpsl_for_position(signal, confirmed_entry_price)

                            if set_tpsl_success:
                                daily_stats['last_ema_cross_signal'] = signal
                                save_daily_stats()
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
            error_msg = f"⛔️ Error: เกิดข้อผิดพลาดใน Main Loop\nรายละเอียด: {e} | Retry อีกครั้งใน {ERROR_RETRY_SLEEP_SECONDS} วินาที."
            logger.error(error_msg, exc_info=True)
            send_telegram(error_msg)
            time.sleep(ERROR_RETRY_SLEEP_SECONDS)

# ==============================================================================
# 15. จุดเริ่มต้นการทำงานของโปรแกรม (ENTRY POINT)
# ==============================================================================
if __name__ == '__main__':
    main()

