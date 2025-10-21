# main.py — Binance Futures (USDT-M, isolated, one-way)
# SMC (H1 BOS/CHOCH) + Strict Fibo Zone + M1 Pullback POC + SL Hierarchy (POC→Swing→Fibo80)
# Entry: (M1 CHOCH OR MACD cross) on closed candle, same as trend
# TP1→TP2 + Emergency (เฉพาะ TP2 phase เท่านั้น)
# Fibo anchoring: หาก BOS แล้วราคาไม่ย่อถึง 33 ก่อน BOS ใหม่ → เลื่อน Fibo 0 ไป high/low ใหม่ แต่ Fibo 100 ยังคงเดิม
# One-shot alerts (C2), no duplicates

import os, time, math, json, logging, threading
from datetime import datetime, timedelta
from collections import defaultdict

import ccxt
import requests
import pandas as pd

# ================== CONFIG (ปรับได้) ==================
API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET  = os.getenv('BINANCE_SECRET',    'YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING')

SYMBOL            = 'BTC/USFT:USDT' if False else 'BTC/USDT:USDT'  # Binance USDT-M perp
TIMEFRAME_H1      = '1h'
TIMEFRAME_M5      = '5m'
TIMEFRAME_M1      = '1m'
LEVERAGE          = 25

# Mode B (Safe) — สำหรับพอร์ตเล็ก
TARGET_POSITION_SIZE_FACTOR = 0.35    # ใช้ ~35% ของ equity เพื่อคำนวณ notional ตั้งต้น
TARGET_RISK_PCT             = 0.005   # ความเสี่ยงต่อไม้ 0.5% ของ equity
MARGIN_BUFFER_USDT          = 5       # กันเงินไม่ใช้หมด

# Alerts / Toggles
STEP_ALERT = True
USE_M1_CHOCH_CONFIRM = True
USE_MACD_CONFIRM     = True
USE_POC_FILTER       = True  # ถ้า H1 ปิดผิดฝั่ง M1 POC → ยกเลิก setup

# MACD STD
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9

# TP/SL
TP1_CLOSE_PERCENT = 0.60
CHECK_INTERVAL    = 15
COOLDOWN_H1_AFTER_TRADE = 3  # hours

# Precision
PRICE_TOLERANCE_PCT = 0.0005      # 0.05%
POC_FILTER_TOL      = 0.001       # 0.1% ใช้เช็คยกเลิก setup
POC_SL_BUFFER       = 0.001       # 0.10% ใต้/เหนือ POC

# Files
STATS_FILE = 'trades_stats.json'

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
log = logging.getLogger('binance_smc_full')

# ================== GLOBALS ==================
exchange = None
market_info = None

# 状態
current_position = None
pending_trade    = None
cooldown_until   = None

# One-shot alerts
last_notices = set()

# STEP machine + Fibo anchor state
state = {
    'step': 1,           # 1 H1 SMC, 2 Zone+POC, 3 M1 Confirm, 99 in-position
    'bias': None,        # 'up'|'down'
    'latest_h1': None,
    # Fibo anchoring: we keep 100 anchored, 0 movable when repetitive BOS without 33 pullback
    'fibo100': None,
    'fibo0':   None,
    'fibo':    None,
    'entry_zone': None,  # (33..78.6)
    'poc_m1':  None,
    'touched_33_since_last_bos': True,  # flag for anchor rule
}

monthly_stats = {'month_year': None, 'tp_count': 0, 'sl_count': 0.0, 'total_pnl': 0.0, 'trades': []}

# ================== TELEGRAM ==================
def send_telegram(msg: str):
    try:
        token = TELEGRAM_TOKEN
        chat_id = TELEGRAM_CHAT_ID
    except NameError:
        # กรณี interpreter ยังไม่เห็นตัวแปร
        log.info("[TG-SKIP] Telegram variables not defined yet")
        return

    # ถ้ายังไม่ตั้งค่าบน Railway → ให้ข้าม ไม่ส่ง
    if not token or not chat_id or token.startswith("YOUR_") or chat_id.startswith("YOUR_"):
        log.info("[TG-SKIP] " + (msg[:120] if msg else ""))
        return

    try:
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        params = {'chat_id': chat_id, 'text': msg, 'parse_mode': 'HTML'}
        requests.get(url, params=params, timeout=10)
        log.info("[TG] " + (msg[:120] if msg else ""))
    except Exception as e:
        log.error(f"[TG-ERROR] {e}")
        
def alert_once(key: str, message: str):
    if STEP_ALERT and key not in last_notices:
        last_notices.add(key); send_telegram(message)

def reset_alerts(prefix: str | None = None):
    global last_notices
    if prefix is None: last_notices.clear()
    else: last_notices = {k for k in last_notices if not k.startswith(prefix)}

# ================== EXCHANGE ==================
def setup_exchange():
    global exchange, market_info
    exchange = ccxt.binance({
        'apiKey': API_KEY, 'secret': SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True},
        'timeout': 30000
    })
    exchange.load_markets()
    if SYMBOL not in exchange.markets:
        raise RuntimeError(f"Symbol {SYMBOL} not found. Make sure it's USDT-M perp like BTC/USDT:USDT")
    market_info = exchange.market(SYMBOL)
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
        log.info(f"Leverage set {LEVERAGE}x for {SYMBOL}")
    except Exception as e:
        log.warning(f"set_leverage failed (continue): {e}")

# ================== DATA HELPERS ==================
def fetch_ohlcv_safe(symbol, timeframe, limit=200):
    for _ in range(3):
        try: return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as e:
            log.warning(f"fetch_ohlcv error: {e}, retry..."); time.sleep(3)
    raise RuntimeError("fetch_ohlcv failed")

def get_price():
    return float(exchange.fetch_ticker(SYMBOL)['last'])

# ================== SMC (Swings & BOS/CHOCH) ==================
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

# ================== FIBO ANCHORING ==================
def build_fibo(low, high):
    diff = high-low
    return {
        '0': high, '100': low,
        '33': high - 0.33*diff,
        '38.2': high - 0.382*diff,
        '50': high - 0.5*diff,
        '61.8': high - 0.618*diff,
        '71.8': high - 0.718*diff,
        '78.6': high - 0.786*diff,
        '80':  high - 0.80*diff,
        'ext133': low + 1.33*diff,
        'ext161.8': low + 1.618*diff
    }

def init_fibo_from_h1(ohlcv_h1, bias):
    recent = ohlcv_h1[-50:]
    swing_high = max(b[2] for b in recent)
    swing_low  = min(b[3] for b in recent)
    if bias=='up':
        fibo100 = swing_low; fibo0 = swing_high
    else:
        # สำหรับ downtrend ใช้ mapping mirror (ให้ 0 เป็น low, 100 เป็น high) แต่เพื่อความง่ายใช้แบบเดียวกันเชิงตัวเลข
        fibo100 = swing_low; fibo0 = swing_high
    fib = build_fibo(fibo100, fibo0)
    return fibo100, fibo0, fib, (fib['33'], fib['78.6'])

def update_fibo_on_bos_without_pullback_to_33(new_bos_price, bias):
    """
    ถ้า BOS ใหม่เกิด และยังไม่แตะ 33 ตั้งแต่ BOS ก่อนหน้า → เลื่อน fibo 0 ไป high/low ใหม่ (ตาม bias) โดยคง fibo100 เดิม
    """
    if state['fibo100'] is None or state['fibo0'] is None: return
    if state['touched_33_since_last_bos']: return  # ถ้าเคยแตะ 33 แล้ว ไม่ต้องเลื่อน
    # เลื่อนเฉพาะ 0
    if bias=='up':
        if new_bos_price > state['fibo0']:
            state['fibo0'] = new_bos_price
    else:
        if new_bos_price < state['fibo0']:
            state['fibo0'] = new_bos_price
    # rebuild fibo + zone
    fib = build_fibo(state['fibo100'], state['fibo0'])
    state['fibo'] = fib
    state['entry_zone'] = (fib['33'], fib['78.6'])
    alert_once(f"FIBO_SHIFT_{int(datetime.utcnow().timestamp())}",
               "🧭 [FIBO] BOS ต่อเนื่องโดยไม่แตะ 33 → ขยับ Fibo 0 ไปสวิงใหม่ (คง 100 เดิม)")

# ================== POC (M1 pullback subset) ==================
def vp_poc_from_bars(bars, bucket_size=None):
    if not bars: return None
    prices=[(b[2]+b[3]+b[4])/3 for b in bars]; vols=[b[5] or 0.0 for b in bars]
    mn, mx = min(prices), max(prices)
    width = max((mx-mn)/40.0, 0.5)
    bins = defaultdict(float)
    for p, v in zip(prices, vols):
        idx=int((p-mn)/width); center=mn+(idx+0.5)*width; bins[center]+=v
    if not bins: return None
    return max(bins.items(), key=lambda x:x[1])[0]

def m1_pullback_subset_for_poc(m1, fib, bias):
    if not m1: return []
    lo, hi = fib['100'], fib['0']
    if bias=='up':
        # ช่วงย่อ: โซน [100..61.8] ใกล้ปลายล่าง
        return [b for b in m1 if fib['100'] <= b[4] <= fib['61.8']]
    else:
        # สำหรับลง: mirror แบบคร่าว ๆ → ใช้โซน [38.2..0]
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

def contract_size_btc():
    cs=market_info.get('contractSize', 0.001)
    return float(cs or 0.001)

def max_contracts_by_risk(equity, entry, sl):
    if sl is None: return 1
    dist=abs(entry-sl); 
    if dist<=0: return 1
    risk_per_contract = dist * contract_size_btc()
    max_risk = equity * TARGET_RISK_PCT
    return int(max(1, math.floor(max_risk / max(1e-9, risk_per_contract))))

def propose_contracts_from_equity(equity, entry):
    use_equity = max(0.0, equity) * TARGET_POSITION_SIZE_FACTOR
    target_notional = use_equity * LEVERAGE
    target_btc = target_notional / max(1e-9, entry)
    raw = target_btc / contract_size_btc()
    return max(1, int(round(raw)))

def final_contracts(equity, entry, sl):
    return max(1, min(propose_contracts_from_equity(equity, entry), max_contracts_by_risk(equity, entry, sl)))

def open_market(direction, contracts):
    side='buy' if direction=='long' else 'sell'
    params={'reduceOnly': False, 'positionSide':'BOTH', 'marginType':'ISOLATED'}
    try:
        amt=float(exchange.amount_to_precision(SYMBOL, contracts))
        exchange.create_market_order(SYMBOL, side, amt, params=params)
        send_telegram(f"✅ [ENTRY] {direction.upper()} {amt} สัญญา @ {get_price():.2f}")
        return True
    except Exception as e:
        log.error(f"open_market failed: {e}"); send_telegram(f"⛔ เปิดออเดอร์ล้มเหลว: {e}"); return False

def close_market(contracts, side_now):
    side_to_close='sell' if side_now=='long' else 'buy'
    params={'reduceOnly': True, 'positionSide':'BOTH', 'marginType':'ISOLATED'}
    try:
        amt=float(exchange.amount_to_precision(SYMBOL, contracts))
        exchange.create_market_order(SYMBOL, side_to_close, amt, params=params)
        return True
    except Exception as e:
        log.error(f"close_market failed: {e}"); send_telegram(f"⛔ ปิดออเดอร์ล้มเหลว: {e}"); return False

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

# ================== POC Filter (H1 close vs M1 POC) ==================
def check_poc_filter_h1_close_vs_poc(bias, poc_price, ohlcv_h1):
    if not USE_POC_FILTER or poc_price is None or not ohlcv_h1 or len(ohlcv_h1)<2:
        return True
    last_closed=ohlcv_h1[-2]; c=float(last_closed[4])
    if bias=='up' and c < poc_price*(1-POC_FILTER_TOL):
        alert_once(f"POC_CANCEL_{last_closed[0]}", "❌ [POC] H1 ปิดต่ำกว่า M1 POC → ยกเลิก Long Setup"); return False
    if bias=='down' and c > poc_price*(1+POC_FILTER_TOL):
        alert_once(f"POC_CANCEL_{last_closed[0]}", "❌ [POC] H1 ปิดสูงกว่า M1 POC → ยกเลิก Short Setup"); return False
    return True

# ================== SL Hierarchy ==================
def derive_sl(fib, bias, poc_m1, price_now, m1=None):
    # 1) ถ้า POC อยู่ใน 80–100 → SL ใต้/เหนือ POC 0.10%
    if poc_m1 is not None and min(fib['80'], fib['100']) <= poc_m1 <= max(fib['80'], fib['100']):
        return poc_m1*(1-POC_SL_BUFFER) if bias=='up' else poc_m1*(1+POC_SL_BUFFER)
    # 2) ถ้าแตะ 80–100 และมีสวิง M1 → SL = swing low/high ในโซน
    if m1 is None: m1=fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=300)
    touched_80_100 = min(fib['80'],fib['100']) <= price_now <= max(fib['80'],fib['100'])
    if touched_80_100:
        closed=m1[:-1] if len(m1)>1 else m1
        zone=[b for b in closed if min(fib['80'],fib['100'])<=b[4]<=max(fib['80'],fib['100'])]
        if zone:
            lows=[b[3] for b in zone]; highs=[b[2] for b in zone]
            return min(lows) if bias=='up' else max(highs)
    # 3) ยังไม่แตะ 80–100 → SL = Fibo 80
    return fib['80']

# ================== STATS ==================
def add_trade_record(reason, pos_info, closed_price):
    try:
        entry=pos_info.get('entry_price',0.0); size=pos_info.get('contracts',0)
        cs=contract_size_btc()
        pnl = (closed_price-entry)*size*cs if pos_info['side']=='long' else (entry-closed_price)*size*cs
        rec={'time':datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), 'side':pos_info['side'],
             'entry':entry, 'closed':closed_price, 'size':size, 'pnl':pnl, 'reason':reason}
        monthly_stats['trades'].append(rec); monthly_stats['total_pnl']+=pnl
        if reason=='TP': monthly_stats['tp_count']+=1
        elif reason=='SL': monthly_stats['sl_count']+=1
        with open(STATS_FILE,'w') as f: json.dump(monthly_stats,f,indent=2)
    except Exception as e:
        log.error(f"add_trade_record error: {e}")

# ================== MAIN LOOP ==================
def main_loop():
    global current_position, pending_trade, cooldown_until
    log.info("Main loop started (Binance FULL)")
    while True:
        try:
            if cooldown_until and datetime.utcnow()<cooldown_until:
                time.sleep(CHECK_INTERVAL); continue

            price = get_price()
            pos   = fetch_position_simple()
            current_position = pos

            # --- H1 SMC update ---
            h1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_H1, limit=180)
            latest, bias = latest_smc(h1)

            # Update 'touched_33_since_last_bos' flag
            if state['fibo']:
                if in_zone(price, state['fibo']['33'], state['fibo']['0']):
                    state['touched_33_since_last_bos'] = True

            # If BOS occurred: manage Fibo anchor rule
            if latest and latest['sig']=='BOS' and bias:
                if state['bias'] == bias:
                    # same direction BOS; if not touched 33 since previous BOS -> shift fibo0
                    update_fibo_on_bos_without_pullback_to_33(latest['p'], bias)
                    # reset pullback flag for next BOS tracking
                    state['touched_33_since_last_bos'] = False
                else:
                    # direction changed; reset pullback flag
                    state['touched_33_since_last_bos'] = False

            # CLOSE on opposite CHOCH? (เฉพาะเมื่อมีโพซิชัน) — นอกเหนือ TP/SL flow
            # *หมายเหตุ: Emergency เราจะจำกัดไว้เฉพาะ TP2 phase ตามที่สั่ง*
            # สำหรับ CHOCH opposites ระหว่าง OPEN/TP1 phase ให้ยังคุมด้วย SL ปกติ

            # ====== STEP MACHINE ======
            if not current_position:
                # STEP1: Need H1 SMC
                if state['step']==1:
                    if not bias:
                        alert_once("STEP1_WAIT","🧭 [STEP1] รอ H1 SMC (BOS/CHOCH)"); time.sleep(CHECK_INTERVAL); continue
                    state['bias']=bias; state['latest_h1']=latest
                    # init fibo anchor
                    f100, f0, fib, zone = init_fibo_from_h1(h1, bias)
                    state['fibo100']=f100; state['fibo0']=f0; state['fibo']=fib; state['entry_zone']=zone
                    state['touched_33_since_last_bos'] = False
                    state['step']=2; reset_alerts()
                    alert_once("STEP1_OK", f"🧭 [STEP1→OK] H1 {latest['sig']} → เทรนด์ {bias.upper()} (ไป STEP2)")

                # STEP2: Zone + POC(M1) + POC filter
                if state['step']==2:
                    # refresh fibo if bias updated earlier
                    if not state['fibo'] or not state['entry_zone']:
                        f100, f0, fib, zone = init_fibo_from_h1(h1, state['bias'])
                        state['fibo100']=f100; state['fibo0']=f0; state['fibo']=fib; state['entry_zone']=zone

                    fib = state['fibo']
                    poc_m1 = compute_m1_poc(fib, state['bias'])
                    # POC validity: must be near 80–100 zone to be meaningful; ignore if <71.8 in uptrend
                    if poc_m1 is not None and state['bias']=='up' and poc_m1 < fib['71.8']:
                        poc_m1 = None
                    state['poc_m1'] = poc_m1

                    if not check_poc_filter_h1_close_vs_poc(state['bias'], state['poc_m1'], h1):
                        state['step']=1; time.sleep(CHECK_INTERVAL); continue

                    alert_once("STEP2_WAIT", "⌛ [STEP2] รอราคาเข้าโซน Fibo 33–78.6")
                    if in_zone(price, state['entry_zone'][0], state['entry_zone'][1]):
                        reset_alerts("STEP2_")
                        alert_once("STEP2_INZONE", "📍 [STEP2] ราคาเข้าโซน Fibo (H1) → ไป STEP3")
                        state['step']=3; reset_alerts()
                    else:
                        time.sleep(CHECK_INTERVAL); continue

                # STEP3: Entry confirm (M1 CHOCH OR MACD cross) + strict zone
                if state['step']==3:
                    alert_once("STEP3_WAIT","🧪 [STEP3] รอ M1 Confirm")
                    if not in_zone(price, state['entry_zone'][0], state['entry_zone'][1]):
                        alert_once("STEP3_OUTZONE","⏸️ [STEP3] ราคาออกนอกโซน Fibo → รอกลับเข้าโซน")
                        time.sleep(CHECK_INTERVAL); continue

                    direction = 'up' if state['bias']=='up' else 'down'
                    choch_ok = m1_choch_closed_in_dir(direction) if USE_M1_CHOCH_CONFIRM else False
                    m1 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=300)
                    macd_dir = macd_cross_dir_closed(m1) if USE_MACD_CONFIRM else None
                    macd_ok = (macd_dir==direction) if USE_MACD_CONFIRM else False

                    if ( (USE_M1_CHOCH_CONFIRM and choch_ok) or (USE_MACD_CONFIRM and macd_ok) ):
                        sl = derive_sl(state['fibo'], state['bias'], state['poc_m1'], price, m1)
                        eq = get_equity()
                        contracts = final_contracts(eq, price, sl)
                        if contracts<=0:
                            send_telegram("⚠ ขนาดสัญญาหลัง risk cap = 0 ข้ามไม้"); state['step']=1; continue
                        opened = open_market('long' if direction=='up' else 'short', contracts)
                        if not opened: time.sleep(CHECK_INTERVAL); continue
                        # set pending
                        tp1 = state['fibo']['0'] if direction=='up' else state['fibo']['100']
                        pending_trade = {
                            'side': 'long' if direction=='up' else 'short',
                            'contracts': contracts,
                            'entry_price': price,
                            'state': 'OPEN',
                            'tp1_price': tp1,
                            'sl_price': sl,
                            'trend': direction,
                        }
                        state['step']=99; reset_alerts()
                        send_telegram(f"🎯 SL เริ่มต้น: {sl:.2f} | TP1: {tp1:.2f}")
                        time.sleep(CHECK_INTERVAL); continue
                    else:
                        time.sleep(CHECK_INTERVAL); continue

            # ====== POSITION MANAGEMENT ======
            if current_position:
                price = get_price()
                # TP1
                if pending_trade and pending_trade['state']=='OPEN':
                    tp1 = pending_trade['tp1_price']
                    hit = (price >= tp1*(1-PRICE_TOLERANCE_PCT)) if pending_trade['side']=='long' else (price <= tp1*(1+PRICE_TOLERANCE_PCT))
                    if hit:
                        close_amt = max(1, int(round(pending_trade['contracts']*TP1_CLOSE_PERCENT)))
                        if close_market(close_amt, pending_trade['side']):
                            send_telegram(f"✅ [TP1] ปิด {TP1_CLOSE_PERCENT*100:.0f}% @ {price:.2f}")
                            # compute fibo2 (แบบง่ายจาก TP1)
                            # ใช้ M5 คำนวณ extension
                            m5 = fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M5, limit=200)
                            highs=[b[2] for b in m5]; lows=[b[3] for b in m5]
                            base = tp1
                            if pending_trade['side']=='long':
                                hh = max(highs) if highs else base*1.03
                                diff = hh-base
                                fibo2 = {'78.6': base + 0.786*diff, 'ext133': base + 1.33*diff, 'ext161.8': base + 1.618*diff}
                            else:
                                ll = min(lows) if lows else base*0.97
                                diff = base-ll
                                fibo2 = {'78.6': base - 0.786*diff, 'ext133': base - 1.33*diff, 'ext161.8': base - 1.618*diff}
                            pending_trade['fibo2']=fibo2
                            pending_trade['sl2']=fibo2['78.6']
                            pending_trade['state']='TP1_HIT'
                            send_telegram(f"🔁 [SL] เลื่อนไป Fibo2 78.6 = {fibo2['78.6']:.2f} | TP2 {fibo2['ext133']:.2f}–{fibo2['ext161.8']:.2f}")

                # TP2 / Emergency (เฉพาะ TP2 phase) / SL2
                if pending_trade and pending_trade.get('state')=='TP1_HIT' and 'fibo2' in pending_trade:
                    fib2=pending_trade['fibo2']; lo,hi=fib2['ext133'], fib2['ext161.8']
                    in_tp2 = (price >= lo*(1-PRICE_TOLERANCE_PCT)) and (price <= hi*(1+PRICE_TOLERANCE_PCT))
                    # Emergency เฉพาะ TP2 phase เท่านั้น (ตามที่สั่ง)
                    # เช็ค momentum ผิดฝั่งอย่างหยาบด้วย M1 3 แท่ง
                    m1=fetch_ohlcv_safe(SYMBOL, TIMEFRAME_M1, limit=10)
                    emergency=False
                    if len(m1)>=3:
                        last, p1, p2 = m1[-1][4], m1[-2][4], m1[-3][4]
                        if pending_trade['side']=='long' and last<p1 and p1<p2: emergency=True
                        if pending_trade['side']=='short' and last>p1 and p1>p2: emergency=True
                    if in_tp2:
                        # close remaining
                        remain = current_position['contracts']  # เก็บเท่าที่ exchange รายงาน
                        if remain>0 and close_market(remain, pending_trade['side']):
                            send_telegram(f"🏁 [TP2] ปิดส่วนที่เหลือ @ {price:.2f}")
                            add_trade_record('TP', {'side':pending_trade['side'],'entry_price':pending_trade['entry_price'],'contracts':remain}, price)
                            cooldown_until = datetime.utcnow()+timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            pending_trade=None; current_position=None
                            # reset to step1
                            state.update({'step':1,'bias':None,'latest_h1':None,'fibo100':None,'fibo0':None,'fibo':None,'entry_zone':None,'poc_m1':None})
                            reset_alerts(); alert_once("STEP1_WAIT","🔁 [RESET] กลับ STEP1")
                            time.sleep(5); continue
                    elif emergency:
                        remain = current_position['contracts']
                        if remain>0 and close_market(remain, pending_trade['side']):
                            send_telegram(f"⚠ [EMERGENCY@TP2] ปิดส่วนที่เหลือ @ {price:.2f}")
                            add_trade_record('Emergency Close', {'side':pending_trade['side'],'entry_price':pending_trade['entry_price'],'contracts':remain}, price)
                            cooldown_until = datetime.utcnow()+timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                            pending_trade=None; current_position=None
                            state.update({'step':1,'bias':None,'latest_h1':None,'fibo100':None,'fibo0':None,'fibo':None,'entry_zone':None,'poc_m1':None})
                            reset_alerts(); alert_once("STEP1_WAIT","🔁 [RESET] กลับ STEP1")
                            time.sleep(5); continue
                    # SL2
                    sl2 = pending_trade.get('sl2')
                    if sl2:
                        if (pending_trade['side']=='long' and price<=sl2*(1+PRICE_TOLERANCE_PCT)) or \
                           (pending_trade['side']=='short' and price>=sl2*(1-PRICE_TOLERANCE_PCT)):
                            remain=current_position['contracts']
                            if remain>0 and close_market(remain, pending_trade['side']):
                                send_telegram(f"🛑 [SL2] ปิดส่วนที่เหลือ @ {price:.2f}")
                                add_trade_record('SL', {'side':pending_trade['side'],'entry_price':pending_trade['entry_price'],'contracts':remain}, price)
                                cooldown_until = datetime.utcnow()+timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                                pending_trade=None; current_position=None
                                state.update({'step':1,'bias':None,'latest_h1':None,'fibo100':None,'fibo0':None,'fibo':None,'entry_zone':None,'poc_m1':None})
                                reset_alerts(); alert_once("STEP1_WAIT","🔁 [RESET] กลับ STEP1")
                                time.sleep(5); continue

                # SL initial
                if pending_trade and pending_trade.get('state')=='OPEN':
                    sl = pending_trade.get('sl_price')
                    if sl:
                        if (pending_trade['side']=='long' and price<=sl*(1+PRICE_TOLERANCE_PCT)) or \
                           (pending_trade['side']=='short' and price>=sl*(1-PRICE_TOLERANCE_PCT)):
                            amt=current_position['contracts']
                            if amt>0 and close_market(amt, pending_trade['side']):
                                send_telegram(f"❌ [SL] ปิด @ {price:.2f}")
                                add_trade_record('SL', {'side':pending_trade['side'],'entry_price':pending_trade['entry_price'],'contracts':amt}, price)
                                cooldown_until = datetime.utcnow()+timedelta(hours=COOLDOWN_H1_AFTER_TRADE)
                                pending_trade=None; current_position=None
                                state.update({'step':1,'bias':None,'latest_h1':None,'fibo100':None,'fibo0':None,'fibo':None,'entry_zone':None,'poc_m1':None})
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
    send_telegram("🤖 เริ่มบอท Binance SMC+Fibo+POC (isolated, one-way, Mode B, TH alerts)")
    main_loop()

if __name__ == '__main__':
    start_bot()
