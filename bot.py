import time, threading, requests
from flask import Flask, jsonify, render_template_string, request
import os

app = Flask(__name__)

COINS = {"bitcoin":"BTC","ethereum":"ETH","solana":"SOL","dogecoin":"DOGE","ripple":"XRP","cardano":"ADA","binancecoin":"BNB"}
selected_coin = "bitcoin"
prices = []
history = []
position = {"side":"Nahi","entry":0,"pnl":0,"coin":""}
current_price = 0
last_change = 0

def get_price(cid):
    try:
        r = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={cid}&vs_currencies=usd", timeout=10)
        return r.json()[cid]["usd"]
    except: return None

def get_candles(cid):
    try:
        r = requests.get(f"https://api.coingecko.com/api/v3/coins/{cid}/ohlc?vs_currency=usd&days=1", timeout=10)
        out=[]
        for t,o,h,l,c in r.json()[-60:]:
            out.append({"time":int(t/1000),"open":o,"high":h,"low":l,"close":c})
        return out
    except: return []

def bot_loop():
    global current_price, last_change, position
    prev=None; last_coin=selected_coin
    while True:
        if selected_coin!=last_coin:
            prices.clear(); position={"side":"Nahi","entry":0,"pnl":0,"coin":""}; prev=None; last_coin=selected_coin
        p=get_price(selected_coin)
        if p:
            current_price=p; prices.append(p)
            if len(prices)>100: prices.pop(0)
            if prev:
                last_change=((p-prev)/prev)*100
                cn=COINS[selected_coin]
                if position["side"]=="Nahi":
                    if last_change>=0.5:
                        position={"side":"LONG","entry":p,"pnl":0,"coin":cn}; history.append(f"{cn} LONG @{p} | {last_change:.2f}%")
                    elif last_change<=-0.5:
                        position={"side":"SHORT","entry":p,"pnl":0,"coin":cn}; history.append(f"{cn} SHORT @{p} | {last_change:.2f}%")
                else:
                    if position["side"]=="LONG":
                        pnl=((p-position["entry"])/position["entry"])*100; position["pnl"]=pnl
                        if pnl>=0.3 or pnl<=-0.3:
                            history.append(f"{cn} LONG band @{p} | PNL {pnl:.2f}%"); position={"side":"Nahi","entry":0,"pnl":0,"coin":""}
                    if position["side"]=="SHORT":
                        pnl=((position["entry"]-p)/position["entry"])*100; position["pnl"]=pnl
                        if pnl>=0.3 or pnl<=-0.3:
                            history.append(f"{cn} SHORT band @{p} | PNL {pnl:.2f}%"); position={"side":"Nahi","entry":0,"pnl":0,"coin":""}
            prev=p
        time.sleep(60)

@app.route("/")
def home():
    btns="".join([f'<button onclick="selectCoin(\'{k}\')" style="padding:10px;margin:4px;font-size:16px">{v}</button>' for k,v in COINS.items()])
    return render_template_string(f"""
    <h2>Future Bot - CoinSwitch Style</h2><div>{btns}</div><hr>
    <h1><span id="coin">BTC</span> : $ <span id="price">--</span></h1>
    <h3>1 Min: <span id="change">--</span>% | Position: <span id="pos">--</span> | PNL: <span id="pnl">--</span>%</h3>
    <div id="candle-chart" style="height:380px"></div>
    <h3>History</h3><ul id="hist"></ul>
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
    <script>
    const chart=LightweightCharts.createChart(document.getElementById('candle-chart'),{{width:380,height:380}});
    const cs=chart.addCandlestickSeries();
    async function sel(c){{await fetch('/api/select?coin='+c); loadC();}}
    async function selectCoin(c){{await sel(c);}}
    async function loadC(){{let r=await fetch('/api/candles'); let d=await r.json(); cs.setData(d);}}
    async function load(){{let r=await fetch('/api/data'); let d=await r.json(); document.getElementById('coin').innerText=d.coin; document.getElementById('price').innerText=d.price; document.getElementById('change').innerText=d.change.toFixed(2); document.getElementById('pos').innerText=d.position.side+' '+d.position.coin; document.getElementById('pnl').innerText=d.position.pnl.toFixed(2); document.getElementById('hist').innerHTML=d.history.map(h=>'<li>'+h+'</li>').join('');}}
    setInterval(load,5000); setInterval(loadC,60000); load(); loadC();
    </script>
    """)

@app.route("/api/select")
def sel():
    global selected_coin
    c=request.args.get("coin")
    if c in COINS: selected_coin=c
    return jsonify({"ok":True})

@app.route("/api/candles")
def can(): return jsonify(get_candles(selected_coin))

@app.route("/api/data")
def api(): return jsonify({"price":current_price,"change":last_change,"coin":COINS[selected_coin],"position":position,"prices":prices,"history":history[-20:]})

threading.Thread(target=bot_loop, daemon=True).start()
if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
