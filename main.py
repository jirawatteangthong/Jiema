# main.py
# Binance Futures Bot: EMA50/100 + LuxAlgo Nadaraya-Watson Envelope (Touch Mode)
# Precision version: สอดคล้องกับกราฟ LuxAlgo TradingView มากที่สุด

import os, time, math, statistics, logging
from dataclasses import dataclass
from datetime import datetime
import ccxt
from dotenv import load_dotenv

load_dotenv(override=True)

# ============ USER CONFIG ============
API_KEY = os.getenv('BINANCE_API_KEY', '')
SECRET = os.getenv('BINANCE_SECRET', '')
SYMBOL = os.getenv('SYMBOL', "BTC/USDT:USDT")
TIMEFRAME = os.getenv('TIMEFRAME', "15m")

LEVERAGE = 15
POSITION_MARGIN_FRACTION = 0.5  # 50% ของพอร์ตต่อไม้
ENTRY_MODE = 'touch'

EMA_FAST = 50
EMA_SLOW = 100

# 🎯 ปรับสูตร Nadaraya ให้ตรง LuxAlgo ที่สุด
NW_BANDWIDTH = 4.5
NW_MULT = 2.5
NW_LOOKBACK = 400

SL_POINTS = 300.0
BREAKEVEN_OFFSET = 100.0
LOOP_SECONDS = 10
OHLCV_CACHE_SECONDS = 60

DEBUG_MODE = True  # เปิด/ปิด log debug

# ============ LOGGING ============
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger("luxalgo-bot")

def dlog(msg):
    if DEBUG_MODE:
        log.info("[DEBUG] " + msg)

# ============ INDICATORS ============
def ema_series(values, n):
    if len(values) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e

# ✅ เวอร์ชันตรง LuxAlgo (Smoothed Non-Repaint)
def nwe_non_repaint(closes, h, mult, win):
    if len(closes) < win + 1:
        return None, None, None

    coefs = [math.exp(-(i ** 2) / (2 * h ** 2)) for i in range(win)]
    den = sum(coefs)
    weighted_sum = sum(closes[-1 - i] * coefs[i] for i in range(win))
    mid = weighted_sum / den

    # เพิ่ม smoothing layer แบบ LuxAlgo (EMA-like)
    smooth_mid = (mid + statistics.fmean(closes[-3:])) / 2

    diffs = [abs(closes[-1 - i] - smooth_mid) for i in range(1, win + 1)]
    mae = (sum(diffs) / win) * mult

    upper = smooth_mid + mae
    lower = smooth_mid - mae
    return upper, lower, smooth_mid

def trend_from_ema(closes):
    e50 = ema_series(closes, EMA_FAST)
    e100 = ema_series(closes, EMA_SLOW)
    return ("buy" if e50 > e100 else "sell"), e50, e100

def compute_sl(entry, side):
    return entry - SL_POINTS if side == 'long' else entry + SL_POINTS

# ============ EXCHANGE ============
class BinanceFutures:
    def __init__(self):
        self.ex = None

    def setup(self):
        self.ex = ccxt.binance({
            'apiKey': API_KEY,
            'secret': SECRET,
            'enableRateLimit': True,
            'options': {'defaultType': 'future'},
        })
        self.ex.load_markets()
        self.ex.set_leverage(LEVERAGE, SYMBOL)

    def fetch_ohlcv(self, tf, limit): return self.ex.fetch_ohlcv(SYMBOL, tf, limit)
    def ticker_last(self): return float(self.ex.fetch_ticker(SYMBOL)['last'])

    def fetch_position(self):
        ps = self.ex.fetch_positions([SYMBOL])
        for p in ps:
            if float(p.get('contracts', 0)) != 0:
                return {
                    'side': p['side'],
                    'contracts': float(p['contracts']),
                    'entry': float(p['entryPrice'])
                }
        return None

    def free_usdt(self):
        b = self.ex.fetch_balance({'type': 'future'})
        return float(b['USDT']['free'])

    def calc_qty(self, price):
        bal = self.free_usdt()
        margin = bal * POSITION_MARGIN_FRACTION
        notional = margin * LEVERAGE
        qty = notional / price
        return float(self.ex.amount_to_precision(SYMBOL, qty))

    def open_market(self, side, price):
        qty = self.calc_qty(price)
        side_ccxt = 'buy' if side == 'long' else 'sell'
        self.ex.create_market_order(SYMBOL, side_ccxt, qty)
        return self.fetch_position()

    def close_market(self):
        pos = self.fetch_position()
        if not pos: return
        side_ccxt = 'sell' if pos['side'] == 'long' else 'buy'
        self.ex.create_market_order(SYMBOL, side_ccxt, pos['contracts'], None, {'reduceOnly': True})

# ============ DATA CLASSES ============
@dataclass
class Bands:
    upper: float
    lower: float
    mid: float

@dataclass
class Position:
    side: str
    entry: float
    contracts: float
    sl: float
    tp: float
    sl_moved: bool = False

# ============ MAIN LOOP ============
def run():
    ex = BinanceFutures(); ex.setup()
    log.info(f"✅ Started LuxAlgo Precision Bot (loop={LOOP_SECONDS}s, debug={DEBUG_MODE})")

    pos_state = None
    sl_lock = False
    last_sl_side = None
    ohlcv_cache, last_fetch = None, 0

    while True:
        try:
            now = time.time()
            if ohlcv_cache is None or now - last_fetch > OHLCV_CACHE_SECONDS:
                ohlcv_cache = ex.fetch_ohlcv(TIMEFRAME, NW_LOOKBACK + 5)
                last_fetch = now

            # ✅ ใช้แท่งล่าสุดแทนแท่งก่อนหน้า (real-time touch)
            closes = [c[4] for c in ohlcv_cache]
            last_close = closes[-1]
            upper, lower, mid = nwe_non_repaint(closes, NW_BANDWIDTH, NW_MULT, NW_LOOKBACK)
            trend, ema50, ema100 = trend_from_ema(closes)
            price = ex.ticker_last()

            dlog(f"EMA50={ema50:.2f}, EMA100={ema100:.2f}, Trend={trend.upper()}")
            dlog(f"Close={last_close:.2f}, Upper={upper:.2f}, Lower={lower:.2f}, Mid={mid:.2f}")

            # ============ Unlock SL Lock ============
            if sl_lock:
                if last_sl_side == 'long' and price > mid:
                    sl_lock = False; last_sl_side = None
                    log.info(f"Unlock: price {price:.2f} > mid {mid:.2f}")
                elif last_sl_side == 'short' and price < mid:
                    sl_lock = False; last_sl_side = None
                    log.info(f"Unlock: price {price:.2f} < mid {mid:.2f}")
                else:
                    time.sleep(LOOP_SECONDS)
                    continue

            # ============ Manage Position ============
            if pos_state:
                if (pos_state.side == 'long' and price <= pos_state.sl) or (pos_state.side == 'short' and price >= pos_state.sl):
                    ex.close_market(); log.info(f"SL HIT {pos_state.side.upper()} at {price:.2f}")
                    sl_lock, last_sl_side, pos_state = True, pos_state.side, None
                    time.sleep(LOOP_SECONDS)
                    continue
                if (pos_state.side == 'long' and price >= pos_state.tp) or (pos_state.side == 'short' and price <= pos_state.tp):
                    ex.close_market(); log.info(f"TP HIT {pos_state.side.upper()} at {price:.2f}")
                    pos_state = None; time.sleep(LOOP_SECONDS); continue
                if not pos_state.sl_moved:
                    if pos_state.side == 'long' and last_close > mid:
                        pos_state.sl = pos_state.entry + BREAKEVEN_OFFSET; pos_state.sl_moved = True
                        log.info(f"Move SL → breakeven+{BREAKEVEN_OFFSET:.0f} ({pos_state.sl:.2f})")
                    elif pos_state.side == 'short' and last_close < mid:
                        pos_state.sl = pos_state.entry - BREAKEVEN_OFFSET; pos_state.sl_moved = True
                        log.info(f"Move SL → breakeven-{BREAKEVEN_OFFSET:.0f} ({pos_state.sl:.2f})")
                time.sleep(LOOP_SECONDS)
                continue

            # ============ Entry ============
            signal = None
            if trend == 'buy' and last_close <= lower and not sl_lock:
                signal = 'long'; dlog("✅ Entry → LONG (close<lower)")
            elif trend == 'sell' and last_close >= upper and not sl_lock:
                signal = 'short'; dlog("✅ Entry → SHORT (close>upper)")
            else:
                diff_upper = upper - last_close
                diff_lower = last_close - lower
                dlog(f"❌ No entry: sl_lock={sl_lock}, Δupper={diff_upper:.1f}, Δlower={diff_lower:.1f}")

            if signal:
                pos = ex.open_market(signal, price)
                if pos:
                    ps = Position(signal, pos['entry'], pos['contracts'], compute_sl(pos['entry'], signal),
                                  upper if signal == 'long' else lower)
                    pos_state = ps
                    log.info(f"OPEN {signal.upper()} entry={ps.entry:.2f} SL={ps.sl:.2f} TP={ps.tp:.2f}")

            time.sleep(LOOP_SECONDS)

        except Exception as e:
            log.warning(f"loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run()
