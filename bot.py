import threading, time, requests
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

COINS = ["BTC","ETH","SOL","DOGE","XRP","ADA","BNB"]
MAP = {"BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","DOGE":"DOGEUSDT","XRP":"XRPUSDT","ADA":"ADAUSDT","BNB":"BNBUSDT"}

# Binance ka public server - ye kabhi block nahi hota
BASE = "https://data-api.binance.vision"

data_store = {c: {"price":0, "change":0, "position":"--", "pnl":0, "history":[], "candles":[]} for c in COINS}

def get_price(c):
    try:
        r = requests.get(f"{BASE}/api/v3/ticker/24hr?symbol={MAP[c]}", timeout=10)
        j = r.json()
        return float(j['lastPrice']), float(j['priceChangePercent'])
    except:
        return 0, 0

def get_candles(c):
    try:
        r = requests.get(f"{BASE}/api/v3/klines?symbol={MAP[c]}&interval=1m&limit=50", timeout=10)
        j = r.json()
        out = []
        for k in j:
            out.append({"time": int(k[0]/1000), "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4])})
        return out
    except:
        return []

def bot_loop():
    while True:
        for c in COINS:
            try:
                p, ch = get_price(c)
                if p > 0:
                    d = data_store[c]
                    d["price"] = p
                    d["change"] = ch
                    d["candles"] = get_candles(c)
                    d["history"].append({"price": p, "time": time.time()})
                    if len(d["history"]) > 20: d["history"].pop(0)
                    # Simple Signal
                    if len(d["history"]) >= 2:
                        if p > d["history"][-2]["price"]:
                            d["position"] = "LONG 🟢"
                        else:
                            d["position"] = "SHORT 🔴"
                        d["pnl"] = round(((p - d["history"][0]["price"]) / d["history"][0]["price"] * 100), 2) if d["history"][0]["price"] else 0
            except Exception as e:
                print(e)
        time.sleep(15)

threading.Thread(target=bot_loop, daemon=True).start()

HTML = """
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Future Bot</title>
<script src="https://unpkg.com/lightweight-charts@4.1.0/dist/lightweight-charts.standalone.production.js"></script>
<style>body{font-family:Arial;background:#fff;color:#000;padding:10px}button{padding:10px 14px;margin:3px;font-size:16px;border:1px solid #000;background:#fff;border-radius:8px}button.active{background:#000;color:#fff}#chart{width:100%;height:380px;border:1px solid #ddd}</style>
</head><body>
<h2>Future Bot - CoinSwitch Style</h2>
<div id="btns"></div>
<h1 id="price">Loading...</h1>
<p id="info"></p>
<div id="chart"></div>
<h3>History</h3><div id="hist"></div>
<script>
let cur='BTC', chart, series;
function initChart(){
 chart = LightweightCharts.createChart(document.getElementById('chart'), {width: document.getElementById('chart').clientWidth, height: 380});
 series = chart.addCandlestickSeries();
}
initChart();
let coins=['BTC','ETH','SOL','DOGE','XRP','ADA','BNB'];
document.getElementById('btns').innerHTML = coins.map(c=>`<button onclick="setCoin('${c}')" id="b-${c}">${c}</button>`).join('');
function setCoin(c){cur=c; coins.forEach(x=>document.getElementById('b-'+x).classList.remove('active')); document.getElementById('b-'+c).classList.add('active'); load();}
document.getElementById('b-BTC').classList.add('active');
async function load(){
 try{
  let r = await fetch('/api/data?coin='+cur); let d = await r.json();
  document.getElementById('price').innerText = cur+' : $ '+d.price;
  document.getElementById('info').innerText = '1 Min: '+d.change+'% | Position: '+d.position+' | PNL: '+d.pnl+'%';
  if(d.candles && d.candles.length>0) series.setData(d.candles);
  document.getElementById('hist').innerHTML = d.history.map(h=>`$${h.price} <br>`).join('');
 }catch(e){}
}
setInterval(load, 5000); load();
</script></body></html>
"""

@app.route("/")
def home(): return render_template_string(HTML)

@app.route("/api/data")
def api():
    from flask import request
    c = request.args.get("coin","BTC")
    return jsonify(data_store.get(c, {}))

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
