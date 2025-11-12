# ==============================================
# Binance Futures NW Bot — LuxAlgo Repaint Precision
# ==============================================
import os, time, json, math, logging, requests
from datetime import datetime
import ccxt
from dotenv import load_dotenv
load_dotenv(override=True)

# ========== ENV ==========
API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_BINANCE_API_KEY")
SECRET  = os.getenv("BINANCE_SECRET", "YOUR_BINANCE_SECRET")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ========== CONFIG ==========
SYMBOL   = "BTC/USDT:USDT"
TIMEFRAME = "15m"
LEVERAGE = 15
POSITION_MARGIN_FRACTION = 0.80
NW_H, NW_MULT = 8.0, 3.0
LOOP_SECONDS = 10

# ========== LOG ==========
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger("luxalgo_nw")

# ========== TG ==========
def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.get(url,params={'chat_id':TELEGRAM_CHAT_ID,'text':text,'parse_mode':'HTML'},timeout=10)
    except: pass

# ========== DAILY REPORT ==========
STATS_FILE = "daily_stats.json"
DAILY_HH, DAILY_MM = 23, 59
class DailyStats:
    def __init__(self):
        self.path=STATS_FILE
        self.data={'date':datetime.now().strftime("%Y-%m-%d"),'trades':[],'pnl':0.0}
        if os.path.exists(self.path):
            try:
                with open(self.path,'r') as f:self.data=json.load(f)
            except:pass
    def save(self): json.dump(self.data,open(self.path,"w"),indent=2)
    def roll(self):
        d=datetime.now().strftime("%Y-%m-%d")
        if self.data.get("date")!=d:
            self.send_report(force=True)
            self.data={'date':d,'trades':[],'pnl':0.0};self.save()
    def add(self,side,entry,close,pnl,reason):
        self.data['trades'].append({'t':datetime.now().strftime("%H:%M:%S"),'side':side,'e':entry,'c':close,'pnl':pnl,'r':reason})
        self.data['pnl']+=pnl;self.save()
    def send_report(self,force=False):
        now=datetime.now()
        if not force and not (now.hour==DAILY_HH and now.minute==DAILY_MM):return
        total=self.data['pnl'];tr=self.data['trades']
        if not tr and total==0:return
        msg=[f"📊 <b>สรุปผลรายวัน</b> — {self.data['date']}",
             f"Σ PnL: <b>{total:+.2f} USDT</b>",
             "────────────"]
        for t in tr[-20:]:
            msg.append(f"{t['t']} | {t['side']} | {t['e']:.2f}→{t['c']:.2f} | {t['pnl']:+.2f} ({t['r']})")
        tg_send("\n".join(msg))
stats=DailyStats()

# ========== LUXALGO NWE ==========
def nwe_luxalgo_repaint(closes, h=8.0, mult=3.0):
    n=len(closes)
    if n<100: return None, None, None
    win=min(499,n-1)
    coefs=[math.exp(-(i**2)/(2*(h**2))) for i in range(win)]
    den=sum(coefs)
    out_series=[]
    for idx in range(win,n):
        num=sum(closes[idx-j]*coefs[j] for j in range(win))
        out_series.append(num/den)
    if not out_series: return None,None,None
    mean=out_series[-1]
    win_s=int(h*10)
    if win_s>len(out_series): win_s=len(out_series)
    diffs=[abs(closes[-i]-out_series[-i]) for i in range(1,win_s+1)]
    mae=(sum(diffs)/len(diffs))*mult*1.50  # ✅ ปรับ calibration กว้างขึ้น
    upper=mean+mae;lower=mean-mae
    return upper,lower,mean
  
# ========== EXCHANGE ==========
def setup_exchange():
    ex=ccxt.binance({
        'apiKey':API_KEY,'secret':SECRET,
        'enableRateLimit':True,
        'options':{'defaultType':'future'}
    })
    ex.load_markets()
    ex.set_leverage(LEVERAGE,SYMBOL)
    return ex

def free_usdt(ex):
    try:
        bal=ex.fetch_balance({'type':'future'})
        v=(bal.get('USDT') or {}).get('free')
        return float(v) if v else 0.0
    except: return 0.0

# ========== MAIN ==========
def main():
    ex=setup_exchange()
    log.info("✅ Started Binance Futures NW Bot (LuxAlgo Repaint Mode, 80% Margin)")
    sl_lock=False;pos=None

    while True:
        try:
            stats.roll();stats.send_report()
            ohlcv=ex.fetch_ohlcv(SYMBOL,timeframe=TIMEFRAME,limit=505)
            closes=[c[4] for c in ohlcv]
            result=nwe_luxalgo_repaint(closes,NW_H,NW_MULT)
            if result is None or any(v is None for v in result):
                time.sleep(LOOP_SECONDS);continue
            upper,lower,mid=result
            close=closes[-1]
            ema50=sum(closes[-50:])/50;ema100=sum(closes[-100:])/100
            trend="BUY" if ema50>ema100 else "SELL" if ema50<ema100 else "NONE"

            log.info(f"[DEBUG] Close={close:.2f}, Upper={upper:.2f}, Lower={lower:.2f}, Mid={mid:.2f}, Trend={trend}")
            # --- ตัวอย่างตรวจ entry/exit (เพิ่มกลยุทธ์ต่อได้) ---
            # if trend=="BUY" and close<=lower: ...
            time.sleep(LOOP_SECONDS)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.warning(f"loop err: {e}")
            time.sleep(LOOP_SECONDS)

if __name__=="__main__":
    main()
