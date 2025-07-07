import ccxt
import time
import os
from datetime import datetime

class OKXTradingBot:
def **init**(self):
# OKX API credentials - ใส่ใน environment variables
self.api_key = os.getenv(‘RAILWAY_API_KEY’)
self.secret = os.getenv(‘RAILWAY_SECRET’)
self.passphrase = os.getenv(‘RAILWAY_PASSPHRASE’)

```
    # Initialize exchange
    self.exchange = ccxt.okx({
        'apiKey': self.api_key,
        'secret': self.secret,
        'password': self.passphrase,
        'sandbox': False,  # Set to True for testnet
        'options': {
            'defaultType': 'future',  # สำหรับ futures trading
        }
    })
    
    # Trading parameters
    self.symbol = 'BTC/USDT:USDT'
    self.initial_balance = 153  # USDT
    self.position_size_percent = 0.8  # 80% ของทุน
    self.leverage = 30
    self.tp_points = 200  # Take profit +200 USDT
    self.sl_points = 400  # Stop loss -400 USDT
    
def setup_leverage(self):
    """ตั้งค่า leverage"""
    try:
        result = self.exchange.set_leverage(self.leverage, self.symbol)
        print(f"Leverage set to {self.leverage}x: {result}")
        return True
    except Exception as e:
        print(f"Error setting leverage: {e}")
        return False

def get_current_price(self):
    """ดึงราคาปัจจุบันของ BTC/USDT"""
    try:
        ticker = self.exchange.fetch_ticker(self.symbol)
        return ticker['last']
    except Exception as e:
        print(f"Error fetching price: {e}")
        return None

def calculate_position_size(self, price):
    """คำนวณขนาดของ position"""
    try:
        # ใช้ 80% ของทุนกับ leverage 30x
        position_value = self.initial_balance * self.position_size_percent * self.leverage
        quantity = position_value / price
        return quantity
    except Exception as e:
        print(f"Error calculating position size: {e}")
        return None

def open_long_position(self):
    """เปิด Long position"""
    try:
        current_price = self.get_current_price()
        if not current_price:
            return False
        
        quantity = self.calculate_position_size(current_price)
        if not quantity:
            return False
        
        # เปิด Long position
        order = self.exchange.create_market_buy_order(
            symbol=self.symbol,
            amount=quantity,
            params={
                'tdMode': 'cross',  # Cross margin
                'side': 'buy',
                'posSide': 'long'
            }
        )
        
        print(f"Long position opened: {order}")
        print(f"Entry price: {current_price}")
        print(f"Quantity: {quantity}")
        
        # ตั้ง TP/SL
        self.set_tp_sl(current_price, quantity)
        
        return True
        
    except Exception as e:
        print(f"Error opening long position: {e}")
        return False

def set_tp_sl(self, entry_price, quantity):
    """ตั้ง Take Profit และ Stop Loss"""
    try:
        # คำนวณราคา TP และ SL
        tp_price = entry_price + self.tp_points
        sl_price = entry_price - self.sl_points
        
        # Take Profit Order
        tp_order = self.exchange.create_order(
            symbol=self.symbol,
            type='limit',
            side='sell',
            amount=quantity,
            price=tp_price,
            params={
                'tdMode': 'cross',
                'posSide': 'long',
                'ordType': 'conditional',
                'triggerPx': tp_price,
                'orderPx': tp_price
            }
        )
        
        # Stop Loss Order
        sl_order = self.exchange.create_order(
            symbol=self.symbol,
            type='market',
            side='sell',
            amount=quantity,
            params={
                'tdMode': 'cross',
                'posSide': 'long',
                'ordType': 'conditional',
                'triggerPx': sl_price,
                'orderPx': '-1'  # Market order
            }
        )
        
        print(f"TP set at: {tp_price}")
        print(f"SL set at: {sl_price}")
        print(f"TP Order: {tp_order}")
        print(f"SL Order: {sl_order}")
        
    except Exception as e:
        print(f"Error setting TP/SL: {e}")

def get_account_balance(self):
    """ดูยอดเงินในบัญชี"""
    try:
        balance = self.exchange.fetch_balance()
        return balance['USDT']['free']
    except Exception as e:
        print(f"Error fetching balance: {e}")
        return None

def get_positions(self):
    """ดูตำแหน่งปัจจุบัน"""
    try:
        positions = self.exchange.fetch_positions([self.symbol])
        return positions
    except Exception as e:
        print(f"Error fetching positions: {e}")
        return None

def run_bot(self):
    """เริ่มรันบอท"""
    print("=" * 50)
    print("OKX Trading Bot Starting...")
    print(f"Time: {datetime.now()}")
    print("=" * 50)
    
    # ตรวจสอบการเชื่อมต่อ
    try:
        markets = self.exchange.load_markets()
        print("✓ Connected to OKX successfully")
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        return
    
    # ดูยอดเงินปัจจุบัน
    balance = self.get_account_balance()
    if balance:
        print(f"Current balance: {balance} USDT")
    
    # ตั้งค่า leverage
    if not self.setup_leverage():
        print("Failed to set leverage")
        return
    
    # ดูราคาปัจจุบัน
    current_price = self.get_current_price()
    if current_price:
        print(f"Current BTC price: {current_price}")
    
    # เปิด Long position
    if self.open_long_position():
        print("✓ Long position opened successfully")
    else:
        print("✗ Failed to open long position")
        return
    
    # Monitor position
    print("\nBot is now monitoring the position...")
    while True:
        try:
            positions = self.get_positions()
            if positions:
                for pos in positions:
                    if pos['size'] > 0:
                        print(f"Position: {pos['side']} {pos['size']} BTC")
                        print(f"Entry Price: {pos['entryPrice']}")
                        print(f"Mark Price: {pos['markPrice']}")
                        print(f"PnL: {pos['unrealizedPnl']} USDT")
                    else:
                        print("No active positions")
                        break
            
            time.sleep(60)  # Check every minute
            
        except KeyboardInterrupt:
            print("\nBot stopped by user")
            break
        except Exception as e:
            print(f"Error in monitoring: {e}")
            time.sleep(60)
```

if **name** == “**main**”:
bot = OKXTradingBot()
bot.run_bot()
