import requests
from flask import Flask, jsonify, render_template_string, request
import os

app = Flask(__name__)
COINS = {"BTC":"BTC","ETH":"ETH","SOL":"SOL","DOGE":"DOGE","XRP":"XRP","ADA":"ADA","BNB":"BNB"}
HISTORY = []

def get_data(coin):
    sym = coin + "USDT"
    try:
        r = requests.get(f"https://data-api.binance.vision/api/v3/ticker/24hr?symbol={sym}", timeout=10, headers={"User-Agent":"Mozilla/5.0"}).json()
        price = float(r["lastPrice"]); ch = float(r["priceChangePercent"])
        r2 = requests.get(f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=1m&limit=40", timeout=10, headers={"User-Agent":"Mozilla/5.0"}).json()
        candles = [{"time":int(int(k[0])/1000),"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),"close":float(k[4])} for k in r2]
        pos = "LONG 🟢" if ch>=0 else "SHORT 🔴"
        # History me add karo agar naya signal hai
        if not HISTORY or HISTORY[-1].split("|")[0].strip() != f"{coin} {pos}":
            HISTORY.append(f"{coin} {pos} | $ {price} | {ch:.2f}%")
            if len(HISTORY)>20: HISTORY.pop(0)
        return {"price":price,"change":round(ch,2),"position":pos,"pnl":round(ch,2),"candles":candles,"history":HISTORY[::-1],"src":"Binance-Vision"}
    except Exception as e:
        return {"price":0,"change":0,"position":"Error: "+str(e)[:80],"pnl":0,"candles":[],"history":HISTORY[::-1],"src":"none"}

HTML = """<style>body{font-family:Arial;text-align:center}button{padding:10px 14px;margin:3px;font-size:16px;border-radius:8px;border:1px solid #000}h1{font-size:28px}</style><h2>🚀 Future Bot - Final</h2><div><button onclick="load('BTC')">BTC</button><button onclick="load('ETH')">ETH</button><button onclick="load('SOL')">SOL</button><button onclick="load('DOGE')">DOGE</button><button onclick="load('XRP')">XRP</button><button onclick="load('ADA')">ADA</button><button onclick="load('BNB')">BNB</button></div><h1 id="p">Loading...</h1><p id="src"></p><canvas id="c" width="800" height="320" style="border:1px solid #000;width:100%;max-width:800px"></canvas><h3>📜 History</h3><div id="h"></div><script>let cur='BTC';async function load(coin){cur=coin;fetchData();}async function fetchData(){let r=await fetch('/api/data?coin='+cur);let d=await r.json();document.getElementById('p').innerText=cur+' : $ '+d.price+' | '+d.position+' | '+d.change+'%';document.getElementById('src').innerText='Source: '+d.src+' | Auto Refresh 5 sec';document.getElementById('h').innerHTML=d.history.map(x=>'<div>'+x+'</div>').join('');let cv=document.getElementById('c');let x=cv.getContext('2d');x.clearRect(0,0,800,320);if(!d.candles.length){x.fillText('No candle',20,50);return;}let lo=Math.min(...d.candles.map(a=>a.low));let hi=Math.max(...d.candles.map(a=>a.high));let rg=hi-lo||1;let w=800/d.candles.length;d.candles.forEach((c,i)=>{let px=i*w+w/2;let yH=10+((hi-c.high)/rg)*280;let yL=10+((hi-c.low)/rg)*280;let yO=10+((hi-c.open)/rg)*280;let yC=10+((hi-c.close)/rg)*280;let g=c.close>=c.open;x.strokeStyle=g?'green':'red';x.fillStyle=g?'green':'red';x.beginPath();x.moveTo(px,yH);x.lineTo(px,yL);x.stroke();x.fillRect(px-6,Math.min(yO,yC),12,Math.max(3,Math.abs(yC-yO)));});}setInterval(fetchData,5000);fetchData();</script>"""

@app.route("/")
def home(): return render_template_string(HTML)
@app.route("/api/data")
def api(): return jsonify(get_data(request.args.get("coin","BTC")))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
