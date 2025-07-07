import ccxt
import time
import os
from datetime import datetime
import math # Import math for calculations like log10

class OKXTradingBot:
    def __init__(self):
        # OKX API credentials - MAKE SURE THESE MATCH YOUR RAILWAY ENVIRONMENT VARIABLES
        self.api_key = os.getenv('RAILWAY_API_KEY') # Assuming RAILWAY_API_KEY consistent with previous
        self.secret = os.getenv('RAILWAY_SECRET')   # Assuming RAILWAY_SECRET consistent with previous
        self.passphrase = os.getenv('RAILWAY_PASSWORD') # Assuming RAILWAY_PASSWORD maps to passphrase

        # Validate credentials
        if not all([self.api_key, self.secret, self.passphrase]):
            print("Error: Please set RAILWAY_API_KEY, RAILWAY_SECRET, and RAILWAY_PASSWORD environment variables.")
            exit()

        # Initialize exchange
        self.exchange = ccxt.okx({
            'apiKey': self.api_key,
            'secret': self.secret,
            'password': self.passphrase, # CCXT uses 'password' for OKX's 'passphrase'
            'sandbox': False,  # Set to True for testnet
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap',  # Changed to 'swap' for perpetuals
                'defaultMarket': 'linear',
                'marginMode': 'cross', # Explicitly setting cross margin
            },
            'urls': { # Explicitly setting URLs for robustness
                'api': {
                    'public': 'https://www.okx.com/api/v5/public',
                    'private': 'https://www.okx.com/api/v5/private',
                }
            }
        })
        
        # Trading parameters
        self.symbol = 'ETH/USDT' # Changed to ETH/USDT for feasibility with 153 USDT
        self.position_size_percent = 0.8  # 80% of available margin
        self.leverage = 30
        self.tp_distance = 30  # Take profit distance (e.g., +30 USDT from entry)
        self.sl_distance = 50  # Stop loss distance (e.g., -50 USDT from entry)
        self.margin_buffer = 5 # Buffer for fees/slippage

        # Based on previous tests, OKX ETH/USDT uses 0.01 as a step size for amount
        self.forced_amount_step_size = 0.01 
        # Based on previous tests, we found 43.5 USDT notional worked manually
        self.target_notional_usdt = 43.5 # Notional value to target for order size

        # Load markets early
        try:
            self.exchange.load_markets()
            print("✅ OKX markets loaded successfully.")
        except Exception as e:
            print(f"❌ Failed to load OKX markets: {e}")
            print("Please check your API keys, network connection, or OKX status.")
            exit()
    
    def setup_leverage(self):
        """ตั้งค่า leverage และ margin mode"""
        try:
            # OKX set_leverage can also set margin mode.
            # We already set 'marginMode': 'cross' in init, so this primarily sets leverage.
            result = self.exchange.set_leverage(self.leverage, self.symbol, {'marginMode': 'cross'})
            print(f"Leverage set to {self.leverage}x for {self.symbol}: {result}")
            return True
        except ccxt.ExchangeError as e:
            print(f"Error setting leverage: {e}. This often happens if leverage is already set, or in 'isolated' mode. Details: {e}")
            # If leverage is already set, it might still be OK to continue.
            # You might want to check current leverage if this error occurs frequently.
            return True # Allow to continue if it's just 'already set' error
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
        exchange_amount_step = market_info['limits']['amount']['step'] if 'amount' in market_info['limits'] and 'step' in market_info['limits']['amount'] and market_info['limits']['amount']['step'] is not None else self.forced_amount_step_size
        actual_step_size = max(self.forced_amount_step_size, float(exchange_amount_step))

        # Calculate contracts based on TARGET_NOTIONAL_USDT
        contracts_raw = self.target_notional_usdt / price
        
        # Round contracts to the nearest actual_step_size
        contracts_to_open = round(contracts_raw / actual_step_size) * actual_step_size
        # Ensure floating point precision
        contracts_to_open = float(f"{contracts_to_open:.10f}") 

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

        return (contracts_to_open, required_margin)

    def get_account_balance(self):
        """ดูยอดเงินในบัญชี (เฉพาะ USDT free balance)"""
        try:
            balance = self.exchange.fetch_balance({'type': 'swap'}) # Specify 'swap' for perpetuals
            return float(balance.get('USDT', {}).get('free', 0))
        except Exception as e:
            print(f"Error fetching balance: {e}")
            return None

    def get_positions(self):
        """ดูตำแหน่งปัจจุบัน"""
        try:
            positions = self.exchange.fetch_positions([self.symbol])
            return [pos for pos in positions if pos['contracts'] != 0] # Filter out zero positions
        except Exception as e:
            print(f"Error fetching positions: {e}")
            return None

    def open_short_position(self): # Changed to open_short_position for consistency
        """เปิด Short position"""
        try:
            current_price = self.get_current_price()
            if not current_price:
                print("Failed to get current price.")
                return False
            
            # Fetch current balance dynamically
            available_balance = self.get_account_balance()
            if available_balance is None:
                print("Failed to get available balance.")
                return False

            # Check for existing short position before opening
            existing_positions = self.get_positions()
            for pos in existing_positions:
                if pos['symbol'] == self.symbol and pos['side'] == 'short' and pos['contracts'] > 0:
                    print(f"⚠️ An open short position already exists for {self.symbol} (size: {pos['contracts']}). Skipping new order.")
                    return False
            
            order_amount, estimated_used_margin = self.calculate_order_details(available_balance, current_price)
            
            if order_amount == 0:
                print("❌ Calculated order amount is zero or insufficient. Cannot open position.")
                return False
            
            print(f"📈 Estimated Margin for Order: {estimated_used_margin:.2f} USDT")
            # Calculate decimal places for printing based on actual step size
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
                    'tdMode': 'cross', # Cross margin mode
                    'reduceOnly': False, # This is to open a new position
                }
            )
            print(f"✅ Market SELL order placed: ID → {order['id']}")
            # Wait for order to be confirmed on the exchange
            time.sleep(2) 

            # --- Step 2: Set Take Profit Order ---
            print(f"⏳ Setting Take Profit order at {tp_price}...")
            try:
                tp_order = self.exchange.create_order(
                    symbol=self.symbol,
                    type='limit', # TP is typically a limit order
                    side='buy',   # To close a SHORT position, you BUY
                    amount=float(order_amount),
                    price=tp_price,
                    params={
                        'tdMode': 'cross',
                        'posSide': 'short', # This TP is for a SHORT position
                        'reduceOnly': True, # This order is only to reduce existing position
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
                    type='stop_market', # Stop-loss market order
                    side='buy',         # To close a SHORT position, you BUY
                    amount=float(order_amount),
                    price=None,         # Market order, so price is None
                    params={
                        'tdMode': 'cross',
                        'posSide': 'short', # This SL is for a SHORT position
                        'reduceOnly': True,
                        'triggerPx': str(sl_price), # Trigger price for the stop
                        'ordPx': '-1'               # -1 means market order upon trigger
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
        print("OKX Trading Bot Starting...")
        print(f"Time: {datetime.now()}")
        print("=" * 50)
        
        # Initial setup (leverage, etc.)
        if not self.setup_leverage():
            print("Failed initial setup. Exiting.")
            return
        
        # Main bot loop
        print("\nBot is now monitoring and attempting to open position...")
        while True:
            try:
                # Check for existing positions
                current_positions = self.get_positions()
                if not current_positions:
                    print("\nNo active positions. Attempting to open new SHORT position...")
                    if self.open_short_position(): # Changed to open_short_position
                        print("✓ Successfully opened a new position. Monitoring...")
                    else:
                        print("✗ Failed to open new position. Retrying after delay...")
                else:
                    print("\nActive positions found. Monitoring current state...")
                    for pos in current_positions:
                        # Check position side to display correctly
                        pos_side_display = 'LONG' if pos['side'] == 'long' else 'SHORT'
                        print(f"Position: {pos_side_display} {pos['contracts']:.4f} {pos['symbol']}")
                        print(f"Entry Price: {pos['entryPrice']:.2f}")
                        print(f"Mark Price: {pos['markPrice']:.2f}")
                        print(f"PnL: {pos['unrealizedPnl']:.2f} USDT")

            except KeyboardInterrupt:
                print("\nBot stopped by user")
                break
            except Exception as e:
                print(f"Error in main loop: {e}")
            
            time.sleep(60)  # Check every minute

if __name__ == "__main__":
    bot = OKXTradingBot()
    bot.run_bot()

