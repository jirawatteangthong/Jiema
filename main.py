import ccxt
import os
import time

# ------------------------------------------------------------------------------
# 🔐 โหลด API จาก Environment
# ------------------------------------------------------------------------------
API_KEY = os.getenv("RAILWAY_API_KEY", "put_your_api_key_here")
SECRET = os.getenv("RAILWAY_SECRET", "put_your_secret_here")
PASSWORD = os.getenv("RAILWAY_PASSWORD", "put_your_password_here")

# ------------------------------------------------------------------------------
# ⚙️ ตั้งค่าพื้นฐาน
# ------------------------------------------------------------------------------
SYMBOL = 'BTC/USDT:USDT'  # Futures
TP_DISTANCE = 100          # TP +100$
SL_DISTANCE = 400          # SL -400$
LEVERAGE = 30

# ------------------------------------------------------------------------------
# 🔌 Connect OKX Futures
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
# 🔢 คำนวณจำนวนสัญญาแบบปลอดภัย (~90% ของ available margin)
# ------------------------------------------------------------------------------
def calculate_order_amount(available_usdt: float, btc_price: float, leverage: int) -> int:
    safe_margin = available_usdt * 0.90
    max_position_value = safe_margin * leverage
    contracts = int(max_position_value / btc_price)
    return contracts

# ------------------------------------------------------------------------------
# ✅ เช็กโพซิชันที่เปิดอยู่
# ------------------------------------------------------------------------------
def get_open_position():
    positions = exchange.fetch_positions([SYMBOL])
    for pos in positions:
        if pos['symbol'] == SYMBOL and pos['contracts'] > 0 and pos['side'] == 'long':
            return pos
    return None

# ------------------------------------------------------------------------------
# 🟩 เปิด Long Order + ตั้ง TP/SL
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

    # เช็กโพซิชันซ้ำ
    if get_open_position():
        print("⚠️ มีโพซิชันเปิดอยู่แล้ว (long) → ยกเลิกการเปิดซ้ำ")
        return

    # ตั้ง TP / SL
    tp_price = round(price + TP_DISTANCE, 1)
    sl_price = round(price - SL_DISTANCE, 1)

    print(f"🎯 TP: {tp_price} | 🛑 SL: {sl_price}")

    # เปิด Market Order พร้อม TP/SL
    try:
        order = exchange.create_market_buy_order(
            symbol=SYMBOL,
            amount=order_amount,
            params={
                "tdMode": "cross",
                "posSide": "long",
                "tpTriggerPx": str(tp_price),
                "tpOrdPx": "-1",  # market
                "slTriggerPx": str(sl_price),
                "slOrdPx": "-1",  # market
                "reduceOnly": False
            }
        )
        print("✅ เปิด Long สำเร็จ:", order['id'])

    except ccxt.BaseError as e:
        print("❌ เกิดข้อผิดพลาดตอนเปิดออเดอร์:", str(e))

# ------------------------------------------------------------------------------
# 🚀 เริ่มทำงาน
# ------------------------------------------------------------------------------
print("\n🚀 เริ่มต้นการทำงาน...\n")
open_long_order()
