# main.py
# (Same header as previous) — updated: sync price scale between OHLCV closes and ticker_last
import os, time, math, statistics, json, logging
from dataclasses import dataclass
from datetime import datetime
import ccxt
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

# ============ CONFIG (same as before) ============
API_KEY = os.getenv('BINANCE_API_KEY', '')
SECRET  = os.getenv('BINANCE_SECRET', '')

SYMBOL = os.getenv('SYMBOL', "BTC/USDT:USDT")
TIMEFRAME = os.getenv('TIMEFRAME', "15m")

LEVERAGE = int(os.getenv('LEVERAGE', "15"))
POSITION_MARGIN_FRACTION = float(os.getenv('POSITION_MARGIN_FRACTION', "0.5"))

EMA_FAST = int(os.getenv('EMA_FAST', "50"))
EMA_SLOW = int(os.getenv('EMA_SLOW', "100"))

NW_BANDWIDTH = float(os.getenv('NW_BANDWIDTH', "4.5"))
NW_MULT = float(os.getenv('NW_MULT', "2.5"))
NW_LOOKBACK = int(os.getenv('NW_LOOKBACK', "400"))

SL_POINTS = float(os.getenv('SL_POINTS', "300.0"))
BREAKEVEN_OFFSET = float(os.getenv('BREAKEVEN_OFFSET', "100.0"))

LOOP_SECONDS = int(os.getenv('LOOP_SECONDS', "10"))
OHLCV_CACHE_SECONDS = int(os.getenv('OHLCV_CACHE_SECONDS', "60"))

REPORT_HH = int(os.getenv('REPORT_HH', "23"))
REPORT_MM = int(os.getenv('REPORT_MM', "59"))

LOG_FILE = os.getenv('LOG_FILE', "bot.log")
STATS_FILE = os.getenv('STATS_FILE', "daily_stats.json")

DEBUG_MODE = os.getenv('DEBUG_MODE', "True").lower() in ("1", "true", "yes")

# ============ Logging ============
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")])
log = logging.getLogger("luxalgo-precision-bot")

def dlog(msg: str):
    if DEBUG_MODE:
        log.info("[DEBUG] " + msg)

# ============ Telegram (daily only) ============
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

def tg_send(text: str):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("YOUR") or not TELEGRAM_CHAT_ID:
        log.info("[TG Disabled] " + text)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.get(url, params={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        log.warning(f"TG error: {e}")

# ============ Daily Stats ============
class DailyStats:
    def __init__(self, path=STATS_FILE):
        self.path = path
        self.data = {'date': datetime.now().strftime('%Y-%m-%d'), 'trades': [], 'pnl_usdt': 0.0}
        self._last_report_key = None
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self.data.update(loaded)
        except Exception as e:
            log.warning(f"daily stats load err: {e}")

    def _save(self):
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning(f"daily stats save err: {e}")

    def roll_if_new_day(self):
        today = datetime.now().strftime('%Y-%m-%d')
        if self.data.get('date') != today:
            self.send_report(force=True)
            self.data = {'date': today, 'trades': [], 'pnl_usdt': 0.0}
            self._save()

    def add_trade(self, side, entry, close, qty, pnl, reason):
        rec = {
            'time': datetime.now().strftime('%H:%M:%S'),
            'side': side,
            'entry': float(entry),
            'close': float(close),
            'qty': float(qty),
            'pnl_usdt': float(pnl),
            'reason': reason
        }
        self.data['trades'].append(rec)
        self.data['pnl_usdt'] = float(self.data.get('pnl_usdt', 0.0)) + float(pnl)
        self._save()

    def send_report(self, force=False):
        now = datetime.now()
        key = f"{self.data['date']}:{REPORT_HH}:{REPORT_MM}"
        if not force:
            if not (now.hour == REPORT_HH and now.minute == REPORT_MM):
                return
            if self._last_report_key == key:
                return
        trades = self.data.get('trades', [])
        if not trades and not force:
            self._last_report_key = key
            return
        total = float(self.data.get('pnl_usdt', 0.0))
        wins = [t for t in trades if t['pnl_usdt'] > 0]
        losses = [t for t in trades if t['pnl_usdt'] < 0]
        winrate = (len(wins) / len(trades) * 100) if trades else 0.0
        lines = [
            f"📊 <b>รายงานผลประจำวัน</b> — {self.data['date']}",
            f"Σ PnL: <b>{total:+.2f} USDT</b>",
            f"Trades: {len(trades)} | Win: {len(wins)} | Loss: {len(losses)} | Winrate: {winrate:.1f}%",
            "────────────"
        ]
        for t in trades[-20:]:
            lines.append(f"{t['time']} | {t['side'].upper()} | {t['entry']:.2f}→{t['close']:.2f} | {t['pnl_usdt']:+.2f} ({t['reason']})")
        tg_send("\n".join(lines))
        self._last_report_key = key

stats = DailyStats()

# ============ Indicators / Nadaraya ============
def ema_series(values, n):
    n = int(n)
    if len(values) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e

def nwe_non_repaint(closes, h, mult, win):
    if len(closes) < win + 1:
        return None, None, None
    coefs = [math.exp(-(i ** 2) / (2 * h ** 2)) for i in range(win)]
    den = sum(coefs)
    weighted_sum = sum(closes[-1 - i] * coefs[i] for i in range(win))
    mid = weighted_sum / den
    smooth_mid = (mid + statistics.fmean(closes[-3:])) / 2
    diffs = [abs(closes[-1 - i] - smooth_mid) for i in range(1, win + 1)]
    mae = (sum(diffs) / win) * mult
    upper = smooth_mid + mae
    lower = smooth_mid - mae
    return upper, lower, smooth_mid

def trend_from_ema(closes):
    e50 = ema_series(closes, EMA_FAST)
    e100 = ema_series(closes, EMA_SLOW)
    if e50 is None or e100 is None:
        return "none", e50, e100
    return ("buy" if e50 > e100 else "sell"), e50, e100

# ============ Exchange wrapper ============
class BinanceFutures:
    def __init__(self):
        self.ex = None
        self.market = None

    def setup(self):
        if not API_KEY or 'YOUR' in API_KEY:
            raise RuntimeError("Please set BINANCE_API_KEY / BINANCE_SECRET in .env")
        self.ex = ccxt.binance({
            'apiKey': API_KEY, 'secret': SECRET, 'enableRateLimit': True,
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

    def fetch_ohlcv(self, timeframe, limit):
        return self.ex.fetch_ohlcv(SYMBOL, timeframe=timeframe, limit=limit)

    def ticker_last(self):
        try:
            return float(self.ex.fetch_ticker(SYMBOL)['last'])
        except Exception as e:
            log.warning(f"ticker_last err: {e}")
            return None

    def fetch_position(self):
        try:
            ps = self.ex.fetch_positions([SYMBOL])
            for p in ps:
                contracts = float(p.get('contracts') or p.get('contract') or 0)
                if contracts != 0:
                    entry = float(p.get('entryPrice') or p.get('info', {}).get('entryPrice') or 0)
                    side = p.get('side') or ('long' if contracts > 0 else 'short')
                    return {'side': side, 'contracts': abs(contracts), 'entry': entry}
            return None
        except Exception as e:
            log.warning(f"fetch_position err: {e}")
            return None

    def free_usdt(self):
        try:
            bal = self.ex.fetch_balance({'type': 'future'})
            v = (bal.get('USDT') or {}).get('free')
            if v is None:
                for a in (bal.get('info', {}) or {}).get('assets', []):
                    if a.get('asset') == 'USDT':
                        v = a.get('availableBalance'); break
            return float(v) if v is not None else 0.0
        except Exception as e:
            log.warning(f"free_usdt err: {e}")
            return 0.0

    def amount_to_precision(self, qty):
        try:
            return float(self.ex.amount_to_precision(SYMBOL, qty))
        except Exception:
            return float(f"{qty:.6f}")

    def calc_qty_by_margin(self, price):
        if not price or not self.market:
            return 0.0
        free = self.free_usdt()
        margin = max(0.0, free * POSITION_MARGIN_FRACTION)
        if margin <= 0:
            return 0.0
        notional = margin * LEVERAGE
        try:
            min_amt = float(self.market['limits']['amount'].get('min', 0.000001))
            min_cost = float(self.market['limits']['cost'].get('min', 5.0))
        except Exception:
            min_amt = 0.000001
            min_cost = 5.0
        notional = max(notional, min_cost, min_amt * price)
        qty = notional / price
        return max(self.amount_to_precision(qty), min_amt)

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
        side = 'sell' if pos['side'] == 'long' else 'buy'
        qty = pos['contracts']
        try:
            self.ex.create_market_order(SYMBOL, side, qty, None, {'reduceOnly': True})
            time.sleep(1)
            return True
        except Exception as e:
            log.warning(f"reduce_only_close err: {e}")
            return False

# ============ Data classes ============
@dataclass
class PositionState:
    side: str
    entry: float
    contracts: float
    entry_time: datetime
    sl_price: float
    tp_price: float
    sl_moved: bool = False

# ============ Utility: scale sync ============
def normalize_closes_scale(closes):
    if not closes:
        return closes
    maxv = max(closes)
    attempts = 0
    while maxv < 10000 and attempts < 5:
        closes = [c * 10 for c in closes]
        maxv = max(closes)
        attempts += 1
    return closes

# ============ MAIN LOOP ============
def run():
    ex = BinanceFutures()
    ex.setup()
    log.info(f"✅ Started LuxAlgo-Precision Bot (loop={LOOP_SECONDS}s, debug={DEBUG_MODE})")

    pos_state = None
    sl_lock_active = False
    last_sl_side = None

    ohlcv_cache = None
    last_ohlcv_fetch = 0
    last_snapshot = 0

    while True:
        try:
            stats.roll_if_new_day()
            stats.send_report(force=False)

            now = time.time()
            try:
                if ohlcv_cache is None or (now - last_ohlcv_fetch) > OHLCV_CACHE_SECONDS:
                    ohlcv_cache = ex.fetch_ohlcv(TIMEFRAME, NW_LOOKBACK + 5)
                    last_ohlcv_fetch = now
            except ccxt.base.errors.DDoSProtection:
                log.warning("Rate limit when fetching OHLCV, sleep 10s")
                time.sleep(10); continue
            except Exception as e:
                log.warning(f"ohlcv fetch err: {e}")
                time.sleep(5); continue

            if not ohlcv_cache or len(ohlcv_cache) < NW_LOOKBACK + 2:
                time.sleep(LOOP_SECONDS); continue

            # raw closes and raw ticker
            closes_raw = [float(c[4]) for c in ohlcv_cache]
            price_raw = ex.ticker_last()

            # debug: show raw relationship
            dlog(f"raw last_close={closes_raw[-1]:.6f} price_raw={price_raw:.6f}")

            # prefer syncing scale by comparing last OHLCV close and ticker last
            closes = list(closes_raw)  # copy
            try:
                if price_raw and closes_raw[-1] > 0:
                    scale = price_raw / closes_raw[-1]
                    # only apply scale if reasonable (avoid insane multiplicative error)
                    if 0.1 < scale < 10:
                        closes = [c * scale for c in closes_raw]
                        dlog(f"Applied scale factor from ticker: {scale:.6f}")
                    else:
                        # fallback normalization if scale is weird
                        closes = normalize_closes_scale(closes_raw)
                        dlog("Scale factor unreasonable, used normalize_closes_scale()")
                else:
                    closes = normalize_closes_scale(closes_raw)
                    dlog("No price_raw or last_close zero -> normalize_closes_scale() used")
            except Exception as e:
                closes = normalize_closes_scale(closes_raw)
                dlog(f"Scale sync exception -> normalize_closes_scale(): {e}")

            last_close = closes[-1]

            # compute bands
            bands_res = nwe_non_repaint(closes, NW_BANDWIDTH, NW_MULT, NW_LOOKBACK)
            if bands_res is None:
                time.sleep(LOOP_SECONDS); continue
            upper, lower, mid = bands_res

            # compute EMA trend on scaled closes
            trend, e50, e100 = trend_from_ema(closes)
            price_now = price_raw  # use raw ticker (we already scaled closes to match price if possible)
            if price_now is None:
                time.sleep(LOOP_SECONDS); continue

            # debug logs showing both raw and scaled
            if e50 is not None and e100 is not None:
                dlog(f"EMA50={e50:.2f}, EMA100={e100:.2f}, Trend={trend.upper()}")
            dlog(f"Close={last_close:.2f}, Upper={upper:.2f}, Lower={lower:.2f}, Mid={mid:.2f}")
            dlog(f"Ticker price={price_now:.2f}")

            # unlock sl lock if touched mid
            if sl_lock_active:
                if last_sl_side == 'long' and price_now > mid:
                    sl_lock_active = False; last_sl_side = None
                    log.info(f"Unlock SL lock: price {price_now:.2f} > mid {mid:.2f}")
                elif last_sl_side == 'short' and price_now < mid:
                    sl_lock_active = False; last_sl_side = None
                    log.info(f"Unlock SL lock: price {price_now:.2f} < mid {mid:.2f}")
                else:
                    time.sleep(LOOP_SECONDS); continue

            # sync existing exchange pos if any
            live_pos = ex.fetch_position()
            if live_pos and pos_state is None:
                ps = PositionState(side=live_pos['side'], entry=live_pos['entry'], contracts=live_pos['contracts'],
                                   entry_time=datetime.now(), sl_price=(live_pos['entry'] - SL_POINTS if live_pos['side']=='long' else live_pos['entry'] + SL_POINTS),
                                   tp_price=(upper if live_pos['side']=='long' else lower))
                pos_state = ps
                sl_lock_active = False
                last_sl_side = None
                log.info("Synced existing position from exchange")

            # manage position (SL/TP/SL-move/EMA flip)
            if pos_state:
                if pos_state.side == 'long' and price_now <= pos_state.sl_price:
                    ex.reduce_only_close()
                    pnl = (price_now - pos_state.entry) * pos_state.contracts
                    stats.add_trade('long', pos_state.entry, price_now, pos_state.contracts, pnl, 'SL')
                    log.info(f"SL HIT LONG entry={pos_state.entry:.2f} last={price_now:.2f} pnl={pnl:+.2f}")
                    last_sl_side = 'long'; sl_lock_active = True; pos_state = None
                    time.sleep(LOOP_SECONDS); continue
                if pos_state.side == 'short' and price_now >= pos_state.sl_price:
                    ex.reduce_only_close()
                    pnl = (pos_state.entry - price_now) * pos_state.contracts
                    stats.add_trade('short', pos_state.entry, price_now, pos_state.contracts, pnl, 'SL')
                    log.info(f"SL HIT SHORT entry={pos_state.entry:.2f} last={price_now:.2f} pnl={pnl:+.2f}")
                    last_sl_side = 'short'; sl_lock_active = True; pos_state = None
                    time.sleep(LOOP_SECONDS); continue

                if pos_state.side == 'long' and price_now >= pos_state.tp_price:
                    ex.reduce_only_close()
                    pnl = (price_now - pos_state.entry) * pos_state.contracts
                    stats.add_trade('long', pos_state.entry, price_now, pos_state.contracts, pnl, 'TP')
                    log.info(f"TP HIT LONG entry={pos_state.entry:.2f} last={price_now:.2f} pnl={pnl:+.2f}")
                    pos_state = None; time.sleep(LOOP_SECONDS); continue
                if pos_state.side == 'short' and price_now <= pos_state.tp_price:
                    ex.reduce_only_close()
                    pnl = (pos_state.entry - price_now) * pos_state.contracts
                    stats.add_trade('short', pos_state.entry, price_now, pos_state.contracts, pnl, 'TP')
                    log.info(f"TP HIT SHORT entry={pos_state.entry:.2f} last={price_now:.2f} pnl={pnl:+.2f}")
                    pos_state = None; time.sleep(LOOP_SECONDS); continue

                if not pos_state.sl_moved:
                    if pos_state.side == 'long' and last_close > mid:
                        pos_state.sl_price = pos_state.entry + BREAKEVEN_OFFSET
                        pos_state.sl_moved = True
                        log.info(f"Move SL to breakeven+{BREAKEVEN_OFFSET:.0f} -> {pos_state.sl_price:.2f}")
                    elif pos_state.side == 'short' and last_close < mid:
                        pos_state.sl_price = pos_state.entry - BREAKEVEN_OFFSET
                        pos_state.sl_moved = True
                        log.info(f"Move SL to breakeven-{BREAKEVEN_OFFSET:.0f} -> {pos_state.sl_price:.2f}")

                if (pos_state.side == 'long' and trend == 'sell') or (pos_state.side == 'short' and trend == 'buy'):
                    old_tp = pos_state.tp_price
                    pos_state.tp_price = mid
                    log.info(f"EMA flip in-pos -> move TP {old_tp:.2f} -> mid {mid:.2f}")

                time.sleep(LOOP_SECONDS); continue

            # entries (touch using latest close)
            signal = None
            if trend == 'buy' and last_close <= lower and not sl_lock_active:
                signal = 'long'; dlog("Entry -> LONG (close <= lower)")
            elif trend == 'sell' and last_close >= upper and not sl_lock_active:
                signal = 'short'; dlog("Entry -> SHORT (close >= upper)")
            else:
                diff_up = upper - last_close
                diff_lo = last_close - lower
                dlog(f"No entry: sl_lock={sl_lock_active}, Δupper={diff_up:.1f}, Δlower={diff_lo:.1f}")

            if signal:
                if ex.fetch_position():
                    log.info("Exchange reports a position exists — skipping open")
                else:
                    pos = ex.open_market(signal, price_now)
                    if pos:
                        tp_init = upper if signal == 'long' else lower
                        ps = PositionState(side=signal, entry=pos['entry'], contracts=pos['contracts'],
                                           entry_time=datetime.now(),
                                           sl_price=(pos['entry'] - SL_POINTS if signal=='long' else pos['entry'] + SL_POINTS),
                                           tp_price=tp_init)
                        pos_state = ps
                        log.info(f"OPEN {signal.upper()} entry={ps.entry:.2f} size={ps.contracts:.6f} SL={ps.sl_price:.2f} TP={ps.tp_price:.2f}")
                    else:
                        log.warning("Open market failed or not confirmed")

            # periodic snapshot
            nowt = time.time()
            if nowt - last_snapshot > 60:
                last_snapshot = nowt
                dlog(f"Snapshot price={price_now:.2f} pos_state={'yes' if pos_state else 'no'}")

            time.sleep(LOOP_SECONDS)

        except KeyboardInterrupt:
            log.info("KeyboardInterrupt, exiting")
            break
        except Exception as e:
            log.exception(f"Main loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run()
