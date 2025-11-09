# main.py
# Binance Futures EMA50/100 + Nadaraya-Watson Envelope Bot
# with Daily Detailed Report (23:59 Telegram Summary)

import os, time, json, logging, math
from dataclasses import dataclass
from datetime import datetime
import requests, statistics
import ccxt
from dotenv import load_dotenv

load_dotenv(override=True)

# ======== ENV =========
API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY')
SECRET  = os.getenv('BINANCE_SECRET', 'YOUR_BINANCE_SECRET')
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID')

# ======== CONFIG =========
SYMBOL = "BTC/USDT:USDT"
TIMEFRAME = "15m"
LEVERAGE = 15
POSITION_MARGIN_FRACTION = 0.50
EMA_FAST = 50
EMA_SLOW = 100
NW_BANDWIDTH = 8.0
NW_MULT = 3.0
NW_LOOKBACK = 500
SL_POINTS = 300.0
LOOP_SECONDS = 3
REPORT_HH, REPORT_MM = 23, 59
LOG_FILE = "bot.log"
STATS_FILE = "daily_stats.json"

# ======== LOGGING =========
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")])
log = logging.getLogger("binance.m15.bot")

# ======== HELPERS =========
def ema_series(values, n):
    if len(values) < n: return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    out = [None] * (n - 1) + [e]
    for v in values[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def last_ema(values, n):
    es = ema_series(values, n)
    return es[-1] if es else None

def _gauss(x, h): return math.exp(-(x * x) / (2.0 * h * h))

def nwe_non_repaint(closes, h, mult, win):
    if len(closes) < win + 1: return None, None, None
    coefs = [_gauss(i, h) for i in range(win)]
    den = sum(coefs)
    mid = sum(closes[-1 - i] * coefs[i] for i in range(win)) / den
    diffs = [abs(closes[-1 - i] - mid) for i in range(1, win + 1)]
    mae = (sum(diffs) / win) * mult
    return mid + mae, mid - mae, mid

def tg_send(text):
    if not TELEGRAM_TOKEN or "YOUR" in TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.get(url, params={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}, timeout=10)
    except: pass

# ======== DAILY STATS =========
class DailyStats:
    def __init__(self, path=STATS_FILE):
        self.path = path
        self.data = {'date': datetime.now().strftime('%Y-%m-%d'), 'trades': [], 'pnl_usdt': 0.0}
        self._load()
        self.last_report_key = None

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, 'r', encoding='utf-8') as f:
                    self.data.update(json.load(f))
        except: pass

    def _save(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def add_trade(self, side, entry, close, qty, pnl, reason, entry_time):
        duration = (datetime.now() - entry_time).total_seconds()
        self.data['trades'].append({
            'time': datetime.now().strftime('%H:%M:%S'),
            'side': side,
            'entry': entry,
            'close': close,
            'qty': qty,
            'pnl_usdt': pnl,
            'reason': reason,
            'duration_sec': duration
        })
        self.data['pnl_usdt'] += pnl
        self._save()

    def roll_if_new_day(self):
        today = datetime.now().strftime('%Y-%m-%d')
        if self.data.get('date') != today:
            self.send_report(force=True)
            self.data = {'date': today, 'trades': [], 'pnl_usdt': 0.0}
            self._save()

    def send_report(self, force=False):
        now = datetime.now()
        if not force and not (now.hour == REPORT_HH and now.minute == REPORT_MM): return
        key = f"{self.data['date']}:{REPORT_HH}:{REPORT_MM}"
        if not force and self.last_report_key == key: return

        trades = self.data.get('trades', [])
        total_pnl = self.data.get('pnl_usdt', 0.0)
        if not trades: return

        wins = [t for t in trades if t['pnl_usdt'] > 0]
        losses = [t for t in trades if t['pnl_usdt'] < 0]
        avg_pnl = total_pnl / len(trades)
        avg_qty = sum(t['qty'] for t in trades) / len(trades)
        avg_dur = sum(t['duration_sec'] for t in trades) / len(trades)
        max_win = max(t['pnl_usdt'] for t in trades)
        max_loss = min(t['pnl_usdt'] for t in trades)
        stddev = statistics.stdev([t['pnl_usdt'] for t in trades]) if len(trades) > 1 else 0.0
        winrate = (len(wins) / len(trades)) * 100

        lines = [
            f"📊 <b>รายงานผลประจำวัน</b> — {self.data['date']}",
            "──────────────────────",
            f"💰 กำไร/ขาดทุนรวม: <b>{total_pnl:+.2f} USDT</b>",
            f"📈 ชนะ: {len(wins)} | 📉 แพ้: {len(losses)} | รวมทั้งหมด: {len(trades)} เทรด",
            f"🎯 Winrate: {winrate:.1f}%",
            f"⚖️ กำไรเฉลี่ย/เทรด: {avg_pnl:+.2f} USDT",
            f"⚙️ ขนาดเฉลี่ย (Qty): {avg_qty:.4f}",
            f"⏱ ถือเฉลี่ย: {avg_dur/60:.1f} นาที",
            "──────────────────────",
            "📋 รายการเทรด:"
        ]

        for t in trades[-15:]:
            lines.append(f"{t['time']} | {t['side'].upper()} | {t['entry']:.2f}→{t['close']:.2f} | {t['pnl_usdt']:+.2f} ({t['reason']})")

        lines += [
            "──────────────────────",
            f"📈 กำไรสูงสุดต่อเทรด: +{max_win:.2f}",
            f"📉 ขาดทุนสูงสุดต่อเทรด: {max_loss:.2f}",
            f"📊 StdDev PnL: {stddev:.2f}"
        ]

        tg_send("\n".join(lines))
        self.last_report_key = key

stats = DailyStats()

# ======== EXCHANGE (BINANCE FUTURES) =========
class BinanceFutures:
    def __init__(self):
        self.ex = None
    def setup(self):
        if not API_KEY or "YOUR" in API_KEY:
            raise RuntimeError("Missing API key")
        self.ex = ccxt.binance({
            'apiKey': API_KEY,
            'secret': SECRET,
            'enableRateLimit': True,
            'options': {'defaultType': 'future'},
        })
        self.ex.load_markets()
        try: self.ex.set_leverage(LEVERAGE, SYMBOL)
        except Exception as e: log.warning(f"set_leverage warn: {e}")
    def ticker_last(self): return float(self.ex.fetch_ticker(SYMBOL)['last'])
    def fetch_ohlcv(self, tf, limit): return self.ex.fetch_ohlcv(SYMBOL, timeframe=tf, limit=limit)
    def free_usdt(self):
        bal = self.ex.fetch_balance({'type': 'future'})
        v = (bal.get('USDT') or {}).get('free')
        return float(v) if v else 0.0
    def fetch_position(self):
        ps = self.ex.fetch_positions([SYMBOL])
        for p in ps:
            if p.get('symbol') == SYMBOL and float(p.get('contracts') or 0) != 0:
                return {'side': p['side'], 'contracts': float(p['contracts']), 'entry': float(p['entryPrice'])}
        return None
    def open_market(self, side, notional, price):
        free = self.free_usdt()
        margin = free * POSITION_MARGIN_FRACTION
        notional = margin * LEVERAGE
        qty = float(self.ex.amount_to_precision(SYMBOL, notional / price))
        side_ccxt = 'buy' if side == 'long' else 'sell'
        self.ex.create_market_order(SYMBOL, side_ccxt, qty)
        time.sleep(1)
        return self.fetch_position()
    def reduce_only_close(self):
        pos = self.fetch_position()
        if not pos: return True
        side_ccxt = 'sell' if pos['side']=='long' else 'buy'
        self.ex.create_market_order(SYMBOL, side_ccxt, pos['contracts'], None, {'reduceOnly': True})
        time.sleep(1)
        return True

# ======== STRATEGY =========
@dataclass
class PositionState:
    side: str
    entry: float
    contracts: float
    entry_time: datetime
    sl_price: float

def compute_sl(entry, side):
    return entry - SL_POINTS if side=='long' else entry + SL_POINTS

def trend_from_ema(closes):
    e50, e100 = last_ema(closes, EMA_FAST), last_ema(closes, EMA_SLOW)
    if e50 is None or e100 is None: return "none"
    return "buy" if e50 > e100 else "sell"

@dataclass
class Bands:
    upper: float; lower: float; mid: float
def compute_bands(closes):
    up, lo, mid = nwe_non_repaint(closes, NW_BANDWIDTH, NW_MULT, NW_LOOKBACK)
    if up is None: return None
    return Bands(up, lo, mid)

# ======== MAIN LOOP =========
def run():
    ex = BinanceFutures()
    ex.setup()
    pos_state = None
    log.info("✅ Started Binance Futures EMA50/100 + Nadaraya Bot (with Daily Report)")
    while True:
        try:
            stats.roll_if_new_day()
            stats.send_report(force=False)

            ohlcv = ex.fetch_ohlcv(TIMEFRAME, NW_LOOKBACK + 5)
            closes = [c[4] for c in ohlcv[:-1]]
            price = ex.ticker_last()
            trend = trend_from_ema(closes)
            bands = compute_bands(closes)
            if not bands: time.sleep(LOOP_SECONDS); continue

            live = ex.fetch_position()
            if live: pos_state = live

            # manage open
            if pos_state and isinstance(pos_state, PositionState):
                if pos_state.side == 'long' and price <= pos_state.sl_price:
                    ex.reduce_only_close()
                    pnl = (price - pos_state.entry) * pos_state.contracts
                    stats.add_trade('long', pos_state.entry, price, pos_state.contracts, pnl, 'SL', pos_state.entry_time)
                    pos_state = None
                elif pos_state.side == 'short' and price >= pos_state.sl_price:
                    ex.reduce_only_close()
                    pnl = (pos_state.entry - price) * pos_state.contracts
                    stats.add_trade('short', pos_state.entry, price, pos_state.contracts, pnl, 'SL', pos_state.entry_time)
                    pos_state = None
                elif pos_state.side == 'long' and price >= bands.upper:
                    ex.reduce_only_close()
                    pnl = (price - pos_state.entry) * pos_state.contracts
                    stats.add_trade('long', pos_state.entry, price, pos_state.contracts, pnl, 'TP', pos_state.entry_time)
                    pos_state = None
                elif pos_state.side == 'short' and price <= bands.lower:
                    ex.reduce_only_close()
                    pnl = (pos_state.entry - price) * pos_state.contracts
                    stats.add_trade('short', pos_state.entry, price, pos_state.contracts, pnl, 'TP', pos_state.entry_time)
                    pos_state = None
                time.sleep(LOOP_SECONDS)
                continue

            # open new
            if not pos_state:
                if trend == "buy" and price <= bands.lower:
                    pos = ex.open_market('long', ex.free_usdt(), price)
                    if pos:
                        ps = PositionState(side='long', entry=pos['entry'], contracts=pos['contracts'],
                                           entry_time=datetime.now(), sl_price=compute_sl(pos['entry'], 'long'))
                        pos_state = ps
                        log.info(f"📈 OPEN LONG {ps.entry:.2f}")
                elif trend == "sell" and price >= bands.upper:
                    pos = ex.open_market('short', ex.free_usdt(), price)
                    if pos:
                        ps = PositionState(side='short', entry=pos['entry'], contracts=pos['contracts'],
                                           entry_time=datetime.now(), sl_price=compute_sl(pos['entry'], 'short'))
                        pos_state = ps
                        log.info(f"📉 OPEN SHORT {ps.entry:.2f}")

            time.sleep(LOOP_SECONDS)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception(f"loop error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    run()
