# main.py
# Binance Futures M15 Bot — EMA50/100 + Nadaraya-Watson Envelope
# Loop 10s, OHLCV cache 60s
# Entry uses last closed bar; TP/SL checked realtime each loop
# SL lock after SL hit: wait for a closed bar inside zone to unlock
# If EMA flips while in position → move TP to mid of band (keep SL from entry)

import os, time, json, logging, math, statistics
from dataclasses import dataclass
from datetime import datetime
import requests
import ccxt
from dotenv import load_dotenv

load_dotenv(override=True)

# ======== ENV / CONFIG ========
API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY')
SECRET  = os.getenv('BINANCE_SECRET', 'YOUR_BINANCE_SECRET')
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

SYMBOL = os.getenv('SYMBOL', "BTC/USDT:USDT")
TIMEFRAME = os.getenv('TIMEFRAME', "15m")

LEVERAGE = int(os.getenv('LEVERAGE', "15"))
POSITION_MARGIN_FRACTION = float(os.getenv('POSITION_MARGIN_FRACTION', "0.50"))  # 50% per position

# Use touch mode default but ENTRY uses last closed bar per your rule
ENTRY_MODE = os.getenv('ENTRY_MODE', 'touch').lower()

EMA_FAST = int(os.getenv('EMA_FAST', "50"))
EMA_SLOW = int(os.getenv('EMA_SLOW', "100"))

NW_BANDWIDTH = float(os.getenv('NW_BANDWIDTH', "8.0"))
NW_MULT = float(os.getenv('NW_MULT', "3.0"))
NW_LOOKBACK = int(os.getenv('NW_LOOKBACK', "500"))

SL_POINTS = float(os.getenv('SL_POINTS', "300.0"))  # fixed SL distance from entry

LOOP_SECONDS = int(os.getenv('LOOP_SECONDS', "10"))
OHLCV_CACHE_SECONDS = int(os.getenv('OHLCV_CACHE_SECONDS', "60"))

REPORT_HH = int(os.getenv('REPORT_HH', "23"))
REPORT_MM = int(os.getenv('REPORT_MM', "59"))

LOG_FILE = os.getenv('LOG_FILE', "bot.log")
STATS_FILE = os.getenv('STATS_FILE', "daily_stats.json")

# ======== LOGGING ========
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")])
log = logging.getLogger("binance.m15.bot")

# ======== INDICATORS & HELPERS ========
def ema_series(values, n):
    if len(values) < n: return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out = [None] * (n - 1) + [e]
    for v in values[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def last_ema(values, n):
    es = ema_series(values, n)
    return es[-1] if es else None

def _gauss(x, h):
    return math.exp(-(x * x) / (2.0 * h * h))

def nwe_non_repaint(closes, h, mult, win):
    n = len(closes)
    if n < win + 1:
        return None, None, None
    coefs = [_gauss(i, h) for i in range(win)]
    den = sum(coefs)
    s = 0.0
    for i in range(win):
        s += closes[-1 - i] * coefs[i]
    mid = s / den
    diffs = [abs(closes[-1 - i] - mid) for i in range(1, win + 1)]
    mae = (sum(diffs) / win) * mult
    upper = mid + mae
    lower = mid - mae
    return upper, lower, mid

def tg_send(text: str):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("YOUR") or not TELEGRAM_CHAT_ID:
        log.info("[TG Disabled] " + text)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.get(url, params={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        log.warning(f"TG error: {e}")

# ======== Daily Stats ========
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
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self.data.update(loaded)
        except Exception:
            pass

    def _save(self):
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning(f"save stats error: {e}")

    def add_trade(self, side, entry, close, qty, pnl, reason, entry_time):
        duration = (datetime.now() - entry_time).total_seconds() if entry_time else 0.0
        rec = {
            'time': datetime.now().strftime('%H:%M:%S'),
            'side': side,
            'entry': float(entry),
            'close': float(close),
            'qty': float(qty),
            'pnl_usdt': float(pnl),
            'reason': reason,
            'duration_sec': duration
        }
        self.data['trades'].append(rec)
        self.data['pnl_usdt'] = float(self.data.get('pnl_usdt', 0.0)) + float(pnl)
        self._save()

    def roll_if_new_day(self):
        today = datetime.now().strftime('%Y-%m-%d')
        if self.data.get('date') != today:
            self.send_report(force=True)
            self.data = {'date': today, 'trades': [], 'pnl_usdt': 0.0}
            self._save()

    def send_report(self, force=False):
        now = datetime.now()
        if not force and not (now.hour == REPORT_HH and now.minute == REPORT_MM):
            return
        key = f"{self.data['date']}:{REPORT_HH}:{REPORT_MM}"
        if not force and self.last_report_key == key:
            return
        trades = self.data.get('trades', [])
        total = float(self.data.get('pnl_usdt', 0.0))
        if not trades:
            tg_send(f"📊 รายงานผลประจำวัน — {self.data['date']}\nไม่มีเทรดวันนี้")
            self.last_report_key = key
            return

        wins = [t for t in trades if t['pnl_usdt'] > 0]
        losses = [t for t in trades if t['pnl_usdt'] < 0]
        avg_pnl = total / len(trades) if trades else 0.0
        avg_qty = sum(t['qty'] for t in trades) / len(trades) if trades else 0.0
        avg_dur = sum(t['duration_sec'] for t in trades) / len(trades) if trades else 0.0
        max_win = max(t['pnl_usdt'] for t in trades) if trades else 0.0
        max_loss = min(t['pnl_usdt'] for t in trades) if trades else 0.0
        stddev = statistics.stdev([t['pnl_usdt'] for t in trades]) if len(trades) > 1 else 0.0
        winrate = (len(wins) / len(trades) * 100) if trades else 0.0

        lines = [
            f"📊 <b>รายงานผลประจำวัน</b> — {self.data['date']}",
            "──────────────────────",
            f"💰 กำไร/ขาดทุนรวม: <b>{total:+.2f} USDT</b>",
            f"📈 ชนะ: {len(wins)} | 📉 แพ้: {len(losses)} | รวมทั้งหมด: {len(trades)} เทรด",
            f"🎯 Winrate: {winrate:.1f}%",
            f"⚖️ กำไรเฉลี่ย/เทรด: {avg_pnl:+.2f} USDT",
            f"⚙️ ขนาดเฉลี่ย (Qty): {avg_qty:.6f}",
            f"⏱ ถือเฉลี่ย: {avg_dur/60:.1f} นาที",
            "──────────────────────",
            "📋 รายการเทรด (ล่าสุด 15):"
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

# ======== Exchange wrapper: Binance Futures ========
class BinanceFutures:
    def __init__(self):
        self.ex = None
        self.market = None

    def setup(self):
        if not API_KEY or "YOUR" in API_KEY:
            raise RuntimeError("Missing API key")
        self.ex = ccxt.binance({
            'apiKey': API_KEY,
            'secret': SECRET,
            'enableRateLimit': True,
            'options': {'defaultType': 'future'},
            'timeout': 60000
        })
        self.ex.load_markets()
        try:
            self.ex.set_leverage(LEVERAGE, SYMBOL)
        except Exception as e:
            log.warning(f"set_leverage warn: {e}")
        try:
            self.market = self.ex.market(SYMBOL)
        except Exception as e:
            log.warning(f"load market warn: {e}")

    def ticker_last(self):
        try:
            return float(self.ex.fetch_ticker(SYMBOL)['last'])
        except Exception as e:
            log.warning(f"ticker_last err: {e}")
            return None

    def fetch_ohlcv(self, timeframe, limit):
        return self.ex.fetch_ohlcv(SYMBOL, timeframe=timeframe, limit=limit)

    def free_usdt(self):
        try:
            bal = self.ex.fetch_balance({'type': 'future'})
            v = (bal.get('USDT') or {}).get('free')
            if v is None:
                for a in (bal.get('info', {}) or {}).get('assets', []):
                    if a.get('asset') == 'USDT':
                        v = a.get('availableBalance')
                        break
            return float(v) if v is not None else 0.0
        except Exception as e:
            log.warning(f"free_usdt err: {e}")
            return 0.0

    def amount_to_precision(self, qty):
        try:
            return float(self.ex.amount_to_precision(SYMBOL, qty))
        except Exception:
            return float(f"{qty:.6f}")

    def fetch_position(self):
        try:
            ps = self.ex.fetch_positions([SYMBOL])
            for p in ps:
                sym = p.get('symbol') or p.get('info', {}).get('symbol')
                contracts = float(p.get('contracts') or p.get('contract') or 0)
                if (sym == SYMBOL or sym == (self.market and self.market.get('symbol'))) and contracts != 0:
                    entry = float(p.get('entryPrice') or p.get('info', {}).get('entryPrice') or 0)
                    side = p.get('side') or ('long' if float(p.get('contracts') or 0) > 0 else 'short')
                    return {'side': side, 'contracts': abs(contracts), 'entry': entry}
            return None
        except Exception as e:
            log.warning(f"fetch_position err: {e}")
            return None

    def calc_qty_by_margin(self, price):
        if not price:
            return 0.0
        free = self.free_usdt()
        margin = max(0.0, free * POSITION_MARGIN_FRACTION)
        if margin <= 0:
            return 0.0
        notional = margin * LEVERAGE
        min_amt = 0.000001
        try:
            min_amt = float((self.market or {}).get('limits', {}).get('amount', {}).get('min', 0.000001))
            min_cost = float((self.market or {}).get('limits', {}).get('cost', {}).get('min', 5.0))
        except Exception:
            min_cost = 5.0
        notional = max(notional, min_cost, min_amt * price)
        qty = notional / price
        qty = self.amount_to_precision(qty)
        return max(qty, min_amt)

    def open_market(self, side, price):
        qty = self.calc_qty_by_margin(price)
        if qty <= 0:
            log.info("Not enough margin to open")
            return None
        side_ccxt = 'buy' if side == 'long' else 'sell'
        try:
            self.ex.create_market_order(SYMBOL, side_ccxt, qty)
            time.sleep(1)
            pos = self.fetch_position()
            if pos and pos['side'] == side:
                return pos
            return pos
        except Exception as e:
            log.warning(f"open_market err: {e}")
            return None

    def reduce_only_close(self):
        pos = self.fetch_position()
        if not pos:
            return True
        side_ccxt = 'sell' if pos['side'] == 'long' else 'buy'
        qty = pos['contracts']
        try:
            self.ex.create_market_order(SYMBOL, side_ccxt, qty, None, {'reduceOnly': True})
            time.sleep(1)
            return True
        except Exception as e:
            log.warning(f"reduce_only_close err: {e}")
            return False

# ======== STRATEGY / POSITION ========
@dataclass
class PositionState:
    side: str
    entry: float
    contracts: float
    entry_time: datetime
    sl_price: float
    tp_price: float  # current TP target (can be changed to mid on EMA flip)

def compute_sl_price(entry_price, side):
    return entry_price - SL_POINTS if side == 'long' else entry_price + SL_POINTS

def trend_from_ema(closes):
    e_fast = last_ema(closes, EMA_FAST)
    e_slow = last_ema(closes, EMA_SLOW)
    if e_fast is None or e_slow is None:
        return "none"
    return "buy" if e_fast > e_slow else "sell"

@dataclass
class Bands:
    upper: float
    lower: float
    mid: float

def compute_bands(closes):
    up, lo, mid = nwe_non_repaint(closes, NW_BANDWIDTH, NW_MULT, NW_LOOKBACK)
    if up is None:
        return None
    return Bands(upper=up, lower=lo, mid=mid)

# ======== MAIN LOOP ========
def run():
    ex = BinanceFutures()
    ex.setup()
    log.info(f"✅ Started Binance Futures Bot (ENTRY_MODE={ENTRY_MODE.upper()}, loop={LOOP_SECONDS}s)")
    pos_state: PositionState | None = None
    sl_lock_active = False

    # OHLCV cache
    ohlcv_cache = None
    last_ohlcv_fetch = 0

    # previous closed close for entry detection (we use last closed close for entry)
    # but we still keep price_prev in case needed
    price_prev = None

    while True:
        try:
            stats.roll_if_new_day()
            stats.send_report(force=False)

            now = time.time()
            # fetch OHLCV at most every OHLCV_CACHE_SECONDS seconds
            try:
                if ohlcv_cache is None or (now - last_ohlcv_fetch) > OHLCV_CACHE_SECONDS:
                    ohlcv_cache = ex.fetch_ohlcv(TIMEFRAME, NW_LOOKBACK + 5)
                    last_ohlcv_fetch = now
            except ccxt.base.errors.DDoSProtection as e:
                log.warning("DDoSProtection / RateLimit when fetching OHLCV: sleeping 10s")
                time.sleep(10)
                continue
            except Exception as e:
                log.warning(f"ohlcv fetch error: {e}")
                time.sleep(5)
                continue

            if not ohlcv_cache or len(ohlcv_cache) < NW_LOOKBACK + 2:
                time.sleep(LOOP_SECONDS)
                continue

            # closed bars: exclude the current live bar (we use closed for entry and bands/EMA)
            closes_closed = [c[4] for c in ohlcv_cache[:-1]]
            last_closed_close = closes_closed[-1]
            bands = compute_bands(closes_closed)
            trend = trend_from_ema(closes_closed)
            price_now = ex.ticker_last()
            if price_now is None:
                time.sleep(LOOP_SECONDS)
                continue

            # Sync live position from exchange (always check)
            live_pos = ex.fetch_position()
            if live_pos and (pos_state is None):
                # sync local state from exchange (best-effort)
                ps = PositionState(side=live_pos['side'],
                                   entry=live_pos['entry'],
                                   contracts=live_pos['contracts'],
                                   entry_time=datetime.now(),
                                   sl_price=compute_sl_price(live_pos['entry'], live_pos['side']),
                                   tp_price=(bands.upper if live_pos['side']=='long' else bands.lower) if bands else 0.0)
                pos_state = ps
                sl_lock_active = False
                log.info("Synced existing position from exchange")

            # If SL lock active -> check unlock condition using closed bar
            if sl_lock_active:
                # unlock only when last_closed_close is inside [lower, upper]
                if bands and bands.lower <= last_closed_close <= bands.upper:
                    sl_lock_active = False
                    log.info("Unlocked from SL lock: a closed bar is inside envelope zone")
                    tg_send("🔓 SL lock released — price closed inside envelope")
                else:
                    # still locked; do not open new positions
                    # but continue to monitor existing pos (should be none)
                    time.sleep(LOOP_SECONDS)
                    price_prev = price_now
                    continue

            # If we have a position -> manage exits (SL based on entry; TP based on bands)
            if pos_state:
                # 1) check SL (absolute from entry) using realtime price
                if pos_state.side == 'long' and price_now <= pos_state.sl_price:
                    ok = ex.reduce_only_close()
                    pnl = (price_now - pos_state.entry) * pos_state.contracts
                    stats.add_trade('long', pos_state.entry, price_now, pos_state.contracts, pnl, 'SL', pos_state.entry_time)
                    log.info(f"SL HIT close LONG entry={pos_state.entry:.2f} last={price_now:.2f} pnl={pnl:+.2f}")
                    tg_send(f"🛑 SL HIT LONG {price_now:.2f} → PnL {pnl:+.2f} USDT")
                    pos_state = None
                    sl_lock_active = True
                    price_prev = price_now
                    time.sleep(LOOP_SECONDS)
                    continue
                if pos_state.side == 'short' and price_now >= pos_state.sl_price:
                    ok = ex.reduce_only_close()
                    pnl = (pos_state.entry - price_now) * pos_state.contracts
                    stats.add_trade('short', pos_state.entry, price_now, pos_state.contracts, pnl, 'SL', pos_state.entry_time)
                    log.info(f"SL HIT close SHORT entry={pos_state.entry:.2f} last={price_now:.2f} pnl={pnl:+.2f}")
                    tg_send(f"🛑 SL HIT SHORT {price_now:.2f} → PnL {pnl:+.2f} USDT")
                    pos_state = None
                    sl_lock_active = True
                    price_prev = price_now
                    time.sleep(LOOP_SECONDS)
                    continue

                # 2) check TP using latest bands (recomputed from closed bars)
                if bands:
                    if pos_state.side == 'long' and price_now >= pos_state.tp_price:
                        ok = ex.reduce_only_close()
                        pnl = (price_now - pos_state.entry) * pos_state.contracts
                        stats.add_trade('long', pos_state.entry, price_now, pos_state.contracts, pnl, 'TP', pos_state.entry_time)
                        log.info(f"TP HIT close LONG entry={pos_state.entry:.2f} last={price_now:.2f} pnl={pnl:+.2f}")
                        tg_send(f"✅ TP HIT LONG {price_now:.2f} → PnL {pnl:+.2f} USDT")
                        pos_state = None
                        price_prev = price_now
                        time.sleep(LOOP_SECONDS)
                        continue
                    if pos_state.side == 'short' and price_now <= pos_state.tp_price:
                        ok = ex.reduce_only_close()
                        pnl = (pos_state.entry - price_now) * pos_state.contracts
                        stats.add_trade('short', pos_state.entry, price_now, pos_state.contracts, pnl, 'TP', pos_state.entry_time)
                        log.info(f"TP HIT close SHORT entry={pos_state.entry:.2f} last={price_now:.2f} pnl={pnl:+.2f}")
                        tg_send(f"✅ TP HIT SHORT {price_now:.2f} → PnL {pnl:+.2f} USDT")
                        pos_state = None
                        price_prev = price_now
                        time.sleep(LOOP_SECONDS)
                        continue

                # 3) EMA flip while in position -> change TP to mid (keep SL unchanged)
                # We compute current trend from closed bars (trend variable already)
                # If trend flips against the position side -> set TP to mid (if not already)
                if bands:
                    if (pos_state.side == 'long' and trend == 'sell') or (pos_state.side == 'short' and trend == 'buy'):
                        # set tp to mid of envelope
                        old_tp = pos_state.tp_price
                        pos_state.tp_price = bands.mid
                        log.info(f"EMA flip detected while in pos -> move TP from {old_tp:.2f} to mid {bands.mid:.2f}")
                        tg_send(f"🔁 EMA flipped — moved TP -> mid {bands.mid:.2f} (SL unchanged)")

                # no exit conditions met this loop
                time.sleep(LOOP_SECONDS)
                price_prev = price_now
                continue

            # NO position -> find entry using last closed bar (per your rule)
            # Use trend computed from closed bars and last_closed_close
            signal = None
            if bands is None:
                time.sleep(LOOP_SECONDS)
                continue

            # only allow entry if not in SL lock (checked earlier)
            # For ENTRY_MODE touch or zone: but per your rule use closed bar to decide entry
            if trend == 'buy':
                # want LONG when last_closed_close <= bands.lower (closed bar inside or at lower)
                if last_closed_close <= bands.lower:
                    signal = 'long'
            elif trend == 'sell':
                if last_closed_close >= bands.upper:
                    signal = 'short'

            # If we have signal -> open market (one order at a time)
            if signal:
                # double-check pos still empty on exchange
                if ex.fetch_position():
                    log.info("Exchange reports a position exists — skipping open to avoid duplicate")
                else:
                    pos = ex.open_market(signal, price_now)
                    if pos:
                        tp_initial = bands.upper if signal == 'long' else bands.lower
                        ps = PositionState(side=signal, entry=pos['entry'], contracts=pos['contracts'],
                                           entry_time=datetime.now(), sl_price=compute_sl_price(pos['entry'], signal),
                                           tp_price=tp_initial)
                        pos_state = ps
                        log.info(f"OPEN {signal.upper()} entry={ps.entry:.2f} size={ps.contracts:.6f} SL={ps.sl_price:.2f} TP={ps.tp_price:.2f}")
                        tg_send(f"✅ OPEN {signal.upper()} entry={ps.entry:.2f} size={ps.contracts:.6f} SL={ps.sl_price:.2f} TP={ps.tp_price:.2f}")
                    else:
                        log.warning("Open market failed or not confirmed")
            # loop housekeeping
            price_prev = price_now
            time.sleep(LOOP_SECONDS)

        except KeyboardInterrupt:
            log.info("KeyboardInterrupt — exiting")
            break
        except Exception as e:
            log.exception(f"Main loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run()
