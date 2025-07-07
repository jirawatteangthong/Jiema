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
MARGIN_BUFFER = 2
MIN_NOTIONAL_VALUE_USDT = 10 # ลองเริ่มต้นด้วยค่านี้ก่อน อาจจะต้องปรับขึ้นหากยัง error
CONTRACT_SIZE_UNIT = 0.001 # ETH contracts are usually in 0.001 or 0.01 units. Check OKX's minimum trade unit for ETH/USDT.


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
# ✅ สิ่งที่ต้องแก้: บังคับโหลดตลาดทันทีหลังจากเชื่อมต่อ
try:
    exchange.load_markets()
    print("✅ OKX markets loaded successfully.")
except Exception as e:
    print(f"❌ Failed to load OKX markets: {e}")
    print("Please check your API keys, network connection, or OKX status.")
    exit()

# ------------------------------------------------------------------------------
# 🔢 Calculate number of contracts based on 80% of balance
# ------------------------------------------------------------------------------
def calculate_order_amount(available_usdt: float, price: float, leverage: int) -> float:
    if price <= 0 or leverage <= 0:
        print("Error: Price and leverage must be positive.")
        return 0

    desired_margin = available_usdt * 0.80
    notional = desired_margin * leverage

    min_contracts_by_notional = MIN_NOTIONAL_VALUE_USDT / price
    calculated_contracts = notional / price

    contracts_to_open = max(calculated_contracts, min_contracts_by_notional)

    required_margin = (contracts_to_open * price) / leverage

    # ดึง market_info ภายในฟังก์ชันเพื่อใช้ amount_to_precision ได้อย่างถูกต้อง
    market_info = exchange.market(SYMBOL)
    if not market_info: # ควรจะโหลดแล้ว แต่เพิ่มเช็คเผื่อ
        print(f"❌ Could not fetch market info for {SYMBOL} in calculate_order_amount.")
        return 0

    # ตรวจสอบว่ามีเงินพอสำหรับ contracts_to_open หรือไม่
    # และว่าจำนวนสัญญาที่คำนวณได้ไม่น้อยกว่าขั้นต่ำของ Exchange
    if available_usdt < required_margin + MARGIN_BUFFER or \
       contracts_to_open < market_info['limits']['amount']['min']:
        print(f"❌ Margin or min amount not sufficient. Available: {available_usdt:.2f}, Required: {required_margin:.2f} + {MARGIN_BUFFER} USDT, Calculated Contracts: {contracts_to_open:.4f}, Min Exchange Contracts: {market_info['limits']['amount']['min']}.")

        # ลองพยายามเปิดแค่ minimum amount ที่ Exchange ต้องการ ถ้าเงินพอ
        min_exchange_amount = market_info['limits']['amount']['min']
        min_exchange_notional = min_exchange_amount * price
        min_exchange_margin = min_exchange_notional / leverage

        if available_usdt >= min_exchange_margin + MARGIN_BUFFER:
            print(f"✅ Sufficient funds to open minimum exchange amount: {min_exchange_amount} contracts.")
            return exchange.amount_to_precision(SYMBOL, min_exchange_amount)
        return 0 # ไม่พอจริงๆ

    return exchange.amount_to_precision(SYMBOL, contracts_to_open)


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

        order_amount = calculate_order_amount(available_usdt, current_price, LEVERAGE)

        if float(order_amount) == 0: # ตรวจสอบเป็น float
            print("❌ Cannot open order as calculated amount is zero or insufficient.")
            return

        estimated_used_margin = (float(order_amount) * current_price) / LEVERAGE
        print(f"📈 Estimated Margin for Order: {estimated_used_margin:.2f} USDT")
        print(f"🔢 Opening quantity: {order_amount} contracts")

        tp_price = round(current_price - TP_DISTANCE, 1)
        sl_price = round(current_price + SL_DISTANCE, 1)
        print(f"🎯 Calculated TP: {tp_price} | 🛑 Calculated SL: {sl_price}")

        # --- ขั้นตอนที่ 1: เปิด Market Short Order โดยไม่มี TP/SL ---
        print(f"⏳ Placing market SELL order for {order_amount} contracts of {SYMBOL}...")
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
