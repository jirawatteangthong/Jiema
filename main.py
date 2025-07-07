# ==============================================================================
# 1. IMPORTS & CONFIGURATION
# ==============================================================================
import ccxt
import os

API_KEY = os.getenv('RAILWAY_API_KEY', 'YOUR_API_KEY_HERE_FOR_LOCAL')
SECRET = os.getenv('RAILWAY_SECRET', 'YOUR_SECRET_HERE_FOR_LOCAL')
PASSWORD = os.getenv('RAILWAY_PASSWORD', 'YOUR_PASSWORD_HERE_FOR_LOCAL')

SYMBOL = "BTC/USDT:USDT"
LEVERAGE = 30
TP_DISTANCE = 100
SL_DISTANCE = 400
SAFETY_BUFFER = 0.95  # ใช้ได้ 95% ของ available margin
CONTRACT_VALUE = 100  # 1 contract = 100 USD

exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',  # futures
    }
})

# ==============================================================================
# 2. FUNCTIONS
# ==============================================================================

def set_leverage():
    exchange.set_leverage(LEVERAGE, SYMBOL, params={'marginMode': 'cross'})

def get_available_margin():
    balance = exchange.fetch_balance({'type': 'swap'})
    details = balance['info'].get('details', [])
    for item in details:
        if item['ccy'] == 'USDT':
            return float(item.get('availBal', 0))
    return 0

def calculate_order_amount(avail_margin: float):
    usable = avail_margin * SAFETY_BUFFER
    max_position_value = usable * LEVERAGE
    contracts = max_position_value / CONTRACT_VALUE
    return round(contracts, 2)

def check_existing_position():
    positions = exchange.fetch_positions([SYMBOL], {'type': 'swap'})
    for p in positions:
        if p['symbol'] == SYMBOL and p['contracts'] > 0:
            return p
    return None

def open_long_order():
    price = exchange.fetch_ticker(SYMBOL)['last']
    avail_margin = get_available_margin()
    amount = calculate_order_amount(avail_margin)

    print(f"\n📊 ราคา BTC: {price:.2f} USDT")
    print(f"💰 Available Margin (OKX): {avail_margin:.2f} USDT")
    print(f"📈 ใช้หลัง buffer: {avail_margin * SAFETY_BUFFER:.2f} USDT")
    print(f"🔢 เปิดจำนวน: {amount} สัญญา (ประมาณ {amount * CONTRACT_VALUE / LEVERAGE:.2f} USDT margin)")

    if amount < 1:
        print("❌ Margin ไม่พอเปิดออเดอร์ขั้นต่ำ (ต้องได้อย่างน้อย 1 สัญญา)")
        return

    # === Open Long Order ===
    exchange.create_market_buy_order(
        symbol=SYMBOL,
        amount=amount,
        params={
            'marginMode': 'cross',
            'reduceOnly': False
        }
    )

    tp_price = round(price + TP_DISTANCE, 2)
    sl_price = round(price - SL_DISTANCE, 2)
    print(f"🎯 TP: {tp_price} | 🛑 SL: {sl_price}")

    # === Set TP ===
    exchange.create_order(
        symbol=SYMBOL,
        type='take_profit_market',
        side='sell',
        amount=amount,
        params={
            'triggerPrice': tp_price,
            'orderPx': '-1',
            'marginMode': 'cross',
            'reduceOnly': True
        }
    )

    # === Set SL ===
    exchange.create_order(
        symbol=SYMBOL,
        type='stop_market',
        side='sell',
        amount=amount,
        params={
            'triggerPrice': sl_price,
            'orderPx': '-1',
            'marginMode': 'cross',
            'reduceOnly': True
        }
    )

    print("✅ เปิดออเดอร์เรียบร้อย พร้อม TP/SL\n")

# ==============================================================================
# 3. MAIN
# ==============================================================================

if __name__ == '__main__':
    print("🚀 เริ่มต้นการทำงาน...\n")
    set_leverage()

    position = check_existing_position()
    if position:
        print(f"⚠️ มีโพซิชันเปิดอยู่แล้ว ({position['side']}) จำนวน: {position['contracts']} สัญญา")
        print("❌ ยกเลิกการเปิดออเดอร์ซ้ำ\n")
    else:
        open_long_order()
