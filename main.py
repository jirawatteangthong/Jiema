import ccxt
import os
import time

# ------------------------------------------------------------------------------
# 🔐 Load API Credentials from Environment
# ------------------------------------------------------------------------------
API_KEY = os.getenv("RAILWAY_API_KEY")
SECRET = os.getenv("RAILWAY_SECRET")
PASSWORD = os.getenv("RAILWAY_PASSWORD")

if not all([API_KEY, SECRET, PASSWORD]):
    print("Error: Please set OKX_API_KEY, OKX_SECRET, and OKX_PASSWORD environment variables.")
    exit()

# ------------------------------------------------------------------------------
# ⚙️ Config Settings
# ------------------------------------------------------------------------------
SYMBOL = 'ETH/USDT'
# สำหรับ Short: TP_DISTANCE จะเป็นระยะห่างจากราคาปัจจุบัน "ลงไป"
# SL_DISTANCE จะเป็นระยะห่างจากราคาปัจจุบัน "ขึ้นไป"
TP_DISTANCE = 30    # Take Profit เมื่อราคาลงไป 30 USDT
SL_DISTANCE = 50    # Stop Loss เมื่อราคาขึ้นไป 50 USDT
LEVERAGE = 30
MARGIN_BUFFER = 2

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
def calculate_order_amount(available_usdt: float, price: float, leverage: int) -> int:
    if price <= 0 or leverage <= 0:
        print("Error: Price and leverage must be positive.")
        return 0

    desired_margin = available_usdt * 0.80
    notional = desired_margin * leverage
    contracts = int(notional / price)

    min_margin_for_one_contract = price / leverage
    if contracts < 1 and available_usdt >= min_margin_for_one_contract + MARGIN_BUFFER:
        return 1
    return contracts

# ------------------------------------------------------------------------------
# 🔍 Check if a Short position already exists
# ------------------------------------------------------------------------------
def get_open_position():
    positions = exchange.fetch_positions([SYMBOL])
    for pos in positions:
        # 🟢 จุดที่ 1: เปลี่ยนจาก 'long' เป็น 'short' เพื่อตรวจสอบสถานะ Short
        if pos['symbol'] == SYMBOL and pos['contracts'] > 0 and pos['side'] == 'short':
            return pos
    return None

# ------------------------------------------------------------------------------
# 📉 Open Short Market Order + TP/SL
# ------------------------------------------------------------------------------
def open_short_order(): # 🟢 เปลี่ยนชื่อฟังก์ชันให้สอดคล้อง
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        current_price = ticker['last']
        print(f"\n📊 Current Price of {SYMBOL}: {current_price:.2f} USDT")

        balance = exchange.fetch_balance({'type': 'swap'})
        available_usdt = float(balance.get('total', {}).get('USDT', 0))
        print(f"💰 Available Margin (OKX): {available_usdt:.2f} USDT")

        # 🟢 จุดที่ 2: ตรวจสอบสถานะ Short ที่เปิดอยู่แล้ว
        existing_position = get_open_position()
        if existing_position:
            print(f"⚠️ An open short position already exists for {SYMBOL} (size: {existing_position['contracts']}). Skipping new order.")
            return

        order_amount = calculate_order_amount(available_usdt, current_price, LEVERAGE)

        if order_amount < 1:
            print("❌ Insufficient margin to open even the minimum order (1 contract).")
            return

        estimated_used_margin = (order_amount * current_price) / LEVERAGE
        print(f"📈 Estimated Margin for Order: {estimated_used_margin:.2f} USDT")
        print(f"🔢 Opening quantity: {order_amount} contracts")

        # 🟢 จุดที่ 3: คำนวณ TP/SL สำหรับ Short
        # สำหรับ Short: TP จะต่ำกว่าราคาปัจจุบัน, SL จะสูงกว่าราคาปัจจุบัน
        tp_price = round(current_price - TP_DISTANCE, 1) # TP_DISTANCE ลดจากราคาเข้า
        sl_price = round(current_price + SL_DISTANCE, 1) # SL_DISTANCE เพิ่มจากราคาเข้า
        print(f"🎯 TP: {tp_price} | 🛑 SL: {sl_price}")

        # 🟢 จุดที่ 4: ใช้ create_market_sell_order แทน create_market_buy_order
        # และเปลี่ยน posSide เป็น 'short'
        order = exchange.create_market_sell_order( # เปลี่ยนเป็น sell order
            symbol=SYMBOL,
            amount=order_amount,
            params={
                "tdMode": "cross",
                "posSide": "short", # เปลี่ยนเป็น 'short'
                "reduceOnly": False,
            },
            takeProfit={'type': 'market', 'price': tp_price},
            stopLoss={'type': 'market', 'price': sl_price},
        )
        print(f"✅ Short order successfully placed: Order ID → {order['id']}")

    except ccxt.NetworkError as e:
        print(f"❌ Network error during order placement: {e}")
    except ccxt.ExchangeError as e:
        print(f"❌ Exchange error during order placement: {e}")
    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")

# ------------------------------------------------------------------------------
# 🚀 Start Bot
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n🚀 Starting the trading bot...\n")
    open_short_order() # 🟢 เรียกใช้ฟังก์ชัน open_short_order
