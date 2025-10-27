# -*- coding: utf-8 -*-
# FINAL: Binance Futures SMC Flow
# H1 (BOS/CHOCH) -> M5 pullback CHOCH -> Fibo(H1 from the swing that caused signal) + POC -> Zone(33–78.6)
# -> M5 CHOCH back + MACD -> OPEN
# SL1 = Fibo80 (or POC if POC is inside 0–78.6 zone)
# TP1 = Fibo0 (60% partial; show P/L; then Fibo2: 100=H1 Fibo0, 0=M5 swing-in-zone)
# TP2 = ext >= 1.33 (or 1.618) -> close all; show P/L
# SL2 = 0 -> close all; no P/L message
# One position at a time; after TP2/SL2 reset and wait new H1

import os, sys, time, json, math, logging, threading
from datetime import datetime
import ccxt
import requests

# ================== CONFIG ==================
API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET  = os.getenv('BINANCE_SECRET',    'YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING')

SYMBOL            = 'ETH/USDT:USDT'
TIMEFRAME_H1      = '1h'
TIMEFRAME_M5      = '5m'

LEVERAGE          = 10
POSITION_MARGIN_PERCENT = 0.50   # ใช้ 50% ของ Free USDT เป็น margin ต่อไม้

# EMA Filter (H1 close only)
USE_EMA_FILTER    = False
EMA_FILTER_PERIOD = 200

# MACD
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9

# Swings
H1_SW_LEFT  = 3
H1_SW_RIGHT = 3
M5_SW_LEFT  = 2
M5_SW_RIGHT = 2

# POC / VP
POC_BUCKETS       = 40
TP1_CLOSE_RATIO   = 0.60          # close 60% at TP1

# Fibo2 / TP2
TP2_MIN_EXT   = 1.33              # ext >= 1.33 ปิดทันที
FIBO2_EXT1618 = 1.618

# Loop
LOOP_SEC      = 4
SNAPSHOT_SEC  = 30

# Telegram
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# ================== LOG ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("smc_final")

# ================== TG (anti-spam) ==================
_sent = set()
def send_telegram(msg: str, tag: str|None=None):
    if tag:
        if tag in _sent: return
        _sent.add(tag)
    if (not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith('YOUR')) or (not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID.startswith('YOUR')):
        log.info("[TG]\n" + msg); return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.get(url, params={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode':'HTML'}, timeout=10)
    except Exception as e:
        log.error(f"TG error: {e}")

def clear_sent(prefix: str):
    for k in list(_sent):
        if k.startswith(prefix): _sent.remove(k)

# ================== EXCHANGE ==================
exchange = None
market   = None

def setup_exchange():
    global exchange, market
    if not API_KEY or not SECRET or 'YOUR_' in API_KEY or 'YOUR_' in SECRET:
        send_telegram("⛔ API key/secret not set."); sys.exit(1)
    exchange = ccxt.binance({
        'apiKey': API_KEY, 'secret': SECRET, 'enableRateLimit': True,
        'options': {'defaultType':'future', 'marginMode': 'isolated'},
        'timeout': 60000
    })
    exchange.load_markets()
    market = exchange.market(SYMBOL)
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
    except Exception as e:
        log.warning(f"set_leverage warn: {e}")

def price_now():
    try:
        return float(exchange.fetch_ticker(SYMBOL)['last'])
    except:
        return None

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

def calc_qty_by_margin(price):
    """ใช้ margin = 50% ของ free เป็นฐาน, notional = margin*leverage, qty = notional/price"""
    if not market or not price: return 0.0
    free = get_free_usdt()
    margin = max(0.0, free * POSITION_MARGIN_PERCENT)
    if margin <= 0: return 0.0
    notional = margin * LEVERAGE
    # เคารพ min cost/amount
    min_amt  = float(market['limits']['amount'].get('min', 0.001))
    min_cost = float(market['limits']['cost'].get('min', 5.0))
    notional = max(notional, min_cost, min_amt*price)
    qty = float(exchange.amount_to_precision(SYMBOL, notional/price))
    return max(qty, min_amt)

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

def open_market(side: str):
    p = price_now()
    if p is None: return None
    qty = calc_qty_by_margin(p)
    if qty<=0:
        send_telegram("⛔ Margin ไม่พอเปิดออเดอร์"); return None
    side_ccxt = 'buy' if side=='long' else 'sell'
    try:
        exchange.create_market_order(SYMBOL, side_ccxt, qty)
        time.sleep(1)
        pos = fetch_position()
        if pos and pos['side']==side:
            return pos
        send_telegram("⚠ เปิดสำเร็จแต่ยืนยันไม่เจอ position"); return pos
    except Exception as e:
        send_telegram(f"⛔ เปิดออเดอร์ล้มเหลว: {e}")
        return None

def reduce_only_all():
    pos = fetch_position()
    if not pos: return True
    side = 'buy' if pos['side']=='short' else 'sell'
    qty  = pos['contracts']
    try:
        exchange.create_market_order(SYMBOL, side, qty, None, {'reduceOnly':True})
        time.sleep(1)
        return True
    except Exception as e:
        send_telegram(f"❌ reduceOnly error: {e}")
        return False

# ================== INDICATORS / STRUCTURE ==================
def ema(values, n):
    if len(values)<n: return None
    k=2/(n+1)
    e=sum(values[:n])/n
    for v in values[n:]:
        e=v*k + e*(1-k)
    return e

def ema_series(values, n):
    if len(values)<n: return []
    k=2/(n+1)
    e=sum(values[:n])/n
    out=[None]*(n-1)+[e]
    for v in values[n:]:
        e=v*k + e*(1-k)
        out.append(e)
    return out

def macd_from_closes(closes):
    if len(closes) < MACD_SLOW + MACD_SIGNAL + 2: return None
    ef = ema_series(closes, MACD_FAST)
    es = ema_series(closes, MACD_SLOW)
    dif=[]
    for i in range(len(closes)):
        if i<len(ef) and i<len(es) and ef[i] is not None and es[i] is not None:
            dif.append(ef[i]-es[i])
    dea = ema_series(dif, MACD_SIGNAL)
    if len(dif)<2 or len(dea)<2: return None
    return dif[-2], dif[-1], dea[-2], dea[-1]

def macd_up(dif_p,dif_n,dea_p,dea_n):   return dif_p<=dea_p and dif_n>dea_n
def macd_down(dif_p,dif_n,dea_p,dea_n): return dif_p>=dea_p and dif_n<dea_n

def find_swings_from_ohlcv(ohlcv, left=2, right=2):
    out=[]; highs=[c[2] for c in ohlcv]; lows=[c[3] for c in ohlcv]
    n=len(ohlcv)
    for i in range(left, n-right):
        if highs[i]==max(highs[i-left:i+right+1]): out.append(('high',i,ohlcv[i][0],highs[i]))
        if lows[i]==min(lows[i-left:i+right+1]):   out.append(('low', i,ohlcv[i][0],lows[i]))
    return out

def detect_bos_choch_from_swings(ohlcv, swings):
    out=[]; last_trend=None
    for k in range(1, len(swings)):
        ptype, ip, _, pp = swings[k-1]
        stype, i, ts, p = swings[k]
        close = ohlcv[i][4]
        sig=None; trend=last_trend
        if stype=='high' and close>pp:
            sig='BOS'; trend='up'
        elif stype=='low' and close<pp:
            sig='BOS'; trend='down'
        else:
            if last_trend=='up' and close<pp:
                sig='CHOCH'; trend='down'
            elif last_trend=='down' and close>pp:
                sig='CHOCH'; trend='up'
        if sig:
            out.append({'signal':sig,'trend':trend,'price':p,'ts':ts,'i':i})
            last_trend=trend
    return out

def m5_choch_direction():
    o = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME_M5, limit=150)
    sw = find_swings_from_ohlcv(o, left=1, right=1)
    st = detect_bos_choch_from_swings(o, sw)
    if not st: return None
    return st[-1]['trend']  # 'up' or 'down'

# ================== FIBO / POC ==================
def pick_h1_swing_for_signal(ohlcv_h1, direction: str):
    n=len(ohlcv_h1)
    if n<20:
        lows=[c[3] for c in ohlcv_h1]; highs=[c[2] for c in ohlcv_h1]
        return (min(lows), max(highs), None, None)
    k=3
    if direction=='up':
        i_low  = min(range(n-k-10, n-k), key=lambda i: ohlcv_h1[i][3])
        i_high = max(range(i_low, n-k),   key=lambda i: ohlcv_h1[i][2])
        return (ohlcv_h1[i_low][3], ohlcv_h1[i_high][2], i_low, i_high)
    else:
        i_high = max(range(n-k-10, n-k), key=lambda i: ohlcv_h1[i][2])
        i_low  = min(range(i_high, n-k), key=lambda i: ohlcv_h1[i][3])
        return (ohlcv_h1[i_low][3], ohlcv_h1[i_high][2], i_low, i_high)

def set_fibo_h1_from_signal(direction: str):
    ohlcv_h1 = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME_H1, limit=300)
    lo, hi, _, _ = pick_h1_swing_for_signal(ohlcv_h1, direction)
    diff = hi - lo
    if direction=='up':
        fibo={'0':hi,'33':hi-0.33*diff,'61.8':hi-0.618*diff,'78.6':hi-0.786*diff,'80':hi-0.80*diff,'100':lo}
        left, right = fibo['100'], fibo['0']
    else:
        fibo={'0':lo,'33':lo+0.33*diff,'61.8':lo+0.618*diff,'78.6':lo+0.786*diff,'80':lo+0.80*diff,'100':hi}
        left, right = fibo['0'], fibo['100']
    state['fibo']=fibo
    # POC within this fibo range (for info & SL override if POC in 0–78.6)
    state['poc_h1'] = calc_poc_in_range(ohlcv_h1, min(left,right), max(left,right))
    state['entered_zone']=False
    state['waiting_reenter']=False

def calc_poc_in_range(ohlcv, low_bound, high_bound, buckets=POC_BUCKETS):
    if not ohlcv or high_bound<=low_bound: return None
    lo=float(low_bound); hi=float(high_bound)
    step=(hi-lo)/float(buckets)
    if step<=0: return None
    bins={}
    for c in ohlcv:
        if len(c)<5: continue
        px=min(hi, max(lo, c[4])); vol=(c[5] if len(c)>5 and c[5] is not None else 0.0)
        idx=int((px-lo)/step); idx=max(0, min(buckets-1, idx))
        center=lo+(idx+0.5)*step
        bins[center]=bins.get(center,0.0)+vol
    if not bins: return None
    return max(bins.items(), key=lambda kv: kv[1])[0]

def price_in_fibo_zone(price, fibo):
    lo=min(fibo['33'],fibo['78.6']); hi=max(fibo['33'],fibo['78.6'])
    return lo<=price<=hi

def build_fibo2(side: str, h1_fibo0: float, m5_swing_in_zone: float):
    if side=='short':
        top=h1_fibo0; bot=m5_swing_in_zone
        diff=top-bot
        return {
            '100': top, '0': bot,
            '78.6': top-0.786*diff,
            'ext133': bot-1.33*diff,
            'ext161.8': bot-1.618*diff
        }
    else:
        bot=h1_fibo0; top=m5_swing_in_zone
        diff=top-bot
        return {
            '100': bot, '0': top,
            '78.6': bot+0.786*diff,
            'ext133': top+1.33*diff,
            'ext161.8': top+1.618*diff
        }

# ================== EMA FILTER (H1 close only) ==================
def h1_close_and_ema():
    o = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME_H1, limit=EMA_FILTER_PERIOD+5)
    if not o or len(o)<EMA_FILTER_PERIOD+2: return None, None
    closes = [c[4] for c in o[:-1]]   # use last closed
    ema_v = ema(closes, EMA_FILTER_PERIOD)
    return closes[-1], ema_v

def ema_filter_allows(side: str) -> bool:
    if not USE_EMA_FILTER: return True
    c, e = h1_close_and_ema()
    if c is None or e is None: return True
    if side=='long':
        return c > e
    else:
        return c < e

# ================== STATE ==================
state = {
    'phase': 'WAIT_H1',           # WAIT_H1 -> WAIT_M5 -> FIBO_SET -> IN_ZONE -> IN_POSITION
    'h1_dir': None,               # 'up'/'down'
    'fibo': None,
    'poc_h1': None,
    'entered_zone': False,
    'waiting_reenter': False,
    'm5_pre_entry_swing': None,   # ใช้ทำ Fibo2(0)
    'tp1_level': None, 'tp1_via': None,
    'fibo2': None, 'tp1_done': False, 'sl2': None
}
last_snapshot = 0.0

# ================== H1 helper ==================
def h1_last_signal():
    o = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME_H1, limit=400)
    sw = find_swings_from_ohlcv(o, H1_SW_LEFT, H1_SW_RIGHT)
    st = detect_bos_choch_from_swings(o, sw)
    return (o, st[-1]) if st else (o, None)

def h1_dir_now():
    _, sig = h1_last_signal()
    if not sig: return None
    return sig['trend']  # 'up'/'down'

# ================== P/L helper ==================
def pnl_usdt(side: str, entry: float, exit_price: float, qty: float) -> float:
    pts = (exit_price - entry) if side=='long' else (entry - exit_price)
    return float(pts * qty)

# ================== MAIN LOOP ==================
def main():
    global last_snapshot
    setup_exchange()
    send_telegram("🤖 เริ่มบอท: SMC H1→M5 + Fibo + POC + Fibo2\nLeverage 15x | Margin/ไม้ = 50% ของพอร์ต")

    while True:
        try:
            last = price_now()
            if last is None:
                time.sleep(LOOP_SEC); continue

            # One position at a time
            pos_live = fetch_position()

            # 1) WAIT_H1: หา H1 signal
            if state['phase']=='WAIT_H1':
                o_h1, sig = h1_last_signal()
                if sig:
                    state['h1_dir'] = sig['trend']   # 'up' or 'down'
                    send_telegram(f"🧭 H1 {sig['signal']} → Trend <b>{sig['trend'].upper()}</b>", tag=f"h1:{sig['ts']}")
                    state['phase']='WAIT_M5'
                    state['fibo']=None; state['poc_h1']=None
                    state['entered_zone']=False; state['waiting_reenter']=False
                    clear_sent("enter_zone"); clear_sent("entry"); clear_sent("tp1"); clear_sent("tp2")

            # 2) WAIT_M5: ต้องการ M5 CHOCH "สวน" H1 เพื่อเริ่มตี Fibo
            if state['phase']=='WAIT_M5' and state['h1_dir']:
                dir_m5 = m5_choch_direction()
                if dir_m5:
                    if (state['h1_dir']=='down' and dir_m5=='up') or (state['h1_dir']=='up' and dir_m5=='down'):
                        set_fibo_h1_from_signal(state['h1_dir'])
                        state['phase']='FIBO_SET'
                    else:
                        if state['fibo'] and not state['entered_zone']:
                            state['waiting_reenter']=True

            # 3) FIBO_SET/WAIT_M5: จัดการ waiting_reenter + เข้าโซน
            if state['phase'] in ('FIBO_SET','WAIT_M5'):
                if state['waiting_reenter'] and state['fibo'] and not state['entered_zone']:
                    # ถ้า H1 มี BOS ใหม่ทิศเดิม -> ขยับ Fibo 0 (คง 100 เดิม) -- (ฉบับย่อ: ตั้งใหม่จากสวิงล่าสุดทิศเดิม)
                    cur = h1_dir_now()
                    if cur and cur==state['h1_dir']:
                        set_fibo_h1_from_signal(state['h1_dir'])
                        send_telegram("🧭 H1 BOS ใหม่ (ทิศเดิม) → รีเฟรช Fibo สวิงล่าสุด", tag="refit:keep100")
                        state['waiting_reenter']=False

                # เข้าโซน?
                if state['fibo'] and price_in_fibo_zone(last, state['fibo']):
                    state['entered_zone']=True
                    state['phase']='IN_ZONE'
                    send_telegram("📍 ราคาเข้าโซน Fibo(H1) → รอ M5 CHOCH กลับทิศ + MACD", tag="enter_zone")

            # 4) IN_ZONE: รอ M5 CHOCH "กลับเข้าทิศ H1" + MACD -> OPEN
            if state['phase']=='IN_ZONE' and state['entered_zone'] and (not pos_live):
                want = 'short' if state['h1_dir']=='down' else 'long'
                # EMA filter (H1 close only)
                if not ema_filter_allows(want):
                    # ไม่อนุญาตตาม EMA filter
                    time.sleep(LOOP_SEC)
                else:
                    dir_back = m5_choch_direction()
                    # MACD เฉพาะตอนเข้า
                    o_m5 = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME_M5, limit=200)
                    closes=[c[4] for c in o_m5]
                    m = macd_from_closes(closes)
                    macd_ok = False
                    if m:
                        dif_p,dif_n,dea_p,dea_n = m
                        macd_ok = macd_down(dif_p,dif_n,dea_p,dea_n) if want=='short' else macd_up(dif_p,dif_n,dea_p,dea_n)
                    ok_dir = (dir_back=='down' and want=='short') or (dir_back=='up' and want=='long')
                    if ok_dir and macd_ok:
                        # เก็บ M5 swing ในโซนไว้ทำ Fibo2(0)
                        swing_in_zone = find_recent_m5_swing_in_zone(state['fibo'], want)
                        state['m5_pre_entry_swing'] = swing_in_zone

                        # กำหนด SL1: Fibo80 เป็นค่า default; ถ้า POC อยู่ในโซน 0–78.6 → ใช้ POC เป็น SL1
                        fibo = state['fibo']; poc = state.get('poc_h1')
                        sl1 = fibo['80']
                        # POC valid zone check
                        z_lo=min(fibo['0'], fibo['78.6']); z_hi=max(fibo['0'], fibo['78.6'])
                        if poc is not None and z_lo <= poc <= z_hi:
                            sl1 = poc  # ใช้ POC เป็น SL1 เมื่ออยู่ในโซน 0–78.6

                        # TP1 = Fibo0 (ตามที่ยืนยันล่าสุด)
                        tp1 = fibo['0']; tp1_via='FIBO0'
                        state['tp1_level']=tp1; state['tp1_via']=tp1_via

                        pos = open_market(want)
                        if pos:
                            # รวมแจ้งเตือนครั้งเดียว
                            msg = (f"✅ เปิดโพซิชัน <b>{want.upper()}</b>\n"
                                   f"Entry: <code>{pos['entry']:.2f}</code> | Size: <code>{pos['contracts']:.6f}</code>\n"
                                   f"🛡 SL1: <code>{sl1:.2f}</code> ({'POC' if sl1==poc else 'Fibo 80'})\n"
                                   f"🎯 TP1: <code>{tp1:.2f}</code> ({tp1_via}) | POC: <code>{(poc and round(poc,2)) if poc else '—'}</code>")
                            send_telegram(msg, tag="entry:open")
                            state['phase']='IN_POSITION'
                            state['tp1_done']=False; state['fibo2']=None; state['sl2']=None
                            # (ตั้ง stop-order SL1? ที่นี่ให้เฝ้าด้วยบอทเพื่อตามเงื่อนไข dynamic)
                        else:
                            send_telegram("⛔ เปิดไม่สำเร็จ", tag="entry:fail")

            # 5) IN_POSITION: จัดการ TP1 -> Fibo2 -> TP2 / SL1 / SL2
            pos_live = fetch_position()  # refresh
            if state['phase']=='IN_POSITION' and pos_live:
                last = price_now() or pos_live['entry']
                side = pos_live['side']; qty_all = pos_live['contracts']; entry = pos_live['entry']
                fibo = state['fibo']; poc = state.get('poc_h1')
                tp1  = state.get('tp1_level')
                sl1  = fibo['80'] if fibo else None
                if poc is not None and fibo:
                    z_lo=min(fibo['0'], fibo['78.6']); z_hi=max(fibo['0'], fibo['78.6'])
                    if z_lo <= poc <= z_hi:
                        sl1 = poc  # POC override

                # SL1 hit?
                if sl1:
                    sl1_hit = (last >= sl1) if side=='short' else (last <= sl1)
                    if sl1_hit and not state.get('tp1_done'):
                        # ปิดทั้งหมด (SL1)
                        try:
                            side_close='buy' if side=='short' else 'sell'
                            exchange.create_market_order(SYMBOL, side_close, qty_all, None, {'reduceOnly':True})
                        except Exception as e:
                            log.warning(f"SL1 close warn: {e}")
                        # P/L
                        loss = pnl_usdt(side, entry, last, qty_all)
                        send_telegram(f"🛑 SL1 HIT @ <code>{last:.2f}</code>\nขาดทุน: <b>{loss:+.2f} USDT</b>", tag="sl1:done")
                        # reset to WAIT_H1
                        state.update({'phase':'WAIT_H1','h1_dir':None,'fibo':None,'poc_h1':None,'entered_zone':False,
                                      'waiting_reenter':False,'m5_pre_entry_swing':None,'tp1_level':None,'tp1_via':None,
                                      'fibo2':None,'tp1_done':False,'sl2':None})
                        time.sleep(LOOP_SEC); continue

                # TP1 hit?
                if tp1 and not state.get('tp1_done'):
                    hit = (last<=tp1) if side=='short' else (last>=tp1)
                    if hit:
                        qty_close = float(exchange.amount_to_precision(SYMBOL, qty_all*TP1_CLOSE_RATIO))
                        try:
                            side_close='buy' if side=='short' else 'sell'
                            exchange.create_market_order(SYMBOL, side_close, qty_close, None, {'reduceOnly':True})
                        except Exception as e:
                            log.warning(f"TP1 partial close warn: {e}")
                        gain = pnl_usdt(side, entry, last, qty_close)
                        send_telegram(f"✅ TP1 HIT @ <code>{last:.2f}</code>\nกำไร: <b>{gain:+.2f} USDT</b>", tag="tp1:done")
                        state['tp1_done']=True
                        # สร้าง Fibo2
                        h1_f0 = fibo['0']
                        # 0 = swing M5 ในโซนก่อนเข้า
                        m5_zero = state['m5_pre_entry_swing'] if state['m5_pre_entry_swing'] is not None else last
                        state['fibo2'] = build_fibo2(side, h1_f0, m5_zero)
                        state['sl2']   = state['fibo2']['0']
                        send_telegram(f"🔁 เลื่อน SL2 → 0 (Fibo2)\n0=<code>{state['sl2']:.2f}</code> | ext1.33=<code>{state['fibo2']['ext133']:.2f}</code> | 1.618=<code>{state['fibo2']['ext161.8']:.2f}</code>")

                # Fibo2 phase → TP2 / SL2
                if state.get('tp1_done') and state.get('fibo2'):
                    f2 = state['fibo2']; sl2 = f2['0']
                    # TP2
                    if side=='long':
                        if last >= f2['ext133'] or last >= f2['ext161.8']:
                            # close rest
                            try:
                                side_close='sell'
                                exchange.create_market_order(SYMBOL, side_close, fetch_position()['contracts'], None, {'reduceOnly':True})
                            except Exception as e:
                                log.warning(f"TP2 close warn: {e}")
                            # P/L on remainder
                            pos_after = fetch_position()
                            qty_closed = qty_all - (pos_after['contracts'] if pos_after else 0.0)
                            gain = pnl_usdt(side, entry, last, qty_closed)
                            send_telegram(f"🏁 TP2 HIT (ext≥1.33) @ <code>{last:.2f}</code>\nกำไร: <b>{gain:+.2f} USDT</b>", tag="tp2:done")
                            state.update({'phase':'WAIT_H1','h1_dir':None,'fibo':None,'poc_h1':None,'entered_zone':False,
                                          'waiting_reenter':False,'m5_pre_entry_swing':None,'tp1_level':None,'tp1_via':None,
                                          'fibo2':None,'tp1_done':False,'sl2':None})
                            continue
                        # SL2
                        if last <= sl2:
                            # close rest quietly (no P/L shown)
                            try:
                                side_close='sell'
                                exchange.create_market_order(SYMBOL, side_close, fetch_position()['contracts'], None, {'reduceOnly':True})
                            except Exception as e:
                                log.warning(f"SL2 close warn: {e}")
                            send_telegram("🛑 SL2 (Fibo2 0) ปิดทั้งหมด", tag="sl2:done")
                            state.update({'phase':'WAIT_H1','h1_dir':None,'fibo':None,'poc_h1':None,'entered_zone':False,
                                          'waiting_reenter':False,'m5_pre_entry_swing':None,'tp1_level':None,'tp1_via':None,
                                          'fibo2':None,'tp1_done':False,'sl2':None})
                            continue
                    else:
                        if last <= f2['ext133'] or last <= f2['ext161.8']:
                            try:
                                side_close='buy'
                                exchange.create_market_order(SYMBOL, side_close, fetch_position()['contracts'], None, {'reduceOnly':True})
                            except Exception as e:
                                log.warning(f"TP2 close warn: {e}")
                            pos_after = fetch_position()
                            qty_closed = qty_all - (pos_after['contracts'] if pos_after else 0.0)
                            gain = pnl_usdt(side, entry, last, qty_closed)
                            send_telegram(f"🏁 TP2 HIT (ext≥1.33) @ <code>{last:.2f}</code>\nกำไร: <b>{gain:+.2f} USDT</b>", tag="tp2:done")
                            state.update({'phase':'WAIT_H1','h1_dir':None,'fibo':None,'poc_h1':None,'entered_zone':False,
                                          'waiting_reenter':False,'m5_pre_entry_swing':None,'tp1_level':None,'tp1_via':None,
                                          'fibo2':None,'tp1_done':False,'sl2':None})
                            continue
                        if last >= sl2:
                            try:
                                side_close='buy'
                                exchange.create_market_order(SYMBOL, side_close, fetch_position()['contracts'], None, {'reduceOnly':True})
                            except Exception as e:
                                log.warning(f"SL2 close warn: {e}")
                            send_telegram("🛑 SL2 (Fibo2 0) ปิดทั้งหมด", tag="sl2:done")
                            state.update({'phase':'WAIT_H1','h1_dir':None,'fibo':None,'poc_h1':None,'entered_zone':False,
                                          'waiting_reenter':False,'m5_pre_entry_swing':None,'tp1_level':None,'tp1_via':None,
                                          'fibo2':None,'tp1_done':False,'sl2':None})
                            continue

            # 6) Snapshot
            now = time.time()
            if now - last_snapshot >= SNAPSHOT_SEC:
                last_snapshot = now
                log.info(json.dumps({
                    'phase': state['phase'],
                    'h1_dir': state['h1_dir'],
                    'price': last,
                    'zoneEntered': state['entered_zone'],
                    'tp1': state['tp1_level'],
                    'fibo2': state['fibo2'] and {k: round(v,2) for k,v in state['fibo2'].items()}
                }, default=str))

            time.sleep(LOOP_SEC)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception(f"loop error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
