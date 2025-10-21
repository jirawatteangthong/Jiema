# -*- coding: utf-8 -*-
# Binance Futures (USDT-M, isolated, one-way) — ETH mode
# SMC (H1 BOS/CHOCH) + Fibo Zone + M1 Pullback POC + SL Hierarchy
# Entry C2: (M1 CHOCH OR MACD cross) on closed candle, same trend
# Fibo anchor rule: BOS ต่อเนื่องก่อนแตะ 33 → ขยับ Fibo0 (Fibo100 คงเดิม); แตะโซนแล้ว → Lock
# TP1 (60%) → Fibo2 (100 = Fibo0(H1), 0 = pre-CHOCH swing on M1) → SL=78.6 → TP2=133–161.8
# Emergency close เฉพาะ TP2 phase เท่านั้น
# Anti-spam alerts + Safe Telegram

import os, time, math, json, logging
from datetime import datetime, timedelta
from collections import defaultdict

import ccxt
import requests
import pandas as pd

# ================== CONFIG ==================
API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET  = os.getenv('BINANCE_SECRET',    'YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING')

SYMBOL            = 'ETH/USDT:USDT'   # ✔ ETH perp (USDT-M)
TIMEFRAME_H1      = '1h'
TIMEFRAME_M5      = '5m'
TIMEFRAME_M1      = '1m'
LEVERAGE          = 25

# Risk Mode B (safe for small account)
TARGET_POSITION_SIZE_FACTOR = 0.35    # use ~35% of free equity for initial sizing
TARGET_RISK_PCT             = 0.005   # risk cap per trade (0.5% of equity)
MARGIN_BUFFER_USDT          = 5       # keep some cash unused

# MACD std
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9

# Behavior / alerts
CHECK_INTERVAL     = 15
TP1_CLOSE_PERCENT  = 0.60
COOLDOWN_HOURS     = 3
STEP_ALERT         = True

# Tolerances
PRICE_TOLERANCE_PCT = 0.0005    # 0.05%
POC_FILTER_TOL      = 0.001     # 0.1% around POC for H1 close filter
POC_SL_BUFFER       = 0.001     # 0.10% beyond POC for SL

# Stats
STATS_FILE = 'trades_stats.json'

# Telegram
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN',   'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
log = logging.getLogger('eth_smc_final')

# ================== GLOBALS ==================
exchange = None
market_info = None

current_position = None     # {'side','contracts','entry_price'}
pending_trade    = None     # state for current trade
cooldown_until   = None
last_notices     = set()    # one-shot alerts

state = {
    'step': 1,             # 1 H1 SMC, 2 Zone, 3 Confirm, 99 in-position
    'bias': None,          # 'up'|'down'
    'latest_h1': None,
    'fibo100': None,       # anchored
    'fibo0':   None,       # movable on BOS-without-33 rule
    'fibo':    None,
    'entry_zone': None,    # (33..78.6)
    'poc_m1':  None,
    'touched_33_since_last_bos': True,
    'fibo_locked': False,  # 🔒 lock when price enters zone
    'last_fibo_shift_key': None,  # anti-spam per shift
}

monthly_stats = {'month_year': None, 'tp_count': 0, 'sl_count': 0, 'total_pnl': 0.0, 'trades': []}

# ================== TELEGRAM ==================
def send_telegram(msg: str):
    """Safe Telegram sender (no crash even if ENV missing)."""
    try:
        token = TELEGRAM_TOKEN
        chat_id = TELEGRAM_CHAT_ID
    except NameError:
        log.info("[TG-SKIP] variables not ready")
        return

    if (not token) or (not chat_id) or token.startswith('YOUR_') or chat_id.startswith('YOUR_'):
        log.info("[TG-SKIP] " + (msg[:160] if msg else ''))
        return

    try:
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        params = {'chat_id': chat_id, 'text': msg, 'parse_mode': 'HTML'}
        requests.get(url, params=params, timeout=10)
        log.info("[TG] " + (msg.splitlines()[0] if msg else ''))
    except Exception as e:
        log.error(f"[TG-ERROR] {e}")

def alert_once(key: str, message: str):
    if STEP_ALERT and key not in last_notices:
        last_notices.add(key)
        send_telegram(message)

def reset_alerts(prefix: str | None = None):
    global last_notices
    if prefix is None:
        last_notices.clear()
    else:
        last_notices = {k for k in last_notices if not k.startswith(prefix)}

# ================== EXCHANGE ==================
def setup_exchange():
    global exchange, market_info
    exchange = ccxt.binance({
        'apiKey': API_KEY,
        'secret': SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True},
        'timeout': 30000
    })
    exchange.load_markets()
    if SYMBOL not in exchange.markets:
        raise RuntimeError(f"Symbol {SYMBOL} not found (USDT-M perp).")
    market_info = exchange.market(SYMBOL)
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
        log.info(f"Leverage set {LEVERAGE}x for {SYMBOL}")
    except Exception as e:
        log.warning(f"set_leverage failed (continue): {e}")

# ================== DATA HELPERS ==================
def fetch_ohlcv_safe(symbol, timeframe, limit=200):
    for _ in range(3):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as e:
            log.warning(f"fetch_ohlcv error: {e}, retrying...")
            time.sleep(3)
    raise RuntimeError("fetch_ohlcv failed")

def get_price():
    return float(exchange.fetch_ticker(SYMBOL)['last'])

# ================== SMC ==================
def pivots(ohlcv, left=3, right=3):
    highs=[c[2] for c in ohlcv]; lows=[c[3] for c in ohlcv]; L=len(ohlcv)
    sh, sl = {}, {}
    for i in range(left, L-right):
        if highs[i] == max(highs[i-left:i+right+1]): sh[i]=highs[i]
        if lows[i]  == min(lows[i-left:i+right+1]) : sl[i]=lows[i]
    return sh, sl

def smc_events(ohlcv, left=3, right=3):
    if not ohlcv: return []
    sh, sl = pivots(ohlcv, left, right)
    last_high=None; crossed_high=True
    last_low=None;  crossed_low=True
    bias=None; evs=[]
    for i in range(len(ohlcv)):
        if i in sh: last_high=sh[i]; crossed_high=False
        if i in sl: last_low =sl[i]; crossed_low =False
        c=ohlcv[i][4]; t=ohlcv[i][0]
        if (last_high is not None) and (not crossed_high) and (c>last_high):
            sig='BOS' if bias in (None,'up') else 'CHOCH'
            bias='up'; crossed_high=True
            evs.append({'i':i,'t':t,'p':last_high,'kind':'high','sig':sig,'bias':bias})
        if (last_low is not None) and (not crossed_low) and (c<last_low):
            sig='BOS' if bias in (None,'down') else 'CHOCH'
            bias='down'; crossed_low=True
            evs.append({'i':i,'t':t,'p':last_low,'kind':'low','sig':sig,'bias':bias})
    return evs

def latest_smc(ohlcv):
    evs = smc_events(ohlcv, left=3, right=3)
    if not evs: return None, None
    return evs[-1], evs[-1]['bias']

# ================== FIBO & RULE ==================
def build_fibo(low, high):
    diff = high-low
    return {
        '0': high, '100': low,
        '33':   high - 0.33*diff,
        '38.2': high - 0.382*diff,
        '50':   high - 0.5*diff,
        '61.8': high - 0.618*diff,
        '71.8': high - 0.718*diff,
        '78.6': high - 0.786*diff,
        '80':   high - 0.80*diff,
        'ext133': low + 1.33*diff,
        'ext161.8': low + 1.618*diff
    }

def init_fibo_from_h1(ohlcv_h1, bias):
    recent = ohlcv_h1[-50:]
    swing_high = max(b[2] for b in recent)
    swing_low  = min(b[3] for b in recent)
    fibo100 = swing_low
    fibo0   = swing_high
    fib = build_fibo(fibo100, fibo0)
    return fibo100, fibo0, fib, (fib['33'], fib['78.6'])

def update_fibo_on_bos_without_pullback_to_33(new_bos_price, bias):
    """Shift Fibo0 ONLY if: not locked, not touched 33 since last BOS, same-direction BOS."""
    if state['fibo_locked']:
        return
    if state['fibo100'] is None or state['fibo0'] is None:
        return
    if state['touched_33_since_last_bos']:
        return
    # shift only fibo0 (uptrend: raise; downtrend: lower)
    shifted = False
    if bias == 'up' and new_bos_price > state['fibo0']:
        state['fibo0'] = new_bos_price; shifted = True
    elif bias == 'down' and new_bos_price < state['fibo0']:
        state['fibo0'] = new_bos_price; shifted = True
    if shifted:
        state['fibo'] = build_fibo(state['fibo100'], state['fibo0'])
        state['entry_zone'] = (state['fibo']['33'], state['fibo']['78.6'])
        # anti-spam per-shift key
        key = f"FIBO_SHIFT_{int(state['fibo0'])}"
        if key != state['last_fibo_shift_key']:
            state['last_fibo_shift_key'] = key
            alert_once(key, "🧭 [FIBO] BOS ต่อเนื่องโดยยังไม่แตะ 33 → ขยับ Fibo 0 (100 คงเดิม)")

# ================== POC (M1 pullback subset) ==================
def vp_poc_from_bars(bars):
    if not bars: return None
    prices=[(b[2]+b[3]+b[4])/3 for b in bars]; vols=[b[5] or 0.0 for b in bars]
    mn, mx = min(prices), max(prices)
    width = max((mx-mn)/40.0, 0.2)  # ETH ช่วงแคบกว่า ใช้บัคเก็ตเล็กลง
    bins = defaultdict(float)
    for p, v in zip(prices, vols):
        idx=int((p-mn)/width); center=mn+(idx+0.5)*width; bins[center]+=v
    if not bins: return None
    return max(bins.items(), key=lambda x:x[1])[0]

def m1_pullback_subset_for_poc(m1, fib, bias):
    if not m1: return []
    if bias=='up':
        return [b for b in m1 if fib['100'] <= b[4] <= fib['61.8']]
    else:
        return [b for b in m1 if fib['38.2'] <= b[4] <= fib['0']]

def compute_m1_poc(fib, bias):
    m1=fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=300)
    subset=m1_pullback_subset_for_poc(m1, fib, bias)
    return vp_poc_from_bars(subset)

# ================== MACD & M1 CHOCH ==================
def macd_vals(closes, f=MACD_FAST, s=MACD_SLOW, sig=MACD_SIGNAL):
    ser=pd.Series(closes); ef=ser.ewm(span=f, adjust=False).mean(); es=ser.ewm(span=s, adjust=False).mean()
    macd=ef-es; signal=macd.ewm(span=sig, adjust=False).mean()
    return macd.values, signal.values

def macd_cross_dir_closed(ohlcv):
    if not ohlcv or len(ohlcv)<3: return None
    data=ohlcv[:-1]; closes=[b[4] for b in data]
    macd, sig = macd_vals(closes)
    if len(macd)<2: return None
    prev = macd[-2]-sig[-2]; curr=macd[-1]-sig[-1]
    if prev<=0 and curr>0: return 'up'
    if prev>=0 and curr<0: return 'down'
    return None

def m1_choch_closed_in_dir(direction):
    m1=fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=300)
    evs=smc_events(m1[:-1], left=1, right=1)
    if not evs: return False
    e=evs[-1]
    return e['sig']=='CHOCH' and e['bias']==direction

# ================== HELPERS ==================
def in_zone(price, lo, hi):
    a=min(lo,hi); b=max(lo,hi)
    return (price>=a*(1-PRICE_TOLERANCE_PCT)) and (price<=b*(1+PRICE_TOLERANCE_PCT))

def get_equity():
    try:
        bal=exchange.fetch_balance({'type':'future'})
        if 'USDT' in bal and 'total' in bal['USDT']:
            total=float(bal['USDT']['total']); used=float(bal['USDT'].get('used',0) or 0)
            free=max(0.0, total-used)
            return max(0.0, free - MARGIN_BUFFER_USDT)
        return float(bal.get('info',{}).get('availableBalance',0.0))
    except Exception as e:
        log.error(f"get_equity error: {e}"); return 0.0

def contract_size_coin():
    # ETH perp contract size, typically 0.01 ETH on Binance
    return float(market_info.get('contractSize', 0.01) or 0.01)

def max_contracts_by_risk(equity, entry, sl):
    if sl is None: return 1
    dist=abs(entry-sl); 
    if dist<=0: return 1
    risk_per_contract = dist * contract_size_coin()
    max_risk = equity * TARGET_RISK_PCT
    return int(max(1, math.floor(max_risk / max(1e-9, risk_per_contract))))

def propose_contracts_from_equity(equity, entry):
    use_equity = max(0.0, equity) * TARGET_POSITION_SIZE_FACTOR
    target_notional = use_equity * LEVERAGE
    target_coin = target_notional / max(1e-9, entry)
    raw = target_coin / contract_size_coin()
    return max(1, int(round(raw)))

def final_contracts(equity, entry, sl):
    return max(1, min(propose_contracts_from_equity(equity, entry),
                      max_contracts_by_risk(equity, entry, sl)))

# ================== ORDERS ==================
def open_market(direction, contracts):
    side='buy' if direction=='long' else 'sell'
    params={'reduceOnly': False, 'positionSide':'BOTH', 'marginType':'ISOLATED'}
    try:
        amt=float(exchange.amount_to_precision(SYMBOL, contracts))
        exchange.create_market_order(SYMBOL, side, amt, params=params)
        send_telegram(f"✅ [ENTRY] {direction.upper()} {amt} สัญญา @ {get_price():.2f}")
        return True
    except Exception as e:
        log.error(f"open_market failed: {e}")
        send_telegram(f"⛔ เปิดออเดอร์ล้มเหลว: {e}")
        return False

def close_market(contracts, side_now):
    side_to_close='sell' if side_now=='long' else 'buy'
    params={'reduceOnly': True, 'positionSide':'BOTH', 'marginType':'ISOLATED'}
    try:
        amt=float(exchange.amount_to_precision(SYMBOL, contracts))
        exchange.create_market_order(SYMBOL, side_to_close, amt, params=params)
        return True
    except Exception as e:
        log.error(f"close_market failed: {e}")
        send_telegram(f"⛔ ปิดออเดอร์ล้มเหลว: {e}")
        return False

def fetch_position_simple():
    try:
        positions=exchange.fetch_positions([SYMBOL])
        for p in positions:
            sym = p.get('symbol') or p.get('info',{}).get('symbol')
            if sym==SYMBOL or sym==market_info.get('id'):
                amt=float(p.get('contracts', 0) or 0)
                if abs(amt) < 1e-9: return None
                side='long' if amt>0 else 'short'
                entry=float(p.get('entryPrice') or p.get('entry_price') or 0)
                return {'side':side, 'contracts':abs(int(round(amt))), 'entry_price':entry}
        return None
    except Exception as e:
        log.warning(f"fetch_position_simple error: {e}"); return None

# ================== POC FILTER (H1 close vs POC) ==================
def check_poc_filter_h1_close_vs_poc(bias, poc_price, ohlcv_h1):
    if poc_price is None or not ohlcv_h1 or len(ohlcv_h1)<2:
        return True
    last_closed=ohlcv_h1[-2]; c=float(last_closed[4])
    if bias=='up' and c < poc_price*(1-POC_FILTER_TOL):
        alert_once(f"POC_CANCEL_{last_closed[0]}", "❌ [POC] H1 ปิดต่ำกว่า M1 POC → ยกเลิก Long Setup"); return False
    if bias=='down' and c > poc_price*(1+POC_FILTER_TOL):
        alert_once(f"POC_CANCEL_{last_closed[0]}", "❌ [POC] H1 ปิดสูงกว่า M1 POC → ยกเลิก Short Setup"); return False
    return True

# ================== SL HIERARCHY ==================
def derive_sl(fib, bias, poc_m1, price_now, m1=None):
    # 1) POC in 80–100 → SL beyond POC
    if poc_m1 is not None and min(fib['80'], fib['100']) <= poc_m1 <= max(fib['80'], fib['100']):
        return poc_m1*(1-POC_SL_BUFFER) if bias=='up' else poc_m1*(1+POC_SL_BUFFER)
    # 2) touched 80–100 with recent M1 swing → SL at swing low/high inside that zone
    if m1 is None: m1=fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=300)
    touched_80_100 = min(fib['80'],fib['100']) <= price_now <= max(fib['80'],fib['100'])
    if touched_80_100:
        closed=m1[:-1] if len(m1)>1 else m1
        zone=[b for b in closed if min(fib['80'],fib['100'])<=b[4]<=max(fib['80'],fib['100'])]
        if zone:
            lows=[b[3] for b in zone]; highs=[b[2] for b in zone]
            return min(lows) if bias=='up' else max(highs)
    # 3) otherwise Fibo 80
    return fib['80']

# ================== FIBO2 (M1) ==================
def compute_fibo2(h1_fibo0, m1_pre_choch_swing, side):
    """Return {'78.6', 'ext133', 'ext161.8'} using: 100=h1_fibo0, 0=m1_pre_choch."""
    low = m1_pre_choch_swing
    high = h1_fibo0
    diff = high - low
    if side == 'short':
        # mirror for short
        low, high = h1_fibo0, m1_pre_choch_swing
        diff = high - low
    return {
        '78.6': low + 0.786*diff,
        'ext133': low + 1.33*diff,
        'ext161.8': low + 1.618*diff
    }

# ================== STATS ==================
def add_trade_record(reason, pos_info, closed_price):
    try:
        entry=pos_info.get('entry_price',0.0); size=pos_info.get('contracts',0)
        cs=contract_size_coin()
        pnl = (closed_price-entry)*size*cs if pos_info['side']=='long' else (entry-closed_price)*size*cs
        rec={'time':datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), 'side':pos_info['side'],
             'entry':entry, 'closed':closed_price, 'size':size, 'pnl':pnl, 'reason':reason}
        monthly_stats['trades'].append(rec); monthly_stats['total_pnl']+=pnl
        if reason=='TP': monthly_stats['tp_count']+=1
        elif reason=='SL': monthly_stats['sl_count']+=1
        monthly_stats['month_year']=datetime.utcnow().strftime('%Y-%m')
        with open(STATS_FILE,'w') as f: json.dump(monthly_stats,f,indent=2)
    except Exception as e:
        log.error(f"add_trade_record error: {e}")

# ================== MAIN LOOP ==================
def main_loop():
    global current_position, pending_trade, cooldown_until

    send_telegram("🤖 เริ่มบอท: ETH SMC+Fibo+POC (isolated, one-way, Mode B)")
    while True:
        try:
            if cooldown_until and datetime.utcnow()<cooldown_until:
                time.sleep(CHECK_INTERVAL); continue

            price = get_price()
            pos   = fetch_position_simple()
            current_position = pos

            # ---- H1 SMC update ----
            h1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_H1, limit=180)
            latest, bias = latest_smc(h1)

            # flag touched33
            if state['fibo'] and in_zone(price, state['fibo']['33'], state['fibo']['0']):
                state['touched_33_since_last_bos'] = True
                state['fibo_locked'] = True  # 🔒 lock once entered zone

            # BOS handling (only if not locked)
            if latest and latest['sig']=='BOS' and bias:
                if state['bias'] == bias:
                    update_fibo_on_bos_without_pullback_to_33(latest['p'], bias)
                    if not state['fibo_locked']:
                        state['touched_33_since_last_bos'] = False
                else:
                    if not state['fibo_locked']:
                        state['touched_33_since_last_bos'] = False

            # ====== STEP MACHINE ======
            if not current_position:
                # STEP1: need H1 SMC
                if state['step']==1:
                    if not bias:
                        alert_once("STEP1_WAIT","🧭 [STEP1] รอ H1 SMC (BOS/CHOCH)"); time.sleep(CHECK_INTERVAL); continue
                    state['bias']=bias; state['latest_h1']=latest
                    f100,f0,fib,zone = init_fibo_from_h1(h1, bias)
                    state.update({'fibo100':f100,'fibo0':f0,'fibo':fib,'entry_zone':zone,
                                  'touched_33_since_last_bos':False,'fibo_locked':False,'last_fibo_shift_key':None})
                    state['step']=2; reset_alerts()
                    alert_once("STEP1_OK", f"🧭 [STEP1→OK] H1 {latest['sig']} → Trend {bias.upper()} (ไป STEP2)")

                # STEP2: zone + POC + H1 close filter
                if state['step']==2:
                    fib=state['fibo']
                    poc_m1 = compute_m1_poc(fib, state['bias'])
                    # uptrend: ignore POC below 71.8
                    if poc_m1 is not None and state['bias']=='up' and poc_m1 < fib['71.8']:
                        poc_m1 = None
                    state['poc_m1']=poc_m1
                    if not check_poc_filter_h1_close_vs_poc(state['bias'], poc_m1, h1):
                        state['step']=1; time.sleep(CHECK_INTERVAL); continue
                    alert_once("STEP2_WAIT","⌛ [STEP2] รอราคาเข้าโซน Fibo 33–78.6")
                    if in_zone(price, state['entry_zone'][0], state['entry_zone'][1]):
                        state['fibo_locked'] = True  # 🔒 lock here
                        reset_alerts("STEP2_"); alert_once("STEP2_INZONE","📍 [STEP2] ราคาเข้าโซน Fibo (H1) → ไป STEP3")
                        state['step']=3; reset_alerts()
                    else:
                        time.sleep(CHECK_INTERVAL); continue

                # STEP3: Entry confirm (M1 CHOCH OR MACD cross) on closed candle
                if state['step']==3:
                    alert_once("STEP3_WAIT","🧪 [STEP3] รอ M1 Confirm")
                    if not in_zone(price, state['entry_zone'][0], state['entry_zone'][1]):
                        alert_once("STEP3_OUTZONE","⏸️ [STEP3] ราคาออกนอกโซน → รอกลับเข้าโซน"); time.sleep(CHECK_INTERVAL); continue
                    direction = 'up' if state['bias']=='up' else 'down'
                    choch_ok = m1_choch_closed_in_dir(direction)
                    m1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=300)
                    macd_dir = macd_cross_dir_closed(m1)
                    macd_ok = (macd_dir==direction)
                    if choch_ok or macd_ok:
                        sl = derive_sl(state['fibo'], state['bias'], state['poc_m1'], price, m1)
                        eq = get_equity()
                        contracts = final_contracts(eq, price, sl)
                        if contracts<=0:
                            send_telegram("⚠ ขนาดสัญญาหลัง risk cap = 0 ข้ามไม้"); state['step']=1; continue
                        opened = open_market('long' if direction=='up' else 'short', contracts)
                        if not opened: time.sleep(CHECK_INTERVAL); continue
                        tp1 = state['fibo']['0'] if direction=='up' else state['fibo']['100']
                        pending_trade = {
                            'side':'long' if direction=='up' else 'short',
                            'contracts':contracts,
                            'entry_price':price,
                            'state':'OPEN',
                            'tp1_price':tp1,
                            'sl_price':sl,
                            'trend':direction,
                            'm1_pre_choch_swing': None,  # จะอัปเดตเมื่อ detect ตอนยืนยัน (ง่ายสุด: ใช้ last swing ใน m1)
                        }
                        # เก็บ swing ก่อน CHOCH (ถ้าใช้ CHOCH เป็นตัว confirm)
                        if choch_ok:
                            # หา swing ก่อนหน้าสุดใน M1 (low สำหรับ long, high สำหรับ short)
                            lows=[b[3] for b in m1[:-1]]; highs=[b[2] for b in m1[:-1]]
                            if direction=='up' and lows: pending_trade['m1_pre_choch_swing'] = min(lows[-5:])  # โซนใกล้ ๆ
                            if direction=='down' and highs: pending_trade['m1_pre_choch_swing'] = max(highs[-5:])
                        state['step']=99; reset_alerts()
                        send_telegram(f"🎯 SL เริ่มต้น: {sl:.2f} | TP1: {tp1:.2f}")
                        time.sleep(CHECK_INTERVAL); continue
                    else:
                        time.sleep(CHECK_INTERVAL); continue

            # ====== POSITION MANAGEMENT ======
            if current_position and pending_trade:
                price = get_price()
                # Initial SL
                if pending_trade['state']=='OPEN':
                    sl=pending_trade['sl_price']
                    if sl and ((pending_trade['side']=='long' and price<=sl*(1+PRICE_TOLERANCE_PCT)) or
                               (pending_trade['side']=='short' and price>=sl*(1-PRICE_TOLERANCE_PCT))):
                        amt=current_position['contracts']
                        if amt>0 and close_market(amt, pending_trade['side']):
                            send_telegram(f"❌ [SL] ปิด @ {price:.2f}")
                            add_trade_record('SL', {'side':pending_trade['side'],'entry_price':pending_trade['entry_price'],'contracts':amt}, price)
                            cooldown_until = datetime.utcnow()+timedelta(hours=COOLDOWN_HOURS)
                            pending_trade=None; current_position=None
                            state.update({'step':1,'bias':None,'latest_h1':None,'fibo100':None,'fibo0':None,'fibo':None,
                                          'entry_zone':None,'poc_m1':None,'touched_33_since_last_bos':True,'fibo_locked':False})
                            reset_alerts(); alert_once("STEP1_WAIT","🔁 [RESET] กลับ STEP1")
                            time.sleep(5); continue
                    # TP1
                    tp1=pending_trade['tp1_price']
                    hit = (price >= tp1*(1-PRICE_TOLERANCE_PCT)) if pending_trade['side']=='long' else (price <= tp1*(1+PRICE_TOLERANCE_PCT))
                    if hit:
                        close_amt = max(1, int(round(pending_trade['contracts']*TP1_CLOSE_PERCENT)))
                        if close_market(close_amt, pending_trade['side']):
                            send_telegram(f"✅ [TP1] ปิด {TP1_CLOSE_PERCENT*100:.0f}% @ {price:.2f}")
                            # ---- FIBO2 ----
                            h1_fibo0 = state['fibo']['0']
                            pre = pending_trade.get('m1_pre_choch_swing')
                            if pre is None:
                                m1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=300)
                                lows=[b[3] for b in m1[:-1]]; highs=[b[2] for b in m1[:-1]]
                                if pending_trade['side']=='long' and lows: pre = min(lows[-10:])
                                if pending_trade['side']=='short' and highs: pre = max(highs[-10:])
                            fib2 = compute_fibo2(h1_fibo0, pre if pre else (tp1*0.99 if pending_trade['side']=='long' else tp1*1.01),
                                                 'long' if pending_trade['side']=='long' else 'short')
                            pending_trade['fibo2']=fib2
                            pending_trade['sl2']=fib2['78.6']
                            pending_trade['state']='TP1_HIT'
                            send_telegram(f"🔁 [SL] เลื่อนไป Fibo2 78.6 = {fib2['78.6']:.2f} | TP2 {fib2['ext133']:.2f}–{fib2['ext161.8']:.2f}")
                            time.sleep(CHECK_INTERVAL); continue

                # TP2 / Emergency (only in TP2 phase) / SL2
                if pending_trade.get('state')=='TP1_HIT' and 'fibo2' in pending_trade:
                    fib2=pending_trade['fibo2']; lo,hi=fib2['ext133'], fib2['ext161.8']
                    in_tp2 = (price >= lo*(1-PRICE_TOLERANCE_PCT)) and (price <= hi*(1+PRICE_TOLERANCE_PCT))
                    # Emergency ONLY during TP2 phase
                    m1=fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=10)
                    emergency=False
                    if len(m1)>=3:
                        last, p1, p2 = m1[-1][4], m1[-2][4], m1[-3][4]
                        if pending_trade['side']=='long' and last<p1 and p1<p2: emergency=True
                        if pending_trade['side']=='short' and last>p1 and p1>p2: emergency=True
                    if in_tp2:
                        remain = current_position['contracts']
                        if remain>0 and close_market(remain, pending_trade['side']):
                            send_telegram(f"🏁 [TP2] ปิดส่วนที่เหลือ @ {price:.2f}")
                            add_trade_record('TP', {'side':pending_trade['side'],'entry_price':pending_trade['entry_price'],'contracts':remain}, price)
                            cooldown_until = datetime.utcnow()+timedelta(hours=COOLDOWN_HOURS)
                            pending_trade=None; current_position=None
                            state.update({'step':1,'bias':None,'latest_h1':None,'fibo100':None,'fibo0':None,'fibo':None,
                                          'entry_zone':None,'poc_m1':None,'touched_33_since_last_bos':True,'fibo_locked':False})
                            reset_alerts(); alert_once("STEP1_WAIT","🔁 [RESET] กลับ STEP1")
                            time.sleep(5); continue
                    elif emergency:
                        remain = current_position['contracts']
                        if remain>0 and close_market(remain, pending_trade['side']):
                            send_telegram(f"⚠ [EMERGENCY@TP2] ปิดส่วนที่เหลือ @ {price:.2f}")
                            add_trade_record('Emergency Close', {'side':pending_trade['side'],'entry_price':pending_trade['entry_price'],'contracts':remain}, price)
                            cooldown_until = datetime.utcnow()+timedelta(hours=COOLDOWN_HOURS)
                            pending_trade=None; current_position=None
                            state.update({'step':1,'bias':None,'latest_h1':None,'fibo100':None,'fibo0':None,'fibo':None,
                                          'entry_zone':None,'poc_m1':None,'touched_33_since_last_bos':True,'fibo_locked':False})
                            reset_alerts(); alert_once("STEP1_WAIT","🔁 [RESET] กลับ STEP1")
                            time.sleep(5); continue
                    # SL2
                    sl2=pending_trade.get('sl2')
                    if sl2 and ((pending_trade['side']=='long' and price<=sl2*(1+PRICE_TOLERANCE_PCT)) or
                                (pending_trade['side']=='short' and price>=sl2*(1-PRICE_TOLERANCE_PCT))):
                        remain=current_position['contracts']
                        if remain>0 and close_market(remain, pending_trade['side']):
                            send_telegram(f"🛑 [SL2] ปิดส่วนที่เหลือ @ {price:.2f}")
                            add_trade_record('SL', {'side':pending_trade['side'],'entry_price':pending_trade['entry_price'],'contracts':remain}, price)
                            cooldown_until = datetime.utcnow()+timedelta(hours=COOLDOWN_HOURS)
                            pending_trade=None; current_position=None
                            state.update({'step':1,'bias':None,'latest_h1':None,'fibo100':None,'fibo0':None,'fibo':None,
                                          'entry_zone':None,'poc_m1':None,'touched_33_since_last_bos':True,'fibo_locked':False})
                            reset_alerts(); alert_once("STEP1_WAIT","🔁 [RESET] กลับ STEP1")
                            time.sleep(5); continue

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            log.exception(f"Loop error: {e}")
            send_telegram(f"⛔ Bot error: {e}")
            time.sleep(10)

# ================== START ==================
def start_bot():
    setup_exchange()
    main_loop()

if __name__ == '__main__':
    start_bot()
