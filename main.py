# -*- coding: utf-8 -*-
# Final SMC (H1) → M5 Confirm → Fibo (H1) → Entry (Binance Futures)
# Flow:
#   1) H1 BOS/CHOCH = เลือกฝั่งเทรนด์
#   2) รอ M5 CHOCH ไปทิศเดียวกัน
#   3) ค่อย "ตี Fibo จาก Swing H1" แล้วรอให้ LOW ของ H1 แตะโซน 33–78.6
#   4) Entry ต้องครบ: M5 CHOCH + MACD cross (จะเกิดอะไรก่อนก็ได้ แต่ต้องครบทั้งคู่ก่อน)
#   5) SL จาก swing M5 + buffer, TP1→Fibo2→TP2, Emergency ใช้เฉพาะ TP2 phase
#
# Exchange: Binance Futures | Isolated | One-way
# Symbol   : ETH/USDT:USDT (เปิดง่าย ทุนต่ำไม่ติด min notional)
#
# © You

import os, sys, time, json, math, threading, logging, calendar
from datetime import datetime
from collections import deque

import ccxt
import numpy as np
import requests

# ================== CONFIG ==================
API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET  = os.getenv('BINANCE_SECRET',    'YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING')

SYMBOL            = 'ETH/USDT:USDT'
TIMEFRAME_H1      = '1h'
TIMEFRAME_M5      = '5m'
LEVERAGE          = 25
TARGET_POSITION_SIZE_FACTOR = 0.9    # ใช้ % ของ Free USDT
MARGIN_BUFFER_USDT = 5               # กันเงินไม่ใช้ทั้งหมด

# ---- MACD ----
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9

# ---- Swings / Fibo ----
H1_SW_LEFT  = 3
H1_SW_RIGHT = 3
M5_SW_LEFT  = 2
M5_SW_RIGHT = 2
SL_M5_BUFFER = 0.25  # % buffer เพิ่มจาก swing

# ---- TP / Trailing (อย่างย่อเวอร์ชันนี้) ----
TP1_CLOSE_PCT = 0.60
FIBO2_SL_LEVEL = 0.786
FIBO2_EXT_MIN  = 1.33
FIBO2_EXT_MAX  = 1.618

# ---- Loop / Snapshot ----
LOOP_SEC = 5
SNAPSHOT_SEC = 30

# ---- Telegram ----
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# ================== LOG ==================
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("smc_m5")

# ================== TELEGRAM ==================
_sent = set()
def send_telegram(msg: str, tag: str | None = None):
    """ส่งข้อความครั้งเดียวต่อ tag (anti-spam)"""
    if tag:
        if tag in _sent: 
            return
        _sent.add(tag)
    if (not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith('YOUR')) or (not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID.startswith('YOUR')):
        log.info("[TG] " + msg)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.get(url, params={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        log.error(f"TG error: {e}")

def clear_sent(prefix: str):
    for k in list(_sent):
        if k.startswith(prefix):
            _sent.remove(k)

# ================== EXCHANGE ==================
exchange = None
market = None

def setup_exchange():
    global exchange, market
    if not API_KEY or not SECRET or 'YOUR_' in API_KEY or 'YOUR_' in SECRET:
        send_telegram("⛔ API key/secret ไม่ถูกตั้งค่า"); sys.exit(1)

    exchange = ccxt.binance({
        'apiKey': API_KEY, 'secret': SECRET, 'enableRateLimit': True,
        'options': {'defaultType': 'future', 'marginMode': 'isolated'},
        'timeout': 60000
    })
    exchange.load_markets()
    market = exchange.market(SYMBOL)
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
    except Exception as e:
        log.warning(f"set_leverage warn: {e}")

def fetch_ohlcv(tf: str, limit: int):
    for _ in range(3):
        try:
            return exchange.fetch_ohlcv(SYMBOL, timeframe=tf, limit=limit)
        except Exception as e:
            log.warning(f"fetch_ohlcv {tf} retry: {e}")
            time.sleep(1.0)
    return []

def price_now():
    try:
        return float(exchange.fetch_ticker(SYMBOL)['last'])
    except:
        return None

# ================== INDICATORS ==================
def ema(values, period):
    if len(values) < period: return None
    k = 2/(period+1)
    e = sum(values[:period])/period
    for v in values[period:]:
        e = v*k + e*(1-k)
    return e

def macd_last(closes):
    if len(closes) < MACD_SLOW + MACD_SIGNAL + 3: return None
    # full series for cross accuracy
    def ema_series(vals, n):
        if len(vals) < n: return []
        k=2/(n+1)
        e=sum(vals[:n])/n
        out=[None]*(n-1)+[e]
        for v in vals[n:]:
            e=v*k + e*(1-k)
            out.append(e)
        return out
    ef = ema_series(closes, MACD_FAST)
    es = ema_series(closes, MACD_SLOW)
    dif=[]
    for i in range(len(closes)):
        if i<len(ef) and i<len(es) and ef[i] is not None and es[i] is not None:
            dif.append(ef[i]-es[i])
    dea = ema_series(dif, MACD_SIGNAL)
    if len(dif)<2 or len(dea)<2: return None
    return (dif[-2], dif[-1], dea[-2], dea[-1])

def macd_up(dif_p,dif_n,dea_p,dea_n):   return dif_p<=dea_p and dif_n>dea_n
def macd_down(dif_p,dif_n,dea_p,dea_n): return dif_p>=dea_p and dif_n<dea_n

# ================== STRUCTURE ==================
def find_swings(ohlcv, L=3, R=3):
    """return list of ('high'/'low', index, ts, price)"""
    res=[]
    highs=[c[2] for c in ohlcv]
    lows =[c[3] for c in ohlcv]
    n=len(ohlcv)
    for i in range(L, n-R):
        if highs[i]==max(highs[i-L:i+R+1]):
            res.append(('high', i, ohlcv[i][0], highs[i]))
        if lows[i]==min(lows[i-L:i+R+1]):
            res.append(('low', i, ohlcv[i][0], lows[i]))
    return res

def detect_bos_choch(ohlcv, swings):
    """simple structure from swing sequence (close-based)"""
    out=[]; last_trend=None
    for k in range(1, len(swings)):
        prev_type, i_prev, _, p_prev = swings[k-1]
        stype, i, ts, p = swings[k]
        close = ohlcv[i][4]
        sig=None; trend=last_trend
        if stype=='high' and close>p_prev:
            sig='BOS'; trend='up'
        elif stype=='low' and close<p_prev:
            sig='BOS'; trend='down'
        else:
            if last_trend=='up' and close<p_prev:
                sig='CHOCH'; trend='down'
            elif last_trend=='down' and close>p_prev:
                sig='CHOCH'; trend='up'
        if sig:
            out.append({'signal':sig,'trend':trend,'price':p,'ts':ts,'i':i})
            last_trend=trend
    return out

# ================== FIBO ==================
def fibo_from_swing(low, high):
    diff = high-low
    return {
        '0': high,
        '100': low,
        '33': high - 0.33*diff,
        '38.2': high - 0.382*diff,
        '50': high - 0.5*diff,
        '61.8': high - 0.618*diff,
        '78.6': high - 0.786*diff
    }

def fibo2_from_base(tp1_price, new_high):
    diff = new_high - tp1_price
    return {
        '100': tp1_price,
        '78.6': tp1_price + FIBO2_SL_LEVEL*diff,
        'ext133': tp1_price + FIBO2_EXT_MIN*diff,
        'ext161.8': tp1_price + FIBO2_EXT_MAX*diff
    }

# ================== ORDER SIZE ==================
def get_free_usdt():
    try:
        bal=exchange.fetch_balance({'type':'future'})
        v=(bal.get('USDT') or {}).get('free')
        if v is None:
            for a in (bal.get('info',{}) or {}).get('assets',[]):
                if a.get('asset')=='USDT':
                    v=a.get('availableBalance'); break
        return float(v) if v is not None else 0.0
    except: return 0.0

def calc_qty(price):
    """ใช้ logic แบบ template ของคุณให้เปิดได้จริง (min notional)"""
    if not market: return 0.0
    free = max(0.0, get_free_usdt()-MARGIN_BUFFER_USDT)
    if free<=0: return 0.0
    target_notional = free * TARGET_POSITION_SIZE_FACTOR * LEVERAGE
    # Binance min rules
    min_amt  = float(market['limits']['amount'].get('min', 0.001))
    max_amt  = float(market['limits']['amount'].get('max', 1e9))
    min_cost = float(market['limits']['cost'].get('min', 5.0))  # ~ 5 USDT
    notional = max(target_notional, min_cost, min_amt*price)
    qty = notional/price
    qty = float(exchange.amount_to_precision(SYMBOL, qty))
    qty = max(qty, min_amt); qty = min(qty, max_amt)
    return qty

def open_market(side: str):
    p = price_now()
    if p is None: return None
    qty = calc_qty(p)
    if qty<=0:
        send_telegram("⛔ Margin ไม่พอเปิดออเดอร์"); return None
    side_ccxt = 'buy' if side=='long' else 'sell'
    try:
        exchange.create_market_order(SYMBOL, side_ccxt, qty)
        time.sleep(1)
        pos = fetch_position()
        if pos and pos['side']==side:
            send_telegram(f"✅ เปิดโพซิชัน <b>{side.upper()}</b>\nSize: {pos['contracts']}\nEntry: {pos['entry']}")
            return pos
        send_telegram("⚠ เปิดสำเร็จแต่ยืนยันไม่เจอ position"); return pos
    except Exception as e:
        send_telegram(f"⛔ เปิดออเดอร์ล้มเหลว: {e}")
        return None

def fetch_position():
    try:
        ps = exchange.fetch_positions([SYMBOL])
        for p in ps:
            if p.get('symbol')==SYMBOL and float(p.get('contracts') or 0)!=0:
                return {'side':p.get('side'),
                        'contracts': float(abs(p.get('contracts'))),
                        'entry': float(p.get('entryPrice') or 0.0)}
        return None
    except Exception as e:
        log.error(f"fetch_position err: {e}")
        return None

def reduce_all():
    pos = fetch_position()
    if not pos: return True
    side = 'sell' if pos['side']=='long' else 'buy'
    qty  = pos['contracts']
    try:
        exchange.create_market_order(SYMBOL, side, qty, None, {'reduceOnly':True})
        time.sleep(1)
    except Exception as e:
        send_telegram(f"❌ reduceOnly error: {e}")
        return False
    return True

# ================== STATE ==================
state = {
    'phase': 'IDLE',       # IDLE -> WAIT_M5 -> FIBO_SET -> IN_ZONE -> WAIT_ENTRY -> IN_POSITION
    'h1_dir': None,        # 'up'/'down'
    'h1_last_ts': None,
    'm5_choch_ok': False,
    'macd_ok': False,
    'fibo': None,          # dict levels
    'fibo_locked': False,
    'fibo_pair': None,     # {'low','high'}
    'entered_zone': False,
    'tp1_price': None,
    'fibo2': None,
    'tp1_done': False
}

last_snapshot = 0.0

# ================== HELPERS ==================
def structure_H1():
    o = fetch_ohlcv(TIMEFRAME_H1, 400)
    if len(o) < 50: return None
    swings = find_swings(o, H1_SW_LEFT, H1_SW_RIGHT)
    st = detect_bos_choch(o, swings)
    return {'ohlcv':o, 'swings':swings, 'struct':st}

def structure_M5():
    o = fetch_ohlcv(TIMEFRAME_M5, 600)
    if len(o) < 50: return None
    swings = find_swings(o, M5_SW_LEFT, M5_SW_RIGHT)
    st = detect_bos_choch(o, swings)
    closes = [c[4] for c in o]
    m = macd_last(closes)
    return {'ohlcv':o, 'swings':swings, 'struct':st, 'macd':m}

def last_trend_H1(st):
    if not st or not st['struct']: return None
    return st['struct'][-1]['trend']

def last_signal_H1(st):
    if not st or not st['struct']: return None
    return st['struct'][-1]

def swing_bounds_H1(h1):
    """หาชุด (low, high) ล่าสุดในช่วงหลัง โดยยึดทิศทาง state['h1_dir']"""
    highs=[c[2] for c in h1['ohlcv'][-150:]]
    lows =[c[3] for c in h1['ohlcv'][-150:]]
    return (min(lows), max(highs))

def update_fibo_after_M5_confirm(h1):
    low, high = swing_bounds_H1(h1)
    if state['h1_dir']=='up' and low is not None and high is not None:
        f = fibo_from_swing(low, high)
    elif state['h1_dir']=='down':
        # สำหรับ down เราจะกลับแกน (ตีเหมือนกัน แต่ใช้เขต 33–78.6 เช่นเดิม)
        f = fibo_from_swing(high, low)  # high > low → 100 ต่ำ
    else:
        f=None
    state['fibo']=f
    state['fibo_pair']={'low':low, 'high':high}
    state['fibo_locked']=False
    state['entered_zone']=False
    send_telegram(f"📐 ตั้ง Fibo (H1) แล้ว – dir={state['h1_dir']}\n0={f['0']:.2f}  100={f['100']:.2f}", tag="fibo:set")

def h1_low_touched_zone(h1):
    """ตามข้อ A: นับเข้าโซนเมื่อ 'LOW ของแท่ง H1 ล่าสุด' แตะ 33–78.6 เท่านั้น"""
    if not state['fibo']: return False
    last_h1 = h1['ohlcv'][-1]   # แท่งกำลังก่อตัว
    low = last_h1[3]
    zlo = min(state['fibo']['78.6'], state['fibo']['33'])
    zhi = max(state['fibo']['78.6'], state['fibo']['33'])
    return (low <= zhi) and (low >= zlo)

def maybe_shift_fibo_zero_if_no_touch(h1):
    """ถ้ายังไม่เคยแตะโซน และ H1 ทำ BOS ใหม่ → ขยับ Fibo 0 ตาม (100 คงเดิม)"""
    if state['entered_zone'] or state['fibo_locked']: 
        return
    # หาขอบสวิงใหม่
    new_low, new_high = swing_bounds_H1(h1)
    old = state['fibo_pair']
    if not old: return
    if state['h1_dir']=='up' and new_high>old['high']:
        # ขยับ 0 ขึ้น (100 เดิม)
        f = fibo_from_swing(old['low'], new_high)
        state['fibo']=f; state['fibo_pair']={'low':old['low'], 'high':new_high}
        send_telegram("🧭 [FIBO] BOS ต่อ แต่ยังไม่แตะโซน → ขยับ Fibo 0 ตาม high ใหม่", tag="fibo:shift")
    elif state['h1_dir']=='down' and new_low<old['low']:
        f = fibo_from_swing(new_low, old['high'])
        state['fibo']=f; state['fibo_pair']={'low':new_low, 'high':old['high']}
        send_telegram("🧭 [FIBO] BOS ต่อ แต่ยังไม่แตะโซน → ขยับ Fibo 0 ตาม low ใหม่", tag="fibo:shift")

def open_if_confirm_all(m5_env):
    """ต้องครบ: M5 CHOCH + MACD cross ไปทิศเดียวกับ H1"""
    if not state['m5_choch_ok'] or not state['macd_ok']: 
        return None
    side = 'long' if state['h1_dir']=='up' else 'short'
    pos = open_market(side)
    if pos:
        state['phase']='IN_POSITION'
        # ตั้งค่า TP1 (ตรงไปที่ Fibo 0/100 ตามทิศ)
        state['tp1_price'] = state['fibo']['0'] if side=='long' else state['fibo']['100']
        send_telegram(f"🎯 TP1 @ {state['tp1_price']:.2f}")
    return pos

# ================== MAIN LOOP ==================
def main():
    setup_exchange()
    send_telegram("🤖 บอทเริ่มทำงาน: Binance SMC + M5 Confirm + Fibo (H1)")

    global last_snapshot
    last_snapshot = 0.0

    while True:
        try:
            # 1) อ่าน H1 structure
            h1 = structure_H1()
            if not h1:
                time.sleep(LOOP_SEC); continue
            sig = last_signal_H1(h1)
            trend = last_trend_H1(h1)

            # Phase: IDLE → รอสัญญาณ H1
            if state['phase']=='IDLE':
                if sig:
                    state['h1_dir'] = 'up' if sig['trend']=='up' else 'down'
                    state['h1_last_ts'] = sig['ts']
                    state['phase'] = 'WAIT_M5'
                    state['m5_choch_ok'] = False
                    state['macd_ok'] = False
                    clear_sent("step:")
                    send_telegram(f"🧭 [STEP1] H1 {sig['signal']} → Trend {sig['trend'].upper()} (รอ M5 CHOCH)", tag=f"step:1:{sig['ts']}")

            # 2) WAIT_M5: ต้องให้ M5 เกิด CHOCH ทิศเดียวกันก่อน
            if state['phase']=='WAIT_M5' and state['h1_dir']:
                m5 = structure_M5()
                if m5:
                    # CHOCH ม5
                    m5_sig = m5['struct'][-1] if m5['struct'] else None
                    if m5_sig and ((state['h1_dir']=='up' and m5_sig['trend']=='up') or (state['h1_dir']=='down' and m5_sig['trend']=='down')):
                        state['m5_choch_ok'] = True
                        send_telegram("✍️ [STEP2] M5 CHOCH ตรงทิศแล้ว", tag=f"step:2:m5choch:{m5_sig['ts']}")
                    # MACD
                    if m5['macd']:
                        dif_p,dif_n,dea_p,dea_n = m5['macd']
                        if state['h1_dir']=='up' and macd_up(dif_p,dif_n,dea_p,dea_n):
                            state['macd_ok']=True
                            send_telegram("📈 MACD (M5) ตัดขึ้น", tag=f"step:2:macd:up:{m5['ohlcv'][-2][0]}")
                        if state['h1_dir']=='down' and macd_down(dif_p,dif_n,dea_p,dea_n):
                            state['macd_ok']=True
                            send_telegram("📉 MACD (M5) ตัดลง", tag=f"step:2:macd:dn:{m5['ohlcv'][-2][0]}")
                    # ถ้าครบ 2 อันแล้ว → ไปตั้ง Fibo H1
                    if state['m5_choch_ok'] and state['macd_ok']:
                        state['phase']='FIBO_SET'
                        update_fibo_after_M5_confirm(h1)
                        send_telegram("📐 [STEP3] ตั้ง Fibo H1 แล้ว → รอราคาแตะโซน 33–78.6", tag="step:3:fibo-set")
                        continue

            # 3) FIBO_SET → รอ H1 Low แตะโซน
            if state['phase']=='FIBO_SET' and state['fibo']:
                # ยังไม่แตะ → อาจขยับ 0 ตาม BOS ใหม่
                if not h1_low_touched_zone(h1):
                    maybe_shift_fibo_zero_if_no_touch(h1)
                else:
                    state['entered_zone']=True
                    state['fibo_locked']=True
                    state['phase']='IN_ZONE'
                    send_telegram("📍 [STEP4] H1 LOW แตะโซน Fibo แล้ว → ไปหา Entry", tag="step:4:enter-zone")

            # 4) IN_ZONE → รอให้เงื่อนไข Entry ครบ (กันซ้ำ)
            if state['phase']=='IN_ZONE':
                m5 = structure_M5()
                if m5:
                    # re-evaluate choch/macd (ต้องยังครบอยู่)
                    choch_ok=False; macd_ok=False
                    if m5['struct']:
                        s=m5['struct'][-1]
                        if (state['h1_dir']=='up' and s['trend']=='up') or (state['h1_dir']=='down' and s['trend']=='down'):
                            choch_ok=True
                    if m5['macd']:
                        dif_p,dif_n,dea_p,dea_n = m5['macd']
                        macd_ok = macd_up(dif_p,dif_n,dea_p,dea_n) if state['h1_dir']=='up' else macd_down(dif_p,dif_n,dea_p,dea_n)
                    state['m5_choch_ok']=choch_ok
                    state['macd_ok']=macd_ok
                    pos = open_if_confirm_all(m5)
                    if pos:
                        # ติดตาม TP/SL แบบย่อ
                        send_telegram("🛠 เข้าสู่โหมดติดตาม TP/SL แบบย่อ", tag="follow:start")
                        state['phase']='IN_POSITION'

            # 5) IN_POSITION → ติดตามอย่างย่อ (TP1→Fibo2→TP2)
            if state['phase']=='IN_POSITION':
                pos = fetch_position()
                if not pos:
                    send_telegram("ℹ️ Position ปิดแล้ว → รีเซตกลับไปเริ่มที่ H1", tag="pos:closed")
                    # reset
                    for k in ['phase','h1_dir','h1_last_ts','m5_choch_ok','macd_ok','fibo','fibo_locked','fibo_pair','entered_zone','tp1_price','fibo2','tp1_done']:
                        state[k] = None
                    state['phase']='IDLE'
                else:
                    # TP1
                    last = price_now() or pos['entry']
                    if not state['tp1_done']:
                        if (pos['side']=='long' and last>=state['tp1_price']) or (pos['side']=='short' and last<=state['tp1_price']):
                            # ปิด 60% (reduceOnly partial)
                            try:
                                close_qty = float(exchange.amount_to_precision(SYMBOL, pos['contracts']*TP1_CLOSE_PCT))
                                side = 'sell' if pos['side']=='long' else 'buy'
                                exchange.create_market_order(SYMBOL, side, close_qty, None, {'reduceOnly':True})
                            except Exception as e:
                                log.warning(f"partial close warn: {e}")
                            state['tp1_done']=True
                            # คำนวณ fibo2 (ใช้ M5 high/low หลัง TP1)
                            m5 = structure_M5()
                            highs=[c[2] for c in m5['ohlcv'][-200:]]
                            hi = max(highs) if highs else last*1.02
                            if pos['side']=='short':
                                lows=[c[3] for c in m5['ohlcv'][-200:]]
                                lo = min(lows) if lows else last*0.98
                                state['fibo2']={'78.6': last - (last-lo)* (1-FIBO2_SL_LEVEL),
                                                'ext133': last - (last-lo)*FIBO2_EXT_MIN,
                                                'ext161.8': last - (last-lo)*FIBO2_EXT_MAX}
                            else:
                                state['fibo2']=fibo2_from_base(state['tp1_price'], hi)
                            send_telegram(f"🔁 TP1 เสร็จ → ตั้ง Fibo2 และเลื่อน SL ไป 78.6 ({state['fibo2']['78.6']:.2f})", tag="tp1:done")
                    else:
                        # TP2/SL move (อย่างย่อ)
                        if pos['side']=='long':
                            if last<=state['fibo2']['78.6']:
                                reduce_all(); send_telegram("🛑 SL (Fibo2 78.6) ปิดที่เหลือ", tag="sl:step2"); state['phase']='IDLE'
                            elif last>=state['fibo2']['ext133']:
                                reduce_all(); send_telegram("🏁 TP2 โซน ext133–161.8 (ปิดที่ ext133)", tag="tp2:done"); state['phase']='IDLE'
                        else:
                            if last>=state['fibo2']['78.6']:
                                reduce_all(); send_telegram("🛑 SL (Fibo2 78.6) ปิดที่เหลือ", tag="sl:step2"); state['phase']='IDLE'
                            elif last<=state['fibo2']['ext133']:
                                reduce_all(); send_telegram("🏁 TP2 โซน ext133–161.8 (ปิดที่ ext133)", tag="tp2:done"); state['phase']='IDLE'

            # Snapshot
            now = time.time()
            if now - last_snapshot >= SNAPSHOT_SEC:
                last_snapshot = now
                pn = price_now()
                log.info(json.dumps({
                    'phase': state['phase'],
                    'dir': state['h1_dir'],
                    'price': pn,
                    'fibo': state['fibo'],
                    'entered_zone': state['entered_zone']
                }, default=str))

            time.sleep(LOOP_SEC)

        except Exception as e:
            log.exception(f"loop error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
