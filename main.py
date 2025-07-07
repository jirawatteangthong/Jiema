import ccxt
import os

# ------------------------------------------------------------------------------
# 🔐 Load API Credentials from Environment
# ------------------------------------------------------------------------------
API_KEY = os.getenv("RAILWAY_API_KEY", "your_api_key_here")
SECRET = os.getenv("RAILWAY_SECRET", "your_secret_here")
PASSWORD = os.getenv("RAILWAY_PASSWORD", "your_password_here")

# ------------------------------------------------------------------------------
# ⚙️ Config Settings
# ------------------------------------------------------------------------------
SYMBOL = 'BTC/USDT:USDT'
TP_DISTANCE = 100
SL_DISTANCE = 400
LEVERAGE = 10
MARGIN_BUFFER = 2  # เผื่อค่าธรรมเนียม

# ------------------------------------------------------------------------------
# 🔌 Connect to OKX Exchange (Futures, Cross Margin)
# ------------------------------------------------------------------------------
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',
        'defaultMarket': 'linear',
        'marginMode': 'cross',
    },
})

# ------------------------------------------------------------------------------
# 🔢 Calculate number of contracts based on 80% of balance
# ------------------------------------------------------------------------------
def calculate_order_amount(available_usdt: float, btc_price: float, leverage: int) -> int:
    desired_margin = available_usdt * 0.80
    notional = desired_margin * leverage
    contracts = int(notional / btc_price)

    min_margin = btc_price / leverage
    if contracts < 1 and available_usdt >= min_margin + MARGIN_BUFFER:
        return 1
    return contracts

# ------------------------------------------------------------------------------
# 🔍 Check if a Long position already exists
# ------------------------------------------------------------------------------
def get_open_position():
    positions = exchange.fetch_positions([SYMBOL])
    for pos in positions:
        if pos['symbol'] == SYMBOL and pos['contracts'] > 0 and pos['side'] == 'long':
            return pos
    return None

# ------------------------------------------------------------------------------
# 📈 Open Long Market Order + TP/SL
# ------------------------------------------------------------------------------
def open_long_order():
    ticker = exchange.fetch_ticker(SYMBOL)
    price = ticker['last']
    balance = exchange.fetch_balance({'type': 'swap'})
    available_usdt = float(balance['total']['USDT'])

    print(f"\n📊 ราคา BTC: {price:.2f} USDT")
    print(f"💰 Available Margin (OKX): {available_usdt:.2f} USDT")

    order_amount = calculate_order_amount(available_usdt, price, LEVERAGE)

    if order_amount < 1:
        print("❌ Margin ไม่พอเปิดออเดอร์ขั้นต่ำ (ต้องได้อย่างน้อย 1 สัญญา)")
        return

    used_margin = order_amount * price / LEVERAGE
    print(f"📈 ใช้หลัง buffer: {used_margin:.2f} USDT")
    print(f"🔢 เปิดจำนวน: {order_amount} สัญญา")

    if get_open_position():
        print("⚠️ มีโพซิชันเปิดอยู่แล้ว (long) → ยกเลิกการเปิดซ้ำ")
        return

    tp_price = round(price + TP_DISTANCE, 1)
    sl_price = round(price - SL_DISTANCE, 1)
    print(f"🎯 TP: {tp_price} | 🛑 SL: {sl_price}")

    try:
        order = exchange.create_market_buy_order(
            symbol=SYMBOL,
            amount=order_amount,
            params={
                "tdMode": "cross",
                "posSide": "long",
                "tpTriggerPx": str(tp_price),
                "tpOrdPx": "-1",
                "slTriggerPx": str(sl_price),
                "slOrdPx": "-1",
                "reduceOnly": False,
            }
        )
        print(f"✅ เปิด Long สำเร็จ: order ID → {order['id']}")
    except ccxt.BaseError as e:
        print(f"❌ เกิดข้อผิดพลาดตอนเปิดออเดอร์: {str(e)}")

# ------------------------------------------------------------------------------
# 🚀 Start Bot
# ------------------------------------------------------------------------------
print("\n🚀 เริ่มต้นการทำงาน...\n")
open_long_order()
