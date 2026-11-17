import requests
from flask import Flask, jsonify, render_template_string, request
import os, time, threading

app = Flask(__name__)

STATE = {
    "balance_inr": 10000.0,
    "locked_inr": 0.0,
    "pos": None,
    "history": [],
    "settings": {"auto": True, "leverage": 10, "amount": 1000, "tp": 1.0, "sl": 0.5, "trail": 0.5, "auto_pct": 0.5, "auto_coin": "BTC"},
}

def get_live_price(coin):
    try:
        t = requests.get("https://api.coindcx.com/exchange/ticker", timeout=8, headers={"User-Agent":"Mozilla/5.0"}).json()
        for m in t:
            if m.get("market") == f"{coin}INR":
                return float(m.get("last_price", 0)), float(m.get("change_24_hour", 0))
    except: pass
    try:
        r = requests.get(f"https://data-api.binance.vision/api/v3/ticker/24hr?symbol={coin}USDT", timeout=10).json()
        return float(r["lastPrice"])*88, float(r["priceChangePercent"])
    except: return 0, 0

def get_data_coin(coin, interval="1m"):
    pair = f"I-{coin}_INR"
    price_inr, ch24 = get_live_price(coin)
    candles_inr = []
    try:
        r = requests.get(f"https://public.coindcx.com/market_data/candles?pair={pair}&interval={interval}&limit=50", timeout=10, headers={"User-Agent":"Mozilla/5.0"}).json()
        raw = r.get("data", r) if isinstance(r, dict) else r
        for k in raw:
            if isinstance(k, dict):
                candles_inr.append({"time": int(k.get("time",0)), "open": float(k.get("open",0)), "high": float(k.get("high",0)), "low": float(k.get("low",0)), "close": float(k.get("close",0))})
            else:
                ts = int(k[0]); ts = int(ts/1000) if ts>10000000000 else ts
                candles_inr.append({"time": ts, "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4])})
        candles_inr = sorted(candles_inr, key=lambda x: x["time"])
        if not candles_inr: raise Exception("no candles")
        if price_inr == 0: price_inr = candles_inr[-1]["close"]
        last = candles_inr[-1]
        last["close"] = price_inr
        if price_inr > last["high"]: last["high"] = price_inr
        if price_inr < last["low"]: last["low"] = price_inr
        return price_inr, ch24, candles_inr
    except:
        try:
            sym = coin + "USDT"
            bi = {"1m":"1m","5m":"5m","15m":"15m","1h":"1h","4h":"4h","1d":"1d"}.get(interval,"1m")
            r2 = requests.get(f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval={bi}&limit=50", timeout=10).json()
            candles = [{"time":int(int(k[0])/1000),"open":float(k[1])*88,"high":float(k[2])*88,"low":float(k[3])*88,"close":float(k[4])*88} for k in r2]
            if candles:
                candles[-1]["close"] = price_inr if price_inr>0 else candles[-1]["close"]
                if price_inr>0:
                    if price_inr > candles[-1]["high"]: candles[-1]["high"] = price_inr
                    if price_inr < candles[-1]["low"]: candles[-1]["low"] = price_inr
            return price_inr, ch24, candles
        except: return 0, 0, []

def calc_pnl(pos, curr_inr):
    if not pos: return 0, 0
    pct = ((curr_inr - pos["entry_inr"]) / pos["entry_inr"] * 100) if pos["side"]=="LONG" else ((pos["entry_inr"] - curr_inr) / pos["entry_inr"] * 100)
    pnl = pos["amount"] * (pos["lev"] * pct / 100)
    return round(pct,3), round(pnl,2)

def close_pos(reason, curr_inr):
    pos = STATE["pos"]
    if not pos: return
    pct, pnl = calc_pnl(pos, curr_inr)
    STATE["locked_inr"] -= pos["amount"]
    STATE["balance_inr"] += pos["amount"] + pnl
    STATE["history"].insert(0, f"{pos['coin']} {pos['side']} {reason} @ ₹{curr_inr:,.2f} | PnL ₹{pnl:.2f} ({pct}%) | Lev {pos['lev']}x | Peak ₹{pos.get('peak',pos['entry_inr']):,.2f}")
    STATE["history"] = STATE["history"][:20]
    STATE["pos"] = None

def open_pos(coin, side, entry_inr, auto=False):
    s = STATE["settings"]
    if STATE["pos"]: return False
    if STATE["balance_inr"] < s["amount"]: return False
    STATE["balance_inr"] -= s["amount"]
    STATE["locked_inr"] += s["amount"]
    STATE["pos"] = {"coin":coin,"side":side,"entry_inr":entry_inr,"amount":s["amount"],"lev":s["leverage"],"time":time.time(),"auto":auto,"peak":entry_inr,"trough":entry_inr}
    STATE["history"].insert(0, f"{coin} {side} {'AUTO' if auto else 'MANUAL'} Open @ ₹{entry_inr:,.2f} | Amt ₹{s['amount']} Lev {s['leverage']}x")
    return True

def check_trade_logic(coin, price_inr, candles):
    s = STATE["settings"]
    if s["auto"] and not STATE["pos"] and len(candles)>=2:
        last = candles[-1]
        pct = (last["close"]-last["open"])/last["open"]*100 if last["open"] else 0
        if pct >= s["auto_pct"]: open_pos(coin, "LONG", price_inr, auto=True)
        elif pct <= -s["auto_pct"]: open_pos(coin, "SHORT", price_inr, auto=True)
    if STATE["pos"]:
        pos = STATE["pos"]
        curr = price_inr
        if pos["side"]=="LONG":
            if curr > pos["peak"]: pos["peak"]=curr
        else:
            if curr < pos["trough"]: pos["trough"]=curr
        live_pct, _ = calc_pnl(pos, curr)
        if live_pct >= s["tp"]: close_pos("TP HIT ✅", curr)
        elif live_pct <= -s["sl"]: close_pos("SL HIT ❌", curr)
        elif pos["side"]=="LONG" and curr <= pos["peak"] * (1 - s["trail"]/100) and live_pct > 0: close_pos(f"TRAIL HIT 📈 High se {s['trail']}% gira", curr)
        elif pos["side"]=="SHORT" and curr >= pos["trough"] * (1 + s["trail"]/100) and live_pct > 0: close_pos(f"TRAIL HIT 📉 Low se {s['trail']}% upar", curr)

def background_bot():
    print("🤖 BACKGROUND BOT START")
    while True:
        try:
            s = STATE["settings"]
            coin_to_check = STATE["pos"]["coin"] if STATE["pos"] else s.get("auto_coin","BTC")
            price_inr, _, candles = get_data_coin(coin_to_check, "1m")
            live_p, _ = get_live_price(coin_to_check)
            if live_p>0: price_inr = live_p
            if price_inr>0:
                check_trade_logic(coin_to_check, price_inr, candles)
        except Exception as e:
            print("BG Error:", e)
        time.sleep(5)

threading.Thread(target=background_bot, daemon=True).start()

HTML = """
<style>body{font-family:Arial;text-align:center;padding:8px}button{padding:10px 14px;margin:3px;font-size:15px;border-radius:8px;border:1px solid #000}.long{background:#22c55e;color:#fff}.short{background:#ef4444;color:#fff}.close{background:#000;color:#fff}.tf{background:#eef}.tf.active{background:#000;color:#fff}.tool{background:#fff;border:2px solid #7c3aed}.tool.active{background:#7c3aed;color:#fff}.box{border:1px solid #ccc;border-radius:10px;padding:10px;margin:8px auto;max-width:860px;text-align:left}input,select{padding:8px;margin:4px;font-size:15px;width:110px}.on{color:green;font-weight:bold}.off{color:red;font-weight:bold}.live{color:#ef4444;font-weight:bold;animation:blink 1s infinite}@keyframes blink{50%{opacity:0.3}}.tpbox{background:#f0fdf4;border:1px solid #22c55e;border-radius:8px;padding:8px;margin-top:8px}.slbox{background:#fef2f2;border:1px solid #ef4444;border-radius:8px;padding:8px;margin-top:6px}</style>
<h2>🚀 Future Bot - INDIA PRICE 🇮🇳 + 24x7 AUTO 🤖</h2>
<div><button onclick="load('BTC')">BTC</button><button onclick="load('ETH')">ETH</button><button onclick="load('SOL')">SOL</button><button onclick="load('DOGE')">DOGE</button><button onclick="load('XRP')">XRP</button><button onclick="load('ADA')">ADA</button><button onclick="load('BNB')">BNB</button></div>
<h1 id="p">Loading...</h1>
<div><span class="live">● LIVE</span> <span style="font-size:14px">Chart Price 2 Decimal + TP/SL Live ₹ Preview</span></div>
<h2 id="bal" style="color:#0a7">💰 Wallet: ₹10,000</h2>
<h3 id="locked" style="color:#d97706">🔒 Locked: ₹0 | Total: ₹10,000</h3>
<h3 id="pos">No Position</h3>
<h3 id="trailInfo" style="color:#7c3aed">Trailing: -</h3>
<h3 id="pnl" style="color:blue">Live PnL: ₹0</h3>
<div class="box"><b>⚙️ SETTING</b><br>
Leverage: <select id="lev" onchange="saveSet()"><option>1</option><option>2</option><option>5</option><option selected>10</option><option>20</option></select> x &nbsp;
Amount ₹: <input id="amt" type="number" value="1000" onchange="saveSet()"> &nbsp;
TP %: <input id="tp" type="number" value="1.0" step="0.1" onchange="saveSet()"> &nbsp;
SL %: <input id="sl" type="number" value="0.5" step="0.1" onchange="saveSet()"> &nbsp;
Trail %: <input id="trail" type="number" value="0.5" step="0.1" onchange="saveSet()"><br>
<b>🤖 AUTO TRADE:</b> <select id="auto" onchange="saveSet()"><option value="1" selected>ON ✅</option><option value="0">OFF ❌</option></select>
Auto %: <input id="auto_pct" type="number" value="0.5" step="0.1" min="0.1" onchange="saveSet()"> % &nbsp;
Auto Coin: <select id="auto_coin" onchange="saveSet()"><option>BTC</option><option>ETH</option><option>SOL</option><option>DOGE</option><option>XRP</option><option>ADA</option><option>BNB</option></select><br>
<span id="autoTxt" class="on">AUTO ON - 0.5% Jump/Dump pe khud khulega</span>
<div id="tpPreview"></div>
</div>
<div><button class="long" onclick="trade('LONG')">🟢 LONG Lagao</button><button class="short" onclick="trade('SHORT')">🔴 SHORT Lagao</button><button class="close" onclick="trade('CLOSE')">❌ Band Karo</button><button onclick="resetBal()">🔄 Wallet Reset ₹10,000</button></div>
<div style="margin-top:12px"><b>⏱️ TIME FRAME:</b>
<button class="tf active" id="tf-1m" onclick="setTF('1m')">1m</button><button class="tf" id="tf-5m" onclick="setTF('5m')">5m</button><button class="tf" id="tf-15m" onclick="setTF('15m')">15m</button><button class="tf" id="tf-1h" onclick="setTF('1h')">1h</button><button class="tf" id="tf-4h" onclick="setTF('4h')">4h</button><button class="tf" id="tf-1d" onclick="setTF('1d')">1D</button>
</div>
<div style="margin-top:8px"><b>📐 LINE TOOL:</b>
<button class="tool active" id="tool-none" onclick="setTool('none')">🖱️ Normal</button><button class="tool" id="tool-h" onclick="setTool('h')">➖ H-Line (Price)</button><button class="tool" id="tool-v" onclick="setTool('v')">➡️ V-Line (Time)</button><button class="tool" onclick="clearLines()">🗑️ Lines Clear</button><br><span style="font-size:13px">H-Line ya V-Line select karke Chart pe click karo — line lag jayegi.</span>
</div>
<canvas id="c" width="860" height="340" style="border:1px solid #000;width:100%;max-width:860px;margin-top:10px;cursor:crosshair"></canvas>
<h3>📜 History</h3><div id="h"></div>
<script>
let cur='BTC'; let tf='1m'; let tool='none'; let hLines=[]; let vLines=[]; let lastCandles=[]; let lastPrice=0; let curPos=null; let curEntry=0;
function setTF(t){tf=t;document.querySelectorAll('.tf').forEach(b=>b.classList.remove('active'));document.getElementById('tf-'+t).classList.add('active');fetchData();}
function setTool(t){tool=t;document.querySelectorAll('.tool').forEach(b=>b.classList.remove('active'));if(t!='none')document.getElementById('tool-'+t).classList.add('active');else document.getElementById('tool-none').classList.add('active');}
function clearLines(){hLines=[];vLines=[];drawChart();}
async function load(coin){cur=coin;document.getElementById('auto_coin').value=coin;saveSet();fetchData();}
async function saveSet(){let s={leverage:document.getElementById('lev').value,amount:document.getElementById('amt').value,tp:document.getElementById('tp').value,sl:document.getElementById('sl').value,trail:document.getElementById('trail').value,auto:document.getElementById('auto').value,auto_pct:document.getElementById('auto_pct').value,auto_coin:document.getElementById('auto_coin').value};await fetch('/api/settings?leverage='+s.leverage+'&amount='+s.amount+'&tp='+s.tp+'&sl='+s.sl+'&trail='+s.trail+'&auto='+s.auto+'&auto_pct='+s.auto_pct+'&auto_coin='+s.auto_coin);document.getElementById('autoTxt').innerText=s.auto=='1'?'AUTO ON - '+s.auto_pct+'% Jump/Dump pe khud khulega | Auto Coin: '+s.auto_coin:'AUTO OFF - Sirf Manual';document.getElementById('autoTxt').className=s.auto=='1'?'on':'off';updateTpSlPreview();}
async function trade(side){let r=await fetch('/api/trade?coin='+cur+'&side='+side);let d=await r.json();alert(d.msg);fetchData();}
async function resetBal(){await fetch('/api/reset');fetchData();}
// FIX 1: Ab 76.4k nahi, poora 76,450.88 dikhega - 2 decimal tak
function fmtINR(n){return '₹'+Number(n).toLocaleString('en-IN',{minimumFractionDigits:2, maximumFractionDigits:2});}
function fmtPrice(n){return Number(n).toLocaleString('en-IN',{minimumFractionDigits:2, maximumFractionDigits:2});}
// FIX 2: TP/SL % daalte hi LIVE ₹ Profit/Loss + Price dikhega
function updateTpSlPreview(){
let tp=parseFloat(document.getElementById('tp').value)||0;
let sl=parseFloat(document.getElementById('sl').value)||0;
let lev=parseFloat(document.getElementById('lev').value)||10;
let amt=parseFloat(document.getElementById('amt').value)||1000;
let tpProfit = amt * lev * tp / 100;
let slLoss = amt * lev * sl / 100;
// Agar trade khula hai to uski Entry se, nahi to ab wale Live Price se
let basePrice = (curPos && curEntry>0) ? curEntry : lastPrice;
let side = (curPos && curPos.side) ? curPos.side : 'LONG';
let tpPrice, slPrice;
if(side=='LONG'){ tpPrice = basePrice * (1 + tp/100); slPrice = basePrice * (1 - sl/100); }
else { tpPrice = basePrice * (1 - tp/100); slPrice = basePrice * (1 + sl/100); }
let baseLabel = (curPos && curEntry>0) ? ('Entry ₹'+fmtPrice(basePrice)+' ('+side+') se') : ('Live ₹'+fmtPrice(basePrice)+' pe '+side+' lagao to');
let html = '<div class="tpbox"><b>🎯 TP Preview - '+baseLabel+'</b><br>✅ TP '+tp+'% → Price <b>₹'+fmtPrice(tpPrice)+'</b> | Profit <b style="color:green">+₹'+tpProfit.toFixed(2)+'</b> <span style="font-size:13px">(Tere ₹'+amt+' pe '+(lev*tp).toFixed(1)+'% )</span></div>';
html += '<div class="slbox"><b>🛑 SL Preview - '+baseLabel+'</b><br>❌ SL '+sl+'% → Price <b>₹'+fmtPrice(slPrice)+'</b> | Loss <b style="color:red">-₹'+slLoss.toFixed(2)+'</b> <span style="font-size:13px">(Tere ₹'+amt+' pe '+(lev*sl).toFixed(1)+'% )</span></div>';
document.getElementById('tpPreview').innerHTML = html;
}
function drawChart(){
let cv=document.getElementById('c');let x=cv.getContext('2d');x.clearRect(0,0,860,340);if(!lastCandles.length)return;
let allLow=[...lastCandles.map(a=>a.low), lastPrice]; let allHigh=[...lastCandles.map(a=>a.high), lastPrice];
let lo=Math.min(...allLow); let hi=Math.max(...allHigh);
let rg=hi-lo||1; lo-=rg*0.08; hi+=rg*0.08; rg=hi-lo;
let chartW=700; let w=chartW/lastCandles.length;let yOf=p=>10+((hi-p)/rg)*300;let xOf=i=>i*w+w/2;
x.font='12px Arial';x.fillStyle='#666';x.strokeStyle='#eee';
for(let i=0;i<=5;i++){let p=lo+(rg*i/5);let y=yOf(p);x.beginPath();x.moveTo(0,y);x.lineTo(chartW,y);x.stroke();x.fillText(fmtINR(p),708,y+4);}
let yL=yOf(lastPrice);x.strokeStyle='#2563eb';x.setLineDash([4,3]);x.lineWidth=1;x.beginPath();x.moveTo(0,yL);x.lineTo(chartW,yL);x.stroke();x.setLineDash([]);x.lineWidth=1;
hLines.forEach(p=>{let y=yOf(p);x.strokeStyle='#7c3aed';x.setLineDash([6,4]);x.lineWidth=1;x.beginPath();x.moveTo(0,y);x.lineTo(chartW,y);x.stroke();x.setLineDash([]);});
vLines.forEach(idx=>{let px=xOf(idx);x.strokeStyle='#f59e0b';x.setLineDash([6,4]);x.lineWidth=1;x.beginPath();x.moveTo(px,0);x.lineTo(px,310);x.stroke();x.setLineDash([]);});
lastCandles.forEach((c,i)=>{
let px=xOf(i);let yH=yOf(c.high);let yL2=yOf(c.low);let yO=yOf(c.open);let yC=yOf(c.close);let isLast=(i==lastCandles.length-1);let g=c.close>=c.open;
x.strokeStyle=g?'green':'red';x.fillStyle=g?'green':'red';x.beginPath();x.moveTo(px,yH);x.lineTo(px,yL2);x.stroke();
let bw = isLast ? 10 : 7; x.fillRect(px-bw/2,Math.min(yO,yC),bw,Math.max(3,Math.abs(yC-yO)));
if(isLast){x.strokeStyle='#000';x.lineWidth=2.5;x.setLineDash([]);x.strokeRect(px-bw/2-1,Math.min(yO,yC)-1,bw+2,Math.max(4,Math.abs(yC-yO))+2);x.lineWidth=1;x.fillStyle='#000';x.font='bold 11px Arial';x.fillText('● LIVE', px-18, Math.min(yO,yC)-6);x.font='12px Arial';}
});
x.fillStyle='#2563eb';x.fillRect(708,yL-10,142,20);x.fillStyle='#fff';x.fillText(fmtINR(lastPrice),712,yL+4);x.fillStyle='#666';
hLines.forEach(p=>{let y=yOf(p);x.fillStyle='#7c3aed';x.fillRect(708,y-10,142,20);x.fillStyle='#fff';x.fillText(fmtINR(p),712,y+4);x.fillStyle='#666';});
}
document.getElementById('c').addEventListener('click',function(e){if(tool=='none')return;let rect=this.getBoundingClientRect();let cx=(e.clientX-rect.left)*(860/rect.width);let cy=(e.clientY-rect.top)*(340/rect.height);if(!lastCandles.length)return;let lo=Math.min(...lastCandles.map(a=>a.low));let hi=Math.max(...lastCandles.map(a=>a.high));let rg=hi-lo||1;lo-=rg*0.08;hi+=rg*0.08;rg=hi-lo;if(tool=='h'){let price=hi-((cy-10)/300)*rg;hLines.push(price);}if(tool=='v'){let idx=Math.floor(cx/(700/lastCandles.length));vLines.push(idx);}drawChart();});
async function fetchData(){let r=await fetch('/api/data?coin='+cur+'&interval='+tf);let d=await r.json();lastCandles=d.candles;lastPrice=d.price_inr;curPos=d.pos_obj;curEntry=d.entry_inr;document.getElementById('p').innerText=cur+' : ₹'+d.price_inr.toLocaleString('en-IN',{maximumFractionDigits:2})+' | '+d.position+' | 24h '+d.change+'%';document.getElementById('bal').innerText='💰 Available Wallet: ₹'+d.balance.toLocaleString('en-IN',{maximumFractionDigits:2});document.getElementById('locked').innerText='🔒 Locked: ₹'+d.locked.toLocaleString('en-IN',{maximumFractionDigits:2})+' | Total: ₹'+(d.balance+d.locked).toLocaleString('en-IN',{maximumFractionDigits:2});document.getElementById('pos').innerText=d.pos_msg;document.getElementById('trailInfo').innerText=d.trail_msg;let p=document.getElementById('pnl');p.innerText='Live PnL: ₹'+d.live_pnl+' ('+d.live_pct+'%)';p.style.color=d.live_pnl>=0?'green':'red';document.getElementById('h').innerHTML=d.history.map(x=>'<div>'+x+'</div>').join('');drawChart();updateTpSlPreview();}
setInterval(fetchData,5000);fetchData();
</script>
"""

@app.route("/")
def home(): return render_template_string(HTML)

@app.route("/api/settings")
def settings():
    try:
        STATE["settings"]["leverage"] = int(request.args.get("leverage",10))
        STATE["settings"]["amount"] = float(request.args.get("amount",1000))
        STATE["settings"]["tp"] = float(request.args.get("tp",1.0))
        STATE["settings"]["sl"] = float(request.args.get("sl",0.5))
        STATE["settings"]["trail"] = float(request.args.get("trail",0.5))
        STATE["settings"]["auto"] = request.args.get("auto","1")=="1"
        STATE["settings"]["auto_pct"] = float(request.args.get("auto_pct",0.5))
        STATE["settings"]["auto_coin"] = request.args.get("auto_coin","BTC")
    except: pass
    return jsonify({"ok":True})

@app.route("/api/reset")
def reset():
    STATE["balance_inr"]=10000.0; STATE["locked_inr"]=0.0; STATE["pos"]=None; STATE["history"]=[]
    return jsonify({"ok":True})

@app.route("/api/trade")
def trade():
    coin=request.args.get("coin","BTC"); side=request.args.get("side","LONG")
    interval=request.args.get("interval","1m")
    price_inr,_ = get_live_price(coin)
    if price_inr==0: price_inr,_,_=get_data_coin(coin, interval)
    if side=="CLOSE":
        if not STATE["pos"]: return jsonify({"msg":"Koi trade khula nahi hai"})
        close_pos("MANUAL BAND", price_inr)
        return jsonify({"msg":f"Trade band! Wallet ab ₹{STATE['balance_inr']:.2f}"})
    if STATE["pos"]: return jsonify({"msg":f"Pehle {STATE['pos']['coin']} {STATE['pos']['side']} band karo"})
    if open_pos(coin, side, price_inr, auto=False):
        return jsonify({"msg":f"{coin} {side} lag gaya @ ₹{price_inr:,.2f}"})
    return jsonify({"msg":"Wallet me paisa kam hai!"})

@app.route("/api/data")
def api():
    coin=request.args.get("coin","BTC")
    interval=request.args.get("interval","1m")
    price_inr, ch24, candles = get_data_coin(coin, interval)
    live_p, live_ch = get_live_price(coin)
    if live_p>0: price_inr = live_p; ch24 = live_ch
    candles = [{"time":c["time"],"open":round(c["open"],2),"high":round(c["high"],2),"low":round(c["low"],2),"close":round(c["close"],2)} for c in candles]
    live_pct, live_pnl = 0, 0
    pos_msg = "No Position"
    trail_msg = "Trailing: -"
    pos_obj = Non
