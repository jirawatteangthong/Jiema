import ccxt
import time
import os
from datetime import datetime
import math

class BinanceTradingBot:
    def __init__(self):
        # Binance API credentials - MAKE SURE THESE MATCH YOUR RAILWAY ENVIRONMENT VARIABLES
        self.api_key = os.getenv('BINANCE_API_KEY') 
        self.secret = os.getenv('BINANCE_SECRET')   

        if not all([self.api_key, self.secret]):
            print("Error: Please set BINANCE_API_KEY and BINANCE_SECRET environment variables.")
            exit()

        # Initialize exchange
        self.exchange = ccxt.binance({ 
            'apiKey': self.api_key,
            'secret': self.secret,
            'sandbox': False,  # Set to True for testnet
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future', # For Binance Futures
                'marginMode': 'cross', # Explicitly setting cross margin
            },
        })
        
        # Trading parameters
        self.symbol = 'BTC/USDT' # ใช้ BTC/USDT ตามที่คุณต้องการทดสอบ
        self.position_size_percent = 0.8  
        self.leverage = 30 # Leverage 30x ตามที่คุณต้องการ
        self.tp_distance = 100  # Take profit +100 USDT
        self.sl_distance = 200  # Stop loss -200 USDT
        self.margin_buffer = 5 

        self.forced_amount_step_size = 0.001 
        self.target_notional_usdt = 80 # Notional Value สำหรับ BTC (เพื่อให้ได้ 0.001 BTC ขั้นต่ำ)

        # Load markets early
        try:
            self.exchange.load_markets()
            print("✅ Binance markets loaded successfully.")
        except Exception as e:
            print(f"❌ Failed to load Binance markets: {e}") 
            print("Please check your API keys, network connection, or Binance status.")
            exit()
    
    def setup_leverage(self):
        """ตั้งค่า leverage และ margin mode"""
        try:
            result = self.exchange.set_leverage(self.leverage, self.symbol)
            print(f"Leverage set to {self.leverage}x for {self.symbol}: {result}")
            return True
        except ccxt.ExchangeError as e:
            if "leverage is not valid" in str(e) or "not valid for this symbol" in str(e):
                print(f"❌ Error: Leverage {self.leverage}x is not valid for {self.symbol} on Binance.")
                print("Please check Binance UI for max allowed leverage for BTC/USDT and update self.leverage in config.")
                return False
            print(f"Error setting leverage: {e}. Details: {e}")
            return False 
        except Exception as e:
            print(f"An unexpected error occurred setting leverage: {e}")
            return False

    def get_current_price(self):
        """ดึงราคาปัจจุบันของ Symbol"""
        try:
            ticker = self.exchange.fetch_ticker(self.symbol)
            return ticker['last']
        except Exception as e:
            print(f"Error fetching price for {self.symbol}: {e}")
            return None

    def calculate_order_details(self, available_usdt: float, price: float) -> tuple[float, float]:
        """
        Calculates the order amount (contracts) and estimated margin required
        based on target notional value and exchange rules.
        """
        if price <= 0 or self.leverage <= 0 or self.target_notional_usdt <= 0:
            print("Error: Price, leverage, and target_notional_usdt must be positive.")
            return (0, 0)

        market_info = self.exchange.market(self.symbol)
        if not market_info:
            print(f"❌ Could not fetch market info for {self.symbol}.")
            return (0, 0)
        
        exchange_amount_step = market_info['limits']['amount']['step'] if 'amount' in market_info['limits'] and 'step' in market_info['limits']['amount'] and market_info['limits']['amount']['step'] is not None else self.forced_amount_step_size
        actual_step_size = max(self.forced_amount_step_size, float(exchange_amount_step))

        contracts_raw = self.target_notional_usdt / price
        
        contracts_to_open = round(contracts_raw / actual_step_size) * actual_step_size
        contracts_to_open = float(f"{contracts_to_open:.10f}") 

        actual_notional_after_precision = contracts_to_open * price
        required_margin = actual_notional_after_precision / self.leverage

        min_exchange_amount = market_info['limits']['amount']['min'] if 'amount' in market_info['limits'] and 'min' in market_info['limits']['amount'] and market_info['limits']['amount']['min'] is not None else 0
        
        if contracts_to_open < min_exchange_amount:
            print(f"❌ Calculated amount {contracts_to_open:.8f} is less than exchange's minimum amount {min_exchange_amount:.8f}. Cannot open.") 
            return (0, 0)
        
        if available_usdt < required_margin + self.margin_buffer:
            print(f"❌ Margin not sufficient. Available: {available_usdt:.2f}, Required: {required_margin:.2f} + {self.margin_buffer} (Buffer) = {required_margin + self.margin_buffer:.2f} USDT.")
            return (0, 0)
        
        print(f"💡 DEBUG (calculate_order_details): Target Notional: {self.target_notional_usdt:.2f}")
        print(f"💡 DEBUG (calculate_order_details): Raw contracts: {contracts_raw:.8f}") 
        print(f"💡 DEBUG (calculate_order_details): Actual Step Size Used: {actual_step_size}")
        print(f"💡 DEBUG (calculate_order_details): Contracts after step size adjustment: {contracts_to_open:.8f}") 
        print(f"💡 DEBUG (calculate_order_details): Actual Notional after step size: {actual_notional_after_precision:.2f}")
        print(f"💡 DEBUG (calculate_order_details): Calculated Required Margin: {required_margin:.2f} USDT")
        print(f"💡 DEBUG (calculate_order_details): Min Exchange Amount: {min_exchange_amount:.8f}") 


        return (contracts_to_open, required_margin)

    def get_account_balance(self):
        """ดูยอดเงินในบัญชี (เฉพาะ USDT free balance)"""
        try:
            balance = self.exchange.fetch_balance() 
            free_usdt = balance.get('USDT', {}).get('free', 0)
            if free_usdt == 0:
                for asset_info in balance.get('info', {}).get('assets', []):
                    if asset_info.get('asset') == 'USDT':
                        free_usdt = float(asset_info.get('availableBalance', 0)) 
                        break
            
            return float(free_usdt)
        except Exception as e:
            print(f"Error fetching balance: {e}")
            return None

    def get_positions(self):
        """ดูตำแหน่งปัจจุบัน"""
        try:
            positions = self.exchange.fetch_positions([self.symbol])
            return [pos for pos in positions if pos['contracts'] != 0] 
        except Exception as e:
            print(f"Error fetching positions: {e}")
            return None

    def cancel_open_tp_sl_orders(self):
        """ยกเลิกคำสั่ง TP/SL ที่ค้างอยู่สำหรับ Symbol ปัจจุบัน"""
        print(f"⏳ Checking for and canceling open TP/SL orders for {self.symbol}...")
        try:
            # ดึงคำสั่งที่เปิดอยู่ทั้งหมดสำหรับ Symbol นี้
            open_orders = self.exchange.fetch_open_orders(self.symbol)
            
            canceled_count = 0
            for order in open_orders:
                # ตรวจสอบว่าเป็นคำสั่ง TP/SL ที่เราสร้างขึ้นมาหรือไม่
                # Binance Futures TP/SL orders often have specific types like 'TAKE_PROFIT_MARKET', 'STOP_MARKET', 'STOP'
                # or have 'reduceOnly' param set to true.
                if order['type'] in ['TAKE_PROFIT_MARKET', 'STOP_MARKET', 'STOP', 'TAKE_PROFIT', 'STOP_LOSS'] and \
                   order['reduceOnly'] == True: # Check if it's a reduce-only order
                    try:
                        self.exchange.cancel_order(order['id'], self.symbol)
                        print(f"✅ Canceled old TP/SL order: ID {order['id']}, Type: {order['type']}, Side: {order['side']}, Price: {order['price']}")
                        canceled_count += 1
                    except ccxt.BaseError as e:
                        print(f"❌ Failed to cancel order {order['id']}: {str(e)}")
                elif order['type'] in ['LIMIT', 'MARKET'] and order['reduceOnly'] == True: # In case reduce-only LIMIT/MARKET are used
                     try:
                        self.exchange.cancel_order(order['id'], self.symbol)
                        print(f"✅ Canceled old reduce-only order: ID {order['id']}, Type: {order['type']}, Side: {order['side']}, Price: {order['price']}")
                        canceled_count += 1
                     except ccxt.BaseError as e:
                        print(f"❌ Failed to cancel order {order['id']}: {str(e)}")
            
            if canceled_count == 0:
                print("No old TP/SL orders found to cancel.")
            else:
                print(f"✓ Successfully canceled {canceled_count} old TP/SL orders.")

        except ccxt.NetworkError as e:
            print(f"❌ Network error while fetching/canceling open orders: {e}")
        except ccxt.ExchangeError as e:
            print(f"❌ Exchange error while fetching/canceling open orders: {e}")
        except Exception as e:
            print(f"❌ An unexpected error occurred while canceling orders: {e}")

    def open_short_position(self):
        """เปิด Short position"""
        try:
            current_price = self.get_current_price()
            if not current_price:
                print("Failed to get current price.")
                return False
            
            available_balance = self.get_account_balance()
            if available_balance is None:
                print("Failed to get available balance.")
                return False

            existing_positions = self.get_positions()
            for pos in existing_positions:
                if pos['symbol'] == self.symbol and pos['side'] == 'short' and pos['contracts'] > 0:
                    print(f"⚠️ An open short position already exists for {self.symbol} (size: {pos['contracts']:.8f}). Skipping new order.") 
                    # ✅ เพิ่มการเรียก cancel_open_tp_sl_orders ที่นี่ด้วย ถ้ามีสถานะอยู่แล้ว และไม่ได้ปิดไปก่อนหน้านี้
                    # อาจจะเป็นกรณีที่บอทรันใหม่ แล้วเจอสถานะที่ยังไม่ได้เคลียร์ TP/SL เก่า
                    self.cancel_open_tp_sl_orders() 
                    return False
            
            order_amount, estimated_used_margin = self.calculate_order_details(available_balance, current_price)
            
            if order_amount == 0:
                print("❌ Calculated order amount is zero or insufficient. Cannot open position.")
                return False
            
            print(f"📈 Estimated Margin for Order: {estimated_used_margin:.2f} USDT")
            decimal_places = int(round(-math.log10(self.forced_amount_step_size))) if self.forced_amount_step_size < 1 else 0
            print(f"🔢 Opening quantity: {order_amount:.{decimal_places}f} contracts")
            
            tp_price = round(current_price - self.tp_distance, 1) # TP for Short
            sl_price = round(current_price + self.sl_distance, 1) # SL for Short
            print(f"🎯 Calculated TP: {tp_price} | 🛑 Calculated SL: {sl_price}")

            # --- Step 1: Place Market SELL Order (Short) ---
            print(f"⏳ Placing market SELL order for {order_amount:.{decimal_places}f} contracts of {self.symbol}...")
            order = self.exchange.create_market_sell_order(
                symbol=self.symbol,
                amount=float(order_amount),
                params={
                    'reduceOnly': False,
                }
            )
            print(f"✅ Market SELL order placed: ID → {order['id']}")
            time.sleep(2) 

            # --- Step 2: Set Take Profit Order ---
            print(f"⏳ Setting Take Profit order at {tp_price}...")
            try:
                tp_order = self.exchange.create_order(
                    symbol=self.symbol,
                    type='TAKE_PROFIT_MARKET',
                    side='buy',   
                    amount=float(order_amount),
                    price=None, 
                    params={
                        'stopPrice': tp_price,
                        'reduceOnly': True, 
                    }
                )
                print(f"✅ Take Profit order placed: ID → {tp_order['id']}")
            except ccxt.BaseError as e:
                print(f"❌ Failed to set Take Profit order: {str(e)}")

            # --- Step 3: Set Stop Loss Order ---
            print(f"⏳ Setting Stop Loss order at {sl_price}...")
            try:
                sl_order = self.exchange.create_order(
                    symbol=self.symbol,
                    type='STOP_MARKET', 
                    side='buy',         
                    amount=float(order_amount),
                    price=None,         
                    params={
                        'stopPrice': sl_price,
                        'reduceOnly': True,
                    }
                )
                print(f"✅ Stop Loss order placed: ID → {sl_order['id']}")
            except ccxt.BaseError as e:
                print(f"❌ Failed to set Stop Loss order: {str(e)}")

            return True
            
        except ccxt.NetworkError as e:
            print(f"❌ Network error opening position: {e}")
            return False
        except ccxt.ExchangeError as e:
            print(f"❌ Exchange error opening position: {e}. Error details: {e}")
            return False
        except Exception as e:
            print(f"❌ An unexpected error occurred opening position: {e}")
            return False

    def run_bot(self):
        """เริ่มรันบอท"""
        print("=" * 50)
        print("Binance Futures Trading Bot Starting...") 
        print(f"Time: {datetime.now()}")
        print("=" * 50)
        
        if not self.setup_leverage():
            print("Failed initial setup (leverage). Exiting.")
            return
        
        print("\nBot is now monitoring and attempting to open position...")
        while True:
            try:
                # ✅ เพิ่มการตรวจสอบและยกเลิกคำสั่งค้างอยู่ก่อนที่จะดำเนินการอื่นๆ
                self.cancel_open_tp_sl_orders()

                current_positions = self.get_positions()
                if not current_positions:
                    print("\nNo active positions. Attempting to open new SHORT position...")
                    if self.open_short_position():
                        print("✓ Successfully opened a new position. Monitoring...")
                    else:
                        print("✗ Failed to open new position. Retrying after delay...")
                else:
                    print("\nActive positions found. Monitoring current state...")
                    for pos in current_positions:
                        pos_side_display = 'LONG' if pos['side'] == 'long' else 'SHORT'
                        contracts_val = float(pos.get('contracts', 0))
                        decimal_places = int(round(-math.log10(self.forced_amount_step_size))) if self.forced_amount_step_size < 1 else 0
                        
                        print(f"Position: {pos_side_display} {contracts_val:.{decimal_places}f} {pos['symbol']}")
                        print(f"Entry Price: {pos['entryPrice']:.2f}")
                        print(f"Mark Price: {pos['markPrice']:.2f}")
                        print(f"PnL: {pos['unrealizedPnl']:.2f} USDT")

            except KeyboardInterrupt:
                print("\nBot stopped by user")
                break
            except Exception as e:
                print(f"Error in main loop: {e}")
            
            time.sleep(60)

if __name__ == "__main__":
    bot = BinanceTradingBot()
    bot.run_bot()
