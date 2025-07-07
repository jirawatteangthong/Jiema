import ccxt
import time
import os
from datetime import datetime
import math

class BinanceTradingBot: # ✅ เปลี่ยนชื่อคลาสเป็น BinanceTradingBot
    def __init__(self):
        # Binance API credentials - ใส่ใน environment variables
        # ✅ เปลี่ยนชื่อตัวแปร Environment ให้ตรงกับ Binance
        self.api_key = os.getenv('BINANCE_API_KEY') 
        self.secret = os.getenv('BINANCE_SECRET')   
        # Binance Futures ไม่มี passphrase เหมือน OKX

        # Validate credentials
        if not all([self.api_key, self.secret]): # ✅ ตรวจสอบแค่ api_key และ secret
            print("Error: Please set BINANCE_API_KEY and BINANCE_SECRET environment variables.")
            exit()

        # Initialize exchange
        # ✅ เปลี่ยนเป็น ccxt.binance
        self.exchange = ccxt.binance({ 
            'apiKey': self.api_key,
            'secret': self.secret,
            'sandbox': False,  # Set to True for testnet
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future', # ✅ สำหรับ Binance Futures
                # 'defaultMarket': 'linear', # ไม่จำเป็นสำหรับ Binance.future
                'marginMode': 'cross', # Explicitly setting cross margin
            },
            # Binance ไม่ต้องการ 'urls' เหมือน OKX
        })
        
        # Trading parameters
        self.symbol = 'ETH/USDT' # ✅ Binance Futures มักจะใช้ Symbol แบบนี้
        self.position_size_percent = 0.8  # 80% ของ available margin
        self.leverage = 20 # ✅ ลองใช้ Leverage ที่ 20x บน Binance
        self.tp_distance = 30  # Take profit distance (e.g., +30 USDT from entry)
        self.sl_distance = 50  # Stop loss distance (e.g., -50 USDT from entry)
        self.margin_buffer = 5 # Buffer for fees/slippage

        # Binance มักจะมี precision/step size ที่ชัดเจนกว่า OKX
        # แต่ยังคงกำหนดไว้เพื่อความปลอดภัย
        self.forced_amount_step_size = 0.001 # ✅ Binance ETH/USDT Futures step size มักจะเป็น 0.001
        self.target_notional_usdt = 50 # ✅ เพิ่ม Notional target เป็น 50 USDT (จาก 43.5 เพื่อความปลอดภัย)

        # Load markets early
        try:
            self.exchange.load_markets()
            print("✅ Binance markets loaded successfully.")
        except Exception as e:
            print(f"❌ Failed to load Binance markets: {e}") # ✅ แก้ข้อความ Error
            print("Please check your API keys, network connection, or Binance status.")
            exit()
    
    def setup_leverage(self):
        """ตั้งค่า leverage และ margin mode"""
        try:
            # Binance set_leverage requires 'symbol' for futures.
            # It also often sets the margin mode implicitly.
            result = self.exchange.set_leverage(self.leverage, self.symbol)
            print(f"Leverage set to {self.leverage}x for {self.symbol}: {result}")

            # ✅ ตั้งค่า Margin Mode Explicitly (Binance specifics)
            # ต้องดูว่า OKX ให้ตั้งค่า Margin Mode ใน set_leverage ได้เลยหรือไม่
            # ถ้าไม่ได้ ต้องใช้ exchange.set_margin_mode(marginMode, symbol)
            # แต่ปกติ ccxt.binance จะจัดการให้เมื่อระบุใน options: {'marginMode': 'cross'}
            # หรือ:
            # self.exchange.set_margin_mode('cross', self.symbol)
            # print(f"Margin mode set to Cross for {self.symbol}")

            return True
        except ccxt.ExchangeError as e:
            if "leverage is not valid" in str(e) or "not valid for this symbol" in str(e):
                print(f"❌ Error: Leverage {self.leverage}x is not valid for {self.symbol} on Binance.")
                print("Please check Binance UI for max allowed leverage for ETH/USDT and update self.leverage in config.")
                return False
            print(f"Error setting leverage: {e}. Details: {e}")
            return False # หากตั้ง leverage ไม่ได้ ถือว่ารันต่อไม่ได้
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
        
        # Determine actual step size for amount. Use our FORCED_AMOUNT_STEP_SIZE if it's more restrictive.
        # Otherwise, use what CCXT reports.
        # For Binance, market_info['limits']['amount']['step'] is usually reliable
        exchange_amount_step = market_info['limits']['amount']['step'] if 'amount' in market_info['limits'] and 'step' in market_info['limits']['amount'] and market_info['limits']['amount']['step'] is not None else self.forced_amount_step_size
        actual_step_size = max(self.forced_amount_step_size, float(exchange_amount_step))

        # Calculate contracts based on TARGET_NOTIONAL_USDT
        contracts_raw = self.target_notional_usdt / price
        
        # Round contracts to the nearest actual_step_size
        contracts_to_open = round(contracts_raw / actual_step_size) * actual_step_size
        contracts_to_open = float(f"{contracts_to_open:.10f}") # Ensure floating point precision

        # Recalculate actual notional and required margin based on the precise contracts
        actual_notional_after_precision = contracts_to_open * price
        required_margin = actual_notional_after_precision / self.leverage

        # Final check against available margin and min/max limits
        min_exchange_amount = market_info['limits']['amount']['min'] if 'amount' in market_info['limits'] and 'min' in market_info['limits']['amount'] and market_info['limits']['amount']['min'] is not None else 0
        
        # Check if the calculated amount is less than the exchange's minimum allowed amount
        if contracts_to_open < min_exchange_amount:
            print(f"❌ Calculated amount {contracts_to_open:.4f} is less than exchange's minimum amount {min_exchange_amount:.4f}. Cannot open.")
            return (0, 0)
        
        # Check if there's enough margin (including buffer)
        if available_usdt < required_margin + self.margin_buffer:
            print(f"❌ Margin not sufficient. Available: {available_usdt:.2f}, Required: {required_margin:.2f} + {self.margin_buffer} (Buffer) = {required_margin + self.margin_buffer:.2f} USDT.")
            return (0, 0)
        
        print(f"💡 DEBUG (calculate_order_details): Target Notional: {self.target_notional_usdt:.2f}")
        print(f"💡 DEBUG (calculate_order_details): Raw contracts: {contracts_raw:.4f}")
        print(f"💡 DEBUG (calculate_order_details): Actual Step Size Used: {actual_step_size}")
        print(f"💡 DEBUG (calculate_order_details): Contracts after step size adjustment: {contracts_to_open:.4f}")
        print(f"💡 DEBUG (calculate_order_details): Actual Notional after step size: {actual_notional_after_precision:.2f}")
        print(f"💡 DEBUG (calculate_order_details): Calculated Required Margin: {required_margin:.2f} USDT")
        print(f"💡 DEBUG (calculate_order_details): Min Exchange Amount: {min_exchange_amount:.4f}")


        return (contracts_to_open, required_margin)

    def get_account_balance(self):
        """ดูยอดเงินในบัญชี (เฉพาะ USDT free balance)"""
        try:
            # Binance uses 'USDT' for USDT-M Futures. No 'swap' type needed here.
            balance = self.exchange.fetch_balance() 
            # ✅ สำหรับ Binance Futures, free balance มักจะอยู่ใน 'free' ของสกุลเงินนั้นๆ ใน 'total' object
            return float(balance.get('USDT', {}).get('free', 0))
        except Exception as e:
            print(f"Error fetching balance: {e}")
            return None

    def get_positions(self):
        """ดูตำแหน่งปัจจุบัน"""
        try:
            # Binance fetch_positions for Futures may need to specify 'marginMode' or rely on default.
            # Filter out positions where 'contracts' is zero
            positions = self.exchange.fetch_positions([self.symbol])
            return [pos for pos in positions if pos['contracts'] != 0] 
        except Exception as e:
            print(f"Error fetching positions: {e}")
            return None

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
                    print(f"⚠️ An open short position already exists for {self.symbol} (size: {pos['contracts']:.4f}). Skipping new order.")
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
            # For Binance, 'posSide' is often not needed for simple market orders in One-Way mode.
            # 'tdMode' is OK for OKX, but not a standard CCXT param for Binance create_order.
            order = self.exchange.create_market_sell_order( # ✅ ใช้ create_market_sell_order
                symbol=self.symbol,
                amount=float(order_amount),
                params={
                    # 'tdMode': 'cross', # ✅ ไม่จำเป็นสำหรับ Binance create_market_sell_order
                    # 'posSide': 'short', # ✅ ไม่จำเป็นสำหรับ Binance create_market_sell_order (ใน One-Way mode)
                    'reduceOnly': False,
                }
            )
            print(f"✅ Market SELL order placed: ID → {order['id']}")
            time.sleep(2) 

            # --- Step 2: Set Take Profit Order ---
            print(f"⏳ Setting Take Profit order at {tp_price}...")
            try:
                # For Binance, TP/SL are often set as 'stop' or 'take_profit' orders
                # with a 'stopPrice' (trigger price) and 'price' (limit price if stop-limit)
                # You usually attach TP/SL to the *position* or create separate orders.
                # Here, we create separate TP/SL orders that reduce the position.
                
                # Binance Futures: A Take Profit order is typically a LIMIT order triggered by a stopPrice
                tp_order = self.exchange.create_order(
                    symbol=self.symbol,
                    type='TAKE_PROFIT_MARKET', # ✅ Binance specific type for Take Profit Market Order
                    side='buy',   # To close a SHORT position, you BUY
                    amount=float(order_amount),
                    price=None, # Market order, no limit price
                    params={
                        'stopPrice': tp_price, # This is the trigger price for Binance
                        # 'tdMode': 'cross', # Not for Binance
                        # 'posSide': 'short', # Not for Binance TAKE_PROFIT_MARKET
                        'reduceOnly': True, # This order is only to reduce existing position
                    }
                )
                print(f"✅ Take Profit order placed: ID → {tp_order['id']}")
            except ccxt.BaseError as e:
                print(f"❌ Failed to set Take Profit order: {str(e)}")

            # --- Step 3: Set Stop Loss Order ---
            print(f"⏳ Setting Stop Loss order at {sl_price}...")
            try:
                # Binance Futures: A Stop Loss order is typically a MARKET order triggered by a stopPrice
                sl_order = self.exchange.create_order(
                    symbol=self.symbol,
                    type='STOP_MARKET', # ✅ Binance specific type for Stop Market Order
                    side='buy',         # To close a SHORT position, you BUY
                    amount=float(order_amount),
                    price=None,         # Market order, so price is None
                    params={
                        'stopPrice': sl_price, # This is the trigger price for Binance
                        # 'tdMode': 'cross', # Not for Binance
                        # 'posSide': 'short', # Not for Binance STOP_MARKET
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
        # ✅ แก้ข้อความต้อนรับ
        print("Binance Futures Trading Bot Starting...") 
        print(f"Time: {datetime.now()}")
        print("=" * 50)
        
        if not self.setup_leverage():
            print("Failed initial setup (leverage). Exiting.")
            return
        
        print("\nBot is now monitoring and attempting to open position...")
        while True:
            try:
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
    bot = BinanceTradingBot() # ✅ เปลี่ยนชื่อคลาสที่เรียกใช้
    bot.run_bot()

