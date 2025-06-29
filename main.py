import ccxt
import time
import requests
from datetime import datetime, timedelta
import logging
import threading
import json
import os
import asyncio # เพิ่ม asyncio สำหรับไลบรารีเวอร์ชันใหม่

# === เพิ่ม Library ใหม่เวอร์ชันล่าสุด ===
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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
PORTFOLIO_PERCENT_TRADE = 0.7
CROSS_THRESHOLD_POINTS = 35

# --- Telegram Notification Settings ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# --- Files & Paths ---
STATS_FILE = 'trading_stats.json'

# --- Bot Timing ---
MAIN_LOOP_SLEEP_SECONDS = 60
ERROR_RETRY_SLEEP_SECONDS = 60

# --- Tolerance ---
TP_SL_BE_PRICE_TOLERANCE_PERCENT = 0.005

# (ส่วนที่ 2, 3, 4, 5, 6, 7, 8, 9, 10, 11 ใช้โค้ดเดิมที่คุณส่งมาได้เลยครับ)
# (ผมจะคัดลอกมาให้เพื่อความสมบูรณ์ แต่ส่วนนี้ไม่มีการเปลี่ยนแปลง)

# ==============================================================================
# 2. การตั้งค่า Logging
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
trading_active = threading.Event()
trading_thread = None
current_position = None
entry_price = None
sl_moved = False
portfolio_balance = 0.0
initial_balance = 0.0
current_position_size = 0.0

# ==============================================================================
# 4. โครงสร้างข้อมูลสถิติ (STATISTICS DATA STRUCTURE)
# ==============================================================================
daily_stats = {
    'date': None, 'tp_count': 0, 'sl_count': 0, 'total_pnl': 0.0,
    'trades': [], 'last_report_date': None, 'last_ema_cross_signal': None
}

# ==============================================================================
# 5. การตั้งค่า Exchange (CCXT EXCHANGE SETUP)
# ==============================================================================
try:
    if not API_KEY or API_KEY == 'YOUR_API_KEY_HERE_FOR_LOCAL_TESTING' or \
       not SECRET or SECRET == 'YOUR_SECRET_HERE_FOR_LOCAL_TESTING' or \
       not PASSWORD or PASSWORD == 'YOUR_PASSWORD_HERE_FOR_LOCAL_TESTING':
        raise ValueError("API Keys not set.")
    exchange = ccxt.okx({
        'apiKey': API_KEY, 'secret': SECRET, 'password': PASSWORD,
        'enableRateLimit': True, 'rateLimit': 1000,
        'options': {'defaultType': 'swap'}
    })
    exchange.set_sandbox_mode(False)
    logger.info("✅ OKX Exchange Connected")
except Exception as e:
    logger.critical(f"❌ Could not connect to OKX: {e}")
    exit()

# ==============================================================================
# 6. ฟังก์ชันจัดการสถิติ (STATISTICS MANAGEMENT FUNCTIONS)
# ==============================================================================
def save_daily_stats():
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump(daily_stats, f, indent=4)
    except Exception as e:
        logger.error(f"❌ Error saving stats: {e}")

def load_daily_stats():
    global daily_stats
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, 'r') as f:
                daily_stats = json.load(f)
            logger.info(f"💾 Stats loaded from {STATS_FILE}")
            current_date_str = datetime.now().strftime('%Y-%m-%d')
            if daily_stats.get('date') != current_date_str:
                reset_daily_stats()
        else:
            reset_daily_stats()
    except Exception:
        reset_daily_stats()

def reset_daily_stats():
    global daily_stats
    current_date_str = datetime.now().strftime('%Y-%m-%d')
    last_signal = daily_stats.get('last_ema_cross_signal')
    daily_stats = {
        'date': current_date_str, 'tp_count': 0, 'sl_count': 0, 'total_pnl': 0.0,
        'trades': [], 'last_report_date': daily_stats.get('last_report_date'),
        'last_ema_cross_signal': last_signal
    }
    save_daily_stats()
    logger.info(f"🔄 Daily stats reset for {current_date_str}")

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
# 7. ฟังก์ชันแจ้งเตือน Telegram (TELEGRAM NOTIFICATION FUNCTIONS)
# ==============================================================================
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        params = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
        requests.get(url, params=params, timeout=10).raise_for_status()
        logger.info(f"✉️ Telegram notification sent.")
    except Exception as e:
        logger.error(f"⛔️ Error sending Telegram: {e}")

# ==============================================================================
# 8. ฟังก์ชันดึงข้อมูล Exchange (EXCHANGE DATA RETRIEVAL FUNCTIONS)
# ==============================================================================
# ... (All functions from get_portfolio_balance to move_sl_to_breakeven are the same as the user provided) ...
# ... I am omitting them here for brevity but they should be in the final file. ...
# ... The user should copy their existing functions from Section 8, 9, 10, 11 into the final file ...

# (ฟังก์ชันตั้งแต่ 8-11 ของเดิมที่คุณส่งมาให้ สามารถใช้ได้เลย ไม่ต้องแก้ไข)
# (ผมจะข้ามไปส่วนที่แก้ไขเลยนะครับ)
def get_portfolio_balance():
    # ... Your existing code ...
    pass
def get_current_position():
    # ... Your existing code ...
    pass
def calculate_ema(prices: list[float], period: int):
    # ... Your existing code ...
    pass
def check_ema_cross():
    # ... Your existing code ...
    pass
def open_market_order(direction: str, current_price: float):
    # ... Your existing code ...
    pass
def set_tpsl_for_position(pos_direction: str, entry_price_val: float):
    # ... Your existing code ...
    pass
def move_sl_to_breakeven(pos_direction: str, entry_price_val: float):
    # ... Your existing code ...
    pass
def monitor_position(pos_info: dict | None, current_price: float):
    # ... Your existing code ...
    pass

# ==============================================================================
# 12. ฟังก์ชันตรรกะการเทรดหลัก (MAIN TRADING LOGIC) - (มีการแก้ไขเล็กน้อย)
# ==============================================================================
def trading_logic_loop():
    logger.info("🚀 Trading thread started. Waiting for signal...")
    while trading_active.is_set():
        try:
            current_pos_info = get_current_position()
            time.sleep(1)
            if not current_pos_info:
                signal = check_ema_cross()
                if signal and signal != daily_stats.get('last_ema_cross_signal'):
                    logger.info(f"✨ New valid signal {signal.upper()}. Attempting to open order.")
                    ticker = exchange.fetch_ticker(SYMBOL)
                    if ticker and 'last' in ticker:
                        market_order_success, confirmed_entry_price = open_market_order(signal, ticker['last'])
                        if market_order_success and confirmed_entry_price:
                            set_tpsl_success = set_tpsl_for_position(signal, confirmed_entry_price)
                            if set_tpsl_success:
                                daily_stats['last_ema_cross_signal'] = signal
                                save_daily_stats()
                            else:
                                send_telegram("⛔️ <b>FATAL ERROR:</b> Order opened but FAILED to set TP/SL!")
                    else:
                        logger.warning("⚠️ Could not fetch ticker price to open order.")
            else:
                ticker = exchange.fetch_ticker(SYMBOL)
                if ticker and 'last' in ticker:
                    monitor_position(current_pos_info, ticker['last'])
            
            time.sleep(MAIN_LOOP_SLEEP_SECONDS)
        except Exception as e:
            logger.error(f"⛔️ Error in trading loop: {e}", exc_info=True)
            time.sleep(ERROR_RETRY_SLEEP_SECONDS)
    logger.info("🛑 Trading thread has been stopped.")

# ==============================================================================
# 13. ฟังก์ชันสำหรับคำสั่ง TELEGRAM (TELEGRAM COMMAND HANDLERS) - (เขียนใหม่ทั้งหมด)
# ==============================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_html(msg)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if trading_active.is_set():
        trading_active.clear()
        msg = "🛑 <b>บอทหยุดทำงานแล้ว</b> (จะไม่เปิดออเดอร์ใหม่)"
        logger.info("Bot stopped via /stop.")
    else:
        msg = "ℹ️ บอทหยุดทำงานอยู่แล้ว"
    await update.message.reply_html(msg)

def get_summary_text() -> str:
    # (ฟังก์ชันนี้คัดลอกมาจากโพสต์ก่อนหน้าได้เลย)
    try:
        balance = get_portfolio_balance()
        pnl_from_start = balance - initial_balance if initial_balance > 0 else 0.0
        status_text = "ทำงานอยู่ ✅" if trading_active.is_set() else "หยุดอยู่ 🛑"
        return f"""📊 <b>สรุปผล ณ วันที่ {datetime.now().strftime('%d/%m/%Y %H:%M')}</b>
<b>สถานะบอท:</b> <b>{status_text}</b>
<b>--- สถิติวันนี้ ---</b>
<b>กำไร/ขาดทุน:</b> <code>{daily_stats['total_pnl']:+,.2f} USDT</code>
<b>TP:</b> <code>{daily_stats['tp_count']}</code> | <b>SL:</b> <code>{daily_stats['sl_count']}</code>
<b>--- ภาพรวมพอร์ต ---</b>
<b>คงเหลือปัจจุบัน:</b> <code>{balance:,.2f} USDT</code>
<b>กำไร/ขาดทุนสะสม:</b> <code>{pnl_from_start:+,.2f} USDT</code>"""
    except Exception as e:
        logger.error(f"❌ Error creating summary: {e}")
        return "⛔️ ไม่สามารถสร้างสรุปได้"

async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Received /summary command.")
    summary_message = get_summary_text()
    await update.message.reply_html(summary_message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """<b>คำสั่งที่ใช้ได้:</b>
/start - เริ่มการทำงานของบอท
/stop - หยุดการทำงานของบอท
/summary - ขอดูสรุปผล
/help - แสดงข้อความช่วยเหลือ"""
    await update.message.reply_html(help_text)

# ==============================================================================
# 14. จุดเริ่มต้นการทำงานของโปรแกรม (ENTRY POINT) - (เขียนใหม่ทั้งหมด)
# ==============================================================================
async def post_init(application: Application):
    """ฟังก์ชันที่ทำงานหลังบอทเชื่อมต่อสำเร็จ"""
    await application.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="🤖 <b>บอทออนไลน์แล้ว</b>\nพิมพ์ /start เพื่อเริ่มการเทรด\nพิมพ์ /help เพื่อดูคำสั่ง",
        parse_mode='HTML'
    )

def main():
    """ฟังก์ชันหลักที่รอรับคำสั่งจาก Telegram (เวอร์ชันใหม่)."""
    load_daily_stats()
    
    # ตั้งค่า Application
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # ผูกคำสั่งกับฟังก์ชัน
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("summary", summary_command))
    application.add_handler(CommandHandler("help", help_command))

    # เริ่มรอรับคำสั่งจาก Telegram
    logger.info("🤖 Bot is live, using PTB v20+ syntax, waiting for commands...")
    application.run_polling()

if __name__ == '__main__':
    # สำหรับโค้ดเวอร์ชันใหม่ เราไม่จำเป็นต้องใช้ asyncio.run() โดยตรง
    # แต่การเรียก main() แบบนี้จะทำงานได้ถูกต้อง
    main()
