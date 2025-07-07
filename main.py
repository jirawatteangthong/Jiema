import ccxt
import time
import os
from datetime import datetime
import math

class BinanceTradingBot:
    def __init__(self):
        self.api_key = os.getenv('BINANCE_API_KEY') 
        self.secret = os.getenv('BINANCE_SECRET')   

        if not all([self.api_key, self.secret]):
            print("Error: Please set BINANCE_API_KEY and BINANCE_SECRET environment variables.")
            exit()

        self.exchange = ccxt.binance({ 
            'apiKey': self.api_key,
            'secret': self.secret,
            'sandbox': False,  
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future', 
                'marginMode': 'cross',
            },
        })
        
        # Trading parameters
        self.symbol = 'BTC/USDT' 
        self.leverage = 30 
        self.tp_distance = 100  
        self.sl_distance = 200  
        self.margin_buffer = 5 # เพิ่ม Margin Buffer เพื่อความปลอดภัย (เผื่อค่าธรรมเนียม, slippage)

        self.forced_amount_step_size = 0.001 
        
        # ✅ ปรับ TARGET_POSITION_SIZE_FACTOR เป็น 0.95 เพื่อเปิดเกือบเต็ม Max
        self.target_position_size_factor = 0.95 # ✅ ใช้ 95% ของ Notional ที่สูงสุดที่เปิดได้
        
        # ✅ ลบ self.target_notional_usdt ออกไป เพราะเราจะคำนวณจากทุนจริง
        # ถ้าต้องการให้มีเพดานสูงสุดจริงๆ ก็สามารถตั้งเป็นค่าสูงๆ ได้ เช่น 100000 (1 แสน USDT)
        # self.target_notional_usdt = 135 # <-- ลบบรรทัดนี้ออก หรือตั้งค่าใหม่ตามต้องการ


        try:
            self.exchange.load_markets()
            print("✅ Binance markets loaded successfully.")
        except Exception as e:
            print(f"❌ Failed to load Binance markets: {e}") 
            print("Please check your API keys, network connection, or Binance status.")
            exit()
    
    def setup_leverage(self):
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
        try:
            ticker = self.exchange.fetch_ticker(self.symbol)
            return ticker['last']
        except Exception as e:
            print(f"Error fetching price for {self.symbol}: {e}")
            return None

    def calculate_order_details(self, available_usdt: float, price: float) -> tuple[float, float]:
        if price <= 0 or self.leverage <= 0 or self.target_position_size_factor <= 0: 
            print("Error: Price, leverage, and target_position_size_factor must be positive.")
            return (0, 0)

        market_info = self.exchange.market(self.symbol)
        if not market_info:
            print(f"❌ Could not fetch market info for {self.symbol}.")
            return (0, 0)
        
        exchange_amount_step = market_info['limits']['amount']['step'] if 'amount' in market_info['limits'] and 'step' in market_info['limits']['amount'] and market_info['limits']['amount']['step'] is not None else self.forced_amount_step_size
        actual_step_size = max(self.forced_amount_step_size, float(exchange_amount_step))

        # ✅ คำนวณ Notional Value สูงสุดที่ทำได้จากทุน
        max_notional_from_available_margin = (available_usdt - self.margin_buffer) * self.leverage
        if max_notional_from_available_margin <= 0:
            print(f"❌ Available margin ({available_usdt:.2f}) too low after buffer ({self.margin_buffer}) for any notional value.")
            return (0, 0)

        # ✅ คำนวณ target notional โดยใช้ factor เป็นเปอร์เซ็นต์ของ max_notional_from_available_margin
        target_notional_for_order = max_notional_from_available_margin * self.target_position_size_factor
        
        # ตรวจสอบขั้นต่ำ/สูงสุดของ Notional Value ที่ Exchange อนุญาต (ถ้ามีใน market_info)
        min_notional_exchange = market_info['limits']['cost']['min'] if 'cost' in market_info['limits'] and 'min' in market_info['limits']['cost'] and market_info['limits']['cost']['min'] is not None else 0
        max_notional_exchange = market_info['limits']['cost']['max'] if 'cost' in market_info['limits'] and 'max' in market_info['limits']['cost'] and market_info['limits']['cost']['max'] is not None else float('inf')

        # หาก target_notional_for_order ต่ำกว่ามูลค่าของ min_exchange_amount * price
        min_exchange_amount = market_info['limits']['amount']['min'] if 'amount' in market_info['limits'] and 'min' in market_info['limits']['amount'] and market_info['limits']['amount']['min'] is not None else 0
        min_notional_from_min_amount = min_exchange_amount * price

        # ✅ รวมการตรวจสอบทั้งหมด: ต้องไม่น้อยกว่าขั้นต่ำของ Exchange และไม่เกินสูงสุด
        target_notional_for_order = max(target_notional_for_order, min_notional_exchange, min_notional_from_min_amount)
        target_notional_for_order = min(target_notional_for_order, max_notional_exchange)
        
        # Convert notional to contracts (amount)
        contracts_raw = target_notional_for_order / price
        
        contracts_to_open = round(contracts_raw / actual_step_size) * actual_step_size
        contracts_to_open = float(f"{contracts_to_open:.10f}") 

        actual_notional_after_precision = contracts_to_open * price
        required_margin = actual_notional_after_precision / self.leverage

        if contracts_to_open < min_exchange_amount:
            print(f"❌ Calculated amount {contracts_to_open:.8f} is less than exchange's minimum amount {min_exchange_amount:.8f}. Cannot open (after final precision adjust).") 
            return (0, 0)
        
        if available_usdt < required_margin + self.margin_buffer:
            print(f"❌ Margin not sufficient. Available: {available_usdt:.2f}, Required: {required_margin:.2f} + {self.margin_buffer} (Buffer) = {required_margin + self.margin_buffer:.2f} USDT.")
            return (0, 0)
        
        print(f"💡 DEBUG (calculate_order_details): Max Notional from Available Margin: {max_notional_from_available_margin:.2f}")
        print(f"💡 DEBUG (calculate_order_details): Target Position Size Factor: {self.target_position_size_factor}")
        print(f"💡 DEBUG (calculate_order_details): Final Target Notional for Order: {target_notional_for_order:.2f}")
        print(f"💡 DEBUG (calculate_order_details): Raw contracts: {contracts_raw:.8f}") 
        print(f"💡 DEBUG (calculate_order_details): Actual Step Size Used: {actual_step_size}")
        print(f"💡 DEBUG (calculate_order_details): Contracts after step size adjustment: {contracts_to_open:.8f}") 
        print(f"💡 DEBUG (calculate_order_details): Actual Notional after step size: {actual_notional_after_precision:.2f}")
        print(f"💡 DEBUG (calculate_order_details): Calculated Required Margin: {required_margin:.2f} USDT")
        print(f"💡 DEBUG (calculate_order_details): Min Exchange Amount: {min_exchange_amount:.8f}") 
        print(f"💡 DEBUG (calculate_order_details): Min Notional Exchange: {min_notional_exchange:.2f}")
        print(f"💡 DEBUG (calculate_order_details): Min Notional from Min Amount: {min_notional_from_min_amount:.2f}")


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
            open_orders = self.exchange.fetch_open_orders(self.symbol)
            
            canceled_count = 0
            for order in open_orders:
                # Binance Futures TP/SL orders often have specific types like 'TAKE_PROFIT_MARKET', 'STOP_MARKET', etc.
                # และควรมี 'reduceOnly' เป็น True เพื่อเป็นคำสั่งปิด
                if order['type'] in ['TAKE_PROFIT_MARKET', 'STOP_MARKET', 'STOP', 'TAKE_PROFIT', 'STOP_LOSS'] and \
                   order.get('reduceOnly', False) == True: # ✅ ใช้ .get('reduceOnly', False) เพื่อป้องกัน KeyError
                    try:
                        self.exchange.cancel_order(order['id'], self.symbol)
                        print(f"✅ Canceled old TP/SL order: ID {order['id']}, Type: {order['type']}, Side: {order['side']}, Price: {order['price']}")
                        canceled_count += 1
                    except ccxt.BaseError as e:
                        print(f"❌ Failed to cancel order {order['id']}: {str(e)}")
                # บางทีอาจเป็น Limit/Market order ที่ reduceOnly (เช่น ถ้าเราใช้ create_order(type='limit', reduceOnly=True))
                elif order['type'] in ['LIMIT', 'MARKET'] and order.get('reduceOnly', False) == True:
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
                # ✅ เรียกใช้ฟังก์ชันยกเลิกคำสั่งค้างอยู่
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
