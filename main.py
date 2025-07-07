import ccxt
import os

# โหลด API Key จาก Environment Variables (ใช้ใน Railway หรือ .env)
API_KEY = os.getenv('RAILWAY_API_KEY', 'YOUR_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('RAILWAY_SECRET', 'YOUR_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('RAILWAY_PASSWORD', 'YOUR_PASSWORD_HERE_FOR_LOCAL_TESTING')

# ตั้งค่าพารามิเตอร์หลัก
symbol = "BTC/USDT:USDT"     # Futures Symbol
leverage = 30
tp_distance = 100            # +100 USD
sl_distance = 400            # -400 USD
trade_percent = 0.8          # ใช้ทุน 80% ของพอร์ต

# สร้างอินสแตนซ์ของ OKX
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',  # Futures (Perpetual)
    }
})


def set_leverage():
    exchange.set_leverage(leverage, symbol, params={'marginMode': 'cross'})


def get_cross_balance():
    balances = exchange.fetch_balance({'type': 'swap'})
    usdt_balance = balances['total'].get('USDT', 0)
    return usdt_balance


def calculate_order_amount(balance: float, leverage: float, percent: float = 0.8):
    usdt_to_use = balance * percent
    position_value = usdt_to_use * leverage
    contract_value = 100  # OKX Futures BTC: 1 contract = 100 USDT
    amount = position_value / contract_value
    return round(amount, 2)


def get_open_position():
    positions = exchange.fetch_positions([symbol], {'type': 'swap'})
    for p in positions:
        if p['symbol'] == symbol and p['contracts'] > 0:
            return p
    return None


def open_long_order():
    price = exchange.fetch_ticker(symbol)['last']
    balance = get_cross_balance()
    amount = calculate_order_amount(balance, leverage, trade_percent)

    print(f"\n--- เปิด Long Order ---")
    print(f"Balance: {balance:.2f} USDT")
    print(f"ใช้สัญญา: {amount} @ ราคา: {price:.2f}")

    # เปิด Market Order ฝั่ง Long
    exchange.create_market_buy_order(
        symbol=symbol,
        amount=amount,
        params={
            'marginMode': 'cross',
            'reduceOnly': False
        }
    )

    entry_price = price
    tp_price = round(entry_price + tp_distance, 2)
    sl_price = round(entry_price - sl_distance, 2)

    print(f"TP ตั้งที่: {tp_price} | SL ตั้งที่: {sl_price}")

    # สั่ง Take Profit
    exchange.create_order(
        symbol=symbol,
        type='take_profit_market',
        side='sell',
        amount=amount,
        params={
            'triggerPrice': tp_price,
            'orderPx': '-1',  # market order
            'marginMode': 'cross',
            'reduceOnly': True
        }
    )

    # สั่ง Stop Loss
    exchange.create_order(
        symbol=symbol,
        type='stop_market',
        side='sell',
        amount=amount,
        params={
            'triggerPrice': sl_price,
            'orderPx': '-1',  # market order
            'marginMode': 'cross',
            'reduceOnly': True
        }
    )

    print("--- ✅ ออเดอร์เปิดเรียบร้อยแล้ว ---\n")


if __name__ == '__main__':
    print("🚀 เริ่มต้นการทำงาน...\n")

    set_leverage()

    position = get_open_position()
    if position:
        print(f"⚠️ มีโพซิชันเปิดอยู่แล้ว ({position['side']}) จำนวน: {position['contracts']} สัญญา")
        print("❌ ยกเลิกการเปิดออเดอร์ซ้ำ\n")
    else:
        open_long_order()
