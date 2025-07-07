import ccxt
import os

# โหลดค่า API จาก Environment Variable (เช่น Railway)
API_KEY = os.getenv('RAILWAY_API_KEY', 'YOUR_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('RAILWAY_SECRET', 'YOUR_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('RAILWAY_PASSWORD', 'YOUR_PASSWORD_HERE_FOR_LOCAL_TESTING')

# ตั้งค่าเบื้องต้น
symbol = "BTC/USDT:USDT"   # Futures (Perpetual Swap)
leverage = 30
tp_distance = 100          # TP +100 USD
sl_distance = 400          # SL -400 USD
trade_percent = 0.8        # ใช้ทุน 80% ของพอร์ต

# สร้าง instance ของ OKX
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',  # Futures
    }
})


def set_leverage():
    exchange.set_leverage(leverage, symbol, params={'marginMode': 'cross'})


def get_cross_balance():
    balances = exchange.fetch_balance({'type': 'swap'})
    usdt_balance = balances['total'].get('USDT', 0)
    return usdt_balance


def calculate_order_amount(price: float, balance: float, leverage: float, percent: float = 0.8):
    usdt_to_use = balance * percent
    position_value = usdt_to_use * leverage
    amount = position_value / price
    return round(amount, 3)


def get_open_position():
    positions = exchange.fetch_positions([symbol], {'type': 'swap'})
    for p in positions:
        if p['symbol'] == symbol and p['contracts'] > 0:
            return p
    return None


def open_long_order():
    price = exchange.fetch_ticker(symbol)['last']
    balance = get_cross_balance()
    amount = calculate_order_amount(price, balance, leverage, trade_percent)

    print(f"\n--- เปิด Long Order ---")
    print(f"Balance: {balance:.2f} USDT")
    print(f"ใช้สัญญา: {amount} @ ราคา: {price:.2f}")

    # สั่งซื้อ (Long)
    order = exchange.create_market_buy_order(
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

    # ตั้ง Take Profit (trigger order)
    exchange.create_order(
        symbol=symbol,
        type='take_profit_market',
        side='sell',
        amount=amount,
        params={
            'triggerPrice': tp_price,
            'marginMode': 'cross',
            'reduceOnly': True
        }
    )

    # ตั้ง Stop Loss (trigger order)
    exchange.create_order(
        symbol=symbol,
        type='stop_market',
        side='sell',
        amount=amount,
        params={
            'triggerPrice': sl_price,
            'marginMode': 'cross',
            'reduceOnly': True
        }
    )

    print("--- ออเดอร์เปิดเรียบร้อยแล้ว ---")


if __name__ == '__main__':
    print("🚀 เริ่มต้นการทำงาน...\n")

    set_leverage()

    position = get_open_position()
    if position:
        print(f"⚠️ มีโพซิชันเปิดอยู่แล้ว ({position['side']}) จำนวน: {position['contracts']} สัญญา")
        print("❌ ยกเลิกการเปิดออเดอร์ซ้ำ")
    else:
        open_long_order()
