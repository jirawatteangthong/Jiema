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
MARGIN_BUFFER = 5 # กลับมาใช้ 5 USDT เพราะเราจะจำกัด Notional Value โดยตรง
# ✅ สิ่งที่ต้องปรับ: กำหนด Notional Value เป้าหมายที่ OKX ยอมรับ
# จากที่คุณทดลองเปิดได้ที่ 43.5 USDT (เมื่อลาก slider 1%)
TARGET_NOTIONAL_USDT = 43.5 # ✅ กำหนด Notional Value ที่จะเปิด
# FORCED_AMOUNT_PRECISION: เราจะให้ CCXT จัดการ precision ด้วยตัวเอง (amount_to_precision)
# เนื่องจากเราจะกำหนด Notional Value โดยตรง

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
# ------------------------------------------------------------------------------
# ✅ ฟังก์ชันใหม่เพื่อคำนวณ Amount และ Margin จาก Notional Value ที่กำหนด
def calculate_amount_from_notional(available_usdt: float, price: float, leverage: int, target_notional: float) -> tuple[float, float]:
    if price <= 0 or leverage <= 0 or target_notional <= 0:
        print("Error: Price, leverage, and target_notional must be positive.")
        return (0, 0)

    market_info = exchange.market(SYMBOL)
    if not market_info:
        print(f"❌ Could not fetch market info for {SYMBOL} in calculate_amount_from_notional.")
        return (0, 0)

    # 1. คำนวณ contracts จาก target_notional
    contracts_raw = target_notional / price

    # 2. ใช้ exchange.amount_to_precision เพื่อปัดเศษให้ถูกต้องตาม Exchange
    # CCXT ควรจะใช้ precision.amount หรือ amount.step ที่ถูกต้องจาก market_info
    contracts_to_open = exchange.amount_to_precision(SYMBOL, contracts_raw)
    contracts_to_open = float(contracts_to_open) # Ensure it's float

    # 3. คำนวณ Margin ที่ต้องการจริงๆ จาก contracts_to_open ที่ปัดเศษแล้ว
    actual_notional_after_precision = contracts_to_open * price
    required_margin = actual_notional_after_precision / leverage

    # 4. ตรวจสอบว่ามี Margin เพียงพอหรือไม่
    if available_usdt < required_margin + MARGIN_BUFFER:
        print(f"❌ Margin not sufficient for target Notional. Available: {available_usdt:.2f}, Required: {required_margin:.2f} (Est Cost) + {MARGIN_BUFFER} (Buffer) = {required_margin + MARGIN_BUFFER:.2f} USDT.")
        return (0, 0)

    # 5. ตรวจสอบขั้นต่ำของ amount ที่ Exchange กำหนด
    min_exchange_amount = market_info['limits']['amount']['min'] if 'amount' in market_info['limits'] and 'min' in market_info['limits']['amount'] and market_info['limits']['amount']['min'] is not None else 0
    if contracts_to_open < min_exchange_amount:
        print(f"❌ Calculated amount {contracts_to_open:.4f} is less than exchange's minimum amount {min_exchange_amount:.4f}. Cannot open.")
        return (0, 0)
        
    print(f"💡 DEBUG (calculate_amount_from_notional): Target Notional: {target_notional:.2f}")
    print(f"💡 DEBUG (calculate_amount_from_notional): Raw contracts: {contracts_raw:.4f}")
    print(f"💡 DEBUG (calculate_amount_from_notional): Contracts after precision: {contracts_to_open:.4f}")
    print(f"💡 DEBUG (calculate_amount_from_notional): Actual Notional after precision: {actual_notional_after_precision:.2f}")
    print(f"💡 DEBUG (calculate_amount_from_notional): Required Margin: {required_margin:.2f} USDT")

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

        # ✅ เรียกใช้ฟังก์ชันใหม่ที่กำหนด Notional Value โดยตรง
        order_amount, estimated_used_margin = calculate_amount_from_notional(
            available_usdt, current_price, LEVERAGE, TARGET_NOTIONAL_USDT
        )

        if float(order_amount) == 0:
            print("❌ Cannot open order as calculated amount is zero or insufficient after all checks.")
            return

        print(f"📈 Estimated Margin for Order (Recalculated): {estimated_used_margin:.2f} USDT")
        # ✅ แสดงจำนวนสัญญาในรูปแบบที่เหมาะสม (ไม่ต้องบังคับทศนิยม 2 ตำแหน่งตายตัว)
        # ให้ CCXT amount_to_precision จัดการเอง
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
