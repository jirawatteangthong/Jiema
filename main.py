import ccxt
import os
import time
import math # เพิ่ม import math สำหรับ math.ceil

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

# ✅ เพิ่มค่า Minimum Notional Value (มูลค่าสัญญาขั้นต่ำ) ที่อาจเป็นไปได้สำหรับ OKX
# ถ้าไม่แน่ใจ ให้ลองค่าที่น้อยๆ ก่อน เช่น 10 หรือ 20 USDT
# จากภาพ Max Buy Cost 113.74 อาจบ่งชี้ว่ามีขั้นต่ำสูง
# หรืออาจจะต้องคำนวณจาก .amountToPrecision() ของ Exchange
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

# ------------------------------------------------------------------------------
# 🔢 Calculate number of contracts based on 80% of balance
# ------------------------------------------------------------------------------
def calculate_order_amount(available_usdt: float, price: float, leverage: int) -> float: # ✅ เปลี่ยนเป็น float เพื่อรองรับทศนิยม
    if price <= 0 or leverage <= 0:
        print("Error: Price and leverage must be positive.")
        return 0

    desired_margin = available_usdt * 0.80
    notional = desired_margin * leverage

    # ✅ คำนวณจำนวนสัญญาขั้นต่ำตาม Notional Value ที่ OKX ต้องการ
    # Minimum contracts based on min notional value
    min_contracts_by_notional = MIN_NOTIONAL_VALUE_USDT / price

    # ✅ คำนวณจำนวนสัญญาที่ได้จาก Notional Value ของเรา
    calculated_contracts = notional / price

    # ใช้อันที่มากที่สุดระหว่าง min_contracts_by_notional กับ calculated_contracts
    contracts_to_open = max(calculated_contracts, min_contracts_by_notional)

    # ✅ ตรวจสอบว่ามีเงินพอสำหรับ contracts_to_open หรือไม่
    # Margin ที่ต้องใช้จริง = (contracts_to_open * price) / leverage
    required_margin = (contracts_to_open * price) / leverage

    if available_usdt < required_margin + MARGIN_BUFFER:
        print(f"❌ Margin ไม่พอสำหรับ {contracts_to_open:.4f} สัญญา (ต้องการ {required_margin:.2f} + {MARGIN_BUFFER} USDT)")
        # ถ้าไม่พอจริงๆ ให้ลองเปิดแค่ 1 หน่วยขั้นต่ำของ ETH ถ้ายังไม่พอ
        # This fallback is for 1 unit of ETH, not 1 "contract" as defined by OKX minimum.
        # This needs to be precisely aligned with OKX's actual minimum tradable unit.
        # Let's try to ensure at least 1 unit of CONTRACT_SIZE_UNIT.
        min_unit_notional = CONTRACT_SIZE_UNIT * price
        min_unit_margin = min_unit_notional / leverage
        if available_usdt >= min_unit_margin + MARGIN_BUFFER:
            # ✅ ใช้ exchange.amount_to_precision เพื่อปัดให้ตรงตาม Exchange step size
            # ต้อง fetch market info ก่อนเพื่อให้ amount_to_precision ทำงานได้ถูกต้อง
            # อาจจะต้องย้าย logic การคำนวณ amount ไปอยู่ใน open_short_order
            # เพื่อเข้าถึง exchange.market(SYMBOL)
            return CONTRACT_SIZE_UNIT # หรือ 1 สัญญา ถ้าเราแน่ใจว่า 1 สัญญาคือ 0.001 ETH
        return 0 # ไม่พอจริงๆ

    # ✅ ใช้ exchange.amount_to_precision เพื่อปัดให้ตรงกับ step size ที่ Exchange กำหนด
    # ต้อง fetch market info ก่อนเพื่อให้ amount_to_precision ทำงานได้ถูกต้อง
    # สำหรับตอนนี้ return float ไปก่อน แล้วปัดทีหลัง
    # หรือดึง info ของ Market มาใช้ที่นี่เลย
    # market_info = exchange.market(SYMBOL)
    # return exchange.amount_to_precision(market_info['symbol'], contracts_to_open)
    return contracts_to_open


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
        # ✅ ดึง Market Info ก่อน เพื่อใช้สำหรับ amount_to_precision
        market_info = exchange.market(SYMBOL)
        if not market_info:
            print(f"❌ Could not fetch market info for {SYMBOL}.")
            return

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

        # ✅ คำนวณจำนวนสัญญาและปัดเศษให้ถูกต้องตาม Exchange step size
        raw_order_amount = calculate_order_amount(available_usdt, current_price, LEVERAGE)
        order_amount = exchange.amount_to_precision(SYMBOL, raw_order_amount) # ✅ ปัดเศษให้ถูกต้อง

        # ตรวจสอบว่า order_amount ยังคงเป็น 0 หลังจากปัดเศษหรือไม่ (ถ้าได้ค่าน้อยมากๆ)
        if float(order_amount) < market_info['limits']['amount']['min']: # ตรวจสอบกับ min amount ของ Exchange
             print(f"❌ Calculated order amount {order_amount} is less than exchange's minimum amount {market_info['limits']['amount']['min']}.")
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
            amount=float(order_amount), # ✅ ส่งเป็น float
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
                amount=float(order_amount), # ✅ ส่งเป็น float
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
                amount=float(order_amount), # ✅ ส่งเป็น float
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
        print("💡 Exchange Error. This might be due to insufficient funds (again), or an unexpected parameter from OKX.")
        print(f"Error details: {e}") # พิมพ์รายละเอียด error ทั้งหมด
    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")

# ------------------------------------------------------------------------------
# 🚀 Start Bot
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n🚀 Starting the trading bot...\n")
    open_short_order()
