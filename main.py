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

        if order_amount < 1:
            print("❌ Insufficient margin to open even the minimum order (1 contract).")
            return

        estimated_used_margin = (order_amount * current_price) / LEVERAGE
        print(f"📈 Estimated Margin for Order: {estimated_used_margin:.2f} USDT")
        print(f"🔢 Opening quantity: {order_amount} contracts")

        tp_price = round(current_price - TP_DISTANCE, 1)
        sl_price = round(current_price + SL_DISTANCE, 1)
        print(f"🎯 Calculated TP: {tp_price} | 🛑 Calculated SL: {sl_price}")

        # --- ขั้นตอนที่ 1: เปิด Market Short Order โดยไม่มี TP/SL ---
        print(f"⏳ Placing market SELL order for {order_amount} contracts of {SYMBOL}...")
        order = exchange.create_market_sell_order(
            symbol=SYMBOL,
            amount=order_amount,
            params={
                "tdMode": "cross",
                # ✅ สำคัญ: ลบ "posSide": "short" ออกจากตรงนี้
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
                amount=order_amount,
                price=tp_price,
                params={
                    "tdMode": "cross",
                    "posSide": "short",   # คง posSide ไว้ที่นี่ (สำคัญสำหรับคำสั่งปิด)
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
                amount=order_amount,
                price=None,
                params={
                    "tdMode": "cross",
                    "posSide": "short",   # คง posSide ไว้ที่นี่ (สำคัญสำหรับคำสั่งปิด)
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
        print("💡 General Exchange Error. Check OKX dashboard for more details or current market status.")
        print("💡 The current error indicates 'Parameter posSide error' on the initial market order.")
    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")

# ------------------------------------------------------------------------------
# 🚀 Start Bot
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n🚀 Starting the trading bot...\n")
    open_short_order()
