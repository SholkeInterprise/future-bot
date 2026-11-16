import requests
from flask import Flask, jsonify, render_template_string, request
import os

app = Flask(__name__)

def get_data(coin):
    sym = coin + "-USD"
    try:
        # 1. Coinbase Price - ye Render pe chalta hai
        r = requests.get(f"https://api.coinbase.com/v2/prices/{sym}/spot", timeout=10)
        price = float(r.json()["data"]["amount"])
        # 2. Coinbase Candle - 1 min wali
        r2 = requests.get(f"https://api.exchange.coinbase.com/products/{sym}/candles?granularity=60", timeout=10)
        raw = r2.json()
        candles = []
        # Coinbase ulta deta hai, isliye sort karo
        raw_sorted = sorted(raw, key=lambda x: x[0])
        for k in raw_sorted[-30:]:
            # Coinbase format: [time, low, high, open, close, volume]
            candles.append({"time":int(k[0]),"open":float(k[3]),"high":float(k[2]),"low":float(k[1]),"close":float(k[4])})
        ch = 0
        if len(candles) >= 2:
            ch = ((candles[-1]["close"] - candles[0]["open"]) / candles[0]["open"] * 100)
        return {"price":price,"change":round(ch,2),"position":"LONG 🟢" if ch>=0 else "SHORT 🔴","pnl":round(ch,2),"candles":candles,"history":[{"price":price}]}
    except Exception as e:
        return {"price":0,"change":0,"position":"Error: "+str(e)[:100],"pnl":0,"candles":[],"history":[]}

HTML = """<h2>Future Bot Test</h2><button onclick="load('BTC')">BTC</button><button onclick="load('ETH')">ETH</button><button onclick="load('SOL')">SOL</button><h1 id="p">Loading...</h1><canvas id="c" width="800" height="300" style="border:1px solid #000;width:100%"></canvas><script>
async function load(coin){let r=await fetch('/api/data?coin='+coin);let d=await r.json();document.getElementById('p').innerText=coin+' : $ '+d.price+' | '+d.position;let cv=document.getElementById('c');let x=cv.getContext('2d');x.clearRect(0,0,800,300);if(!d.candles.length){x.fillText('No candle - '+d.position,20,50);return;}let lo=Math.min(...d.candles.map(a=>a.low));let hi=Math.max(...d.candles.map(a=>a.high));let rg=hi-lo||1;let w=800/d.candles.length;d.candles.forEach((c,i)=>{let px=i*w+w/2;let yH=10+((hi-c.high)/rg)*260;let yL=10+((hi-c.low)/rg)*260;let yO=10+((hi-c.open)/rg)*260;let yC=10+((hi-c.close)/rg)*260;let g=c.close>=c.open;x.strokeStyle=g?'green':'red';x.fillStyle=g?'green':'red';x.beginPath();x.moveTo(px,yH);x.lineTo(px,yL);x.stroke();x.fillRect(px-6,Math.min(yO,yC),12,Math.max(3,Math.abs(yC-yO)));});}
load('BTC');</script>"""

@app.route("/")
def home(): return render_template_string(HTML)
@app.route("/api/data")
def api(): return jsonify(get_data(request.args.get("coin","BTC")))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))import requests
from flask import Flask, jsonify, render_template_string, request
import os

app = Flask(__name__)

def get_data(coin):
    sym = coin + "-USD"
    try:
        # 1. Coinbase Price - ye Render pe chalta hai
        r = requests.get(f"https://api.coinbase.com/v2/prices/{sym}/spot", timeout=10)
        price = float(r.json()["data"]["amount"])
        # 2. Coinbase Candle - 1 min wali
        r2 = requests.get(f"https://api.exchange.coinbase.com/products/{sym}/candles?granularity=60", timeout=10)
        raw = r2.json()
        candles = []
        # Coinbase ulta deta hai, isliye sort karo
        raw_sorted = sorted(raw, key=lambda x: x[0])
        for k in raw_sorted[-30:]:
            # Coinbase format: [time, low, high, open, close, volume]
            candles.append({"time":int(k[0]),"open":float(k[3]),"high":float(k[2]),"low":float(k[1]),"close":float(k[4])})
        ch = 0
        if len(candles) >= 2:
            ch = ((candles[-1]["close"] - candles[0]["open"]) / candles[0]["open"] * 100)
        return {"price":price,"change":round(ch,2),"position":"LONG 🟢" if ch>=0 else "SHORT 🔴","pnl":round(ch,2),"candles":candles,"history":[{"price":price}]}
    except Exception as e:
        return {"price":0,"change":0,"position":"Error: "+str(e)[:100],"pnl":0,"candles":[],"history":[]}

HTML = """<h2>Future Bot Test</h2><button onclick="load('BTC')">BTC</button><button onclick="load('ETH')">ETH</button><button onclick="load('SOL')">SOL</button><h1 id="p">Loading...</h1><canvas id="c" width="800" height="300" style="border:1px solid #000;width:100%"></canvas><script>
async function load(coin){let r=await fetch('/api/data?coin='+coin);let d=await r.json();document.getElementById('p').innerText=coin+' : $ '+d.price+' | '+d.position;let cv=document.getElementById('c');let x=cv.getContext('2d');x.clearRect(0,0,800,300);if(!d.candles.length){x.fillText('No candle - '+d.position,20,50);return;}let lo=Math.min(...d.candles.map(a=>a.low));let hi=Math.max(...d.candles.map(a=>a.high));let rg=hi-lo||1;let w=800/d.candles.length;d.candles.forEach((c,i)=>{let px=i*w+w/2;let yH=10+((hi-c.high)/rg)*260;let yL=10+((hi-c.low)/rg)*260;let yO=10+((hi-c.open)/rg)*260;let yC=10+((hi-c.close)/rg)*260;let g=c.close>=c.open;x.strokeStyle=g?'green':'red';x.fillStyle=g?'green':'red';x.beginPath();x.moveTo(px,yH);x.lineTo(px,yL);x.stroke();x.fillRect(px-6,Math.min(yO,yC),12,Math.max(3,Math.abs(yC-yO)));});}
load('BTC');</script>"""

@app.route("/")
def home(): return render_template_string(HTML)
@app.route("/api/data")
def api(): return jsonify(get_data(request.args.get("coin","BTC")))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))https://api.coinbase.com/v2/prices/{sym}/spot
