import ccxt
import os
import time
import math

# ------------------------------------------------------------------------------
# 🔐 Load API Credentials from Environment
# ------------------------------------------------------------------------------
API_KEY = os.getenv("RAILWAY_API_KEY")
SECRET = os.getenv("RAILWAY_SECRET")
PASSWORD = os.getenv("RAILWAY_PASSWORD")

if not all([API_KEY, SECRET, PASSWORD]):
    print("Error: Please set RAILWAY_API_KEY, RAILWAY_SECRET, and RAILWAY_PASSWORD environment variables.")
    exit()

# ------------------------------------------------------------------------------
# ⚙️ Config Settings
# ------------------------------------------------------------------------------
SYMBOL = 'ETH/USDT'
TP_DISTANCE = 30
SL_DISTANCE = 50
LEVERAGE = 30
MARGIN_BUFFER = 15
TARGET_NOTIONAL_USDT = 43.5
FORCED_AMOUNT_STEP_SIZE = 0.01

# ------------------------------------------------------------------------------
# 🔌 Connect to OKX Exchange (Futures, Cross Margin)
# ------------------------------------------------------------------------------
def connect_exchange():
    return ccxt.okx({
        'apiKey': API_KEY,
        'secret': SECRET,
        'password': PASSWORD,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'swap',
            'defaultMarket': 'linear',
            'marginMode': 'cross',
        },
        'urls': {
            'api': {
                'public': 'https://www.okx.com/api/v5/public',
                'private': 'https://www.okx.com/api/v5/private',
            }
        }
    })

exchange = connect_exchange()
try:
    exchange.load_markets()
    print("✅ OKX markets loaded successfully.")
except Exception as e:
    print(f"❌ Failed to load OKX markets: {e}")
    print("Please check your API keys, network connection, or OKX status.")
    exit()

# ------------------------------------------------------------------------------
# 🔢 Calculate number of contracts based on TARGET_NOTIONAL_USDT
# ✅ สิ่งที่ต้องแก้: ย้ายฟังก์ชันนี้ขึ้นมาไว้ตรงนี้
# ------------------------------------------------------------------------------
def calculate_amount_from_notional(available_usdt: float, price: float, leverage: int, target_notional: float) -> tuple[float, float]:
    if price <= 0 or leverage <= 0 or target_notional <= 0:
        print("Error: Price, leverage, and target_notional must be positive.")
        return (0, 0)

    market_info = exchange.market(SYMBOL)
    if not market_info:
        print(f"❌ Could not fetch market info for {SYMBOL} in calculate_amount_from_notional.")
        return (0, 0)

    print(f"💡 DEBUG: Market Info Precision (from CCXT): {market_info.get('precision', {}).get('amount')}")
    print(f"💡 DEBUG: Market Info Limits Amount (from CCXT): {market_info.get('limits', {}).get('amount')}")
    print(f"💡 DEBUG: Market Info Limits Cost (from CCXT): {market_info.get('limits', {}).get('cost')}")


    desired_available_for_margin = available_usdt * 0.80 - MARGIN_BUFFER
    if desired_available_for_margin <= 0:
        print(f"❌ Desired available for margin is too low after buffer: {desired_available_for_margin:.2f} USDT.")
        return (0, 0)

    notional = desired_available_for_margin * leverage

    min_notional_exchange = 0
    max_notional_exchange = float('inf')

    if 'limits' in market_info and 'cost' in market_info['limits']:
        cost_limits = market_info['limits']['cost']
        if 'min' in cost_limits and cost_limits['min'] is not None:
            min_notional_exchange = cost_limits['min']
        if 'max' in cost_limits and cost_limits['max'] is not None:
            max_notional_exchange = cost_limits['max']
    
    min_exchange_amount = market_info['limits']['amount']['min'] if 'amount' in market_info['limits'] and 'min' in market_info['limits']['amount'] and market_info['limits']['amount']['min'] is not None else 0

    actual_min_amount = max(min_exchange_amount, FORCED_AMOUNT_STEP_SIZE)

    target_notional = max(notional, actual_min_amount * price)
    target_notional = min(target_notional, max_notional_exchange)

    contracts_raw = target_notional / price

    contracts_to_open = round(contracts_raw / FORCED_AMOUNT_STEP_SIZE) * FORCED_AMOUNT_STEP_SIZE
    contracts_to_open = float(f"{contracts_to_open:.10f}")

    if contracts_to_open < actual_min_amount:
        contracts_to_open = actual_min_amount
        print(f"💡 DEBUG: Contracts too low after step size, adjusted to actual_min_amount: {contracts_to_open:.4f}")

    actual_notional_after_precision = contracts_to_open * price
    required_margin = actual_notional_after_precision / leverage

    if available_usdt < required_margin + MARGIN_BUFFER:
        print(f"❌ Margin not sufficient after precision adjustment. Available: {available_usdt:.2f}, Required: {required_margin:.2f} (Est Cost) + {MARGIN_BUFFER} (Buffer) = {required_margin + MARGIN_BUFFER:.2f} USDT.")
        return (0, 0)
        
    print(f"💡 DEBUG: Available USDT: {available_usdt:.2f}")
    print(f"💡 DEBUG: Desired Available for Margin (after buffer): {desired_available_for_margin:.2f}")
    print(f"💡 DEBUG: Desired Notional from margin (after buffer): {notional:.2f}")
    print(f"💡 DEBUG: Min Exchange Notional (limits.cost.min): {min_notional_exchange:.2f}")
    print(f"💡 DEBUG: Max Exchange Notional (limits.cost.max): {max_notional_exchange:.2f}")
    print(f"💡 DEBUG: Actual Minimum Amount (based on CCXT or FORCED_AMOUNT_STEP_SIZE): {actual_min_amount:.4f}")
    print(f"💡 DEBUG: Target Notional (after limits): {target_notional:.2f}")
    print(f"💡 DEBUG: Raw contracts: {contracts_raw:.4f}")
    print(f"💡 DEBUG: Contracts after step size adjustment (FORCED): {contracts_to_open:.4f}")
    print(f"💡 DEBUG: Actual Notional (after step size): {actual_notional_after_precision:.2f}")
    print(f"💡 DEBUG: Calculated Required Margin (Estimated Cost): {required_margin:.2f} USDT")

    return (contracts_to_open, required_margin)


# ------------------------------------------------------------------------------
# 🔍 Check if a Short position already exists
# ------------------------------------------------------------------------------
def get_open_position():
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for pos in positions:
            if pos['symbol'] == SYMBOL and pos['contracts'] > 0 and pos['side'] == 'short':
                return pos
        return None
    except ccxt.NetworkError as e:
        print(f"Network error fetching positions: {e}")
        return None
    except ccxt.ExchangeError as e:
        print(f"Exchange error fetching positions: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred while fetching positions: {e}")
        return None

# ------------------------------------------------------------------------------
# 📉 Open Short Market Order + Set TP then SL
# ------------------------------------------------------------------------------
def open_short_order():
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        current_price = ticker['last']
        print(f"\n📊 Current Price of {SYMBOL}: {current_price:.2f} USDT")

        balance = exchange.fetch_balance({'type': 'swap'})
        available_usdt = float(balance.get('total', {}).get('USDT', 0))
        print(f"💰 Available Margin (OKX): {available_usdt:.2f} USDT")

        existing_position = get_open_position()
        if existing_position:
            print(f"⚠️ An open short position already exists for {SYMBOL} (size: {existing_position['contracts']}). Skipping new order.")
            return

        order_amount, estimated_used_margin = calculate_amount_from_notional( # ✅ เรียกใช้ฟังก์ชันที่ย้ายมาด้านบน
            available_usdt, current_price, LEVERAGE, TARGET_NOTIONAL_USDT
        )

        if float(order_amount) == 0:
            print("❌ Cannot open order as calculated amount is zero or insufficient after all checks.")
            return

        print(f"📈 Estimated Margin for Order (Recalculated): {estimated_used_margin:.2f} USDT")
        decimal_places = int(round(-math.log10(FORCED_AMOUNT_STEP_SIZE))) if FORCED_AMOUNT_STEP_SIZE < 1 else 0
        print(f"🔢 Opening quantity: {order_amount:.{decimal_places}f} contracts") 

        tp_price = round(current_price - TP_DISTANCE, 1)
        sl_price = round(current_price + SL_DISTANCE, 1)
        print(f"🎯 Calculated TP: {tp_price} | 🛑 Calculated SL: {sl_price}")

        # --- ขั้นตอนที่ 1: เปิด Market Short Order โดยไม่มี TP/SL ---
        print(f"⏳ Placing market SELL order for {order_amount:.{decimal_places}f} contracts of {SYMBOL}...")
        order = exchange.create_market_sell_order(
            symbol=SYMBOL,
            amount=float(order_amount),
            params={
                "tdMode": "cross",
                "reduceOnly": False,
            }
        )
        print(f"✅ Market SELL order placed: ID → {order['id']}")
        time.sleep(2) # รอ 2 วินาที

        # --- ขั้นตอนที่ 2: ตั้ง TP Order (Limit Order) ---
        print(f"⏳ Setting Take Profit order at {tp_price}...")
        try:
            tp_order = exchange.create_order(
                symbol=SYMBOL,
                type='limit',
                side='buy',
                amount=float(order_amount),
                price=tp_price,
                params={
                    "tdMode": "cross",
                    "posSide": "short",
                    "reduceOnly": True,
                }
            )
            print(f"✅ Take Profit order placed: ID → {tp_order['id']}")
        except ccxt.BaseError as e:
            print(f"❌ Failed to set Take Profit order: {str(e)}")

        # --- ขั้นตอนที่ 3: ตั้ง SL Order (Stop Market Order) ---
        print(f"⏳ Setting Stop Loss order at {sl_price}...")
        try:
            sl_order = exchange.create_order(
                symbol=SYMBOL,
                type='stop_market',
                side='buy',
                amount=float(order_amount),
                price=None,
                params={
                    "tdMode": "cross",
                    "posSide": "short",
                    "reduceOnly": True,
                    "triggerPx": str(sl_price),
                    "ordPx": "-1"
                }
            )
            print(f"✅ Stop Loss order placed: ID → {sl_order['id']}")
        except ccxt.BaseError as e:
            print(f"❌ Failed to set Stop Loss order: {str(e)}")

    except ccxt.NetworkError as e:
        print(f"❌ Network error during order placement: {e}")
    except ccxt.ExchangeError as e:
        print(f"❌ Exchange error during order placement: {e}")
        print(f"Error details: {e}")
    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")

# ------------------------------------------------------------------------------
# 🚀 Start Bot
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n🚀 Starting the trading bot...\n")
    open_short_order()
