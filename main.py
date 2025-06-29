import ccxt
import time
import requests
from datetime import datetime, timedelta
import logging
import threading
import json
import os
# === เพิ่ม Library ใหม่สำหรับการสั่งการผ่าน Telegram ===
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext

# ==============================================================================
# 1. ตั้งค่าพื้นฐาน (CONFIGURATION) - (ใช้ค่าที่คุณส่งมาทั้งหมด)
# ==============================================================================

# --- API Keys & Credentials ---
API_KEY = os.getenv('RAILWAY_API_KEY', 'YOUR_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('RAILWAY_SECRET', 'YOUR_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('RAILWAY_PASSWORD', 'YOUR_PASSWORD_HERE_FOR_LOCAL_TESTING')

# --- Trade Parameters ---
SYMBOL = 'BTC/USDT:USDT'
TIMEFRAME = '15m'
LEVERAGE = 30
TP_VALUE_POINTS = 501
SL_VALUE_POINTS = 999
BE_PROFIT_TRIGGER_POINTS = 350
BE_SL_BUFFER_POINTS = 100
PORTFOLIO_PERCENT_TRADE = 0.8
CROSS_THRESHOLD_POINTS = 35

# --- Telegram Notification Settings ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# --- Files & Paths ---
STATS_FILE = 'trading_stats.json'

# --- Bot Timing ---
# ลดเวลาหน่วงลงเพื่อให้ตอบสนองคำสั่ง /stop ได้เร็วขึ้น
MAIN_LOOP_SLEEP_SECONDS = 60
ERROR_RETRY_SLEEP_SECONDS = 60

# --- Tolerance ---
TP_SL_BE_PRICE_TOLERANCE_PERCENT = 0.005

# ==============================================================================
# 2. การตั้งค่า Logging (เหมือนเดิม)
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==============================================================================
# 3. ตัวแปรสถานะการเทรด (GLOBAL TRADE STATE VARIABLES)
# ==============================================================================

# === ตัวแปรใหม่สำหรับควบคุมการทำงาน ===
trading_active = threading.Event() # ใช้ Event ในการเปิด/ปิดการเทรดอย่างปลอดภัย
trading_thread = None              # เก็บ thread ที่ใช้รันการเทรด

current_position = None
entry_price = None
sl_moved = False
portfolio_balance = 0.0
initial_balance = 0.0
current_position_size = 0.0

# ==============================================================================
# 4. โครงสร้างข้อมูลสถิติ (STATISTICS DATA STRUCTURE) (เหมือนเดิม)
# ==============================================================================
daily_stats = {
    'date': None,
    'tp_count': 0,
    'sl_count': 0,
    'total_pnl': 0.0,
    'trades': [],
    'last_report_date': None,
    'last_ema_cross_signal': None
}

# ==============================================================================
# 5. การตั้งค่า Exchange (CCXT EXCHANGE SETUP) (เหมือนเดิม)
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
    exchange.set_sandbox_mode(False)
    logger.info("✅ เชื่อมต่อกับ OKX Exchange สำเร็จ")
except ValueError as ve:
    logger.critical(f"❌ Configuration Error: {ve}")
    exit()
except Exception as e:
    logger.critical(f"❌ ไม่สามารถเชื่อมต่อกับ OKX Exchange ได้: {e}")
    exit()

# ==============================================================================
# 6. ฟังก์ชันจัดการสถิติ (STATISTICS MANAGEMENT FUNCTIONS) (เหมือนเดิม)
# ==============================================================================
def save_daily_stats():
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump(daily_stats, f, indent=4)
        logger.debug(f"💾 บันทึกสถิติลงไฟล์ {STATS_FILE} สำเร็จ")
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการบันทึกสถิติ: {e}")

def load_daily_stats():
    global daily_stats
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, 'r') as f:
                daily_stats = json.load(f)
            logger.info(f"💾 โหลดสถิติจากไฟล์ {STATS_FILE} สำเร็จ")
            current_date_str = datetime.now().strftime('%Y-%m-%d')
            if daily_stats.get('date') != current_date_str:
                logger.info(f"ℹ️ สถิติเป็นของวันอื่น จะรีเซ็ตสำหรับวันใหม่")
                reset_daily_stats()
        else:
            logger.info(f"🆕 ไม่พบไฟล์สถิติ สร้างไฟล์ใหม่")
            reset_daily_stats()
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการโหลดสถิติ: {e}")
        reset_daily_stats()

def reset_daily_stats():
    global daily_stats
    current_date_str = datetime.now().strftime('%Y-%m-%d')
    # เก็บ signal เก่าไว้ข้ามวัน แต่รีเซ็ตค่าอื่นๆ
    last_signal = daily_stats.get('last_ema_cross_signal')
    daily_stats = {
        'date': current_date_str, 'tp_count': 0, 'sl_count': 0, 'total_pnl': 0.0,
        'trades': [], 'last_report_date': daily_stats.get('last_report_date'),
        'last_ema_cross_signal': last_signal
    }
    save_daily_stats()
    logger.info(f"🔄 รีเซ็ตสถิติสำหรับวันที่ {current_date_str}")

def add_trade_result(reason: str, pnl: float):
    global daily_stats
    current_date_str = datetime.now().strftime('%Y-%m-%d')
    if daily_stats.get('date') != current_date_str:
        reset_daily_stats()

    if reason.upper() == 'TP':
        daily_stats['tp_count'] += 1
    elif reason.upper() in ['SL', 'SL (กันทุน)']:
        daily_stats['sl_count'] += 1
    daily_stats['total_pnl'] += pnl
    daily_stats['trades'].append({
        'time': datetime.now().strftime('%H:%M:%S'), 'reason': reason, 'pnl': pnl
    })
    save_daily_stats()

# ==============================================================================
# 7. ฟังก์ชันแจ้งเตือน Telegram (TELEGRAM NOTIFICATION FUNCTIONS) (เหมือนเดิม)
# ==============================================================================
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("⚠️ TELEGRAM_TOKEN/CHAT_ID ไม่ได้ตั้งค่า ไม่สามารถส่งข้อความได้")
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        params = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        logger.info(f"✉️ Telegram: {msg.splitlines()[0]}...")
    except Exception as e:
        logger.error(f"⛔️ Error sending Telegram message: {e}")

# ==============================================================================
# 8. ฟังก์ชันดึงข้อมูล Exchange (EXCHANGE DATA RETRIEVAL FUNCTIONS) (เหมือนเดิม)
# ==============================================================================
def get_portfolio_balance() -> float:
    global portfolio_balance
    try:
        balance_data = exchange.fetch_balance({'type': 'trading'})
        usdt_balance = float(balance_data.get('USDT', {}).get('total', 0.0))
        portfolio_balance = usdt_balance
        return usdt_balance
    except Exception as e:
        logger.error(f"❌ Error fetching balance: {e}")
        return 0.0

def get_current_position() -> dict | None:
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for pos in positions:
            if float(pos.get('info', {}).get('posAmt', 0)) != 0:
                pos_amount = float(pos['info']['posAmt'])
                return {
                    'side': 'long' if pos_amount > 0 else 'short', 'size': abs(pos_amount),
                    'entry_price': float(pos['entryPrice']), 'unrealized_pnl': float(pos['unrealizedPnl']),
                    'pos_id': pos['id']
                }
        return None
    except Exception as e:
        logger.error(f"❌ Error fetching positions: {e}")
        return None

# ==============================================================================
# 9. ฟังก์ชันคำนวณ Indicators (INDICATOR CALCULATION FUNCTIONS) (เหมือนเดิม)
# ==============================================================================
def calculate_ema(prices: list[float], period: int) -> float | None:
    if len(prices) < period: return None
    sma = sum(prices[:period]) / period
    ema = sma
    multiplier = 2 / (period + 1)
    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))
    return ema

def check_ema_cross() -> str | None:
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=250)
        if len(ohlcv) < 202:
            logger.warning(f"ข้อมูล OHLCV ไม่เพียงพอ ({len(ohlcv)}/202)")
            return None
        closes = [c[4] for c in ohlcv]
        ema50_current = calculate_ema(closes, 50)
        ema200_current = calculate_ema(closes, 200)
        ema50_prev = calculate_ema(closes[:-1], 50)
        ema200_prev = calculate_ema(closes[:-1], 200)
        if None in [ema50_prev, ema200_prev, ema50_current, ema200_current]: return None

        if ema50_prev <= ema200_prev and ema50_current > (ema200_current + CROSS_THRESHOLD_POINTS):
            logger.info(f"🚀 Threshold Golden Cross: EMA50({ema50_current:.2f}) > EMA200({ema200_current:.2f}) + {CROSS_THRESHOLD_POINTS}")
            return 'long'
        elif ema50_prev >= ema200_prev and ema50_current < (ema200_current - CROSS_THRESHOLD_POINTS):
            logger.info(f"🔻 Threshold Death Cross: EMA50({ema50_current:.2f}) < EMA200({ema200_current:.2f}) - {CROSS_THRESHOLD_POINTS}")
            return 'short'
        return None
    except Exception as e:
        logger.error(f"❌ Error in check_ema_cross: {e}")
        return None

# ==============================================================================
# 10. ฟังก์ชันจัดการคำสั่งซื้อขาย (ORDER MANAGEMENT FUNCTIONS) (เหมือนเดิม)
# ==============================================================================
def open_market_order(direction: str, current_price: float) -> tuple[bool, float | None]:
    global current_position_size
    try:
        balance = get_portfolio_balance()
        if balance <= 1:
            send_telegram(f"⛔️ Error: ยอดคงเหลือไม่เพียงพอ ({balance:.2f} USDT).")
            return False, None
        use_balance = balance * PORTFOLIO_PERCENT_TRADE
        market = exchange.market(SYMBOL)
        order_size_in_btc = (use_balance * LEVERAGE) / current_price
        min_amount = market.get('limits', {}).get('amount', {}).get('min', 0)
        if min_amount and order_size_in_btc < min_amount:
            logger.warning(f"⚠️ ขนาดออเดอร์ ({order_size_in_btc:.6f}) ต่ำกว่าขั้นต่ำ ({min_amount:.6f}). ปรับเป็นขั้นต่ำ.")
            order_size_in_btc = min_amount
        order_size_in_btc = float(exchange.amount_to_precision(SYMBOL, order_size_in_btc))
        if order_size_in_btc <= 0:
            send_telegram("⛔️ Error: ขนาดออเดอร์เป็น 0.")
            return False, None
        side = 'buy' if direction == 'long' else 'sell'
        params = {'tdMode': 'cross', 'mgnCcy': 'USDT'}
        logger.info(f"⚡️ กำลังเปิด Market Order ({direction.upper()}, Size: {order_size_in_btc:.6f} BTC)...")
        order = exchange.create_order(SYMBOL, 'market', side, order_size_in_btc, params=params)
        logger.info(f"✅ Market Order ส่งสำเร็จ: {order.get('id', 'N/A')}")
        time.sleep(5) # รอสักครู่ให้โพซิชันปรากฏ
        confirmed_pos_info = get_current_position()
        if confirmed_pos_info and confirmed_pos_info['side'] == direction:
            logger.info(f"✅ ยืนยันโพซิชัน Entry Price: {confirmed_pos_info['entry_price']:.2f}")
            current_position_size = confirmed_pos_info['size']
            return True, confirmed_pos_info['entry_price']
        else:
            logger.error("❌ ไม่สามารถยืนยันโพซิชันได้หลังเปิดออเดอร์.")
            send_telegram("⛔️ Error: ไม่สามารถยืนยันโพซิชันหลังเปิดออเดอร์ได้.")
            return False, None
    except Exception as e:
        send_telegram(f"❌ Error: ไม่สามารถเปิดออเดอร์ตลาดได้\nรายละเอียด: {e}")
        logger.error(f"❌ Market order failed: {e}", exc_info=True)
        return False, None

def set_tpsl_for_position(pos_direction: str, entry_price_val: float) -> bool:
    try:
        tp_price = entry_price_val + TP_VALUE_POINTS if pos_direction == 'long' else entry_price_val - TP_VALUE_POINTS
        sl_price = entry_price_val - SL_VALUE_POINTS if pos_direction == 'long' else entry_price_val + SL_VALUE_POINTS
        tp_price = float(exchange.price_to_precision(SYMBOL, tp_price))
        sl_price = float(exchange.price_to_precision(SYMBOL, sl_price))
        params = {
            'instId': exchange.market(SYMBOL)['id'], 'posSide': 'net', 'mgnMode': 'cross',
            'tpTriggerPx': str(tp_price), 'tpOrdPx': '-1',
            'slTriggerPx': str(sl_price), 'slOrdPx': '-1',
        }
        logger.info(f"⚙️ กำลังตั้งค่า TP/SL ({pos_direction.upper()}) SL:{sl_price:,.1f} TP:{tp_price:,.1f}...")
        response = exchange.private_post_trade_order_tpsl(params)
        if response and response.get('code') == '0':
            logger.info("✅ ตั้งค่า TP/SL สำเร็จ.")
            message = f"""{('📈' if pos_direction == 'long' else '📉')} ✅ <b>เปิด {pos_direction.upper()} & ตั้ง TP/SL</b>
<b>Entry:</b> <code>{entry_price_val:,.1f}</code>
<b>TP:</b> <code>{tp_price:,.1f}</code>
<b>SL:</b> <code>{sl_price:,.1f}</code>"""
            send_telegram(message)
            return True
        else:
            error_detail = response.get('msg', 'No message')
            logger.error(f"❌ ตั้งค่า TP/SL ไม่สำเร็จ: {error_detail}")
            send_telegram(f"⛔️ Error: ตั้งค่า TP/SL ไม่สำเร็จ: {error_detail}")
            return False
    except Exception as e:
        logger.error(f"❌ Error in set_tpsl_for_position: {e}", exc_info=True)
        send_telegram(f"⛔️ Error: เกิดข้อผิดพลาดในการตั้ง TP/SL\nรายละเอียด: {e}")
        return False

def move_sl_to_breakeven(pos_direction: str, entry_price_val: float):
    global sl_moved
    if sl_moved: return
    try:
        new_sl_price = entry_price_val + BE_SL_BUFFER_POINTS if pos_direction == 'long' else entry_price_val - BE_SL_BUFFER_POINTS
        new_sl_price = float(exchange.price_to_precision(SYMBOL, new_sl_price))
        params = {
            'instId': exchange.market(SYMBOL)['id'], 'posSide': 'net', 'mgnMode': 'cross',
            'slTriggerPx': str(new_sl_price), 'slOrdPx': '-1',
        }
        logger.info(f"⚙️ กำลังเลื่อน SL ไปที่กันทุน: {new_sl_price:,.1f}...")
        response = exchange.private_post_trade_order_tpsl(params)
        if response and response.get('code') == '0':
            sl_moved = True
            message = f"""🔄 <b>เลื่อน SL ไปที่ราคาเข้า (Break-even)</b>
<b>📍 Entry:</b> <code>{entry_price_val:,.1f}</code>
<b>🛡️ SL ใหม่:</b> <code>{new_sl_price:,.1f}</code>"""
            send_telegram(message)
            logger.info(f"✅ SL เลื่อนไปที่กันทุนสำเร็จ.")
        else:
            logger.warning(f"⚠️ เลื่อน SL ไม่สำเร็จ: {response.get('msg', 'No message')}")
    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดในการเลื่อน SL: {e}", exc_info=True)

# ==============================================================================
# 11. ฟังก์ชันตรวจสอบสถานะ (MONITORING FUNCTIONS) (เหมือนเดิม)
# ==============================================================================
def monitor_position(pos_info: dict | None, current_price: float):
    global current_position, sl_moved, entry_price, current_position_size, daily_stats
    if not pos_info:
        if current_position:
            logger.info(f"ℹ️ โพซิชัน {current_position.upper()} ถูกปิดแล้ว.")
            pnl_usdt_actual = 0.0
            if entry_price and current_position_size:
                pnl_usdt_actual = (current_price - entry_price) * current_position_size if current_position == 'long' else (entry_price - current_price) * current_position_size
            
            # Logic to determine close reason
            close_reason = "ปิดโดยไม่ทราบสาเหตุ"; emoji = "❓"
            tp_sl_be_tolerance = entry_price * TP_SL_BE_PRICE_TOLERANCE_PERCENT if entry_price else 0
            
            if current_position == 'long' and entry_price:
                if current_price >= (entry_price + TP_VALUE_POINTS - tp_sl_be_tolerance): close_reason, emoji = "TP", "✅"
                elif sl_moved and abs(current_price - (entry_price + BE_SL_BUFFER_POINTS)) <= tp_sl_be_tolerance: close_reason, emoji = "SL (กันทุน)", "🛡️"
                elif current_price <= (entry_price - SL_VALUE_POINTS + tp_sl_be_tolerance): close_reason, emoji = "SL", "❌"
            elif current_position == 'short' and entry_price:
                if current_price <= (entry_price - TP_VALUE_POINTS + tp_sl_be_tolerance): close_reason, emoji = "TP", "✅"
                elif sl_moved and abs(current_price - (entry_price - BE_SL_BUFFER_POINTS)) <= tp_sl_be_tolerance: close_reason, emoji = "SL (กันทุน)", "🛡️"
                elif current_price >= (entry_price + SL_VALUE_POINTS - tp_sl_be_tolerance): close_reason, emoji = "SL", "❌"

            send_telegram(f"{emoji} <b>ปิดออเดอร์ด้วย {close_reason}</b>\n<b>PnL (ประมาณ):</b> <code>{pnl_usdt_actual:,.2f} USDT</code>")
            add_trade_result(close_reason, pnl_usdt_actual)
            current_position, entry_price, current_position_size, sl_moved = None, None, 0.0, False
            daily_stats['last_ema_cross_signal'] = None # รีเซ็ตเพื่อให้รอสัญญาณใหม่ได้
            save_daily_stats()
        return

    current_position, entry_price, current_position_size = pos_info['side'], pos_info['entry_price'], pos_info['size']
    logger.info(f"📊 สถานะ: {current_position.upper()}, PnL: {pos_info['unrealized_pnl']:,.2f} USDT, ราคา: {current_price:,.1f}, เข้า: {entry_price:,.1f}")
    
    pnl_in_points = (current_price - entry_price) if current_position == 'long' else (entry_price - current_price)
    if not sl_moved and pnl_in_points >= BE_PROFIT_TRIGGER_POINTS:
        move_sl_to_breakeven(current_position, entry_price)

# ==============================================================================
# 12. ฟังก์ชันตรรกะการเทรดหลัก (MAIN TRADING LOGIC)
# ==============================================================================
def trading_logic_loop():
    """ฟังก์ชันหลักที่รัน Logic การเทรด (จะถูกเรียกให้ทำงานใน Thread แยก)."""
    logger.info("🚀 Trading thread started. Waiting for signal...")
    while trading_active.is_set():
        try:
            current_pos_info = get_current_position()
            time.sleep(1) # หน่วงเล็กน้อย
            if not current_pos_info:
                signal = check_ema_cross()
                if signal and signal != daily_stats.get('last_ema_cross_signal'):
                    logger.info(f"✨ สัญญาณ {signal.upper()} ใหม่ที่ถูกต้อง. กำลังเปิดออเดอร์.")
                    ticker = exchange.fetch_ticker(SYMBOL)
                    market_order_success, confirmed_entry_price = open_market_order(signal, ticker['last'])
                    if market_order_success and confirmed_entry_price:
                        set_tpsl_success = set_tpsl_for_position(signal, confirmed_entry_price)
                        if set_tpsl_success:
                            daily_stats['last_ema_cross_signal'] = signal
                            save_daily_stats()
                        else:
                            send_telegram("⛔️ <b>ข้อผิดพลาดร้ายแรง:</b> เปิดออเดอร์ได้ แต่ตั้ง TP/SL ไม่สำเร็จ!")
            else: # ถ้ามีโพซิชันอยู่แล้ว
                ticker = exchange.fetch_ticker(SYMBOL)
                monitor_position(current_pos_info, ticker['last'])
            
            time.sleep(MAIN_LOOP_SLEEP_SECONDS)
        except Exception as e:
            logger.error(f"⛔️ Error in trading loop: {e}", exc_info=True)
            time.sleep(ERROR_RETRY_SLEEP_SECONDS)
    logger.info("🛑 Trading thread has been stopped.")

# ==============================================================================
# 13. ฟังก์ชันสำหรับคำสั่ง TELEGRAM (TELEGRAM COMMAND HANDLERS)
# ==============================================================================
def get_summary_text() -> str:
    """สร้างข้อความสรุปสถิติ."""
    try:
        current_date_str = datetime.now().strftime('%Y-%m-%d')
        if daily_stats.get('date') != current_date_str:
            reset_daily_stats()
        
        balance = get_portfolio_balance()
        pnl_from_start = balance - initial_balance if initial_balance > 0 else 0.0
        
        status_text = "ทำงานอยู่ ✅" if trading_active.is_set() else "หยุดอยู่ 🛑"
        
        summary = f"""📊 <b>สรุปผล ณ วันที่ {datetime.now().strftime('%d/%m/%Y %H:%M')}</b>
<b>สถานะบอท:</b> <b>{status_text}</b>
<b>คู่เทรด:</b> <code>{SYMBOL}</code> | <b>TF:</b> <code>{TIMEFRAME}</code>

<b>--- สถิติวันนี้ ---</b>
<b>กำไร/ขาดทุน:</b> <code>{daily_stats['total_pnl']:+,.2f} USDT</code>
<b>TP:</b> <code>{daily_stats['tp_count']}</code> | <b>SL:</b> <code>{daily_stats['sl_count']}</code>

<b>--- ภาพรวมพอร์ต ---</b>
<b>คงเหลือปัจจุบัน:</b> <code>{balance:,.2f} USDT</code>
<b>กำไร/ขาดทุนสะสม:</b> <code>{pnl_from_start:+,.2f} USDT</code>
"""
        return summary
    except Exception as e:
        logger.error(f"❌ Error creating summary: {e}")
        return "⛔️ ไม่สามารถสร้างสรุปได้"

def start_command(update: Update, context: CallbackContext):
    global trading_thread, initial_balance
    if not trading_active.is_set():
        trading_active.set()
        if initial_balance == 0.0:
            initial_balance = get_portfolio_balance()
        if trading_thread is None or not trading_thread.is_alive():
            trading_thread = threading.Thread(target=trading_logic_loop, daemon=True)
            trading_thread.start()
        msg = "✅ <b>บอทเริ่มทำงานแล้ว</b>"
        logger.info("Bot started via /start.")
    else:
        msg = "ℹ️ บอททำงานอยู่แล้ว"
    update.message.reply_html(msg)

def stop_command(update: Update, context: CallbackContext):
    if trading_active.is_set():
        trading_active.clear()
        msg = "🛑 <b>บอทหยุดทำงานแล้ว</b> (จะไม่เปิดออเดอร์ใหม่)"
        logger.info("Bot stopped via /stop.")
    else:
        msg = "ℹ️ บอทหยุดทำงานอยู่แล้ว"
    update.message.reply_html(msg)

def summary_command(update: Update, context: CallbackContext):
    logger.info("Received /summary command.")
    summary_message = get_summary_text()
    update.message.reply_html(summary_message)

def help_command(update: Update, context: CallbackContext):
    help_text = """<b>คำสั่งที่ใช้ได้:</b>
/start - เริ่มการทำงานของบอท
/stop - หยุดการทำงานของบอท
/summary - ขอดูสรุปผล
/help - แสดงข้อความช่วยเหลือ"""
    update.message.reply_html(help_text)

# ==============================================================================
# 14. จุดเริ่มต้นการทำงานของโปรแกรม (ENTRY POINT)
# ==============================================================================
def main():
    global initial_balance
    load_daily_stats()
    
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start_command))
    dispatcher.add_handler(CommandHandler("stop", stop_command))
    dispatcher.add_handler(CommandHandler("summary", summary_command))
    dispatcher.add_handler(CommandHandler("help", help_command))

    logger.info("🤖 Bot is live, waiting for commands...")
    send_telegram("🤖 <b>บอทออนไลน์แล้ว</b>\nพิมพ์ /start เพื่อเริ่มการเทรด\nพิมพ์ /help เพื่อดูคำสั่ง")

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
