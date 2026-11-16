import requests, json
from flask import Flask, jsonify, render_template_string, request
import os

app = Flask(__name__)

def safe_json(r):
    try:
        return r.json()
    except:
        return None

def get_data(coin):
    sym = coin + "USDT"
    debug = ""
    # 1. TRY - Binance Vision (Render ke liye bana hai, kabhi block nahi hota)
    try:
        r = requests.get(f"https://data-api.binance.vision/api/v3/ticker/24hr?symbol={sym}", timeout=10, headers={"User-Agent":"Mozilla/5.0"})
        j = safe_json(r)
        if j and "lastPrice" in j:
            price = float(j["lastPrice"])
            ch = float(j["priceChangePercent"])
            r2 = requests.get(f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=1m&limit=30", timeout=10, headers={"User-Agent":"Mozilla/5.0"})
            j2 = safe_json(r2)
            candles = []
            if j2:
                for k in j2:
                    candles.append({"time":int(int(k[0])/1000),"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),"close":float(k[4])})
            return {"price":price,"change":round(ch,2),"position":"LONG 🟢" if ch>=0 else "SHORT 🔴","pnl":round(ch,2),"candles":candles,"history":[{"price":price}],"src":"Binance-Vision"}
    except Exception as e:
        debug += "Binance Fail:"+str(e)[:50]+" | "

    # 2. TRY - Coinbase (Backup)
    try:
        r = requests.get(f"https://api.coinbase.com/v2/prices/{coin}-USD/spot", timeout=10, headers={"User-Agent":"Mozilla/5.0"})
        j = safe_json(r)
        if j and "data" in j:
            price = float(j["data"]["amount"])
            r2 = requests.get(f"https://api.exchange.coinbase.com/products/{coin}-USD/candles?granularity=60", timeout=10, headers={"User-Agent":"Mozilla/5.0"})
            raw = safe_json(r2)
            candles = []
            if raw:
                for k in sorted(raw, key=lambda x: x[0])[-30:]:
                    candles.append({"time":int(k[0]),"open":float(k[3]),"high":float(k[2]),"low":float(k[1]),"close":float(k[4])})
            ch = ((candles[-1]["close"]-candles[0]["open"])/candles[0]["open"]*100) if len(candles)>=2 else 0
            return {"price":price,"change":round(ch,2),"position":"LONG 🟢" if ch>=0 else "SHORT 🔴","pnl":round(ch,2),"candles":candles,"history":[{"price":price}],"src":"Coinbase"}
    except Exception as e:
        debug += "Coinbase Fail:"+str(e)[:50]

    return {"price":0,"change":0,"position":"Sab Fail: "+debug,"pnl":0,"candles":[],"history":[],"src":"none"}

HTML = """<h2>Future Bot Test</h2><button onclick="load('BTC')">BTC</button><button onclick="load('ETH')">ETH</button><button onclick="load('SOL')">SOL</button><h1 id="p">Loading...</h1><p id="src"></p><canvas id="c" width="800" height="300" style="border:1px solid #000;width:100%"></canvas><script>
async function load(coin){let r=await fetch('/api/data?coin='+coin);let d=await r.json();document.getElementById('p').innerText=coin+' : $ '+d.price+' | '+d.position;document.getElementById('src').innerText='Source: '+(d.src||'');let cv=document.getElementById('c');let x=cv.getContext('2d');x.clearRect(0,0,800,300);if(!d.candles.length){x.fillText('No candle - '+d.position,20,50);return;}let lo=Math.min(...d.candles.map(a=>a.low));let hi=Math.max(...d.candles.map(a=>a.high));let rg=hi-lo||1;let w=800/d.candles.length;d.candles.forEach((c,i)=>{let px=i*w+w/2;let yH=10+((hi-c.high)/rg)*260;let yL=10+((hi-c.low)/rg)*260;let yO=10+((hi-c.open)/rg)*260;let yC=10+((hi-c.close)/rg)*260;let g=c.close>=c.open;x.strokeStyle=g?'green':'red';x.fillStyle=g?'green':'red';x.beginPath();x.moveTo(px,yH);x.lineTo(px,yL);x.stroke();x.fillRect(px-6,Math.min(yO,yC),12,Math.max(3,Math.abs(yC-yO)));});}
load('BTC');</script>"""

@app.route("/")
def home(): return render_template_string(HTML)
@app.route("/api/data")
def api(): return jsonify(get_data(request.args.get("coin","BTC")))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
