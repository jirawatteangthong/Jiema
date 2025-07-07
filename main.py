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
# ✅ ใช้ชื่อ Env Vars ที่เราตกลงกันสำหรับ Binance
API_KEY_ENV = 'BINANCE_API_KEY' 
SECRET_ENV = 'BINANCE_SECRET'
# Binance Futures ไม่มี Passphrase
# PASSWORD_ENV = 'RAILWAY_PASSWORD' 

# --- Trading Parameters ---
SYMBOL = 'BTC/USDT' # ✅ สำหรับ Binance Futures
TIMEFRAME = '15m'
LEVERAGE = 30
TP_VALUE_POINTS = 501 # ✅ ตามโค้ดแนวคิด (สำหรับ BTC)
SL_VALUE_POINTS = 999 # ✅ ตามโค้ดแนวคิด (สำหรับ BTC)
BE_PROFIT_TRIGGER_POINTS = 350 # ✅ ตามโค้ดแนวคิด
BE_SL_BUFFER_POINTS = 100 # ✅ ตามโค้ดแนวคิด
CONTRACTS_PER_SLOT = 40 # จำนวนสัญญาต่อ "หนึ่งไม้" (1 contract = 1 USD สำหรับ BTC/USDT-SWAP)
CROSS_THRESHOLD_POINTS = 5 # จำนวนจุดที่ EMA ต้องห่างกันเพื่อยืนยันสัญญาณ

# เพิ่มค่าตั้งค่าใหม่สำหรับการบริหารความเสี่ยงและออเดอร์
MIN_BALANCE_SAFETY_MARGIN = 50  # ยอดคงเหลือขั้นต่ำที่ต้องเหลือไว้ (USDT)
MAX_POSITION_SIZE_LIMIT = 1000  # จำกัดขนาดโพซิชันสูงสุด (contracts)
REQUIRED_MARGIN_BUFFER_PERCENT = 0.10 # 10% ของ Margin ที่ต้องการ (เผื่อไว้สำหรับค่าธรรมเนียมและ Margin แฝง)

# ค่าสำหรับยืนยันโพซิชันหลังเปิดออเดอร์ (ใช้ใน confirm_position_entry)
CONFIRMATION_RETRIES = 15  # จำนวนครั้งที่ลองยืนยันโพซิชัน
CONFIRMATION_SLEEP = 3  # วินาทีที่รอระหว่างการยืนยัน

# --- Telegram Notification Settings ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# --- Files & Paths ---
# ✅ ถ้าใช้ Railway Volume, ควรจะเป็น '/data/trading_stats.json'
STATS_FILE = os.getenv('STATS_FILE_PATH', 'trading_stats.json') 

# --- Bot Timing ---
MAIN_LOOP_SLEEP_SECONDS = 300 # 6 นาที
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
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(STATS_FILE), 'bot.log'), encoding='utf-8'), # ✅ ให้ log file อยู่ในโฟลเดอร์เดียวกันกับ stats file
        logging.StreamHandler(sys.stdout)
    ]
)
for handler in logging.root.handlers:
    if hasattr(handler, 'flush'):
        handler.flush = lambda: sys.stdout.flush() if isinstance(handler, logging.StreamHandler) else handler.stream.flush()

logger = logging.getLogger(__name__)

# ==============================================================================
# 3. ตัวแปรสถานะการเทรด (GLOBAL TRADE STATE VARIABLES - จะถูกย้ายเข้า Class)
# ==============================================================================
# current_position = None # จะเป็น self.current_position
# entry_price = None # จะเป็น self.entry_price
# sl_moved = False # จะเป็น self.sl_moved
# portfolio_balance = 0.0 # จะเป็น self.portfolio_balance
# last_monthly_report_date = None # จะเป็น self.last_monthly_report_date
# initial_balance = 0.0 # จะเป็น self.initial_balance
# current_position_size = 0.0 # จะเป็น self.current_position_size
# last_ema_position_status = None # จะเป็น self.last_ema_position_status

# ==============================================================================
# 4. โครงสร้างข้อมูลสถิติ (STATISTICS DATA STRUCTURE - จะถูกรวมเข้า Class)
# ==============================================================================
# monthly_stats = {...} # จะเป็น self.monthly_stats

# ==============================================================================
# 5. การตั้งค่า Exchange (CCXT EXCHANGE SETUP - จะถูกรวมเข้า Class __init__)
# ==============================================================================
# try: ... except ... (จะย้ายไปใน __init__)

# ==============================================================================
# 6. คลาสบอทเทรด (BinanceTradingBot Class)
# ==============================================================================

class BinanceTradingBot:
    def __init__(self):
        # --- API Keys & Credentials ---
        self.api_key = os.getenv(API_KEY_ENV)
        self.secret = os.getenv(SECRET_ENV)
        # self.passphrase = os.getenv(PASSWORD_ENV) # Binance ไม่มี Passphrase

        # --- Validate API Keys ---
        if not all([self.api_key, self.secret]):
            logger.critical(f"❌ Configuration Error: {API_KEY_ENV} หรือ {SECRET_ENV} ไม่ถูกตั้งค่าใน Environment Variables.")
            sys.exit(1) # ออกจากโปรแกรมทันที

        # --- Initialize Exchange ---
        try:
            self.exchange = ccxt.binance({ # ✅ ใช้ Binance
                'apiKey': self.api_key,
                'secret': self.secret,
                # 'password': self.passphrase, # ✅ ลบออก
                'enableRateLimit': True,
                'rateLimit': 1000,
                'options': {
                    'defaultType': 'future', # ✅ สำหรับ Binance Futures
                    'marginMode': 'cross', # ✅ Cross Margin
                    'warnOnFetchOHLCVLimitArgument': False,
                    'adjustForTimeDifference': True,
                    # 'loadMarkets': False, # ✅ ไม่ต้องใช้ เพราะเรา load_markets() เอง
                    # 'loadInstruments': False, # ✅ ไม่ต้องใช้
                    # 'loadCurrencies': False, # ✅ ไม่ต้องใช้
                },
                'verbose': False, 
                'timeout': 30000,
            })
            # self.exchange.set_sandbox_mode(False) # ✅ ไม่จำเป็นสำหรับ Binance โดยตรง (ตั้งใน init options)
            
            # ✅ โหลด markets ทันทีหลัง init
            self.exchange.load_markets()
            logger.info("✅ เชื่อมต่อกับ Binance Exchange และโหลด markets สำเร็จ")

        except Exception as e:
            logger.critical(f"❌ ไม่สามารถเชื่อมต่อหรือโหลดข้อมูล Exchange เบื้องต้นได้: {e}", exc_info=True)
            sys.exit(1) # ออกจากโปรแกรมทันที

        # --- Trading Parameters ---
        self.symbol = SYMBOL 
        self.timeframe = TIMEFRAME
        self.leverage = LEVERAGE
        self.tp_value_points = TP_VALUE_POINTS # ✅ ค่าจากโค้ดแนวคิด
        self.sl_value_points = SL_VALUE_POINTS # ✅ ค่าจากโค้ดแนวคิด
        self.be_profit_trigger_points = BE_PROFIT_TRIGGER_POINTS # ✅ ค่าจากโค้ดแนวคิด
        self.be_sl_buffer_points = BE_SL_BUFFER_POINTS # ✅ ค่าจากโค้ดแนวคิด
        self.contracts_per_slot = CONTRACTS_PER_SLOT # ✅ ค่าจากโค้ดแนวคิด
        self.cross_threshold_points = CROSS_THRESHOLD_POINTS # ✅ ค่าจากโค้ดแนวคิด

        self.min_balance_safety_margin = MIN_BALANCE_SAFETY_MARGIN
        self.max_position_size_limit = MAX_POSITION_SIZE_LIMIT
        self.required_margin_buffer_percent = REQUIRED_MARGIN_BUFFER_PERCENT

        self.confirmation_retries = CONFIRMATION_RETRIES
        self.confirmation_sleep = CONFIRMATION_SLEEP

        self.telegram_token = TELEGRAM_TOKEN
        self.telegram_chat_id = TELEGRAM_CHAT_ID
        self.stats_file = STATS_FILE # ใช้ STATS_FILE จาก global config

        self.main_loop_sleep_seconds = MAIN_LOOP_SLEEP_SECONDS
        self.error_retry_sleep_seconds = ERROR_RETRY_SLEEP_SECONDS
        self.monthly_report_day = MONTHLY_REPORT_DAY
        self.monthly_report_hour = MONTHLY_REPORT_HOUR
        self.monthly_report_minute = MONTHLY_REPORT_MINUTE
        self.tp_sl_be_price_tolerance_percent = TP_SL_BE_PRICE_TOLERANCE_PERCENT

        # --- สถานะการเทรด (ใน Class Instance) ---
        self.current_position = None # dict {'side', 'size', 'entry_price', ...}
        self.entry_price = None
        self.sl_moved = False # ใช้สำหรับ Breakeven SL
        self.portfolio_balance = 0.0 # อัปเดตล่าสุด
        self.initial_balance = 0.0 # ยอดเริ่มต้นตอนรันบอท
        self.current_position_size = 0.0 # ขนาดโพซิชันในหน่วย Contracts
        self.last_ema_position_status = None # 'above', 'below', หรือ None (เมื่อเริ่มต้น)

        # --- สถิติ (ใน Class Instance) ---
        self.monthly_stats = {
            'month_year': None,
            'tp_count': 0,
            'sl_count': 0,
            'total_pnl': 0.0,
            'trades': [],
            'last_report_month_year': None,
            'last_ema_cross_signal': None, 
            'last_ema_position_status': None 
        }
        self.last_monthly_report_date = None # เพื่อเก็บวันที่ส่งรายงานล่าสุด

        # --- กำหนด Step Size ของ Amount ด้วยตัวเอง (จากที่ทดลองกับ Binance) ---
        # สำหรับ BTC/USDT Futures บน Binance, step size คือ 0.001 BTC
        self.forced_amount_step_size = 0.001 

        # ✅ กำหนด Factor สำหรับการเปิดออเดอร์ Max (ใช้ 95%)
        self.target_position_size_factor = 0.95

    # ==============================================================================
    # 6. ฟังก์ชันจัดการสถิติ (STATISTICS MANAGEMENT FUNCTIONS)
    # ==============================================================================

    def save_monthly_stats(self):
        """บันทึกสถิติการเทรดประจำเดือนลงในไฟล์ JSON."""
        try:
            self.monthly_stats['last_ema_position_status'] = self.last_ema_position_status
            with open(self.stats_file, 'w') as f:
                json.dump(self.monthly_stats, f, indent=4)
            logger.debug(f"💾 บันทึกสถิติการเทรดลงไฟล์ {self.stats_file} สำเร็จ")
        except Exception as e:
            logger.error(f"❌ เกิดข้อผิดพลาดในการบันทิติสถิติ: {e}")

    def reset_monthly_stats(self):
        """รีเซ็ตสถิติประจำเดือนสำหรับเดือนใหม่."""
        self.monthly_stats['month_year'] = datetime.now().strftime('%Y-%m')
        self.monthly_stats['tp_count'] = 0
        self.monthly_stats['sl_count'] = 0
        self.monthly_stats['total_pnl'] = 0.0
        self.monthly_stats['trades'] = []
        self.last_ema_position_status = None 
        self.save_monthly_stats() 
        logger.info(f"🔄 รีเซ็ตสถิติประจำเดือนสำหรับเดือน {self.monthly_stats['month_year']}")

    def load_monthly_stats(self):
        """โหลดสถิติการเทรดประจำเดือนจากไฟล์ JSON."""
        try:
            if os.path.exists(self.stats_file):
                with open(self.stats_file, 'r') as f:
                    loaded_stats = json.load(f)

                    self.monthly_stats['month_year'] = loaded_stats.get('month_year', None)
                    self.monthly_stats['tp_count'] = loaded_stats.get('tp_count', 0)
                    self.monthly_stats['sl_count'] = loaded_stats.get('sl_count', 0)
                    self.monthly_stats['total_pnl'] = loaded_stats.get('total_pnl', 0.0)
                    self.monthly_stats['trades'] = loaded_stats.get('trades', [])
                    self.monthly_stats['last_report_month_year'] = loaded_stats.get('last_report_month_year', None)
                    self.monthly_stats['last_ema_cross_signal'] = loaded_stats.get('last_ema_cross_signal', None)
                    self.last_ema_position_status = loaded_stats.get('last_ema_position_status', None)

                logger.info(f"💾 โหลดสถิติการเทรดจากไฟล์ {self.stats_file} สำเร็จ")

                if self.monthly_stats['last_report_month_year']:
                    try:
                        year, month = map(int, self.monthly_stats['last_report_month_year'].split('-'))
                        self.last_monthly_report_date = datetime(year, month, 1).date()
                    except ValueError:
                        logger.warning("⚠️ รูปแบบวันที่ last_report_report_month_year ในไฟล์ไม่ถูกต้อง. จะถือว่ายังไม่มีการส่งรายงาน.")
                        self.last_monthly_report_date = None
                else:
                    self.last_monthly_report_date = None

                current_month_year_str = datetime.now().strftime('%Y-%m')
                if self.monthly_stats['month_year'] != current_month_year_str:
                    logger.info(f"ℹ️ สถิติที่โหลดมาเป็นของเดือน {self.monthly_stats['month_year']} ไม่ตรงกับเดือนนี้ {current_month_year_str}. จะรีเซ็ตสถิติสำหรับเดือนใหม่.")
                    self.reset_monthly_stats()

            else:
                logger.info(f"🆕 ไม่พบไฟล์สถิติ {self.stats_file} สร้างไฟล์ใหม่")
                self.reset_monthly_stats()

        except Exception as e:
            logger.error(f"❌ เกิดข้อผิดพลาดในการโหลดสถิติ: {e}", exc_info=True)
            self.monthly_stats = {
                'month_year': None, 'tp_count': 0, 'sl_count': 0, 'total_pnl': 0.0, 'trades': [],
                'last_report_month_year': None, 'last_ema_cross_signal': None, 'last_ema_position_status': None
            }
            self.last_monthly_report_date = None
            self.last_ema_position_status = None
            self.reset_monthly_stats()

    def add_trade_result(self, reason: str, pnl: float):
        """เพิ่มผลการเทรดลงในสถิติประจำเดือน."""
        current_month_year_str = datetime.now().strftime('%Y-%m')

        if self.monthly_stats['month_year'] != current_month_year_str:
            logger.info(f"🆕 เดือนเปลี่ยนใน add_trade_result: {self.monthly_stats['month_year']} -> {current_month_year_str}. กำลังรีเซ็ตสถิติประจำเดือน.")
            self.reset_monthly_stats()

        if reason.upper() == 'TP':
            self.monthly_stats['tp_count'] += 1
        elif reason.upper() == 'SL' or reason.upper() == 'SL (กันทุน)':
            self.monthly_stats['sl_count'] += 1

        self.monthly_stats['total_pnl'] += pnl

        self.monthly_stats['trades'].append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'reason': reason,
            'pnl': pnl
        })
        self.save_monthly_stats()

    # ==============================================================================
    # 7. ฟังก์ชันแจ้งเตือน Telegram (TELEGRAM NOTIFICATION FUNCTIONS)
    # ==============================================================================

    def send_telegram(self, msg: str):
        """ส่งข้อความไปยัง Telegram."""
        if not self.telegram_token or self.telegram_token == 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING' or \
           not self.telegram_chat_id or self.telegram_chat_id == 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING':
            logger.warning("⚠️ TELEGRAM_TOKEN หรือ TELEGRAM_CHAT_ID ไม่ได้ถูกตั้งค่า. ไม่สามารถส่งข้อความ Telegram ได้.")
            return

        try:
            url = f'https://api.telegram.org/bot{self.telegram_token}/sendMessage'
            params = {'chat_id': self.telegram_chat_id, 'text': msg, 'parse_mode': 'HTML'}
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

    def get_portfolio_balance(self) -> float:
        """ดึงยอดคงเหลือ USDT ในพอร์ต (เฉพาะบัญชี Trading/Swap)."""
        retries = 3
        for i in range(retries):
            try:
                logger.debug(f"🔍 กำลังดึงยอดคงเหลือ (Attempt {i+1}/{retries})...")
                # ✅ ใช้ self.exchange
                balance_data = self.exchange.fetch_balance(params={'type': 'future'}) # ✅ สำหรับ Binance Futures
                time.sleep(2)

                # ✅ ปรับการดึง balance สำหรับ Binance Futures (USDT free)
                usdt_balance = float(balance_data.get('USDT', {}).get('free', 0.0))
                if usdt_balance > 0:
                    self.portfolio_balance = usdt_balance
                    logger.info(f"💰 ยอดคงเหลือ USDT (Trading Account): {usdt_balance:,.2f}")
                    return usdt_balance
                else:
                    # ✅ Fallback สำหรับ Binance ถ้า free ไม่ตรง
                    for asset_info in balance_data.get('info', {}).get('assets', []):
                        if asset_info.get('asset') == 'USDT':
                            usdt_balance = float(asset_info.get('availableBalance', 0))
                            self.portfolio_balance = usdt_balance
                            logger.info(f"💰 ยอดคงเหลือ USDT (Trading Account via info.assets): {usdt_balance:,.2f}")
                            return usdt_balance
                    
                    logger.warning("⚠️ ไม่พบ 'free' balance ใน USDT หรือใน info.assets.")
                    self.portfolio_balance = 0.0 # ไม่พบยอดที่ถูกต้อง
                    return 0.0

            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                logger.warning(f"⚠️ Error fetching balance (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
                if i == retries - 1:
                    self.send_telegram(f"⛔️ API Error: ไม่สามารถดึงยอดคงเหลือได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
                time.sleep(15)
            except Exception as e:
                logger.error(f"❌ Unexpected error in get_portfolio_balance: {e}", exc_info=True)
                self.send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึงยอดคงเหลือได้\nรายละเอียด: {e}")
                return 0.0
        logger.error(f"❌ Failed to fetch balance after {retries} attempts.")
        self.send_telegram(f"⛔️ API Error: ล้มเหลวในการดึงยอดคงเหลือหลังจาก {retries} ครั้ง.")
        return 0.0

    def get_current_position(self) -> dict | None:
        """ตรวจสอบและดึงข้อมูลโพซิชัน SYMBOL ปัจจุบัน."""
        retries = 3
        for i in range(retries):
            try:
                logger.debug(f"🔍 กำลังดึงโพซิชันปัจจุบัน (Attempt {i+1}/{retries})...")
                # ✅ ใช้ self.exchange และ self.symbol
                positions = self.exchange.fetch_positions([self.symbol])
                logger.debug(f"DEBUG: Fetched positions raw: {positions}") 
                time.sleep(2)
                for pos in positions:
                    if float(pos.get('contracts', 0)) != 0: # ✅ ใช้ 'contracts' สำหรับปริมาณ (Binance)
                        return {
                            'side': pos['side'],
                            'size': float(pos['contracts']), # ✅ ใช้ 'contracts' เป็นจำนวน Contracts
                            'entry_price': float(pos['entryPrice']),
                            'unrealized_pnl': float(pos['unrealizedPnl']),
                            'pos_id': pos.get('id', 'N/A')
                        }
                return None
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                logger.warning(f"⚠️ Error fetching positions (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
                if i == retries - 1:
                    self.send_telegram(f"⛔️ API Error: ไม่สามารถดึงโพซิชันได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
                time.sleep(15)
            except Exception as e:
                logger.error(f"❌ Unexpected error in get_current_position: {e}", exc_info=True)
                self.send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึงโพซิชันได้\nรายละเอียด: {e}")
                return None # ควร return None แทน 0.0 ถ้าไม่มี position
        logger.error(f"❌ Failed to fetch positions after {retries} attempts.")
        self.send_telegram(f"⛔️ API Error: ล้มเหลวในการดึงโพซิชันหลังจาก {retries} ครั้ง.")
        return None

    # ==============================================================================
    # 9. ฟังก์ชันคำนวณ Indicators (INDICATOR CALCULATION FUNCTIONS)
    # ==============================================================================

    def calculate_ema(self, prices: list[float], period: int) -> float | None:
        """คำนวณ Exponential Moving Average (EMA)."""
        if len(prices) < period:
            return None

        sma = sum(prices[:period]) / period
        ema = sma
        multiplier = 2 / (period + 1)

        for price in prices[period:]:
            ema = (price * multiplier) + (ema * (1 - multiplier))

        return ema

    def check_ema_cross(self) -> str | None:
        """
        ตรวจสอบการตัดกันของ EMA50 และ EMA200 โดยใช้ Threshold เพื่อยืนยัน.
        จะเปิดออเดอร์เมื่อ EMA50 "เปลี่ยนสถานะ" จากเหนือ/ใต้ เป็นใต้/เหนือ และห่างเกิน Threshold.
        ถ้าบอทเพิ่งเริ่มรัน (last_ema_position_status เป็น None) จะบันทึกสถานะปัจจุบันไว้ก่อน ไม่ส่งสัญญาณ.
        """
        try:
            retries = 3
            ohlcv = None
            for i in range(retries):
                logger.debug(f"🔍 กำลังดึงข้อมูล OHLCV สำหรับ EMA ({i+1}/{retries})...")
                try:
                    # ✅ ใช้ self.exchange และ self.symbol และ self.timeframe
                    ohlcv = self.exchange.fetch_ohlcv(self.symbol, self.timeframe, limit=250)
                    time.sleep(2)
                    break
                except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                    logger.warning(f"⚠️ Error fetching OHLCV (Attempt {i+1}/{retries}): {e}. Retrying in 15 seconds...")
                    if i == retries - 1:
                        self.send_telegram(f"⛔️ API Error: ไม่สามารถดึง OHLCV ได้ (Attempt {i+1}/{retries})\nรายละเอียด: {e}")
                    time.sleep(15)
                except Exception as e:
                    logger.error(f"❌ Unexpected error fetching OHLCV: {e}", exc_info=True)
                    self.send_telegram(f"⛔️ Unexpected Error: ไม่สามารถดึง OHLCV ได้\nรายละเอียด: {e}")
                    return None

            if not ohlcv:
                logger.error(f"❌ Failed to fetch OHLCV after {retries} attempts.")
                self.send_telegram(f"⛔️ API Error: ล้มเหลวในการดึง OHLCV หลังจาก {retries} ครั้ง.")
                return None

            if len(ohlcv) < 202: 
                logger.warning(f"ข้อมูล OHLCV ไม่เพียงพอ. ต้องการอย่างน้อย 202 แท่ง ได้ {len(ohlcv)}")
                self.send_telegram(f"⚠️ ข้อมูล OHLCV ไม่เพียงพอ ({len(ohlcv)} แท่ง).")
                return None

            closes = [candle[4] for candle in ohlcv]

            ema50_current = self.calculate_ema(closes, 50) # ✅ ใช้ self.calculate_ema
            ema200_current = self.calculate_ema(closes, 200) # ✅ ใช้ self.calculate_ema

            logger.info(f"💡 EMA Values: Current EMA50={ema50_current:.2f}, EMA200={ema200_current:.2f}") 
            
            if None in [ema50_current, ema200_current]:
                logger.warning("ค่า EMA ไม่สามารถคำนวณได้ (เป็น None).")
                return None

            current_ema_position = None
            if ema50_current > ema200_current:
                current_ema_position = 'above'
            elif ema50_current < ema200_current:
                current_ema_position = 'below'
            
            if self.last_ema_position_status is None: # ✅ ใช้ self.last_ema_position_status
                if current_ema_position:
                    self.last_ema_position_status = current_ema_position
                    self.save_monthly_stats() # ✅ ใช้ self.save_monthly_stats
                    logger.info(f"ℹ️ บอทเพิ่งเริ่มรัน. บันทึกสถานะ EMA ปัจจุบันเป็น: {current_ema_position.upper()}. จะรอสัญญาณการตัดกันครั้งถัดไป.")
                return None

            cross_signal = None

            # Golden Cross (Long)
            if self.last_ema_position_status == 'below' and current_ema_position == 'above' and \
               ema50_current > (ema200_current + self.cross_threshold_points): # ✅ ใช้ self.cross_threshold_points
                cross_signal = 'long'
                logger.info(f"🚀 Threshold Golden Cross: EMA50({ema50_current:.2f}) is {self.cross_threshold_points} points above EMA200({ema200_current:.2f})")

            # Death Cross (Short)
            elif self.last_ema_position_status == 'above' and current_ema_position == 'below' and \
                 ema50_current < (ema200_current - self.cross_threshold_points): # ✅ ใช้ self.cross_threshold_points
                cross_signal = 'short'
                logger.info(f"🔻 Threshold Death Cross: EMA50({ema50_current:.2f}) is {self.cross_threshold_points} points below EMA200({ema200_current:.2f})")

            if current_ema_position and cross_signal is None:
                self.last_ema_position_status = current_ema_position # ✅ ใช้ self.last_ema_position_status
                self.save_monthly_stats() # ✅ ใช้ self.save_monthly_stats

            if cross_signal:
                logger.info(f"✨ สัญญาณ EMA Cross ที่ตรวจพบ: {cross_signal.upper()}")
            else:
                logger.info("🔎 ไม่พบสัญญาณ EMA Cross ที่ชัดเจน.") 
                
            return cross_signal

        except Exception as e:
            logger.error(f"❌ เกิดข้อผิดพลาดในการคำนวณ EMA: {e}", exc_info=True)
            self.send_telegram(f"⛔️ Error: ไม่สามารถคำนวณ EMA ได้\nรายละเอียด: {e}")
            return None

    # ==============================================================================
    # 10. ฟังก์ชันช่วยสำหรับการคำนวณและตรวจสอบออเดอร์
    # ==============================================================================

    def validate_trading_parameters(self, balance: float) -> tuple[bool, str]: # ✅ ลบ contracts_per_slot เพราะจะคำนวณใน calculate_order_details
        """ตรวจสอบความถูกต้องของพารามิเตอร์การเทรด"""
        if balance <= self.min_balance_safety_margin: # ✅ ใช้ self.min_balance_safety_margin
            return False, f"ยอดคงเหลือ ({balance:,.2f} USDT) ต่ำเกินไป (ต้องมีอย่างน้อย {self.min_balance_safety_margin} USDT)"

        if self.contracts_per_slot <= 0: # ✅ ใช้ self.contracts_per_slot
            return False, f"จำนวนสัญญาต่อไม้ไม่ถูกต้อง ({self.contracts_per_slot})"

        if self.leverage <= 0: # ✅ ใช้ self.leverage
            return False, f"ค่า Leverage ไม่ถูกต้อง ({self.leverage})"

        return True, "OK"

    # ✅ ใช้ฟังก์ชัน calculate_order_details จากโค้ดก่อนหน้า
    def calculate_order_details(self, available_usdt: float, price: float) -> tuple[float, float]:
        if price <= 0 or self.leverage <= 0 or self.target_position_size_factor <= 0:
            logger.error("Error: Price, leverage, and target_position_size_factor must be positive.")
            return (0, 0)

        market_info = self.exchange.market(self.symbol) # ✅ ใช้ self.exchange และ self.symbol
        if not market_info:
            logger.error(f"❌ Could not fetch market info for {self.symbol}.")
            return (0, 0)
        
        exchange_amount_step = market_info['limits']['amount']['step'] if 'amount' in market_info['limits'] and 'step' in market_info['limits']['amount'] and market_info['limits']['amount']['step'] is not None else self.forced_amount_step_size
        actual_step_size = max(self.forced_amount_step_size, float(exchange_amount_step))

        # คำนวณ Notional Value สูงสุดที่ทำได้จากทุน
        max_notional_from_available_margin = (available_usdt - self.margin_buffer) * self.leverage
        if max_notional_from_available_margin <= 0:
            logger.warning(f"❌ Available margin ({available_usdt:.2f}) too low after buffer ({self.margin_buffer}) for any notional value.")
            return (0, 0)

        # กำหนด target notional โดยใช้ factor เป็นเปอร์เซ็นต์ของ max_notional_from_available_margin
        target_notional_for_order = max_notional_from_available_margin * self.target_position_size_factor
        
        # ตรวจสอบขั้นต่ำ/สูงสุดของ Notional Value ที่ Exchange อนุญาต (ถ้ามีใน market_info)
        min_notional_exchange = market_info['limits']['cost']['min'] if 'cost' in market_info['limits'] and 'min' in market_info['limits']['cost'] and market_info['limits']['cost']['min'] is not None else 0
        max_notional_exchange = market_info['limits']['cost']['max'] if 'cost' in market_info['limits'] and 'max' in market_info['limits']['cost'] and market_info['limits']['cost']['max'] is not None else float('inf')

        # หาก target_notional_for_order ยังคงต่ำกว่ามูลค่าของ min_exchange_amount * price
        min_exchange_amount = market_info['limits']['amount']['min'] if 'amount' in market_info['limits'] and 'min' in market_info['limits']['amount'] and market_info['limits']['amount']['min'] is not None else 0
        min_notional_from_min_amount = min_exchange_amount * price

        target_notional_for_order = max(target_notional_for_order, min_notional_exchange, min_notional_from_min_amount)
        target_notional_for_order = min(target_notional_for_order, max_notional_exchange)
        
        contracts_raw = target_notional_for_order / price
        
        contracts_to_open = round(contracts_raw / actual_step_size) * actual_step_size
        contracts_to_open = float(f"{contracts_to_open:.10f}") 

        actual_notional_after_precision = contracts_to_open * price
        required_margin = actual_notional_after_precision / self.leverage

        if contracts_to_open < min_exchange_amount:
            logger.warning(f"❌ Calculated amount {contracts_to_open:.8f} is less than exchange's minimum amount {min_exchange_amount:.8f}. Cannot open (after final precision adjust).") 
            return (0, 0)
        
        if available_usdt < required_margin + self.margin_buffer:
            logger.warning(f"❌ Margin not sufficient. Available: {available_usdt:.2f}, Required: {required_margin:.2f} + {self.margin_buffer} (Buffer) = {required_margin + self.margin_buffer:.2f} USDT.")
            return (0, 0)
        
        logger.debug(f"💡 DEBUG (calculate_order_details): Max Notional from Available Margin: {max_notional_from_available_margin:.2f}")
        logger.debug(f"💡 DEBUG (calculate_order_details): Target Position Size Factor: {self.target_position_size_factor}")
        logger.debug(f"💡 DEBUG (calculate_order_details): Final Target Notional for Order: {target_notional_for_order:.2f}")
        logger.debug(f"💡 DEBUG (calculate_order_details): Raw contracts: {contracts_raw:.8f}") 
        logger.debug(f"💡 DEBUG (calculate_order_details): Actual Step Size Used: {actual_step_size}")
        logger.debug(f"💡 DEBUG (calculate_order_details): Contracts after step size adjustment: {contracts_to_open:.8f}") 
        logger.debug(f"💡 DEBUG (calculate_order_details): Actual Notional after step size: {actual_notional_after_precision:.2f}")
        logger.debug(f"💡 DEBUG (calculate_order_details): Calculated Required Margin: {required_margin:.2f} USDT")
        logger.debug(f"💡 DEBUG (calculate_order_details): Min Exchange Amount: {min_exchange_amount:.8f}") 
        logger.debug(f"💡 DEBUG (calculate_order_details): Min Notional Exchange: {min_notional_exchange:.2f}")
        logger.debug(f"💡 DEBUG (calculate_order_details): Min Notional from Min Amount: {min_notional_from_min_amount:.2f}")


        return (contracts_to_open, required_margin)

    def confirm_position_entry(self, expected_direction: str, expected_contracts: float) -> tuple[bool, float | None]: # ✅ เปลี่ยน expected_contracts เป็น float
        """ยืนยันการเปิดโพซิชัน"""
        self.current_position_size = 0.0 # รีเซ็ตก่อนยืนยัน
        size_tolerance = max(self.forced_amount_step_size, expected_contracts * 0.005) # tolerance อย่างน้อย 1 step size

        for attempt in range(self.confirmation_retries): # ✅ ใช้ self.confirmation_retries
            logger.info(f"⏳ ยืนยันโพซิชัน ({attempt + 1}/{self.confirmation_retries})...")
            time.sleep(self.confirmation_sleep) # ✅ ใช้ self.confirmation_sleep
            
            try:
                position_info = self.get_current_position() # ✅ ใช้ self.get_current_position
                
                if position_info and position_info.get('side') == expected_direction:
                    actual_size = position_info.get('size', 0)
                    entry_price = position_info.get('entry_price')
                    
                    if abs(actual_size - expected_contracts) <= size_tolerance:
                        logger.info(f"✅ ยืนยันโพซิชันสำเร็จ:")
                        logger.info(f"   - Entry Price: {entry_price:.2f}")
                        logger.info(f"   - Size: {actual_size:,.8f} Contracts") # ✅ ปรับการแสดงผล
                        logger.info(f"   - Direction: {expected_direction.upper()}")
                        
                        self.current_position_size = actual_size # ✅ อัปเดต self.current_position_size
                        
                        # ส่งการแจ้งเตือน
                        # ✅ ใช้ self.send_telegram
                        self.send_telegram(
                            f"🎯 เปิดโพซิชัน {expected_direction.upper()} สำเร็จ\n"
                            f"📊 ขนาด: {actual_size:,.8f} Contracts\n"
                            f"💰 Entry: {entry_price:.2f}"
                        )
                        # ✅ ไม่ต้องดึง PnL ตอนยืนยัน เพราะอาจจะยังไม่ทันอัปเดต
                        
                        return True, entry_price
                    else:
                        logger.warning(f"⚠️ ขนาดโพซิชันไม่ตรงกัน (คาดหวัง: {expected_contracts:,.8f}, ได้: {actual_size:,.8f})")
                else:
                    logger.warning(f"⚠️ ไม่พบโพซิชันที่ตรงกัน (คาดหวัง: {expected_direction})")
                    
            except Exception as e:
                logger.warning(f"⚠️ Error ในการยืนยันโพซิชัน: {e}", exc_info=True)
                
        logger.error(f"❌ ไม่สามารถยืนยันโพซิชันได้หลังจาก {self.confirmation_retries} ครั้ง")
        self.send_telegram( # ✅ ใช้ self.send_telegram
            f"⛔️ Position Confirmation Failed\n"
            f"🔍 กรุณาตรวจสอบโพซิชันใน Exchange ด่วน!\n"
            f"📊 คาดหวัง: {expected_direction.upper()} {expected_contracts:,.8f} Contracts" # ✅ ปรับการแสดงผล
        )

        return False, None


    # ==============================================================================
    # 11. ฟังก์ชันจัดการคำสั่งซื้อขาย (ORDER MANAGEMENT FUNCTIONS)
    # ==============================================================================

    def open_market_order(self, direction: str) -> tuple[bool, float | None]: # ✅ ลบ current_price เพราะจะดึงในนี้
        """เปิดออเดอร์ Market ด้วยจำนวนสัญญาที่คำนวณจากจำนวนไม้ และคืนราคา Entry Price."""
        try:
            # ดึงราคาปัจจุบัน
            current_price = self.get_current_price() # ✅ ใช้ self.get_current_price
            if not current_price:
                logger.error("❌ ไม่สามารถดึงราคาปัจจุบันได้.")
                return False, None

            # 1. ดึงยอดคงเหลือและตรวจสอบพื้นฐาน
            balance = self.get_portfolio_balance() # ✅ ใช้ self.get_portfolio_balance
            
            # 2. ตรวจสอบความถูกต้องของพารามิเตอร์
            is_valid, error_msg = self.validate_trading_parameters(balance) # ✅ ใช้ self.validate_trading_parameters
            if not is_valid:
                self.send_telegram(f"⛔️ Parameter Error: {error_msg}")
                logger.error(f"❌ {error_msg}")
                return False, None
            
            # 3. คำนวณขนาดโพซิชันที่เหมาะสม
            # calculate_order_details จะดูแลเรื่อง contract size, required margin, limits
            final_contracts, required_margin = self.calculate_order_details(balance, current_price) # ✅ ใช้ self.calculate_order_details
            
            if final_contracts == 0:
                error_msg = "จำนวนสัญญาคำนวณได้เป็นศูนย์หรือติดลบหลังการตรวจสอบทั้งหมด"
                self.send_telegram(f"⛔️ Calculation Error: {error_msg}")
                logger.error(f"❌ {error_msg}")
                return False, None
            
            # 4. แสดงข้อมูลการเทรด
            logger.info(f"ℹ️ Trading Summary:")
            logger.info(f"   - Balance: {balance:,.2f} USDT")
            logger.info(f"   - Contracts: {final_contracts:,.8f}") # ✅ ปรับการแสดงผล
            logger.info(f"   - Required Margin (est.): {required_margin:,.2f} USDT")
            logger.info(f"   - Direction: {direction.upper()}")
            
            # 5. ส่งออเดอร์
            side = 'buy' if direction == 'long' else 'sell'
            # ✅ สำหรับ Binance ไม่ต้องการ params 'tdMode' หรือ 'mgnCcy' ใน create_order
            params = {
                'reduceOnly': False,
            }
            
            order = None
            for attempt in range(3):
                logger.info(f"⚡️ ส่งคำสั่ง Market Order (Attempt {attempt + 1}/3) - {final_contracts:,.8f} Contracts") # ✅ ปรับการแสดงผล
                try:
                    order = self.exchange.create_order( # ✅ ใช้ self.exchange
                        self.symbol, 'market', side, float(final_contracts), # ✅ ส่ง final_contracts เป็น float
                        price=None, params=params
                    )
                    
                    if order and order.get('id'):
                        logger.info(f"✅ Market Order ส่งสำเร็จ: {order.get('id')}")
                        time.sleep(2) 
                        break
                    else:
                        logger.warning(f"⚠️ Order response ไม่สมบูรณ์ (Attempt {attempt + 1}/3)")
                        
                except ccxt.NetworkError as e:
                    logger.warning(f"⚠️ Network Error (Attempt {attempt + 1}/3): {e}")
                    if attempt == 2:
                        self.send_telegram(f"⛔️ Network Error: ไม่สามารถส่งออเดอร์ได้\n{str(e)[:200]}...")
                    time.sleep(15)
                    
                except ccxt.ExchangeError as e:
                    logger.warning(f"⚠️ Exchange Error (Attempt {attempt + 1}/3): {e}")
                    if attempt == 2:
                        self.send_telegram(f"⛔️ Exchange Error: ไม่สามารถส่งออเดอร์ได้\n{str(e)[:200]}...")
                    time.sleep(15)
                    
                except Exception as e:
                    logger.error(f"❌ Unexpected error (Attempt {attempt + 1}/3): {e}", exc_info=True)
                    self.send_telegram(f"⛔️ Unexpected Error: ไม่สามารถส่งออเดอร์ได้\n{str(e)[:200]}...")
                    return False, None
            
            if not order:
                logger.error("❌ ล้มเหลวในการส่งออเดอร์หลังจาก 3 ครั้ง")
                self.send_telegram("⛔️ Order Failed: ล้มเหลวในการส่งออเดอร์หลังจาก 3 ครั้ง")
                return False, None
            
            # 6. ยืนยันโพซิชัน
            return self.confirm_position_entry(direction, final_contracts) # ✅ ใช้ self.confirm_position_entry
            
        except Exception as e:
            logger.error(f"❌ Critical Error in open_market_order: {e}", exc_info=True)
            self.send_telegram(f"⛔️ Critical Error: ไม่สามารถเปิดออเดอร์ได้\n{str(e)[:200]}...")
            return False, None

    def set_tpsl_for_position(self, direction: str, entry_price: float) -> bool:
        """ตั้งค่า Take Profit และ Stop Loss สำหรับโพซิชันที่เปิดอยู่."""
        if not self.current_position_size: # ✅ ใช้ self.current_position_size
            logger.error("❌ ไม่สามารถตั้ง TP/SL ได้: ขนาดโพซิชันเป็น 0.")
            self.send_telegram("⛔️ Error: ไม่สามารถตั้ง TP/SL ได้ (ขนาดโพซิชันเป็น 0).")
            return False

        tp_price = 0.0
        sl_price = 0.0

        if direction == 'long':
            tp_price = entry_price + self.tp_value_points # ✅ ใช้ self.tp_value_points
            sl_price = entry_price - self.sl_value_points # ✅ ใช้ self.sl_value_points
        elif direction == 'short':
            tp_price = entry_price - self.tp_value_points # ✅ ใช้ self.tp_value_points
            sl_price = entry_price + self.sl_value_points # ✅ ใช้ self.sl_value_points
        
        # ✅ ใช้ self.exchange
        tp_price = self.exchange.price_to_precision(self.symbol, tp_price) # ✅ ใช้ self.symbol
        sl_price = self.exchange.price_to_precision(self.symbol, sl_price) # ✅ ใช้ self.symbol

        try:
            tp_sl_side = 'sell' if direction == 'long' else 'buy'
            
            # --- Setting Take Profit ---
            tp_order = self.exchange.create_order( # ✅ ใช้ self.exchange
                symbol=self.symbol,
                type='TAKE_PROFIT_MARKET', # ✅ Binance specific type
                side=tp_sl_side,
                amount=float(self.current_position_size), # ✅ ใช้ self.current_position_size และเป็น float
                price=None, # Market order, no limit price
                params={
                    'stopPrice': tp_price, # ✅ Binance uses stopPrice
                    'reduceOnly': True,
                    # 'tdMode': 'cross', # ✅ ไม่จำเป็นสำหรับ Binance TP/SL
                    # 'posSide': 'long' if direction == 'long' else 'short', # ✅ ไม่จำเป็นสำหรับ Binance TP/SL
                }
            )
            logger.info(f"✅ ส่งคำสั่ง Take Profit สำเร็จ: ID {tp_order.get('id', 'N/A')}, Trigger Price: {tp_price:.2f}")

            # --- Setting Stop Loss ---
            sl_order = self.exchange.create_order( # ✅ ใช้ self.exchange
                symbol=self.symbol,
                type='STOP_MARKET', # ✅ Binance specific type
                side=tp_sl_side,
                amount=float(self.current_position_size), # ✅ ใช้ self.current_position_size และเป็น float
                price=None,
                params={
                    'stopPrice': sl_price, # ✅ Binance uses stopPrice
                    'reduceOnly': True,
                    # 'tdMode': 'cross', # ✅ ไม่จำเป็นสำหรับ Binance TP/SL
                    # 'posSide': 'long' if direction == 'long' else 'short', # ✅ ไม่จำเป็นสำหรับ Binance TP/SL
                }
            )
            logger.info(f"✅ ส่งคำสั่ง Stop Loss สำเร็จ: ID {sl_order.get('id', 'N/A')}, Trigger Price: {sl_price:.2f}")

            return True

        except ccxt.ArgumentsRequired as e:
            logger.error(f"❌ Error setting TP/SL: Arguments missing or incorrect for Binance. {e}", exc_info=True) # ✅ แก้ข้อความ Error
            self.send_telegram(f"⛔️ API Error (TP/SL Arguments): {e.args[0] if e.args else str(e)}") # ✅ ใช้ self.send_telegram
            return False
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"❌ Error setting TP/SL: API/Network issue. {e}", exc_info=True)
            self.send_telegram(f"⛔️ API Error (TP/SL): {e.args[0] if e.args else str(e)}") # ✅ ใช้ self.send_telegram
            return False
        except Exception as e:
            logger.error(f"❌ Unexpected error setting TP/SL: {e}", exc_info=True)
            self.send_telegram(f"⛔️ Unexpected Error (TP/SL): {e}") # ✅ ใช้ self.send_telegram
            return False


    def move_sl_to_breakeven(self, direction: str, entry_price: float) -> bool:
        """เลื่อน Stop Loss ไปที่จุด Breakeven (หรือ +BE_SL_BUFFER_POINTS)."""
        if self.sl_moved: # ✅ ใช้ self.sl_moved
            logger.info("ℹ️ SL ถูกเลื่อนไปที่กันทุนแล้ว ไม่จำเป็นต้องเลื่อนอีก.")
            return True

        if not self.current_position_size: # ✅ ใช้ self.current_position_size
            logger.error("❌ ไม่สามารถเลื่อน SL ได้: ขนาดโพซิชันเป็น 0.")
            return False

        breakeven_sl_price = 0.0
        if direction == 'long':
            breakeven_sl_price = entry_price + self.be_sl_buffer_points # ✅ ใช้ self.be_sl_buffer_points
        elif direction == 'short':
            breakeven_sl_price = entry_price - self.be_sl_buffer_points # ✅ ใช้ self.be_sl_buffer_points
        
        breakeven_sl_price = self.exchange.price_to_precision(self.symbol, breakeven_sl_price) # ✅ ใช้ self.exchange และ self.symbol

        try:
            logger.info("⏳ กำลังยกเลิกคำสั่ง Stop Loss เก่า...")
            
            # ✅ ใช้ fetch_open_orders เพื่อหาคำสั่ง SL เก่า
            open_orders_to_cancel = []
            all_open_orders = self.exchange.fetch_open_orders(self.symbol)
            for order in all_open_orders:
                # ตรวจสอบว่าเป็นคำสั่ง SL (Stop Loss) ที่เปิดอยู่ (และ reduceOnly)
                if order['type'] in ['STOP_MARKET', 'STOP_LOSS', 'STOP'] and order.get('reduceOnly', False) == True:
                    open_orders_to_cancel.append(order)
            
            sl_canceled_count = 0
            if open_orders_to_cancel:
                for sl_order in open_orders_to_cancel:
                    try:
                        self.exchange.cancel_order(sl_order['id'], self.symbol) # ✅ ใช้ self.exchange
                        logger.info(f"✅ ยกเลิก SL Order ID {sl_order['id']} สำเร็จ.")
                        sl_canceled_count += 1
                    except ccxt.OrderNotFound: # ✅ จับ OrderNotFound โดยเฉพาะ
                        logger.info(f"💡 SL Order {sl_order['id']} ไม่พบ/ถูกยกเลิกไปแล้ว. ไม่ต้องทำอะไร.")
                    except Exception as cancel_e:
                        logger.warning(f"⚠️ ไม่สามารถยกเลิก SL Order ID {sl_order['id']} ได้: {cancel_e}")
            
            if sl_canceled_count == 0:
                logger.info("ℹ️ ไม่พบคำสั่ง Stop Loss เก่าที่ต้องยกเลิก.")
            else:
                logger.info(f"✓ ยกเลิก {sl_canceled_count} คำสั่ง Stop Loss เก่าสำเร็จ.")

            time.sleep(1) # รอสักครู่หลังจากยกเลิก

            new_sl_side = 'sell' if direction == 'long' else 'buy'
            new_sl_order = self.exchange.create_order( # ✅ ใช้ self.exchange
                symbol=self.symbol,
                type='STOP_MARKET', # ✅ Binance specific type
                side=new_sl_side,
                amount=float(self.current_position_size), 
                price=None, # Market order
                params={
                    'stopPrice': float(breakeven_sl_price), # ✅ Binance uses stopPrice
                    'reduceOnly': True,
                }
            )
            logger.info(f"✅ เลื่อน SL ไปที่กันทุนสำเร็จ: Trigger Price: {breakeven_sl_price:.2f}, ID: {new_sl_order.get('id', 'N/A')}")
            self.sl_moved = True # ✅ อัปเดต self.sl_moved
            return True

        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"❌ Error moving SL to breakeven: API/Network issue. {e}", exc_info=True)
            self.send_telegram(f"⛔️ API Error (Move SL): {e.args[0] if e.args else str(e)}")
            return False
        except Exception as e:
            logger.error(f"❌ Unexpected error moving SL to breakeven: {e}", exc_info=True)
            self.send_telegram(f"⛔️ Unexpected Error (Move SL): {e}")
            return False

    # ==============================================================================
    # 12. ฟังก์ชันตรวจสอบสถานะ (MONITORING FUNCTIONS)
    # ==============================================================================

    def monitor_position(self, pos_info: dict | None, current_price: float):
        """ตรวจสอบสถานะโพซิชันปัจจุบันและจัดการ Stop Loss."""
        logger.debug(f"🔄 กำลังตรวจสอบสถานะโพซิชัน: Pos_Info={pos_info}, Current_Price={current_price}")
        
        # ✅ เมื่อไม่มีโพซิชัน (สถานะเพิ่งถูกปิดไป)
        if not pos_info:
            if self.current_position: # ถ้าก่อนหน้านี้มีโพซิชัน
                logger.info(f"ℹ️ โพซิชัน {self.current_position.upper()} ถูกปิดแล้ว.")

                closed_price = current_price
                pnl_usdt_actual = 0.0

                # PnL สำหรับ Futures คือ (ราคาปิด - ราคาเข้า) * จำนวนสัญญา
                if self.entry_price and self.current_position_size:
                    if self.current_position == 'long':
                        pnl_usdt_actual = (closed_price - self.entry_price) * self.current_position_size
                    else: # short
                        pnl_usdt_actual = (self.entry_price - closed_price) * self.current_position_size

                close_reason = "ปิดโดยไม่ทราบสาเหตุ"
                emoji = "❓"

                tp_sl_be_tolerance_points = self.entry_price * self.tp_sl_be_price_tolerance_percent if self.entry_price else 0
                
                # ✅ ปรับการตรวจสอบ TP/SL/BE ให้แม่นยำขึ้น โดยใช้ราคาปัจจุบันเทียบกับราคาเป้าหมาย
                if self.current_position == 'long' and self.entry_price:
                    # ตรวจสอบว่าใกล้ TP ไหม
                    if abs(current_price - (self.entry_price + self.tp_value_points)) <= tp_sl_be_tolerance_points:
                        close_reason = "TP"
                        emoji = "✅"
                    # ตรวจสอบว่าใกล้ SL กันทุนไหม
                    elif self.sl_moved and abs(current_price - (self.entry_price + self.be_sl_buffer_points)) <= tp_sl_be_tolerance_points:
                         close_reason = "SL (กันทุน)"
                         emoji = "🛡️"
                    # ตรวจสอบว่าใกล้ SL ปกติไหม
                    elif abs(current_price - (self.entry_price - self.sl_value_points)) <= tp_sl_be_tolerance_points:
                        close_reason = "SL"
                        emoji = "❌"
                elif self.current_position == 'short' and self.entry_price:
                    # ตรวจสอบว่าใกล้ TP ไหม
                    if abs(current_price - (self.entry_price - self.tp_value_points)) <= tp_sl_be_tolerance_points:
                        close_reason = "TP"
                        emoji = "✅"
                    # ตรวจสอบว่าใกล้ SL กันทุนไหม
                    elif self.sl_moved and abs(current_price - (self.entry_price - self.be_sl_buffer_points)) <= tp_sl_be_tolerance_points:
                         close_reason = "SL (กันทุน)"
                         emoji = "🛡️"
                    # ตรวจสอบว่าใกล้ SL ปกติไหม
                    elif abs(current_price - (self.entry_price + self.sl_value_points)) <= tp_sl_be_tolerance_points:
                        close_reason = "SL"
                        emoji = "❌"

                self.send_telegram(f"{emoji} <b>ปิดออเดอร์ด้วย {close_reason}</b>\n<b>PnL (ประมาณ):</b> <code>{pnl_usdt_actual:,.2f} USDT</code>")
                logger.info(f"✅ โพซิชันปิด: {close_reason}, PnL (ประมาณ): {pnl_usdt_actual:.2f}")
                self.add_trade_result(close_reason, pnl_usdt_actual) # ✅ ใช้ close_reason
                
                # ✅ รีเซ็ตสถานะโพซิชันหลังจากปิด
                self.current_position = None
                self.entry_price = None
                self.current_position_size = 0.0
                self.sl_moved = False
                self.last_ema_position_status = None # ✅ รีเซ็ต EMA status เพื่อรอสัญญาณใหม่
                self.save_monthly_stats()

            return # ไม่ต้องทำอะไรต่อถ้าไม่มีโพซิชัน

        # ถ้ามีโพซิชันเปิดอยู่
        self.current_position = pos_info['side']
        self.entry_price = pos_info['entry_price']
        unrealized_pnl = pos_info['unrealized_pnl'] # ✅ แก้ไขจาก 'unrealizedPnl'
        self.current_position_size = pos_info['size'] # ✅ ใช้ 'size' จาก get_current_position

        logger.info(f"📊 สถานะปัจจุบัน: {self.current_position.upper()}, PnL: {unrealized_pnl:,.2f} USDT, ราคา: {current_price:,.1f}, เข้า: {self.entry_price:,.1f}, Size: {self.current_position_size:.8f} Contracts") # ✅ ปรับการแสดงผล

        pnl_in_points = 0
        if self.current_position == 'long':
            pnl_in_points = current_price - self.entry_price
        elif self.current_position == 'short':
            pnl_in_points = self.entry_price - current_price

        if not self.sl_moved and pnl_in_points >= self.be_profit_trigger_points: # ✅ ใช้ self.sl_moved, self.be_profit_trigger_points
            logger.info(f"ℹ️ กำไรถึงจุดเลื่อน SL: {pnl_in_points:,.0f} จุด (PnL: {unrealized_pnl:,.2f} USDT)")
            self.move_sl_to_breakeven(self.current_position, self.entry_price) # ✅ ใช้ self.move_sl_to_breakeven

    # ==============================================================================
    # 13. ฟังก์ชันรายงานประจำเดือน (MONTHLY REPORT FUNCTIONS)
    # ==============================================================================
    def monthly_report(self):
        """ส่งรายงานสถิติการเทรดประจำเดือนไปยัง Telegram."""
        now = datetime.now()
        current_month_year = now.strftime('%Y-%m')

        if self.last_monthly_report_date and \
           self.last_monthly_report_date.year == now.year and \
           self.last_monthly_report_date.month == now.month:
            logger.debug(f"ℹ️ รายงานประจำเดือนสำหรับ {current_month_year} ถูกส่งไปแล้ว.")
            return

        try:
            balance = self.get_portfolio_balance() # ✅ ใช้ self.get_portfolio_balance

            if self.monthly_stats['month_year'] != current_month_year:
                logger.info(f"🆕 สถิติประจำเดือนที่ใช้ไม่ตรงกับเดือนนี้ ({self.monthly_stats['month_year']} vs {current_month_year}). กำลังรีเซ็ตสถิติเพื่อรายงานเดือนนี้.")
                self.reset_monthly_stats() # ✅ ใช้ self.reset_monthly_stats

            tp_count = self.monthly_stats['tp_count']
            sl_count = self.monthly_stats['sl_count']
            total_pnl = self.monthly_stats['total_pnl']
            pnl_from_start = balance - self.initial_balance if self.initial_balance > 0 else 0.0 # ✅ ใช้ self.initial_balance

            message = f"""📊 <b>รายงานสรุปผลประจำเดือน - {now.strftime('%B %Y')}</b>
<b>🔹 กำไรสุทธิเดือนนี้:</b> <code>{total_pnl:+,.2f} USDT</code>
<b>🔹 SL:</b> <code>{sl_count} ครั้ง</code>
<b>🔹 TP:</b> <code>{tp_count} ครั้ง</code>
<b>🔹 คงเหลือปัจจุบัน:</b> <code>{balance:,.2f} USDT</code>
<b>🔹 กำไร/ขาดทุนรวมจากยอดเริ่มต้น:</b> <code>{pnl_from_start:+,.2f} USDT</code>
<b>⏱ บอทยังทำงานปกติ</b> ✅
<b>เวลา:</b> <code>{now.strftime('%H:%M')}</code>"""

            self.send_telegram(message) # ✅ ใช้ self.send_telegram
            self.last_monthly_report_date = now.date() # ✅ ใช้ self.last_monthly_report_date
            self.monthly_stats['last_report_month_year'] = current_month_year
            self.save_monthly_stats() # ✅ ใช้ self.save_monthly_stats
            logger.info("✅ ส่งรายงานประจำเดือนแล้ว.")

        except Exception as e:
            logger.error(f"❌ เกิดข้อผิดพลาดในการส่งรายงานประจำเดือน: {e}", exc_info=True)
            self.send_telegram(f"⛔️ Error: ไม่สามารถส่งรายงานประจำเดือนได้\nรายละเอียด: {e}") # ✅ ใช้ self.send_telegram

    def monthly_report_scheduler(self):
        """ตั้งเวลาสำหรับส่งรายงานประจำเดือน."""
        logger.info("⏰ เริ่ม Monthly Report Scheduler.")
        while True:
            now = datetime.now()
            
            report_day = min(self.monthly_report_day, calendar.monthrange(now.year, now.month)[1]) # ✅ ใช้ self.monthly_report_day
            
            next_report_time = now.replace(day=report_day, hour=self.monthly_report_hour, minute=self.monthly_report_minute, second=0, microsecond=0) # ✅ ใช้ self.monthly_report_hour, self.monthly_report_minute

            if now >= next_report_time:
                if self.last_monthly_report_date is None or \
                   self.last_monthly_report_date.year != now.year or \
                   self.last_monthly_report_date.month != now.month:
                     logger.info(f"⏰ ตรวจพบว่าถึงเวลาส่งรายงานประจำเดือน ({now.strftime('%H:%M')}) และยังไม่ได้ส่งสำหรับเดือนนี้. กำลังส่ง...")
                     self.monthly_report() # ✅ ใช้ self.monthly_report
                
                next_report_time = next_report_time.replace(month=next_report_time.month + 1) if next_report_time.month < 12 else next_report_time.replace(year=next_report_time.year + 1, month=1)
                max_day_in_next_month = calendar.monthrange(next_report_time.year, next_report_time.month)[1]
                report_day_for_next_month = min(self.monthly_report_day, max_day_in_next_month) # ✅ ใช้ self.monthly_report_day
                next_report_time = next_report_time.replace(day=report_day_for_next_month)


            time_to_wait = (next_report_time - datetime.now()).total_seconds()
            if time_to_wait > 0:
                logger.info(f"⏰ กำหนดส่งรายงานประจำเดือนถัดไปในอีก {int(time_to_wait / 86400)} วัน {int((time_to_wait % 86400) / 3600)} ชั่วโมง {int((time_to_wait % 3600) / 60)} นาที.")
                time.sleep(max(time_to_wait, 60))
            else:
                time.sleep(60)

    # ==============================================================================
    # 14. ฟังก์ชันเริ่มต้นบอท (BOT STARTUP FUNCTIONS)
    # ==============================================================================
    def send_startup_message(self):
        """ส่งข้อความแจ้งเตือนเมื่อบอทเริ่มทำงาน."""
        try:
            self.initial_balance = self.get_portfolio_balance() # ✅ ใช้ self.initial_balance, self.get_portfolio_balance
            startup_time = datetime.now().strftime('%d/%m/%Y %H:%M:%S')

            message = f"""🔄 <b>บอทเริ่มทำงาน</b>
<b>🤖 EMA Cross Trading Bot</b>
<b>💰 ยอดเริ่มต้น:</b> <code>{self.initial_balance:,.2f} USDT</code>
<b>⏰ เวลาเริ่ม:</b> <code>{startup_time}</code>
<b>📊 เฟรม:</b> <code>{self.timeframe}</code> | <b>Leverage:</b> <code>{self.leverage}x</code>
<b>🎯 TP:</b> <code>{self.tp_value_points}</code> | <b>SL:</b> <code>{self.sl_value_points}</code>
<b>🔧 ขนาดไม้:</b> <code>{self.contracts_per_slot:,.0f} Contracts</code> ต่อไม้
<b>📈 รอสัญญาณ EMA Cross...</b>"""

            self.send_telegram(message) # ✅ ใช้ self.send_telegram
            logger.info("✅ ส่งข้อความแจ้งเตือนเมื่อบอทเริ่มทำงาน.")

        except Exception as e:
            logger.error(f"❌ เกิดข้อผิดพลาดในการส่งข้อความเริ่มต้น: {e}", exc_info=True)

    # ==============================================================================
    # 15. ฟังก์ชันหลักของบอท (MAIN BOT LOGIC)
    # ==============================================================================
    def run_bot(self): # ✅ เปลี่ยนชื่อฟังก์ชันเป็น run_bot
        """ฟังก์ชันหลักที่รัน Bot."""
        try:
            self.load_monthly_stats() # ✅ ใช้ self.load_monthly_stats
            self.send_startup_message() # ✅ ใช้ self.send_startup_message

            monthly_thread = threading.Thread(target=self.monthly_report_scheduler, daemon=True) # ✅ ใช้ self.monthly_report_scheduler
            monthly_thread.start()
            logger.info("✅ Monthly Report Scheduler Thread Started.")

            if not self.setup_leverage(): # ✅ ตั้งค่า leverage ที่นี่
                logger.error("❌ Failed initial setup (leverage). Exiting.")
                return

        except Exception as e:
            error_msg = f"⛔️ Error: ไม่สามารถเริ่มต้นบอทได้\nรายละเอียด: {e} | Retry อีกครั้งใน {self.error_retry_sleep_seconds} วินาที." # ✅ ใช้ self.error_retry_sleep_seconds
            self.send_telegram(error_msg) # ✅ ใช้ self.send_telegram
            logger.critical(f"❌ Startup error: {e}", exc_info=True)
            time.sleep(self.error_retry_sleep_seconds) # ✅ ใช้ self.error_retry_sleep_seconds
            return

        logger.info("🚀 บอทเข้าสู่ Main Loop แล้วและพร้อมทำงาน...")
        while True:
            try:
                logger.info(f"🔄 เริ่มรอบ Main Loop ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) - กำลังดึงข้อมูลและตรวจสอบ.")
                
                current_pos_info = None
                try:
                    logger.info("🔎 กำลังดึงสถานะโพซิชันปัจจุบัน...")
                    current_pos_info = self.get_current_position() # ✅ ใช้ self.get_current_position
                    logger.info(f"☑️ ดึงสถานะโพซิชันปัจจุบันสำเร็จ: {'มีโพซิชัน' if current_pos_info else 'ไม่มีโพซิชัน'}.")
                except Exception as e:
                    logger.error(f"❌ Error ในการดึงสถานะโพซิชัน: {e}", exc_info=True)
                    self.send_telegram(f"⛔️ API Error: ไม่สามารถดึงสถานะโพซิชันได้. รายละเอียด: {e.args[0] if e.args else str(e)}") # ✅ ใช้ self.send_telegram
                    time.sleep(self.error_retry_sleep_seconds) # ✅ ใช้ self.error_retry_sleep_seconds
                    continue

                ticker = None
                try:
                    logger.info("📊 กำลังดึงราคาล่าสุด (Ticker)...")
                    ticker = self.exchange.fetch_ticker(self.symbol) # ✅ ใช้ self.exchange, self.symbol
                except Exception as e:
                    logger.warning(f"⚠️ Error fetching ticker: {e}. Retrying in {self.error_retry_sleep_seconds} วินาที...") # ✅ ใช้ self.error_retry_sleep_seconds
                    self.send_telegram(f"⛔️ API Error: ไม่สามารถดึงราคาล่าสุดได้. รายละเอียด: {e.args[0] if e.args else str(e)}") # ✅ ใช้ self.send_telegram
                    time.sleep(self.error_retry_sleep_seconds) # ✅ ใช้ self.error_retry_sleep_seconds
                    continue

                if not ticker or 'last' not in ticker:
                    logger.error("❌ Failed to fetch valid ticker. Skipping loop and retrying.")
                    self.send_telegram("⛔️ Error: ไม่สามารถดึงราคาล่าสุดได้ถูกต้อง. Skipping.") # ✅ ใช้ self.send_telegram
                    time.sleep(self.error_retry_sleep_seconds) # ✅ ใช้ self.error_retry_sleep_seconds
                    continue

                current_price = float(ticker['last'])
                logger.info(f"💲 ราคาปัจจุบันของ {self.symbol}: {current_price:,.1f}") # ✅ ใช้ self.symbol

                # มอนิเตอร์โพซิชัน (รวมถึงการเลื่อน SL ไปกันทุน)
                self.monitor_position(current_pos_info, current_price) # ✅ ใช้ self.monitor_position

                # ✅ แก้ไข: ย้าย cancel_open_tp_sl_orders() มาที่นี่
                # จะยกเลิกคำสั่ง TP/SL ที่ค้างอยู่เสมอเมื่อไม่มีโพซิชันเปิดอยู่
                if not current_pos_info:
                    self.cancel_open_tp_sl_orders() # ✅ ยกเลิกก่อนพยายามเปิดออเดอร์ใหม่

                    logger.info("🔍 ไม่มีโพซิชันเปิดอยู่. กำลังตรวจสอบสัญญาณ EMA Cross...")
                    signal = self.check_ema_cross() # ✅ ใช้ self.check_ema_cross

                    if signal: # ถ้าพบสัญญาณ
                        logger.info(f"🌟 ตรวจพบสัญญาณ EMA Cross: {signal.upper()}")
                        logger.info(f"✨ สัญญาณ {signal.upper()} ที่เข้าเงื่อนไข. กำลังพยายามเปิดออเดอร์.")

                        market_order_success, confirmed_entry_price = self.open_market_order(signal) # ✅ ใช้ self.open_market_order

                        if market_order_success and confirmed_entry_price:
                            set_tpsl_success = self.set_tpsl_for_position(signal, confirmed_entry_price) # ✅ ใช้ self.set_tpsl_for_position

                            if set_tpsl_success:
                                self.last_ema_position_status = None 
                                self.save_monthly_stats()
                                logger.info(f"✅ เปิดออเดอร์ {signal.upper()} และตั้ง TP/SL สำเร็จ.")
                            else:
                                logger.error(f"❌ เปิดออเดอร์ {signal.upper()} ได้ แต่ตั้ง TP/SL ไม่สำเร็จ. กรุณาตรวจสอบและปิดออเดอร์ด้วยตนเอง!")
                                self.send_telegram(f"⛔️ <b>ข้อผิดพลาดร้ายแรง:</b> เปิดออเดอร์ {signal.upper()} ได้ แต่ตั้ง TP/SL ไม่สำเร็จ. โพซิชันไม่มี SL/TP! โปรดจัดการด้วยตนเอง!") # ✅ ใช้ self.send_telegram
                        else:
                            logger.warning(f"⚠️ ไม่สามารถเปิด Market Order {signal.upper()} ได้.")
                    else:
                        logger.info("🔎 ไม่พบสัญญาณ EMA Cross ที่ชัดเจน.")
                else:
                    logger.info(f"Current Position: {current_pos_info['side'].upper()}. รอการปิดหรือเลื่อน SL.")

                logger.info(f"😴 จบรอบ Main Loop. รอ {self.main_loop_sleep_seconds} วินาทีสำหรับรอบถัดไป.") # ✅ ใช้ self.main_loop_sleep_seconds
                time.sleep(self.main_loop_sleep_seconds)

            except KeyboardInterrupt:
                logger.info("🛑 บอทหยุดทำงานโดยผู้ใช้ (KeyboardInterrupt).")
                self.send_telegram("🛑 Bot หยุดทำงานโดยผู้ใช้.") # ✅ ใช้ self.send_telegram
                break
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                error_msg = f"⛔️ Error: API Error\nรายละเอียด: {e} | Retry อีกครั้งใน {self.error_retry_sleep_seconds} วินาที."
                logger.error(error_msg, exc_info=True)
                self.send_telegram(error_msg)
                time.sleep(self.error_retry_sleep_seconds)
            except Exception as e:
                error_msg = f"⛔️ Error: เกิดข้อผิดพลาดที่ไม่คาดคิดใน Main Loop\nรายละเอียด: {e} | Retry อีกครั้งใน {self.error_retry_sleep_seconds} วินาที."
                logger.error(error_msg, exc_info=True)
                self.send_telegram(error_msg)
                time.sleep(self.error_retry_sleep_seconds)

# ==============================================================================

# 16. จุดเริ่มต้นการทำงานของโปรแกรม (ENTRY POINT)
# ==============================================================================
if __name__ == '__main__':
    bot = BinanceTradingBot() # ✅ สร้าง instance ของคลาส
    bot.run_bot() # ✅ เรียกใช้เมธอด run_bot

