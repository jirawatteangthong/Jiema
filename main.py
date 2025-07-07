import ccxt
import os

# ดึง API Key จาก Railway Environment Variables
API_KEY = os.getenv('RAILWAY_API_KEY', 'YOUR_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('RAILWAY_SECRET', 'YOUR_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('RAILWAY_PASSWORD', 'YOUR_PASSWORD_HERE_FOR_LOCAL_TESTING')

symbol = "BTC/USDT:USDT"
leverage = 30
tp_distance = 100
sl_distance = 400

exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',
    }
})

def set_leverage():
    exchange.set_leverage(leverage, symbol, params={'marginMode': 'cross'})

def open_long_order():
    price = exchange.fetch_ticker(symbol)['last']
    amount = 1  # จำนวน contract เล็กสุดที่ OKX รับได้

    print(f"เปิด Long ที่ราคา: {price}")
    exchange.create_market_buy_order(
        symbol=symbol,
        amount=amount,
        params={
            'marginMode': 'cross',
            'reduceOnly': False
        }
    )

    entry_price = price
    take_profit = entry_price + tp_distance
    stop_loss = entry_price - sl_distance

    print(f"ตั้ง TP: {take_profit}, SL: {stop_loss}")

    exchange.create_order(
        symbol=symbol,
        type='take_profit_market',
        side='sell',
        amount=amount,
        params={
            'triggerPrice': take_profit,
            'marginMode': 'cross',
            'reduceOnly': True
        }
    )

    exchange.create_order(
        symbol=symbol,
        type='stop_market',
        side='sell',
        amount=amount,
        params={
            'triggerPrice': stop_loss,
            'marginMode': 'cross',
            'reduceOnly': True
        }
    )

if __name__ == '__main__':
    set_leverage()
    open_long_order()
