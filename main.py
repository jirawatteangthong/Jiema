# ==============================================================================
# 1. IMPORTS & CONFIGURATION
# ==============================================================================
import ccxt
import os

# Load API key from environment variables
API_KEY = os.getenv('RAILWAY_API_KEY', 'YOUR_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('RAILWAY_SECRET', 'YOUR_SECRET_HERE_FOR_LOCAL_TESTING')
PASSWORD = os.getenv('RAILWAY_PASSWORD', 'YOUR_PASSWORD_HERE_FOR_LOCAL_TESTING')

# --- Settings ---
SYMBOL = "BTC/USDT:USDT"        # OKX Futures symbol
LEVERAGE = 30                   # Leverage used
TP_DISTANCE = 100               # Take profit (+100 USD)
SL_DISTANCE = 400               # Stop loss (-400 USD)
TRADE_PERCENT = 0.8            # Use 80% of available balance
SAFETY_BUFFER = 0.97           # Use only 97% of calculated funds to prevent margin error

# --- Initialize OKX instance ---
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',  # Futures/Perpetual
    }
})


# ==============================================================================
# 2. UTILITY FUNCTIONS
# ==============================================================================

def set_leverage():
    exchange.set_leverage(LEVERAGE, SYMBOL, params={'marginMode': 'cross'})


def get_cross_balance():
    """ดึงยอด USDT ที่มีใน cross margin futures"""
    balances = exchange.fetch_balance({'type': 'swap'})
    return balances['total'].get('USDT', 0)


def calculate_order_amount(balance: float) -> float:
    available_margin = balance * 0.95  # กันไว้อีก 5% ป้องกัน margin error
    contract_value = 100  # OKX BTC = 100 USD/contract
    max_position_value = available_margin * LEVERAGE
    amount = max_position_value / contract_value
    return round(amount, 2)


def check_existing_position():
    """เช็กว่ามีโพซิชันเปิดอยู่แล้วหรือไม่"""
    positions = exchange.fetch_positions([SYMBOL], {'type': 'swap'})
    for p in positions:
        if p['symbol'] == SYMBOL and p['contracts'] > 0:
            return p
    return None


# ==============================================================================
# 3. ORDER LOGIC
# ==============================================================================

def open_long_order():
    # --- ข้อมูลพื้นฐาน ---
    price = exchange.fetch_ticker(SYMBOL)['last']
    balance = get_cross_balance()
    amount = calculate_order_amount(balance)
    estimated_cost = amount * 100 / LEVERAGE

    print(f"\n📊 ราคา BTC ปัจจุบัน: {price:.2f} USDT")
    print(f"💰 Balance: {balance:.2f} USDT")
    print(f"📈 ใช้ทุนจริงหลัง buffer: {balance * TRADE_PERCENT * SAFETY_BUFFER:.2f} USDT")
    print(f"🔢 เปิดจำนวน: {amount} สัญญา (ต้องใช้ margin ≈ {estimated_cost:.2f} USDT)")

    # --- เช็กว่า margin เพียงพอหรือไม่ ---
    if balance < estimated_cost:
        print(f"❌ Margin ไม่พอ! ต้องการอย่างน้อย {estimated_cost:.2f} USDT")
        return

    # --- เปิด Long Market Order ---
    exchange.create_market_buy_order(
        symbol=SYMBOL,
        amount=amount,
        params={
            'marginMode': 'cross',
            'reduceOnly': False
        }
    )

    # --- ตั้ง TP/SL ---
    entry_price = price
    tp_price = round(entry_price + TP_DISTANCE, 2)
    sl_price = round(entry_price - SL_DISTANCE, 2)

    print(f"🎯 TP: {tp_price} | 🛑 SL: {sl_price}")

    # TP: Trigger Price + Market Execution
    exchange.create_order(
        symbol=SYMBOL,
        type='take_profit_market',
        side='sell',
        amount=amount,
        params={
            'triggerPrice': tp_price,
            'orderPx': '-1',  # Market order
            'marginMode': 'cross',
            'reduceOnly': True
        }
    )

    # SL: Trigger Price + Market Execution
    exchange.create_order(
        symbol=SYMBOL,
        type='stop_market',
        side='sell',
        amount=amount,
        params={
            'triggerPrice': sl_price,
            'orderPx': '-1',  # Market order
            'marginMode': 'cross',
            'reduceOnly': True
        }
    )

    print("✅ เปิดออเดอร์ Long + ตั้ง TP/SL เรียบร้อยแล้ว\n")


# ==============================================================================
# 4. MAIN LOGIC
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
