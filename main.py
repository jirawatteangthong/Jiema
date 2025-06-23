import ccxt
import os
import time
import datetime
import json
import logging
import pytz
import asyncio
from telegram import Bot
from telegram.error import TelegramError

# --- ตั้งค่า Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Environment Variables ---
try:
    API_KEY = os.getenv('RAILWAY_API_KEY')
    SECRET_KEY = os.getenv('RAILWAY_SECRET')
    PASSWORD = os.getenv('RAILWAY_PASSWORD') # OKX specific
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    
    if not all([API_KEY, SECRET_KEY, PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID]):
        raise ValueError("One or more environment variables are missing.")

except ValueError as e:
    logger.error(f"❌ Configuration Error: {e}. Please set all necessary environment variables.")
    exit(1) # Exit if essential environment variables are not set

# --- ตั้งค่า Bot ---
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET_KEY,
    'password': PASSWORD,
    'options': {
        'defaultType': 'swap', # สำหรับ Perpetual Futures
    },
    'enableRateLimit': True,
})

# ตั้งค่า Telegram Bot
bot = Bot(token=TELEGRAM_TOKEN)

# ฟังก์ชันช่วยส่งข้อความ Telegram (เพื่อให้เรียกใช้จากทั้ง async และ non-async context ได้)
async def _send_telegram_async(message: str):
    """ฟังก์ชันหลักสำหรับส่งข้อความ Telegram แบบ asynchronous."""
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML')
        logger.info(f"✉️ Telegram: {message.splitlines()[0]}...") # Log แค่บรรทัดแรกเพื่อความกระชับ
    except TelegramError as e:
        logger.error(f"❌ Telegram HTTP error: {e}. Check TELEGRAM_TOKEN or CHAT_ID.")
        logger.error(f"Error message: {message}")
    except Exception as e:
        logger.error(f"❌ Error sending Telegram message: {type(e).__name__}: {e}")

def send_telegram(message: str):
    """เรียกใช้ฟังก์ชันส่งข้อความ Telegram."""
    try:
        # ตรวจสอบว่ากำลังรันอยู่ใน Asyncio event loop หรือไม่
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # ถ้ามี loop รันอยู่แล้ว ให้สร้าง Task
            loop.create_task(_send_telegram_async(message))
        else:
            # ถ้าไม่มี loop รันอยู่ ให้รันด้วย asyncio.run()
            asyncio.run(_send_telegram_async(message))
    except Exception as e:
        logger.error(f"❌ Failed to send Telegram message due to event loop issue: {type(e).__name__}: {e}")


# ตรวจสอบการเชื่อมต่อ OKX
try:
    account_info = exchange.fetch_balance()
    logger.info("✅ เชื่อมต่อกับ OKX Exchange สำเร็จ")
    time.sleep(1) # เพิ่มหน่วงเวลา
except ccxt.NetworkError as e:
    logger.error(f"❌ Network Error connecting to OKX: {type(e).__name__}: {e}")
    send_telegram(f"⛔️ บอทหยุดทำงาน: Network Error ในการเชื่อมต่อ OKX\nรายละเอียด: {type(e).__name__}: {e}")
    exit(1)
except ccxt.ExchangeError as e:
    logger.error(f"❌ Exchange Error connecting to OKX: {type(e).__name__}: {e}")
    send_telegram(f"⛔️ บอทหยุดทำงาน: Exchange Error ในการเชื่อมต่อ OKX\nรายละเอียด: {type(e).__name__}: {e}")
    exit(1)
except Exception as e:
    logger.error(f"❌ Unexpected error connecting to OKX: {type(e).__name__}: {e}")
    send_telegram(f"⛔️ บอทหยุดทำงาน: เกิดข้อผิดพลาดที่ไม่คาดคิดในการเชื่อมต่อ OKX\nรายละเอียด: {type(e).__name__}: {e}")
    exit(1)

# --- Global Variables and Constants ---
SYMBOL = 'BTC/USDT:USDT' # สัญลักษณ์สำหรับ BTC-USDT Perpetual Swap
TIMEFRAME = '15m' # Timeframe สำหรับ EMA
INTERVAL = 15 # นาที สำหรับการรัน Loop
EMA_SHORT_PERIOD = 50
EMA_LONG_PERIOD = 200
LEVERAGE = 35 # ตั้งค่า Leverage
PORTFOLIO_PERCENT_TRADE = 0.80 # 80% ของเงินที่จะใช้ในการเทรด (เป็น Margin)

# ⚠️ ปรับ TP/SL เป็นจุด
TP_POINTS = 500 # Take Profit +500 จุดจากราคาเข้า
SL_POINTS = 1000 # Stop Loss -1000 จุดจากราคาเข้า

DAILY_REPORT_TIME = datetime.time(0, 5) # เวลาส่งรายงานประจำวัน (00:05 AM)

daily_stats_file = 'trading_stats.json'
daily_stats = {}
last_daily_report_date = None
current_position_size = 0 # เพื่อเก็บขนาดของโพซิชันที่เปิดอยู่

# --- Functions ---

def load_daily_stats():
    """โหลดสถิติการเทรดประจำวันจากไฟล์."""
    global daily_stats, last_daily_report_date
    if os.path.exists(daily_stats_file):
        try:
            with open(daily_stats_file, 'r') as f:
                data = json.load(f)
                daily_stats = data.get('daily_stats', {})
                last_report_date_str = data.get('last_daily_report_date')
                if last_report_date_str:
                    last_daily_report_date = datetime.datetime.strptime(last_report_date_str, '%Y-%m-%d').date()
                logger.info("✅ โหลดสถิติการเทรดประจำวันสำเร็จ.")
        except json.JSONDecodeError as e:
            logger.warning(f"⚠️ Error decoding JSON from {daily_stats_file}: {e}. Initializing new stats.")
            daily_stats = {}
            last_daily_report_date = None
        except Exception as e:
            logger.error(f"❌ Error loading daily stats: {type(e).__name__}: {e}. Initializing new stats.")
            daily_stats = {}
            last_daily_report_date = None
    else:
        logger.info("ℹ️ ไม่พบไฟล์สถิติ trading_stats.json. สร้างไฟล์ใหม่.")
        daily_stats = {}
        last_daily_report_date = None

def save_daily_stats():
    """บันทึกสถิติการเทรดประจำวันลงไฟล์."""
    try:
        data = {
            'daily_stats': daily_stats,
            'last_daily_report_date': last_daily_report_date.strftime('%Y-%m-%d') if last_daily_report_date else None
        }
        with open(daily_stats_file, 'w') as f:
            json.dump(data, f, indent=4)
        logger.info("✅ บันทึกสถิติการเทรดประจำวันสำเร็จ.")
    except Exception as e:
        logger.error(f"❌ Error saving daily stats: {type(e).__name__}: {e}")

def get_portfolio_balance() -> float:
    """
    ดึงยอดคงเหลือ USDT ในบัญชี Cross Margin (เพื่อใช้คำนวณเงินทุนสำหรับเทรด)
    """
    retries = 3
    for i in range(retries):
        try:
            balance = exchange.fetch_balance({'accountType': 'cross'}) # ดึง balance ของ Cross account
            time.sleep(2) # เพิ่มหน่วงเวลา
            usdt_balance = balance['USDT']['free'] if 'USDT' in balance and 'free' in balance['USDT'] else 0.0
            logger.info(f"💰 ยอดคงเหลือ USDT: {usdt_balance:.2f}")
            return usdt_balance
        except ccxt.NetworkError as e:
            logger.warning(f"⚠️ Network Error fetching balance (Attempt {i+1}/{retries}): {type(e).__name__}: {e}. Retrying in 15 seconds...")
            send_telegram(f"⛔️ API Error: ไม่สามารถดึงยอดคงเหลือได้ (Attempt {i+1}/{retries})\nรายละเอียด: {type(e).__name__}: {e}")
            time.sleep(15)
        except ccxt.ExchangeError as e:
            logger.warning(f"⚠️ Exchange Error fetching balance (Attempt {i+1}/{retries}): {type(e).__name__}: {e}. Retrying in 15 seconds...")
            send_telegram(f"⛔️ API Error: ไม่สามารถดึงยอดคงเหลือได้ (Attempt {i+1}/{retries})\nรายละเอียด: {type(e).__name__}: {e}")
            time.sleep(15)
        except Exception as e:
            logger.error(f"❌ Unexpected error in get_portfolio_balance: {type(e).__name__}: {e}")
            send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึงยอดคงเหลือได้\nรายละเอียด: {type(e).__name__}: {e}")
            return 0.0
    logger.error(f"❌ Failed to fetch portfolio balance after {retries} attempts.")
    send_telegram(f"⛔️ API Error: ล้มเหลวในการดึงยอดคงเหลือหลังจาก {retries} ครั้ง.")
    return 0.0

def get_current_position() -> dict | None:
    """
    ตรวจสอบและดึงข้อมูลโพซิชัน BTC/USDT ปัจจุบัน พร้อม retry.
    """
    retries = 3
    for i in range(retries):
        try:
            positions = exchange.fetch_positions([SYMBOL])
            time.sleep(2) # เพิ่มหน่วงเวลา
            for position in positions:
                # OKX returns posAmt as a string, convert to float
                if position['symbol'] == SYMBOL and float(position['info']['posAmt']) != 0: 
                    entry_price = float(position['info']['avgPx'])
                    pos_side = 'long' if float(position['info']['posAmt']) > 0 else 'short'
                    pos_size = float(position['info']['posAmt']) # ขนาดในหน่วย BTC (contract size)
                    
                    logger.info(f"📊 ตรวจพบโพซิชันปัจจุบัน: {pos_side.upper()} Size: {pos_size:.6f} BTC, Entry: {entry_price:.2f}")
                    return {
                        'symbol': SYMBOL,
                        'side': pos_side,
                        'entry_price': entry_price,
                        'size': pos_size
                    }
            logger.info("🚫 ไม่พบโพซิชัน BTC/USDT.")
            return None # ไม่พบโพซิชันที่เปิดอยู่
        except ccxt.NetworkError as e:
            logger.warning(f"⚠️ Network Error fetching positions (Attempt {i+1}/{retries}): {type(e).__name__}: {e}. Retrying in 15 seconds...")
            send_telegram(f"⛔️ API Error: ไม่สามารถดึงโพซิชันได้ (Attempt {i+1}/{retries})\nรายละเอียด: {type(e).__name__}: {e}")
            time.sleep(15)
        except ccxt.ExchangeError as e:
            logger.warning(f"⚠️ Exchange Error fetching positions (Attempt {i+1}/{retries}): {type(e).__name__}: {e}. Retrying in 15 seconds...")
            send_telegram(f"⛔️ API Error: ไม่สามารถดึงโพซิชันได้ (Attempt {i+1}/{retries})\nรายละเอียด: {type(e).__name__}: {e}")
            time.sleep(15)
        except Exception as e:
            logger.error(f"❌ Unexpected error in get_current_position: {type(e).__name__}: {e}")
            send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึงโพซิชันได้\nรายละเอียด: {type(e).__name__}: {e}")
            return None
    logger.error(f"❌ Failed to fetch positions after {retries} attempts.")
    send_telegram(f"⛔️ API Error: ล้มเหลวในการดึงโพซิชันหลังจาก {retries} ครั้ง.")
    return None

def fetch_ohlcv(symbol, timeframe, limit=200):
    """
    ดึงข้อมูล OHLCV พร้อม retry.
    """
    retries = 3
    for i in range(retries):
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            time.sleep(2) # เพิ่มหน่วงเวลา
            return ohlcv
        except ccxt.NetworkError as e:
            logger.warning(f"⚠️ Network Error fetching OHLCV (Attempt {i+1}/{retries}): {type(e).__name__}: {e}. Retrying in 15 seconds...")
            send_telegram(f"⛔️ API Error: ไม่สามารถดึงข้อมูล OHLCV ได้ (Attempt {i+1}/{retries})\nรายละเอียด: {type(e).__name__}: {e}")
            time.sleep(15)
        except ccxt.ExchangeError as e:
            logger.warning(f"⚠️ Exchange Error fetching OHLCV (Attempt {i+1}/{retries}): {type(e).__name__}: {e}. Retrying in 15 seconds...")
            send_telegram(f"⛔️ API Error: ไม่สามารถดึงข้อมูล OHLCV ได้ (Attempt {i+1}/{retries})\nรายละเอียด: {type(e).__name__}: {e}")
            time.sleep(15)
        except Exception as e:
            logger.error(f"❌ Unexpected error in fetch_ohlcv: {type(e).__name__}: {e}")
            send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึงข้อมูล OHLCV ได้\nรายละเอียด: {type(e).__name__}: {e}")
            return []
    logger.error(f"❌ Failed to fetch OHLCV after {retries} attempts.")
    send_telegram(f"⛔️ API Error: ล้มเหลวในการดึงข้อมูล OHLCV หลังจาก {retries} ครั้ง.")
    return []

def calculate_ema(prices, period):
    """คำนวณ Exponential Moving Average (EMA)."""
    ema = []
    if not prices:
        return ema
    
    # Calculate SMA for the first period
    if len(prices) < period:
        return ema # ไม่สามารถคำนวณได้ถ้าข้อมูลไม่พอ
    
    sma = sum(prices[:period]) / period
    ema.append(sma)

    multiplier = 2 / (period + 1)
    for price in prices[period:]:
        ema_value = (price - ema[-1]) * multiplier + ema[-1]
        ema.append(ema_value)
    return ema

def check_ema_cross() -> str | None:
    """
    ตรวจสอบสัญญาณ EMA Cross (Golden Cross หรือ Death Cross).
    """
    ohlcv = fetch_ohlcv(SYMBOL, TIMEFRAME, EMA_LONG_PERIOD + 2) # ดึงข้อมูลให้พอสำหรับ EMA200 + 2 แท่ง (ปัจจุบันและแท่งก่อนหน้า)
    if not ohlcv or len(ohlcv) < EMA_LONG_PERIOD + 2: # ต้องมีข้อมูลเพียงพอสำหรับการคำนวณ EMA200 สองแท่งล่าสุด
        logger.warning("⚠️ ข้อมูล OHLCV ไม่เพียงพอสำหรับการคำนวณ EMA.")
        return None

    closes = [float(candle[4]) for candle in ohlcv]
    
    ema_short = calculate_ema(closes, EMA_SHORT_PERIOD)
    ema_long = calculate_ema(closes, EMA_LONG_PERIOD)

    if len(ema_short) < 2 or len(ema_long) < 2:
        logger.warning("⚠️ EMA calculated arrays are too short for cross check.")
        return None

    # EMA ปัจจุบัน (แท่งล่าสุดที่ปิดแล้ว)
    current_ema_short = ema_short[-1]
    current_ema_long = ema_long[-1]

    # EMA แท่งก่อนหน้า
    prev_ema_short = ema_short[-2]
    prev_ema_long = ema_long[-2]

    logger.info(f"📊 EMA50: {current_ema_short:.2f}, EMA200: {current_ema_long:.2f}")

    # Golden Cross (สัญญาณซื้อ)
    if prev_ema_short <= prev_ema_long and current_ema_short > current_ema_long:
        logger.info(f"🌟 ตรวจพบสัญญาณ EMA Cross: LONG (Golden Cross: EMA50({current_ema_short:.2f}) > EMA200({current_ema_long:.2f}))")
        return "long"
    
    # Death Cross (สัญญาณขาย)
    if prev_ema_short >= prev_ema_long and current_ema_short < current_ema_long:
        logger.info(f"🔻 ตรวจพบสัญญาณ EMA Cross: SHORT (Death Cross: EMA50({current_ema_short:.2f}) < EMA200({current_ema_long:.2f}))")
        return "short"
    
    logger.info("💤 ไม่พบสัญญาณ EMA Cross.")
    return None

def open_market_order(direction: str, current_price: float) -> tuple[bool, float | None]:
    """
    เปิด Market Order สำหรับ Long หรือ Short.
    """
    global current_position_size
    
    try:
        balance = get_portfolio_balance()
        if balance <= 0:
            send_telegram("⛔️ Error: ไม่สามารถดึงยอดคงเหลือได้ หรือยอดคงเหลือเป็นศูนย์.")
            return False, None
        
        # เงินที่จะใช้เป็น Margin (80% ของยอดคงเหลือ)
        use_balance = balance * PORTFOLIO_PERCENT_TRADE 
        
        if use_balance <= 0:
            send_telegram("⛔️ Error: ยอดเงินสำหรับเปิดออเดอร์ไม่เพียงพอ (ใช้ 0% ของพอร์ต).")
            return False, None
        
        # ตั้งค่า Leverage (แม้จะตั้งตอนเริ่มต้นแล้ว แต่ก็ใส่ซ้ำได้เพื่อความชัวร์)
        exchange.set_leverage(LEVERAGE, SYMBOL) 
        time.sleep(2) 
        logger.info(f"📈 ตั้งค่า Leverage เป็น {LEVERAGE}x สำหรับ {SYMBOL}.")

        market = exchange.market(SYMBOL)
        
        # คำนวณขนาดออเดอร์เป็น BTC (Contract Size)
        # target_contract_value_usdt คือมูลค่าสัญญาที่เราต้องการเทรด (รวม leverage)
        target_contract_value_usdt = use_balance * LEVERAGE 
        
        # แปลงมูลค่าสัญญาเป็นจำนวน BTC (Contract Amount)
        order_amount_btc = target_contract_value_usdt / current_price
        
        # ปรับให้เป็น precision ที่ Exchange ต้องการ
        # ใช้ market['precision']['amount'] สำหรับปริมาณ (BTC)
        order_amount_btc = float(exchange.amount_to_precision(SYMBOL, order_amount_btc))

        # ตรวจสอบขั้นต่ำ/สูงสุดของปริมาณ
        # OKX limits could be in contract base currency (BTC) or quote currency (USDT value)
        # Assuming 'amount' limits are in base currency (BTC) for Perpetual Swaps
        min_amount = market['limits']['amount']['min'] if market['limits']['amount'] and market['limits']['amount']['min'] else 0.00001 # Default min amount
        max_amount = market['limits']['amount']['max'] if market['limits']['amount'] and market['limits']['amount']['max'] else 1000.0 # Default max amount

        if order_amount_btc < min_amount:
            logger.warning(f"⚠️ ขนาดออเดอร์ที่คำนวณได้ ({order_amount_btc:.6f} BTC) ต่ำกว่าขั้นต่ำ ({min_amount:.6f} BTC). ปรับขนาดเป็นขั้นต่ำ.")
            order_amount_btc = min_amount
        elif order_amount_btc > max_amount:
            logger.warning(f"⚠️ ขนาดออเดอร์ที่คำนวณได้ ({order_amount_btc:.6f} BTC) สูงกว่าขั้นสูง ({max_amount:.6f} BTC). ปรับขนาดเป็นขั้นสูง.")
            order_amount_btc = max_amount
        
        if order_amount_btc <= 0:
            send_telegram("⛔️ Error: ขนาดออเดอร์คำนวณได้เป็นศูนย์หรือติดลบหลังปรับ precision/ขั้นต่ำ.")
            return False, None
        
        side = 'buy' if direction == 'long' else 'sell'
        
        # พารามิเตอร์เพิ่มเติมสำหรับ OKX
        params = {
            'tdMode': 'cross', # ใช้ Cross Margin
            'ordType': 'market',
            'posSide': 'long' if direction == 'long' else 'short' # ระบุ side ของโพซิชัน (สำคัญสำหรับ OKX)
        }
        
        order = None
        for i in range(3): # ลองเปิดออเดอร์ 3 ครั้ง
            try:
                logger.info(f"⚡️ กำลังเปิด Market Order ({direction.upper()}, Size: {order_amount_btc:.6f} BTC)... (Attempt {i+1}/3)")
                order = exchange.create_order(SYMBOL, 'market', side, order_amount_btc, None, params)
                time.sleep(2) # หน่วงเวลาหลังส่งคำสั่ง
                logger.info(f"✅ Market Order ส่งสำเร็จ: {order['id']}")
                break
            except ccxt.NetworkError as e:
                logger.warning(f"⚠️ Network Error creating order (Attempt {i+1}/3): {type(e).__name__}: {e}. Retrying in 15 seconds...")
                send_telegram(f"⛔️ API Error: ไม่สามารถสร้างออเดอร์ตลาดได้ (Attempt {i+1}/3)\nรายละเอียด: {type(e).__name__}: {e}")
                time.sleep(15)
            except ccxt.ExchangeError as e:
                logger.warning(f"⚠️ Exchange Error creating order (Attempt {i+1}/3): {type(e).__name__}: {e}. Retrying in 15 seconds...")
                send_telegram(f"⛔️ API Error: ไม่สามารถสร้างออเดอร์ตลาดได้ (Attempt {i+1}/3)\nรายละเอียด: {type(e).__name__}: {e}")
                time.sleep(15)
            except Exception as e:
                logger.error(f"❌ Unexpected error creating market order: {type(e).__name__}: {e}")
                send_telegram(f"⛔️ Unexpected Error: ไม่สามารถสร้างออเดอร์ตลาดได้\nรายละเอียด: {type(e).__name__}: {e}")
                return False, None
        
        if not order: # ถ้าเปิดออเดอร์ไม่สำเร็จก็ออกจากฟังก์ชันเลย
            logger.error("❌ Failed to create market order after 3 attempts.")
            send_telegram("⛔️ Error: ล้มเหลวในการสร้างออเดอร์ตลาดหลังจาก 3 ครั้ง.")
            return False, None

        # หลังจากเปิด Market Order แล้ว ให้ดึงโพซิชันอีกครั้งเพื่อยืนยัน Entry Price
        confirmed_pos_info = None
        # เพิ่มจำนวนครั้งและเวลาหน่วงในการรอการยืนยันโพซิชัน
        for i in range(20): # ลองดึง 20 ครั้ง (นานสูงสุด 100 วินาที)
            time.sleep(5) # หน่วงเวลา 5 วินาทีต่อการลองดึงแต่ละครั้ง
            confirmed_pos_info = get_current_position() 
            # ตรวจสอบว่าได้โพซิชันแล้ว และเป็นโพซิชันที่ถูกต้อง (ทิศทางตรงกับที่เราเพิ่งเปิด และขนาดมากกว่า 0)
            if confirmed_pos_info and confirmed_pos_info['side'] == direction and confirmed_pos_info['size'] > 0:
                logger.info(f"✅ ยืนยันโพซิชัน Entry Price: {confirmed_pos_info['entry_price']:.2f}, Size: {confirmed_pos_info['size']:.6f} BTC")
                current_position_size = confirmed_pos_info['size']
                return True, confirmed_pos_info['entry_price']
            logger.info(f"⏳ รอการยืนยันโพซิชัน ({i+1}/20)...")
            
        logger.error("❌ ไม่สามารถยืนยันโพซิชันและ Entry Price ได้หลังเปิด Market Order.")
        send_telegram("⛔️ Error: ไม่สามารถยืนยันโพซิชันหลังเปิดออเดอร์ได้.")
        return False, None

    except Exception as e:
        logger.error(f"❌ Unexpected error in open_market_order: {type(e).__name__}: {e}")
        send_telegram(f"⛔️ Unexpected Error: เกิดข้อผิดพลาดขณะเปิดออเดอร์\nรายละเอียด: {type(e).__name__}: {e}")
        return False, None

def set_tpsl_for_position(direction: str, entry_price: float, position_size: float):
    """
    ตั้งค่า Take Profit (TP) และ Stop Loss (SL) สำหรับโพซิชันที่เปิดอยู่.
    คำนวณจากจุด (points)
    """
    try:
        # คำนวณราคา TP และ SL จากจุด
        if direction == 'long':
            tp_price = entry_price + TP_POINTS
            sl_price = entry_price - SL_POINTS
        else: # short
            tp_price = entry_price - TP_POINTS
            sl_price = entry_price + SL_POINTS
        
        # ปรับราคา TP/SL ให้ตรงตาม precision ของ Exchange
        market = exchange.market(SYMBOL)
        tp_price = float(exchange.price_to_precision(SYMBOL, tp_price))
        sl_price = float(exchange.price_to_precision(SYMBOL, sl_price))

        # ส่งคำสั่ง TP/SL ผ่าน privatePostSet_tpsl_order (OKX specific)
        params = {
            'instId': SYMBOL.replace('/', '-').replace(':USDT', '-USDT-SWAP'), # 'BTC-USDT-SWAP'
            'tdMode': 'cross',
            'posSide': 'long' if direction == 'long' else 'short',
            'tpTriggerPx': str(tp_price),
            'tpOrdPx': '-1', # Market TP
            'slTriggerPx': str(sl_price),
            'slOrdPx': '-1' # Market SL
        }

        response = exchange.privatePostSet_tpsl_order(params)
        time.sleep(2) # เพิ่มหน่วงเวลา
        
        if response and response['code'] == '0':
            logger.info(f"✅ ตั้งค่า TP: {tp_price:.2f} และ SL: {sl_price:.2f} สำเร็จ.")
            send_telegram(f"✅ ตั้งค่า TP/SL สำเร็จ!\n"
                                    f"ทิศทาง: <b>{direction.upper()}</b>\n"
                                    f"Entry Price: <b>{entry_price:.2f}</b>\n"
                                    f"TP: <b>{tp_price:.2f}</b>\n"
                                    f"SL: <b>{sl_price:.2f}</b>")
            return True
        else:
            logger.error(f"❌ ตั้งค่า TP/SL ไม่สำเร็จ: {response}")
            send_telegram(f"⛔️ Error: ตั้งค่า TP/SL ไม่สำเร็จ\nรายละเอียด: {response}")
            return False
    except ccxt.NetworkError as e:
        logger.error(f"❌ Network Error setting TP/SL: {type(e).__name__}: {e}")
        send_telegram(f"⛔️ API Error: ไม่สามารถตั้ง TP/SL ได้\nรายละเอียด: {type(e).__name__}: {e}")
        return False
    except ccxt.ExchangeError as e:
        logger.error(f"❌ Exchange Error setting TP/SL: {type(e).__name__}: {e}")
        send_telegram(f"⛔️ API Error: ไม่สามารถตั้ง TP/SL ได้\nรายละเอียด: {type(e).__name__}: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error in set_tpsl_for_position: {type(e).__name__}: {e}")
        send_telegram(f"⛔️ Unexpected Error: เกิดข้อผิดพลาดขณะตั้ง TP/SL\nรายละเอียด: {type(e).__name__}: {e}")
        return False

def move_sl_to_breakeven(direction: str, entry_price: float, current_price: float, position_size: float):
    """
    เลื่อน Stop Loss ไปที่ Break-even Price (Entry Price).
    """
    try:
        # ตรวจสอบว่าราคาวิ่งไปในทิศทางที่ต้องการพอสมควรแล้วหรือไม่ (เช่น 0.5% ของ Entry Price เพื่อให้คุ้มค่าธรรมเนียม)
        breakeven_threshold_points = 100 # เช่น 100 จุดจากราคาเข้า
        
        if direction == 'long':
            if current_price < entry_price + breakeven_threshold_points:
                logger.info(f"ℹ️ ราคาปัจจุบัน {current_price:.2f} ยังไม่ถึงจุดเบรคอีเวนสำหรับ Long (เป้าหมาย > {entry_price + breakeven_threshold_points:.2f}).")
                return False
            sl_to_set = entry_price # ตั้ง SL ที่ราคา Entry
            tp_to_set = entry_price + TP_POINTS # TP ยังคงเดิม
        else: # short
            if current_price > entry_price - breakeven_threshold_points:
                logger.info(f"ℹ️ ราคาปัจจุบัน {current_price:.2f} ยังไม่ถึงจุดเบรคอีเวนสำหรับ Short (เป้าหมาย < {entry_price - breakeven_threshold_points:.2f}).")
                return False
            sl_to_set = entry_price # ตั้ง SL ที่ราคา Entry
            tp_to_set = entry_price - TP_POINTS # TP ยังคงเดิม
        
        # ปรับราคา SL/TP ให้ตรงตาม precision ของ Exchange
        market = exchange.market(SYMBOL)
        sl_to_set = float(exchange.price_to_precision(SYMBOL, sl_to_set))
        tp_to_set = float(exchange.price_to_precision(SYMBOL, tp_to_set))

        # ส่งคำสั่ง TP/SL ผ่าน privatePostSet_tpsl_order (OKX specific)
        params = {
            'instId': SYMBOL.replace('/', '-').replace(':USDT', '-USDT-SWAP'),
            'tdMode': 'cross',
            'posSide': 'long' if direction == 'long' else 'short',
            'tpTriggerPx': str(tp_to_set),
            'tpOrdPx': '-1', # Market TP
            'slTriggerPx': str(sl_to_set),
            'slOrdPx': '-1' # Market SL
        }

        response = exchange.privatePostSet_tpsl_order(params)
        time.sleep(2) # เพิ่มหน่วงเวลา
        
        if response and response['code'] == '0':
            logger.info(f"✅ เลื่อน Stop Loss ไปที่ Break-even: {sl_to_set:.2f} สำเร็จ.")
            send_telegram(f"✅ เลื่อน SL เป็นกันทุนสำหรับ <b>{direction.upper()}</b> สำเร็จ!\n"
                                    f"Entry Price: <b>{entry_price:.2f}</b>\n"
                                    f"New SL: <b>{sl_to_set:.2f}</b>")
            return True
        else:
            logger.error(f"❌ เลื่อน Stop Loss ไม่สำเร็จ: {response}")
            send_telegram(f"⛔️ Error: เลื่อน SL ไม่สำเร็จ\nรายละเอียด: {response}")
            return False
    except ccxt.NetworkError as e:
        logger.error(f"❌ Network Error moving SL: {type(e).__name__}: {e}")
        send_telegram(f"⛔️ API Error: ไม่สามารถเลื่อน SL ได้\nรายละเอียด: {type(e).__name__}: {e}")
        return False
    except ccxt.ExchangeError as e:
        logger.error(f"❌ Exchange Error moving SL: {type(e).__name__}: {e}")
        send_telegram(f"⛔️ API Error: ไม่สามารถเลื่อน SL ได้\nรายละเอียด: {type(e).__name__}: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error in move_sl_to_breakeven: {type(e).__name__}: {e}")
        send_telegram(f"⛔️ Unexpected Error: เกิดข้อผิดพลาดขณะเลื่อน SL\nรายละเอียด: {type(e).__name__}: {e}")
        return False

def check_and_send_daily_report():
    """ตรวจสอบและส่งรายงานประจำวัน."""
    global daily_stats, last_daily_report_date
    now = datetime.datetime.now(pytz.timezone('Asia/Bangkok'))
    today_date = now.date()

    # ตรวจสอบว่าถึงเวลาส่งรายงานประจำวันแล้วหรือไม่ และยังไม่ได้ส่งสำหรับวันนี้
    if now.hour == DAILY_REPORT_TIME.hour and now.minute >= DAILY_REPORT_TIME.minute and \
       (last_daily_report_date is None or last_daily_report_date < today_date):
        
        logger.info("✅ ตรวจพบว่าถึงเวลาส่งรายงานประจำวันแล้ว และยังไม่ได้ส่งสำหรับวันนี้. กำลังส่งรายงาน...")

        report_date_str = today_date.strftime('%Y-%m-%d')
        
        # รีเซ็ตสถิติประจำวันสำหรับวันนี้ ถ้าเป็นวันใหม่
        if last_daily_report_date is None or last_daily_report_date < today_date:
            logger.info(f"🔄 รีเซ็ตสถิติประจำวันสำหรับวันนี้ {report_date_str}.")
            daily_stats[report_date_str] = {'total_trades': 0, 'wins': 0, 'losses': 0, 'pnl_usdt': 0.0} # รีเซ็ตสำหรับวันใหม่
            last_daily_report_date = today_date
            save_daily_stats() # บันทึกสถานะหลังจากรีเซ็ต

        balance = get_portfolio_balance()

        report_message = (
            f"<b>รายงานประจำวัน - {report_date_str}</b>\n"
            f"💰 ยอดคงเหลือปัจจุบัน: <b>{balance:.2f} USDT</b>\n"
            f"📊 จำนวนเทรดวันนี้: <b>{daily_stats.get(report_date_str, {}).get('total_trades', 0)}</b>\n"
            f"✅ ชนะ: <b>{daily_stats.get(report_date_str, {}).get('wins', 0)}</b>\n"
            f"❌ แพ้: <b>{daily_stats.get(report_date_str, {}).get('losses', 0)}</b>\n"
            f"📈 PnL วันนี้ (ประมาณ): <b>{daily_stats.get(report_date_str, {}).get('pnl_usdt', 0.0):.2f} USDT</b>"
        )
        send_telegram(report_message)
        
        logger.info("✅ ส่งรายงานประจำวันแล้ว.")
        
        # คำนวณเวลาสำหรับรายงานถัดไป (พรุ่งนี้เวลา 00:05)
        next_report_time = datetime.datetime.combine(today_date + datetime.timedelta(days=1), DAILY_REPORT_TIME, tzinfo=pytz.timezone('Asia/Bangkok'))
        time_until_next_report = next_report_time - now
        hours, remainder = divmod(time_until_next_report.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        send_telegram(f"กำหนดส่งรายงานประจำวันถัดไปในอีก {hours} ชั่วโมง {minutes} นาที (เวลา {DAILY_REPORT_TIME.strftime('%H:%M')}).")
    else:
        logger.info(f"💤 ยังไม่ถึงเวลาส่งรายงานประจำวัน หรือส่งไปแล้วสำหรับวันนี้ ({today_date}).")
    
# --- Main Loop ---
async def main():
    logger.info("🚀 บอทเข้าสู่ Main Loop แล้ว...")
    
    # โหลดสถิติเมื่อเริ่มต้น
    load_daily_stats()
    check_and_send_daily_report() # ตรวจสอบและส่งรายงานเมื่อเริ่มทำงาน (ถ้าถึงเวลา)

    # ส่งข้อความแจ้งเตือนเมื่อเริ่มทำงาน
    send_telegram("<b>บอทเริ่มทำงาน</b>")
    send_telegram("ℹ️ ส่งข้อความแจ้งเตือนเมื่อบอทเริ่มทำงาน.")
    logger.info("ℹ️ เริ่ม Daily Report Scheduler.")

    # ตั้งค่า leverage แค่ครั้งเดียวตอนเริ่มต้น
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
        time.sleep(2)
        logger.info(f"📈 ตั้งค่า Leverage เป็น {LEVERAGE}x สำหรับ {SYMBOL} ในช่วงเริ่มต้น.")
    except Exception as e:
        logger.error(f"❌ Error setting initial leverage: {type(e).__name__}: {e}")
        send_telegram(f"⛔️ Error: ไม่สามารถตั้ง Leverage ได้ตั้งแต่เริ่มต้น\nรายละเอียด: {type(e).__name__}: {e}")
        
    while True:
        try:
            current_position = get_current_position()
            
            # ดึงราคาปัจจุบัน
            ticker = exchange.fetch_ticker(SYMBOL)
            time.sleep(2) # เพิ่มหน่วงเวลา
            current_price = ticker['last']
            logger.info(f"Current Price: {current_price:.2f}")

            if current_position:
                # มีโพซิชันเปิดอยู่
                logger.info(f"📊 โพซิชัน {current_position['side'].upper()} อยู่ที่ {current_position['entry_price']:.2f}.")
                # ตรวจสอบและเลื่อน SL ไปที่ Break-even (ถ้า Logic ต้องการ)
                move_sl_to_breakeven(current_position['side'], current_position['entry_price'], current_price, current_position['size'])
                
                # Logic สำหรับการปิดโพซิชันเมื่อถึง TP/SL จะถูกจัดการโดย Exchange (เนื่องจากตั้ง Market TP/SL)
                # บอทจะรู้ว่าโพซิชันปิดไปแล้วในรอบถัดไปเมื่อ get_current_position() คืนค่า None
                pass
            else:
                # ไม่มีโพซิชันเปิดอยู่
                logger.info("🚫 ไม่มีโพซิชันเปิดอยู่. กำลังมองหาสัญญาณ...")
                signal = check_ema_cross()
                
                if signal:
                    # ตรวจสอบอีกครั้งว่ายังมีโพซิชันเปิดอยู่หรือไม่ (ป้องกัน double-open)
                    recheck_position = get_current_position()
                    if recheck_position:
                        logger.warning("⚠️ ตรวจพบสัญญาณ แต่มีโพซิชันเปิดอยู่แล้ว. จะไม่เปิดออเดอร์ใหม่.")
                        continue # ข้ามไปรอบถัดไป
                    
                    # ลองเปิดออเดอร์
                    order_success, entry_price = open_market_order(signal, current_price)
                    
                    if order_success and entry_price:
                        # ถ้าเปิดออเดอร์สำเร็จ ให้ตั้ง TP/SL ทันที
                        current_pos_after_open = get_current_position() # ดึงโพซิชันอีกครั้งเพื่อความชัวร์
                        if current_pos_after_open and current_pos_after_open['side'] == signal and current_pos_after_open['size'] > 0:
                            logger.info(f"✅ ออเดอร์ {signal.upper()} เปิดสำเร็จ. กำลังตั้ง TP/SL.")
                            set_tpsl_for_position(signal, entry_price, current_pos_after_open['size'])
                        else:
                            logger.error("❌ เกิดข้อผิดพลาด: ออเดอร์เปิดแล้ว แต่ไม่สามารถยืนยันโพซิชันเพื่อตั้ง TP/SL ได้ (อาจปิดไปแล้ว หรือขนาดเป็น 0).")
                            send_telegram("⛔️ Error: ออเดอร์เปิดแล้ว แต่ไม่สามารถยืนยันโพซิชันเพื่อตั้ง TP/SL ได้ (อาจปิดไปแล้ว หรือขนาดเป็น 0).")
                    else:
                        logger.warning(f"⚠️ ไม่สามารถเปิด Market Order {signal.upper()} ได้.")
            
            # ตรวจสอบและส่งรายงานประจำวัน (ถ้าถึงเวลา)
            check_and_send_daily_report()

            # หน่วงเวลาตาม INTERVAL ที่กำหนด
            logger.info(f"💤 รอ {INTERVAL} นาที...")
            time.sleep(INTERVAL * 60) # แปลงนาทีเป็นวินาที

        except Exception as e:
            logger.error(f"❌ Unexpected error in main loop: {type(e).__name__}: {e}")
            send_telegram(f"⛔️ Unexpected Error: เกิดข้อผิดพลาดใน Main Loop\nรายละเอียด: {type(e).__name__}: {e}")
            time.sleep(60) # รอ 1 นาที ก่อนวนลูปใหม่ (เพื่อไม่ให้ Loop รันถี่เกินไปหากมี Error ซ้ำๆ)

if __name__ == '__main__':
    asyncio.run(main()) # รันฟังก์ชัน main แบบ asynchronous
