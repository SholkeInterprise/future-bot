import requests
from flask import Flask, jsonify, render_template_string, request
import os, time

app = Flask(__name__)

# ===== STATION - SAB SETTING YAHI HAI =====
STATE = {
    "balance_inr": 10000.0,  # Starting Wallet - 10,000 Rs Nakli Paisa
    "pos": None,  # {"coin","side","entry_inr","amount","lev","time"}
    "history": [],
    "usdinr": 88.0,
    "settings": {"auto": True, "leverage": 10, "amount": 1000, "tp": 0.5, "sl": 0.5},
    "last_candle_time": 0
}

def get_usdinr():
    try:
        r = requests.get("https://data-api.binance.vision/api/v3/ticker/price?symbol=USDTINR", timeout=5, headers={"User-Agent":"Mozilla/5.0"}).json()
        STATE["usdinr"] = float(r["price"])
    except:
        pass
    return STATE["usdinr"]

def get_data_coin(coin):
    sym = coin + "USDT"
    try:
        r = requests.get(f"https://data-api.binance.vision/api/v3/ticker/24hr?symbol={sym}", timeout=10, headers={"User-Agent":"Mozilla/5.0"}).json()
        price_usd = float(r["lastPrice"]); ch24 = float(r["priceChangePercent"])
        r2 = requests.get(f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=1m&limit=40", timeout=10, headers={"User-Agent":"Mozilla/5.0"}).json()
        candles_usd = [{"time":int(int(k[0])/1000),"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),"close":float(k[4])} for k in r2]
        return price_usd, ch24, candles_usd
    except Exception as e:
        return 0, 0, []

def calc_pnl(pos, curr_inr):
    if not pos: return 0, 0
    pct = ((curr_inr - pos["entry_inr"]) / pos["entry_inr"] * 100) if pos["side"]=="LONG" else ((pos["entry_inr"] - curr_inr) / pos["entry_inr"] * 100)
    pnl = pos["amount"] * (pos["lev"] * pct / 100)
    return round(pct,3), round(pnl,2)

def close_pos(reason, curr_inr):
    pos = STATE["pos"]
    if not pos: return
    pct, pnl = calc_pnl(pos, curr_inr)
    STATE["balance_inr"] += pnl
    STATE["history"].insert(0, f"{pos['coin']} {pos['side']} {reason} @ ₹{curr_inr:.2f} | PnL ₹{pnl:.2f} ({pct}%) | Lev {pos['lev']}x")
    STATE["history"] = STATE["history"][:20]
    STATE["pos"] = None

def open_pos(coin, side, entry_inr, auto=False):
    s = STATE["settings"]
    if STATE["pos"]: return False
    if STATE["balance_inr"] < s["amount"]:
        return False
    STATE["pos"] = {"coin":coin,"side":side,"entry_inr":entry_inr,"amount":s["amount"],"lev":s["leverage"],"time":time.time(),"auto":auto}
    STATE["history"].insert(0, f"{coin} {side} {'AUTO' if auto else 'MANUAL'} Open @ ₹{entry_inr:.2f} | Amt ₹{s['amount']} Lev {s['leverage']}x")
    return True

HTML = """
<style>body{font-family:Arial;text-align:center;padding:8px}button{padding:10px 14px;margin:3px;font-size:15px;border-radius:8px;border:1px solid #000}.long{background:#22c55e;color:#fff}.short{background:#ef4444;color:#fff}.close{background:#000;color:#fff}.box{border:1px solid #ccc;border-radius:10px;padding:10px;margin:8px auto;max-width:820px;text-align:left}input,select{padding:8px;margin:4px;font-size:15px;width:110px}.on{color:green;font-weight:bold}.off{color:red;font-weight:bold}</style>
<h2>🚀 Future Bot - FINAL INR Station</h2>
<div><button onclick="load('BTC')">BTC</button><button onclick="load('ETH')">ETH</button><button onclick="load('SOL')">SOL</button><button onclick="load('DOGE')">DOGE</button><button onclick="load('XRP')">XRP</button><button onclick="load('ADA')">ADA</button><button onclick="load('BNB')">BNB</button></div>
<h1 id="p">Loading...</h1>
<h2 id="bal" style="color:#0a7">💰 Wallet: ₹10,000</h2>
<h3 id="pos">No Position</h3>
<h3 id="pnl" style="color:blue">Live PnL: ₹0</h3>

<div class="box"><b>⚙️ SETTING - Tu Khud Badal Sakta Hai</b><br>
Leverage: <select id="lev" onchange="saveSet()"><option>1</option><option>2</option><option>5</option><option selected>10</option><option>20</option></select> x &nbsp;
Amount ₹: <input id="amt" type="number" value="1000" onchange="saveSet()"> &nbsp;
TP %: <input id="tp" type="number" value="0.5" step="0.1" onchange="saveSet()"> &nbsp;
SL %: <input id="sl" type="number" value="0.5" step="0.1" onchange="saveSet()"><br>
Auto Trade: <select id="auto" onchange="saveSet()"><option value="1" selected>ON ✅</option><option value="0">OFF ❌</option></select> <span id="autoTxt" class="on">AUTO ON - 0.5% Jump/Dump pe khud khulega</span>
</div>

<div><button class="long" onclick="trade('LONG')">🟢 LONG Lagao</button><button class="short" onclick="trade('SHORT')">🔴 SHORT Lagao</button><button class="close" onclick="trade('CLOSE')">❌ Band Karo</button><button onclick="resetBal()">🔄 Wallet Reset ₹10,000</button></div>
<canvas id="c" width="800" height="300" style="border:1px solid #000;width:100%;max-width:800px;margin-top:10px"></canvas>
<h3>📜 History</h3><div id="h"></div>
<script>
let cur='BTC';
async function load(coin){cur=coin;fetchData();}
async function saveSet(){let s={leverage:document.getElementById('lev').value,amount:document.getElementById('amt').value,tp:document.getElementById('tp').value,sl:document.getElementById('sl').value,auto:document.getElementById('auto').value};await fetch('/api/settings?leverage='+s.leverage+'&amount='+s.amount+'&tp='+s.tp+'&sl='+s.sl+'&auto='+s.auto);document.getElementById('autoTxt').innerText=s.auto=='1'?'AUTO ON - 0.5% Jump/Dump pe khud khulega':'AUTO OFF - Sirf Manual';document.getElementById('autoTxt').className=s.auto=='1'?'on':'off';}
async function trade(side){let r=await fetch('/api/trade?coin='+cur+'&side='+side);let d=await r.json();alert(d.msg);fetchData();}
async function resetBal(){await fetch('/api/reset');fetchData();}
async function fetchData(){let r=await fetch('/api/data?coin='+cur);let d=await r.json();document.getElementById('p').innerText=cur+' : ₹'+d.price_inr.toLocaleString('en-IN',{maximumFractionDigits:2})+' | '+d.position+' | 24h '+d.change+'%';document.getElementById('bal').innerText='💰 Wallet: ₹'+d.balance.toLocaleString('en-IN',{maximumFractionDigits:2})+' | USDT ₹'+d.usdinr;document.getElementById('pos').innerText=d.pos_msg;let p=document.getElementById('pnl');p.innerText='Live PnL: ₹'+d.live_pnl+' ('+d.live_pct+'%)';p.style.color=d.live_pnl>=0?'green':'red';document.getElementById('h').innerHTML=d.history.map(x=>'<div>'+x+'</div>').join('');let cv=document.getElementById('c');let x=cv.getContext('2d');x.clearRect(0,0,800,300);if(!d.candles.length)return;let lo=Math.min(...d.candles.map(a=>a.low));let hi=Math.max(...d.candles.map(a=>a.high));let rg=hi-lo||1;let w=800/d.candles.length;d.candles.forEach((c,i)=>{let px=i*w+w/2;let yH=10+((hi-c.high)/rg)*270;let yL=10+((hi-c.low)/rg)*270;let yO=10+((hi-c.open)/rg)*270;let yC=10+((hi-c.close)/rg)*270;let g=c.close>=c.open;x.strokeStyle=g?'green':'red';x.fillStyle=g?'green':'red';x.beginPath();x.moveTo(px,yH);x.lineTo(px,yL);x.stroke();x.fillRect(px-6,Math.min(yO,yC),12,Math.max(3,Math.abs(yC-yO)));});}
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
        STATE["settings"]["tp"] = float(request.args.get("tp",0.5))
        STATE["settings"]["sl"] = float(request.args.get("sl",0.5))
        STATE["settings"]["auto"] = request.args.get("auto","1")=="1"
    except: pass
    return jsonify({"ok":True,"settings":STATE["settings"]})

@app.route("/api/reset")
def reset():
    STATE["balance_inr"]=10000.0; STATE["pos"]=None; STATE["history"]=[]
    return jsonify({"ok":True})

@app.route("/api/trade")
def trade():
    coin=request.args.get("coin","BTC"); side=request.args.get("side","LONG")
    usdinr=get_usdinr()
    price_usd,_,_=get_data_coin(coin)
    price_inr=price_usd*usdinr
    if side=="CLOSE":
        if not STATE["pos"]: return jsonify({"msg":"Koi trade khula nahi hai"})
        close_pos("MANUAL BAND", price_inr)
        return jsonify({"msg":f"Trade band! Wallet ab ₹{STATE['balance_inr']:.2f}"})
    if STATE["pos"]: return jsonify({"msg":f"Pehle {STATE['pos']['coin']} {STATE['pos']['side']} band karo"})
    if open_pos(coin, side, price_inr, auto=False):
        return jsonify({"msg":f"{coin} {side} lag gaya @ ₹{price_inr:.2f}"})
    return jsonify({"msg":"Wallet me paisa kam hai! Amount kam karo ya Reset karo"})

@app.route("/api/data")
def api():
    coin=request.args.get("coin","BTC")
    usdinr=get_usdinr()
    price_usd, ch24, candles_usd = get_data_coin(coin)
    price_inr = price_usd * usdinr
    candles = [{"time":c["time"],"open":round(c["open"]*usdinr,2),"high":round(c["high"]*usdinr,2),"low":round(c["low"]*usdinr,2),"close":round(c["close"]*usdinr,2)} for c in candles_usd]

    # ===== AUTO LOGIC - 0.5% in 1 MIN =====
    if len(candles_usd)>=2:
        last = candles_usd[-1]
        one_min_pct = (last["close"]-last["open"])/last["open"]*100
        s = STATE["settings"]
        if s["auto"] and not STATE["pos"]:
            if one_min_pct >= 0.5:
                open_pos(coin, "LONG", price_inr, auto=True)
            elif one_min_pct <= -0.5:
                open_pos(coin, "SHORT", price_inr, auto=True)

    # ===== AUTO TP / SL CHECK =====
    live_pct, live_pnl = 0, 0
    pos_msg = "No Position"
    if STATE["pos"]:
        pos = STATE["pos"]
        # Agar dusri coin kholi to uska price leke PnL nikalo
        if pos["coin"] != coin:
            p2,_,_ = get_data_coin(pos["coin"])
            curr = p2*usdinr
        else:
            curr = price_inr
        live_pct, live_pnl = calc_pnl(pos, curr)
        pos_msg = f"Open: {pos['coin']} {pos['side']} @ ₹{pos['entry_inr']:.2f} | Amt ₹{pos['amount']} Lev {pos['lev']}x | {'AUTO' if pos.get('auto') else 'MANUAL'}"
        # TP / SL
        if live_pct >= STATE["settings"]["tp"]:
            close_pos("TP HIT ✅", curr); pos_msg="TP Hit! Trade Band"; live_pct, live_pnl = 0,0
        elif live_pct <= -STATE["settings"]["sl"]:
            close_pos("SL HIT ❌", curr); pos_msg="SL Hit! Trade Band"; live_pct, live_pnl = 0,0

    pos_sig = "LONG 🟢" if ch24>=0 else "SHORT 🔴"
    return jsonify({"price_inr":round(price_inr,2),"price":round(price_inr,2),"change":round(ch24,2),"position":pos_sig,"candles":candles,"history":STATE["history"],"balance":round(STATE["balance_inr"],2),"usdinr":round(usdinr,2),"pos_msg":pos_msg,"live_pnl":live_pnl,"live_pct":live_pct})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
