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
API_KEY_ENV = 'BINANCE_API_KEY' 
SECRET_ENV = 'BINANCE_SECRET'

# --- Trading Parameters ---
SYMBOL = 'BTC/USDT' 
TIMEFRAME = '15m'
LEVERAGE = 30
TP_VALUE_POINTS = 501 
SL_VALUE_POINTS = 999 
BE_PROFIT_TRIGGER_POINTS = 350 
BE_SL_BUFFER_POINTS = 80 
CONTRACTS_PER_SLOT = 40 
CROSS_THRESHOLD_POINTS = 5 

MIN_BALANCE_SAFETY_MARGIN = 50  
MAX_POSITION_SIZE_LIMIT = 1000  
REQUIRED_MARGIN_BUFFER_PERCENT = 0.10 

CONFIRMATION_RETRIES = 15  
CONFIRMATION_SLEEP = 3  

# --- Telegram Notification Settings ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# --- Files & Paths ---
STATS_FILE = os.getenv('STATS_FILE_PATH', 'trading_stats.json') 

# --- Bot Timing ---
MAIN_LOOP_SLEEP_SECONDS = 300 
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
        logging.FileHandler(os.path.join(os.path.dirname(STATS_FILE), 'bot.log'), encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
for handler in logging.root.handlers:
    if hasattr(handler, 'flush'):
        handler.flush = lambda: sys.stdout.flush() if isinstance(handler, logging.StreamHandler) else handler.stream.flush()

logger = logging.getLogger(__name__)


# ==============================================================================
# 6. คลาสบอทเทรด (BinanceTradingBot Class)
# ==============================================================================

class BinanceTradingBot:
    def __init__(self):
        # --- API Keys & Credentials ---
        self.api_key = os.getenv(API_KEY_ENV)
        self.secret = os.getenv(SECRET_ENV)

        if not all([self.api_key, self.secret]):
            logger.critical(f"❌ Configuration Error: {API_KEY_ENV} หรือ {SECRET_ENV} ไม่ถูกตั้งค่าใน Environment Variables.")
            sys.exit(1)

        # --- Initialize Exchange ---
        try:
            self.exchange = ccxt.binance({ 
                'apiKey': self.api_key,
                'secret': self.secret,
                'sandbox': False,  
                'enableRateLimit': True,
                'rateLimit': 1000,
                'options': {
                    'defaultType': 'future', 
                    'marginMode': 'cross', 
                },
            })
            
            self.exchange.load_markets()
            logger.info("✅ เชื่อมต่อกับ Binance Exchange และโหลด markets สำเร็จ")

        except Exception as e:
            logger.critical(f"❌ ไม่สามารถเชื่อมต่อหรือโหลดข้อมูล Exchange เบื้องต้นได้: {e}", exc_info=True)
            sys.exit(1)

        # --- Trading Parameters ---
        self.symbol = SYMBOL 
        self.timeframe = TIMEFRAME
        self.leverage = LEVERAGE
        self.tp_value_points = TP_VALUE_POINTS 
        self.sl_value_points = SL_VALUE_POINTS 
        self.be_profit_trigger_points = BE_PROFIT_TRIGGER_POINTS 
        self.be_sl_buffer_points = BE_SL_BUFFER_POINTS 
        self.contracts_per_slot = CONTRACTS_PER_SLOT 
        self.cross_threshold_points = CROSS_THRESHOLD_POINTS 

        self.min_balance_safety_margin = MIN_BALANCE_SAFETY_MARGIN
        self.max_position_size_limit = MAX_POSITION_SIZE_LIMIT
        self.required_margin_buffer_percent = REQUIRED_MARGIN_BUFFER_PERCENT

        self.confirmation_retries = CONFIRMATION_RETRIES
        self.confirmation_sleep = CONFIRMATION_SLEEP

        self.telegram_token = TELEGRAM_TOKEN
        self.telegram_chat_id = TELEGRAM_CHAT_ID
        self.stats_file = STATS_FILE 

        self.main_loop_sleep_seconds = MAIN_LOOP_SLEEP_SECONDS
        self.error_retry_sleep_seconds = ERROR_RETRY_SLEEP_SECONDS
        self.monthly_report_day = MONTHLY_REPORT_DAY
        self.monthly_report_hour = MONTHLY_REPORT_HOUR
        self.monthly_report_minute = MONTHLY_REPORT_MINUTE
        self.tp_sl_be_price_tolerance_percent = TP_SL_BE_PRICE_TOLERANCE_PERCENT

        # --- สถานะการเทรด (ใน Class Instance) ---
        self.current_position = None 
        self.entry_price = None
        self.sl_moved = False 
        self.portfolio_balance = 0.0 
        self.initial_balance = 0.0 
        self.current_position_size = 0.0 
        self.last_ema_position_status = None 

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
        self.last_monthly_report_date = None 

        # --- กำหนด Step Size ของ Amount ด้วยตัวเอง (จากที่ทดลองกับ Binance) ---
        self.forced_amount_step_size = 0.001 

        self.target_position_size_factor = 0.95

    def setup_leverage(self):
        """ตั้งค่า leverage และ margin mode"""
        try:
            result = self.exchange.set_leverage(self.leverage, self.symbol, {'marginMode': 'cross'})
            logger.info(f"Leverage set to {self.leverage}x for {self.symbol}: {result}")
            return True
        except ccxt.ExchangeError as e:
            if "leverage is not valid" in str(e) or "not valid for this symbol" in str(e):
                logger.error(f"❌ Error: Leverage {self.leverage}x exceeds the maximum limit for {self.symbol} on Binance.")
                logger.error("Please check Binance UI for max allowed leverage for this symbol and update self.leverage in config.")
                return False 
            logger.error(f"Error setting leverage: {e}. Details: {e}")
            return False 
        except Exception as e:
            logger.error(f"An unexpected error occurred setting leverage: {e}")
            return False

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
            logger.error(f"❌ เกิดข้อผิดพลาดในการบันติติสถิติ: {e}")

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
                balance_data = self.exchange.fetch_balance(params={'type': 'future'})
                time.sleep(2)

                usdt_balance = float(balance_data.get('USDT', {}).get('free', 0.0))
                if usdt_balance > 0:
                    self.portfolio_balance = usdt_balance
                    logger.info(f"💰 ยอดคงเหลือ USDT (Trading Account): {usdt_balance:,.2f}")
                    return usdt_balance
                else:
                    for asset_info in balance_data.get('info', {}).get('assets', []):
                        if asset_info.get('asset') == 'USDT':
                            usdt_balance = float(asset_info.get('availableBalance', 0))
                            self.portfolio_balance = usdt_balance
                            logger.info(f"💰 ยอดคงเหลือ USDT (Trading Account via info.assets): {usdt_balance:,.2f}")
                            return usdt_balance
                    
                    logger.warning("⚠️ ไม่พบ 'free' balance ใน USDT หรือใน info.assets.")
                    self.portfolio_balance = 0.0
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
                positions = self.exchange.fetch_positions([self.symbol])
                logger.debug(f"DEBUG: Fetched positions raw: {positions}") 
                time.sleep(2)
                for pos in positions:
                    if float(pos.get('contracts', 0)) != 0: 
                        return {
                            'side': pos['side'],
                            'size': float(pos['contracts']), 
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
                return None
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

            ema50_current = self.calculate_ema(closes, 50)
            ema200_current = self.calculate_ema(closes, 200)

            logger.info(f"💡 EMA Values: Current EMA50={ema50_current:.2f}, EMA200={ema200_current:.2f}") 
            
            if None in [ema50_current, ema200_current]:
                logger.warning("ค่า EMA ไม่สามารถคำนวณได้ (เป็น None).")
                return None

            current_ema_position = None
            if ema50_current > ema200_current:
                current_ema_position = 'above'
            elif ema50_current < ema200_current:
                current_ema_position = 'below'
            
            if self.last_ema_position_status is None: 
                if current_ema_position:
                    self.last_ema_position_status = current_ema_position
                    self.save_monthly_stats() 
                    logger.info(f"ℹ️ บอทเพิ่งเริ่มรัน. บันทึกสถานะ EMA ปัจจุบันเป็น: {current_ema_position.upper()}. จะรอสัญญาณการตัดกันครั้งถัดไป.")
                return None

            cross_signal = None

            # Golden Cross (Long)
            if self.last_ema_position_status == 'below' and current_ema_position == 'above' and \
               ema50_current > (ema200_current + self.cross_threshold_points): 
                cross_signal = 'long'
                logger.info(f"🚀 Threshold Golden Cross: EMA50({ema50_current:.2f}) is {self.cross_threshold_points} points above EMA200({ema200_current:.2f})")

            # Death Cross (Short)
            elif self.last_ema_position_status == 'above' and current_ema_position == 'below' and \
                 ema50_current < (ema200_current - self.cross_threshold_points): 
                cross_signal = 'short'
                logger.info(f"🔻 Threshold Death Cross: EMA50({ema50_current:.2f}) is {self.cross_threshold_points} points below EMA200({ema200_current:.2f})")

            if current_ema_position and cross_signal is None:
                self.last_ema_position_status = current_ema_position 
                self.save_monthly_stats() 

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

    def validate_trading_parameters(self, balance: float) -> tuple[bool, str]:
        """ตรวจสอบความถูกต้องของพารามิเตอร์การเทรด"""
        if balance <= self.min_balance_safety_margin: 
            return False, f"ยอดคงเหลือ ({balance:,.2f} USDT) ต่ำเกินไป (ต้องมีอย่างน้อย {self.min_balance_safety_margin} USDT)"

        if self.contracts_per_slot <= 0:
            return False, f"จำนวนสัญญาต่อไม้ไม่ถูกต้อง ({self.contracts_per_slot})"

        if self.leverage <= 0:
            return False, f"ค่า Leverage ไม่ถูกต้อง ({self.leverage})"

        return True, "OK"

    def calculate_order_details(self, available_usdt: float, price: float) -> tuple[float, float]:
        if price <= 0 or self.leverage <= 0 or self.target_position_size_factor <= 0: 
            logger.error("Error: Price, leverage, and target_position_size_factor must be positive.")
            return (0, 0)

        market_info = self.exchange.market(self.symbol) 
        if not market_info:
            logger.error(f"❌ Could not fetch market info for {self.symbol}.")
            return (0, 0)
        
        exchange_amount_step = market_info['limits']['amount']['step'] if 'amount' in market_info['limits'] and 'step' in market_info['limits']['amount'] and market_info['limits']['amount']['step'] is not None else self.forced_amount_step_size
        actual_step_size = float(actual_step_size) if isinstance(actual_step_size, str) else actual_step_size 
        
        if actual_step_size == 0:
            actual_step_size = self.forced_amount_step_size 
            logger.warning(f"⚠️ Exchange amount step is 0, using forced_amount_step_size: {actual_step_size}")


        max_notional_from_available_margin = (available_usdt - self.margin_buffer) * self.leverage
        if max_notional_from_available_margin <= 0:
            logger.warning(f"❌ Available margin ({available_usdt:.2f}) too low after buffer ({self.margin_buffer}) for any notional value.")
            return (0, 0)

        target_notional_for_order = max_notional_from_available_margin * self.target_position_size_factor
        
        min_notional_exchange = market_info['limits']['cost']['min'] if 'cost' in market_info['limits'] and 'min' in market_info['limits']['cost'] and market_info['limits']['cost']['min'] is not None else 0
        max_notional_exchange = market_info['limits']['cost']['max'] if 'cost' in market_info['limits'] and 'max' in market_info['limits']['cost'] and market_info['limits']['cost']['max'] is not None else float('inf')

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

    def confirm_position_entry(self, expected_direction: str, expected_contracts: float) -> tuple[bool, float | None]:
        """ยืนยันการเปิดโพซิชัน"""
        self.current_position_size = 0.0 
        size_tolerance = max(self.forced_amount_step_size, expected_contracts * 0.005)

        for attempt in range(self.confirmation_retries): 
            logger.info(f"⏳ ยืนยันโพซิชัน ({attempt + 1}/{self.confirmation_retries})...")
            time.sleep(self.confirmation_sleep) 
            
            try:
                position_info = self.get_current_position()
                
                if position_info and position_info.get('side') == expected_direction:
                    actual_size = position_info.get('size', 0)
                    entry_price = position_info.get('entry_price')
                    
                    if abs(actual_size - expected_contracts) <= size_tolerance:
                        logger.info(f"✅ ยืนยันโพซิชันสำเร็จ:")
                        logger.info(f"   - Entry Price: {entry_price:.2f}")
                        logger.info(f"   - Size: {actual_size:,.8f} Contracts") 
                        logger.info(f"   - Direction: {expected_direction.upper()}")
                        
                        self.current_position_size = actual_size 
                        
                        self.send_telegram(
                            f"🎯 เปิดโพซิชัน {expected_direction.upper()} สำเร็จ\n"
                            f"📊 ขนาด: {actual_size:,.8f} Contracts\n"
                            f"💰 Entry: {entry_price:.2f}"
                        )
                        return True, entry_price
                    else:
                        logger.warning(f"⚠️ ขนาดโพซิชันไม่ตรงกัน (คาดหวัง: {expected_contracts:,.8f}, ได้: {actual_size:,.8f})")
                else:
                    logger.warning(f"⚠️ ไม่พบโพซิชันที่ตรงกัน (คาดหวัง: {expected_direction})")
                    
            except Exception as e:
                logger.warning(f"⚠️ Error ในการยืนยันโพซิชัน: {e}", exc_info=True)
                
        logger.error(f"❌ ไม่สามารถยืนยันโพซิชันได้หลังจาก {self.confirmation_retries} ครั้ง")
        self.send_telegram(
            f"⛔️ Position Confirmation Failed\n"
            f"🔍 กรุณาตรวจสอบโพซิชันใน Exchange ด่วน!\n"
            f"📊 คาดหวัง: {expected_direction.upper()} {expected_contracts:,.8f} Contracts" 
        )

        return False, None


    # ==============================================================================
    # 11. ฟังก์ชันจัดการคำสั่งซื้อขาย (ORDER MANAGEMENT FUNCTIONS)
    # ==============================================================================

    def open_market_order(self, direction: str) -> tuple[bool, float | None]: 
        """เปิดออเดอร์ Market ด้วยจำนวนสัญญาที่คำนวณจากจำนวนไม้ และคืนราคา Entry Price."""
        try:
            current_price = self.get_current_price() 
            if not current_price:
                logger.error("❌ ไม่สามารถดึงราคาปัจจุบันได้.")
                return False, None

            balance = self.get_portfolio_balance() 
            
            is_valid, error_msg = self.validate_trading_parameters(balance) 
            if not is_valid:
                self.send_telegram(f"⛔️ Parameter Error: {error_msg}")
                logger.error(f"❌ {error_msg}")
                return False, None
            
            final_contracts, required_margin = self.calculate_order_details(balance, current_price) 
            
            if final_contracts == 0:
                error_msg = "จำนวนสัญญาคำนวณได้เป็นศูนย์หรือติดลบหลังการตรวจสอบทั้งหมด"
                self.send_telegram(f"⛔️ Calculation Error: {error_msg}")
                logger.error(f"❌ {error_msg}")
                return False, None
            
            logger.info(f"ℹ️ Trading Summary:")
            logger.info(f"   - Balance: {balance:,.2f} USDT")
            logger.info(f"   - Contracts: {final_contracts:,.8f}") 
            logger.info(f"   - Required Margin (est.): {required_margin:,.2f} USDT")
            logger.info(f"   - Direction: {direction.upper()}")
            
            side = 'buy' if direction == 'long' else 'sell'
            params = {
                'reduceOnly': False,
            }
            
            order = None
            for attempt in range(3):
                logger.info(f"⚡️ ส่งคำสั่ง Market Order (Attempt {attempt + 1}/3) - {final_contracts:,.8f} Contracts") 
                try:
                    order = self.exchange.create_order(
                        self.symbol, 'market', side, float(final_contracts), 
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
            
            return self.confirm_position_entry(direction, final_contracts) 
            
        except Exception as e:
            logger.error(f"❌ Critical Error in open_market_order: {e}", exc_info=True)
            self.send_telegram(f"⛔️ Critical Error: ไม่สามารถเปิดออเดอร์ได้\n{str(e)[:200]}...")
            return False, None

    def set_tpsl_for_position(self, direction: str, entry_price: float) -> bool:
        """ตั้งค่า Take Profit และ Stop Loss สำหรับโพซิชันที่เปิดอยู่."""
        if not self.current_position_size: 
            logger.error("❌ ไม่สามารถตั้ง TP/SL ได้: ขนาดโพซิชันเป็น 0.")
            self.send_telegram("⛔️ Error: ไม่สามารถตั้ง TP/SL ได้ (ขนาดโพซิชันเป็น 0).")
            return False

        tp_price = 0.0
        sl_price = 0.0

        if direction == 'long':
            tp_price = entry_price + self.tp_value_points 
            sl_price = entry_price - self.sl_value_points 
        elif direction == 'short':
            tp_price = entry_price - self.tp_value_points 
            sl_price = entry_price + self.sl_value_points 
        
        tp_price = self.exchange.price_to_precision(self.symbol, tp_price) 
        sl_price = self.exchange.price_to_precision(self.symbol, sl_price) 

        try:
            tp_sl_side = 'sell' if direction == 'long' else 'buy'
            
            tp_order = self.exchange.create_order( 
                symbol=self.symbol,
                type='TAKE_PROFIT_MARKET', 
                side=tp_sl_side,
                amount=float(self.current_position_size), 
                price=None, 
                params={
                    'stopPrice': tp_price, 
                    'reduceOnly': True, 
                }
            )
            logger.info(f"✅ ส่งคำสั่ง Take Profit สำเร็จ: ID {tp_order.get('id', 'N/A')}, Trigger Price: {tp_price:.2f}")

            sl_order = self.exchange.create_order( 
                symbol=self.symbol,
                type='STOP_MARKET', 
                side=tp_sl_side,
                amount=float(self.current_position_size), 
                price=None,         
                params={
                    'stopPrice': sl_price, 
                    'reduceOnly': True,
                }
            )
            logger.info(f"✅ ส่งคำสั่ง Stop Loss สำเร็จ: ID {sl_order.get('id', 'N/A')}, Trigger Price: {sl_price:.2f}")

            return True

        except ccxt.ArgumentsRequired as e:
            logger.error(f"❌ Error setting TP/SL: Arguments missing or incorrect for Binance. {e}", exc_info=True) 
            self.send_telegram(f"⛔️ API Error (TP/SL Arguments): {e.args[0] if e.args else str(e)}") 
            return False
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"❌ Error setting TP/SL: API/Network issue. {e}", exc_info=True)
            self.send_telegram(f"⛔️ API Error (TP/SL): {e.args[0] if e.args else str(e)}") 
            return False
        except Exception as e:
            logger.error(f"❌ Unexpected error setting TP/SL: {e}", exc_info=True)
            self.send_telegram(f"⛔️ Unexpected Error (TP/SL): {e}") 
            return False


    def move_sl_to_breakeven(self, direction: str, entry_price: float) -> bool:
        """เลื่อน Stop Loss ไปที่จุด Breakeven (หรือ +BE_SL_BUFFER_POINTS)."""
        if self.sl_moved: 
            logger.info("ℹ️ SL ถูกเลื่อนไปที่กันทุนแล้ว ไม่จำเป็นต้องเลื่อนอีก.")
            return True

        if not self.current_position_size: 
            logger.error("❌ ไม่สามารถเลื่อน SL ได้: ขนาดโพซิชันเป็น 0.")
            return False

        breakeven_sl_price = 0.0
        if direction == 'long':
            breakeven_sl_price = entry_price + self.be_sl_buffer_points 
        elif direction == 'short':
            breakeven_sl_price = entry_price - self.be_sl_buffer_points 
        
        breakeven_sl_price = self.exchange.price_to_precision(self.symbol, breakeven_sl_price) 

        try:
            logger.info("⏳ กำลังยกเลิกคำสั่ง Stop Loss เก่า...")
            
            open_orders_to_cancel = []
            all_open_orders = self.exchange.fetch_open_orders(self.symbol)
            for order in all_open_orders:
                if order['type'] in ['STOP_MARKET', 'STOP_LOSS', 'STOP'] and order.get('reduceOnly', False) == True:
                    open_orders_to_cancel.append(order)
            
            sl_canceled_count = 0
            if open_orders_to_cancel:
                for sl_order in open_orders_to_cancel:
                    try:
                        self.exchange.cancel_order(sl_order['id'], self.symbol) 
                        logger.info(f"✅ ยกเลิก SL Order ID {sl_order['id']} สำเร็จ.")
                        sl_canceled_count += 1
                    except ccxt.OrderNotFound: 
                        logger.info(f"💡 SL Order {sl_order['id']} ไม่พบ/ถูกยกเลิกไปแล้ว. ไม่ต้องทำอะไร.")
                    except Exception as cancel_e:
                        logger.warning(f"⚠️ ไม่สามารถยกเลิก SL Order ID {sl_order['id']} ได้: {cancel_e}")
            
            if sl_canceled_count == 0:
                logger.info("ℹ️ ไม่พบคำสั่ง Stop Loss เก่าที่ต้องยกเลิก.")
            else:
                logger.info(f"✓ ยกเลิก {sl_canceled_count} คำสั่ง Stop Loss เก่าสำเร็จ.")

            time.sleep(1) 

            new_sl_side = 'sell' if direction == 'long' else 'buy'
            new_sl_order = self.exchange.create_order(
                symbol=self.symbol,
                type='STOP_MARKET', 
                side=new_sl_side,
                amount=float(self.current_position_size), 
                price=None, 
                params={
                    'stopPrice': float(breakeven_sl_price), 
                    'reduceOnly': True,
                }
            )
            logger.info(f"✅ เลื่อน SL ไปที่กันทุนสำเร็จ: Trigger Price: {breakeven_sl_price:.2f}, ID: {new_sl_order.get('id', 'N/A')}")
            self.sl_moved = True 
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
        
        if not pos_info:
            if self.current_position: 
                logger.info(f"ℹ️ โพซิชัน {self.current_position.upper()} ถูกปิดแล้ว.")

                closed_price = current_price
                pnl_usdt_actual = 0.0

                if self.entry_price and self.current_position_size:
                    if self.current_position == 'long':
                        pnl_usdt_actual = (closed_price - self.entry_price) * self.current_position_size
                    else: # short
                        pnl_usdt_actual = (self.entry_price - closed_price) * self.current_position_size

                close_reason = "ปิดโดยไม่ทราบสาเหตุ"
                emoji = "❓"

                tp_sl_be_tolerance_points = self.entry_price * self.tp_sl_be_price_tolerance_percent if self.entry_price else 0
                
                if self.current_position == 'long' and self.entry_price:
                    if abs(current_price - (self.entry_price + self.tp_value_points)) <= tp_sl_be_tolerance_points:
                        close_reason = "TP"
                        emoji = "✅"
                    elif self.sl_moved and abs(current_price - (self.entry_price + self.be_sl_buffer_points)) <= tp_sl_be_tolerance_points:
                         close_reason = "SL (กันทุน)"
                         emoji = "🛡️"
                    elif abs(current_price - (self.entry_price - self.sl_value_points)) <= tp_sl_be_tolerance_points:
                        close_reason = "SL"
                        emoji = "❌"
                elif self.current_position == 'short' and self.entry_price:
                    if abs(current_price - (self.entry_price - self.tp_value_points)) <= tp_sl_be_tolerance_points:
                        close_reason = "TP"
                        emoji = "✅"
                    elif self.sl_moved and abs(current_price - (self.entry_price - self.be_sl_buffer_points)) <= tp_sl_be_tolerance_points:
                         close_reason = "SL (กันทุน)"
                         emoji = "🛡️"
                    elif abs(current_price - (self.entry_price + self.sl_value_points)) <= tp_sl_be_tolerance_points:
                        close_reason = "SL"
                        emoji = "❌"

                self.send_telegram(f"{emoji} <b>ปิดออเดอร์ด้วย {close_reason}</b>\n<b>PnL (ประมาณ):</b> <code>{pnl_usdt_actual:,.2f} USDT</code>")
                logger.info(f"✅ โพซิชันปิด: {close_reason}, PnL (ประมาณ): {pnl_usdt_actual:.2f}")
                self.add_trade_result(close_reason, pnl_usdt_actual)
                
                self.current_position = None
                self.entry_price = None
                self.current_position_size = 0.0
                self.sl_moved = False
                self.last_ema_position_status = None 
                self.save_monthly_stats()

            return 

        self.current_position = pos_info['side']
        self.entry_price = pos_info['entry_price']
        unrealized_pnl = pos_info['unrealized_pnl'] 
        self.current_position_size = pos_info['size'] 

        logger.info(f"📊 สถานะปัจจุบัน: {self.current_position.upper()}, PnL: {unrealized_pnl:,.2f} USDT, ราคา: {current_price:,.1f}, เข้า: {self.entry_price:,.1f}, Size: {self.current_position_size:.8f} Contracts") 

        pnl_in_points = 0
        if self.current_position == 'long':
            pnl_in_points = current_price - self.entry_price
        elif self.current_position == 'short':
            pnl_in_points = self.entry_price - current_price

        if not self.sl_moved and pnl_in_points >= self.be_profit_trigger_points: 
            logger.info(f"ℹ️ กำไรถึงจุดเลื่อน SL: {pnl_in_points:,.0f} จุด (PnL: {unrealized_pnl:,.2f} USDT)")
            self.move_sl_to_breakeven(self.current_position, self.entry_price) 

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
            balance = self.get_portfolio_balance() 

            if self.monthly_stats['month_year'] != current_month_year:
                logger.info(f"🆕 สถิติประจำเดือนที่ใช้ไม่ตรงกับเดือนนี้ ({self.monthly_stats['month_year']} vs {current_month_year}). กำลังรีเซ็ตสถิติเพื่อรายงานเดือนนี้.")
                self.reset_monthly_stats() 

            tp_count = self.monthly_stats['tp_count']
            sl_count = self.monthly_stats['sl_count']
            total_pnl = self.monthly_stats['total_pnl']
            pnl_from_start = balance - self.initial_balance if self.initial_balance > 0 else 0.0 

            message = f"""📊 <b>รายงานสรุปผลประจำเดือน - {now.strftime('%B %Y')}</b>
<b>🔹 กำไรสุทธิเดือนนี้:</b> <code>{total_pnl:+,.2f} USDT</code>
<b>🔹 SL:</b> <code>{sl_count} ครั้ง</code>
<b>🔹 TP:</b> <code>{tp_count} ครั้ง</code>
<b>🔹 คงเหลือปัจจุบัน:</b> <code>{balance:,.2f} USDT</code>
<b>🔹 กำไร/ขาดทุนรวมจากยอดเริ่มต้น:</b> <code>{pnl_from_start:+,.2f} USDT</code>
<b>⏱ บอทยังทำงานปกติ</b> ✅
<b>เวลา:</b> <code>{now.strftime('%H:%M')}</code>"""

            self.send_telegram(message) 
            self.last_monthly_report_date = now.date() 
            self.monthly_stats['last_report_month_year'] = current_month_year
            self.save_monthly_stats() 
            logger.info("✅ ส่งรายงานประจำเดือนแล้ว.")

        except Exception as e:
            logger.error(f"❌ เกิดข้อผิดพลาดในการส่งรายงานประจำเดือน: {e}", exc_info=True)
            self.send_telegram(f"⛔️ Error: ไม่สามารถส่งรายงานประจำเดือนได้\nรายละเอียด: {e}") 

    def monthly_report_scheduler(self):
        """ตั้งเวลาสำหรับส่งรายงานประจำเดือน."""
        logger.info("⏰ เริ่ม Monthly Report Scheduler.")
        while True:
            now = datetime.now()
            
            report_day = min(self.monthly_report_day, calendar.monthrange(now.year, now.month)[1]) 
            
            next_report_time = now.replace(day=report_day, hour=self.monthly_report_hour, minute=self.monthly_report_minute, second=0, microsecond=0)

            if now >= next_report_time:
                if self.last_monthly_report_date is None or \
                   self.last_monthly_report_date.year != now.year or \
                   self.last_monthly_report_date.month != now.month:
                     logger.info(f"⏰ ตรวจพบว่าถึงเวลาส่งรายงานประจำเดือน ({now.strftime('%H:%M')}) และยังไม่ได้ส่งสำหรับเดือนนี้. กำลังส่ง...")
                     self.monthly_report() 
                
                next_report_time = next_report_time.replace(month=next_report_time.month + 1) if next_report_time.month < 12 else next_report_time.replace(year=next_report_time.year + 1, month=1)
                max_day_in_next_month = calendar.monthrange(next_report_time.year, next_report_time.month)[1]
                report_day_for_next_month = min(self.monthly_report_day, max_day_in_next_month) 
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
            self.initial_balance = self.get_portfolio_balance() 
            startup_time = datetime.now().strftime('%d/%m/%Y %H:%M:%S')

            message = f"""🔄 <b>บอทเริ่มทำงาน</b>
<b>🤖 EMA Cross Trading Bot</b>
<b>💰 ยอดเริ่มต้น:</b> <code>{self.initial_balance:,.2f} USDT</code>
<b>⏰ เวลาเริ่ม:</b> <code>{startup_time}</code>
<b>📊 เฟรม:</b> <code>{self.timeframe}</code> | <b>Leverage:</b> <code>{self.leverage}x</code>
<b>🎯 TP:</b> <code>{self.tp_value_points}</code> | <b>SL:</b> <code>{self.sl_value_points}</code>
<b>🔧 ขนาดไม้:</b> <code>{self.contracts_per_slot:,.0f} Contracts</code> ต่อไม้
<b>📈 รอสัญญาณ EMA Cross...</b>"""

            self.send_telegram(message) 
            logger.info("✅ ส่งข้อความแจ้งเตือนเมื่อบอทเริ่มทำงาน.")

        except Exception as e:
            logger.error(f"❌ เกิดข้อผิดพลาดในการส่งข้อความเริ่มต้น: {e}", exc_info=True)

    # ==============================================================================
    # 15. ฟังก์ชันหลักของบอท (MAIN BOT LOGIC)
    # ==============================================================================
    def run_bot(self): 
        """ฟังก์ชันหลักที่รัน Bot."""
        try:
            self.load_monthly_stats() 
            self.send_startup_message() 

            monthly_thread = threading.Thread(target=self.monthly_report_scheduler, daemon=True) 
            monthly_thread.start()
            logger.info("✅ Monthly Report Scheduler Thread Started.")

            if not self.setup_leverage(): 
                logger.error("❌ Failed initial setup (leverage). Exiting.")
                return

        except Exception as e:
            error_msg = f"⛔️ Error: ไม่สามารถเริ่มต้นบอทได้\nรายละเอียด: {e} | Retry อีกครั้งใน {self.error_retry_sleep_seconds} วินาที." 
            self.send_telegram(error_msg) 
            logger.critical(f"❌ Startup error: {e}", exc_info=True)
            time.sleep(self.error_retry_sleep_seconds) 
            return

        logger.info("🚀 บอทเข้าสู่ Main Loop แล้วและพร้อมทำงาน...")
        while True:
            try:
                logger.info(f"🔄 เริ่มรอบ Main Loop ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) - กำลังดึงข้อมูลและตรวจสอบ.")
                
                current_pos_info = None
                try:
                    logger.info("🔎 กำลังดึงสถานะโพซิชันปัจจุบัน...")
                    current_pos_info = self.get_current_position() 
                    logger.info(f"☑️ ดึงสถานะโพซิชันปัจจุบันสำเร็จ: {'มีโพซิชัน' if current_pos_info else 'ไม่มีโพซิชัน'}.")
                except Exception as e:
                    logger.error(f"❌ Error ในการดึงสถานะโพซิชัน: {e}", exc_info=True)
                    self.send_telegram(f"⛔️ API Error: ไม่สามารถดึงสถานะโพซิชันได้. รายละเอียด: {e.args[0] if e.args else str(e)}") 
                    time.sleep(self.error_retry_sleep_seconds) 
                    continue

                ticker = None
                try:
                    logger.info("📊 กำลังดึงราคาล่าสุด (Ticker)...")
                    ticker = self.exchange.fetch_ticker(self.symbol) 
                except Exception as e:
                    logger.warning(f"⚠️ Error fetching ticker: {e}. Retrying in {self.error_retry_sleep_seconds} วินาที...") 
                    self.send_telegram(f"⛔️ API Error: ไม่สามารถดึงราคาล่าสุดได้. รายละเอียด: {e.args[0] if e.args else str(e)}") 
                    time.sleep(self.error_retry_sleep_seconds) 
                    continue

                if not ticker or 'last' not in ticker:
                    logger.error("❌ Failed to fetch valid ticker. Skipping loop and retrying.")
                    self.send_telegram("⛔️ Error: ไม่สามารถดึงราคาล่าสุดได้ถูกต้อง. Skipping.") 
                    time.sleep(self.error_retry_sleep_seconds) 
                    continue

                current_price = float(ticker['last'])
                logger.info(f"💲 ราคาปัจจุบันของ {self.symbol}: {current_price:,.1f}")

                self.monitor_position(current_pos_info, current_price)

                if not current_pos_info:
                    # ✅ แก้ไข: ย้ายฟังก์ชันนี้เข้ามาในคลาส
                    self.cancel_open_tp_sl_orders() 

                    logger.info("🔍 ไม่มีโพซิชันเปิดอยู่. กำลังตรวจสอบสัญญาณ EMA Cross...")
                    signal = self.check_ema_cross() 

                    if signal: 
                        logger.info(f"🌟 ตรวจพบสัญญาณ EMA Cross: {signal.upper()}")
                        logger.info(f"✨ สัญญาณ {signal.upper()} ที่เข้าเงื่อนไข. กำลังพยายามเปิดออเดอร์.")

                        market_order_success, confirmed_entry_price = self.open_market_order(signal) 

                        if market_order_success and confirmed_entry_price:
                            set_tpsl_success = self.set_tpsl_for_position(signal, confirmed_entry_price) 

                            if set_tpsl_success:
                                self.last_ema_position_status = None 
                                self.save_monthly_stats()
                                logger.info(f"✅ เปิดออเดอร์ {signal.upper()} และตั้ง TP/SL สำเร็จ.")
                            else:
                                logger.error(f"❌ เปิดออเดอร์ {signal.upper()} ได้ แต่ตั้ง TP/SL ไม่สำเร็จ. กรุณาตรวจสอบและปิดออเดอร์ด้วยตนเอง!")
                                self.send_telegram(f"⛔️ <b>ข้อผิดพลาดร้ายแรง:</b> เปิดออเดอร์ {signal.upper()} ได้ แต่ตั้ง TP/SL ไม่สำเร็จ. โพซิชันไม่มี SL/TP! โปรดจัดการด้วยตนเอง!") 
                        else:
                            logger.warning(f"⚠️ ไม่สามารถเปิด Market Order {signal.upper()} ได้.")
                    else:
                        logger.info("🔎 ไม่พบสัญญาณ EMA Cross ที่ชัดเจน.")
                else:
                    logger.info(f"Current Position: {current_pos_info['side'].upper()}. รอการปิดหรือเลื่อน SL.")

                logger.info(f"😴 จบรอบ Main Loop. รอ {self.main_loop_sleep_seconds} วินาทีสำหรับรอบถัดไป.") 
                time.sleep(self.main_loop_sleep_seconds)

            except KeyboardInterrupt:
                logger.info("🛑 บอทหยุดทำงานโดยผู้ใช้ (KeyboardInterrupt).")
                self.send_telegram("🛑 Bot หยุดทำงานโดยผู้ใช้.") 
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

if __name__ == '__main__':
    bot = BinanceTradingBot()
    bot.run_bot()

