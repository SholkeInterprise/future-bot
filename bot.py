import requests
from flask import Flask, jsonify, render_template_string, request
import os

app = Flask(__name__)

def get_data(coin):
    sym = coin + "USDT"
    try:
        r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}", timeout=10).json()
        d = r["result"]["list"][0]
        price = float(d["lastPrice"])
        ch = float(d["price24hPcnt"])*100
        r2 = requests.get(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval=1&limit=30", timeout=10).json()
        candles = []
        for k in reversed(r2["result"]["list"]):
            candles.append({"time":int(int(k[0])/1000),"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),"close":float(k[4])})
        return {"price":price,"change":round(ch,2),"position":"LONG 🟢" if ch>=0 else "SHORT 🔴","pnl":round(ch,2),"candles":candles,"history":[{"price":price}]}
    except Exception as e:
        return {"price":0,"change":0,"position":"Error: "+str(e),"pnl":0,"candles":[],"history":[]}

HTML = """<h2>Future Bot Test</h2><button onclick="load('BTC')">BTC</button><button onclick="load('ETH')">ETH</button><button onclick="load('SOL')">SOL</button><h1 id="p">Loading...</h1><canvas id="c" width="800" height="300" style="border:1px solid #000;width:100%"></canvas><script>
async function load(coin){let r=await fetch('/api/data?coin='+coin);let d=await r.json();document.getElementById('p').innerText=coin+' : $ '+d.price+' | '+d.position;let cv=document.getElementById('c');let x=cv.getContext('2d');x.clearRect(0,0,800,300);if(!d.candles.length){x.fillText('No candle',20,50);return;}let lo=Math.min(...d.candles.map(a=>a.low));let hi=Math.max(...d.candles.map(a=>a.high));let rg=hi-lo||1;let w=800/d.candles.length;d.candles.forEach((c,i)=>{let px=i*w+w/2;let yH=10+((hi-c.high)/rg)*260;let yL=10+((hi-c.low)/rg)*260;let yO=10+((hi-c.open)/rg)*260;let yC=10+((hi-c.close)/rg)*260;let g=c.close>=c.open;x.strokeStyle=g?'green':'red';x.fillStyle=g?'green':'red';x.beginPath();x.moveTo(px,yH);x.lineTo(px,yL);x.stroke();x.fillRect(px-6,Math.min(yO,yC),12,Math.max(3,Math.abs(yC-yO)));});}
load('BTC');</script>"""

@app.route("/")
def home(): return render_template_string(HTML)
@app.route("/api/data")
def api(): return jsonify(get_data(request.args.get("coin","BTC")))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
