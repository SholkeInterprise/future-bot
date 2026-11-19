import os
import time
import threading
import requests
import random
from collections import deque
from flask import Flask, jsonify, request
app = Flask(__name__)
WALLET_START = 10000.0
API_KEY = os.environ.get("COINSWITCH_API_KEY", "")
API_SECRET = os.environ.get("COINSWITCH_API_SECRET", "")
LIVE_MODE = bool(API_KEY and API_SECRET and os.environ.get("COINSWITCH_LIVE", "false").lower() == "true")
state = {"balance": 14911.48, "locked": 1000.0, "total": 15911.48, "open_trade": None, "history": [], "auto": True, "amount": 1000.0, "leverage": 10, "tp_pct": 1.0, "sl_pct": 0.5, "trail_pct": 0.5, "close_pct": 0.25, "min_range_pct": 0.40, "auto_pct": 0.5, "auto_coin": "BTC", "timeframe": "1m", "coin": "BTC", "total_trades": 0, "total_profit": 0.0, "mode": "REAL LIVE" if LIVE_MODE else "PAPER", "last_real_error": ""}
price_history = deque(maxlen=200)
for i in range(60):
    price_history.append(6612877 + random.randint(-5000, 5000))
def get_price(coin="BTC"):
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=" + coin + "USDT", timeout=5)
        p = float(r.json()["price"]) * 88000
        if coin == "BTC":
            price_history.append(p)
        return p
    except Exception:
        return price_history[-1] if price_history else 6612877.0
def get_24h_change():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=5)
        return float(r.json()["priceChangePercent"])
    except Exception:
        return -1.48
def calc_fee(a):
    return a * 0.01
def close_trade(reason):
    t = state["open_trade"]
    if not t:
        return
    price = get_price(t["coin"])
    entry = t["entry"]; amt = t["amount"]; lev = t["leverage"]; fee = calc_fee(amt)
    if t["side"] == "LONG":
        gross = (price - entry) / entry * 100 * lev * amt / 100
    else:
        gross = (entry - price) / entry * 100 * lev * amt / 100
    net = gross - fee
    state["balance"] += amt + net
    state["locked"] = 0.0
    state["total"] = state["balance"] + state["locked"]
    state["total_profit"] += net
    state["history"].insert(0, {"coin": t["coin"], "side": t["side"], "reason": reason, "entry": round(entry, 2), "exit": round(price, 2), "amount": amt, "leverage": lev, "gross": round(gross, 2), "fee": round(fee, 2), "net": round(net, 2), "high": round(t.get("high", entry), 2), "low": round(t.get("low", entry), 2), "time": time.strftime("%H:%M:%S")})
    state["open_trade"] = None
def open_trade(side, coin="BTC"):
    if state["open_trade"]:
        return {"error": "Trade already open"}
    amt = float(state["amount"])
    if amt > state["balance"]:
        return {"error": "Low balance"}
    price = get_price(coin); fee = calc_fee(amt)
    state["balance"] -= amt; state["locked"] = amt; state["total"] = state["balance"] + state["locked"]
    state["open_trade"] = {"side": side, "coin": coin, "entry": price, "amount": amt, "leverage": int(state["leverage"]), "fee": fee, "high": price, "low": price, "entry_high": price, "time": time.strftime("%H:%M:%S")}
    state["total_trades"] += 1
    return {"success": True}
def auto_loop():
    while True:
        try:
            if state["auto"]:
                coin = state["auto_coin"]; price = get_price(coin)
                if state["open_trade"]:
                    t = state["open_trade"]
                    if price > t["high"]: t["high"] = price
                    if price < t["low"]: t["low"] = price
                    if t["side"] == "LONG":
                        close_target = t["high"] * (1 - float(state["close_pct"]) / 100)
                        tp_price = t["entry"] * (1 + float(state["tp_pct"]) / 100)
                        sl_price = t["entry"] * (1 - float(state["sl_pct"]) / 100)
                        if price >= tp_price and price <= close_target: close_trade("TP Hit")
                        elif price <= sl_price: close_trade("SL Hit")
                        elif price <= close_target and price > t["entry"]:
                            rng = (t["high"] - t["low"]) / t["entry"] * 100
                            if rng >= float(state["min_range_pct"]): close_trade("HIGH-LOW Close")
                    else:
                        close_target = t["low"] * (1 + float(state["close_pct"]) / 100)
                        tp_price = t["entry"] * (1 - float(state["tp_pct"]) / 100)
                        sl_price = t["entry"] * (1 + float(state["sl_pct"]) / 100)
                        if price <= tp_price and price >= close_target: close_trade("TP Hit")
                        elif price >= sl_price: close_trade("SL Hit")
                        elif price >= close_target and price < t["entry"]:
                            rng = (t["high"] - t["low"]) / t["entry"] * 100
                            if rng >= float(state["min_range_pct"]): close_trade("HIGH-LOW Close")
                else:
                    prices = list(price_history)
                    if len(prices) > 10:
                        last = prices[-1]; prev = prices[-5]
                        rng = (max(prices[-10:]) - min(prices[-10:])) / last * 100
                        if rng >= float(state["min_range_pct"]):
                            if last > prev: open_trade("LONG", coin)
                            else: open_trade("SHORT", coin)
            time.sleep(5)
        except Exception as e:
            print("Loop error", e); time.sleep(5)
threading.Thread(target=auto_loop, daemon=True).start()
@app.route("/")
def home():
    coin = state["auto_coin"]; price = get_price(coin); chg = get_24h_change(); t = state["open_trade"]
    live_net = 0; live_gross = 0; trailing_high = price; trailing_low = price; close_target = price
    if t:
        if t["side"] == "LONG": live_gross = (price - t["entry"]) / t["entry"] * 100 * t["leverage"] * t["amount"] / 100
        else: live_gross = (t["entry"] - price) / t["entry"] * 100 * t["leverage"] * t["amount"] / 100
        live_net = live_gross - t["fee"]; trailing_high = t["high"]; trailing_low = t["low"]
        if t["side"] == "LONG": close_target = t["high"] * (1 - float(state["close_pct"]) / 100)
        else: close_target = t["low"] * (1 + float(state["close_pct"]) / 100)
    tp_price = price * (1 + float(state["tp_pct"]) / 100); sl_price = price * (1 - float(state["sl_pct"]) / 100)
    if t:
        if t["side"] == "LONG": tp_price = t["entry"] * (1 + float(state["tp_pct"]) / 100); sl_price = t["entry"] * (1 - float(state["sl_pct"]) / 100)
        else: tp_price = t["entry"] * (1 - float(state["tp_pct"]) / 100); sl_price = t["entry"] * (1 + float(state["sl_pct"]) / 100)
    tp_gross = float(state["amount"]) * float(state["tp_pct"]) * float(state["leverage"]) / 100
    tp_fee = calc_fee(float(state["amount"])); tp_net = tp_gross - tp_fee
    sl_gross = float(state["amount"]) * float(state["sl_pct"]) * float(state["leverage"]) / 100
    sl_net = sl_gross + tp_fee
    hist_html = ""
    for h in state["history"][:20]:
        col = "green" if h["net"] >= 0 else "red"
        hist_html += '<div style="border-bottom:1px solid #eee;padding:6px;font-size:12px;text-align:center;">' + str(h["coin"]) + ' ' + str(h["side"]) + ' CLOSE | ' + str(h["reason"]) + ' | High ' + str(h["high"]) + ' Low ' + str(h["low"]) + '<br>Entry ' + str(h["entry"]) + ' Exit ' + str(h["exit"]) + ' | Amt ' + str(h["amount"]) + ' Lev ' + str(h["leverage"]) + 'x<br><span style="color:' + col + ';font-weight:bold;">Gross ' + str(h["gross"]) + ' - Fee ' + str(h["fee"]) + ' = Net ' + str(h["net"]) + '</span></div>'
    if not hist_html: hist_html = "<p style='text-align:center;color:#888;'>No History Yet</p>"
    signal = "SHORT" if chg < 0 else "LONG"
    if t: open_html = "<p style='text-align:center;'>Open: " + coin + " " + str(t["side"]) + " @ " + str(round(t["entry"], 2)) + " | Amt " + str(t["amount"]) + " Lev " + str(t["leverage"]) + "x | Fee " + str(round(t["fee"], 2)) + "</p><p style='text-align:center;'>Trailing: High " + str(round(trailing_high, 2)) + " | Entry High " + str(round(t["entry"], 2)) + " | Low " + str(round(trailing_low, 2)) + " | Close Target = High - " + str(state["close_pct"]) + "% = " + str(round(close_target, 2)) + "</p><p style='text-align:center;'>Live PnL Net (Fee kat ke): <b>" + str(round(live_net, 2)) + " (" + str(round(live_net, 3)) + "%)</b> | Gross " + str(round(live_gross, 2)) + "</p>"
    else: open_html = "<p style='text-align:center;'>No Open Trade</p>"
    coins_btn = ""
    for c in ["BTC", "ETH", "SOL", "DOGE", "XRP", "ADA", "BNB"]:
        bg = "#007bff" if c == coin else "#e0e0e0"; col2 = "white" if c == coin else "black"
        coins_btn += "<button onclick=\"setCoin('" + c + "')\" style=\"padding:6px 10px;background:" + bg + ";color:" + col2 + ";border:none;border-radius:5px;\">" + c + "</button> "
    usd_price = price / 88000
    auto_sel_on = "selected" if state["auto"] else ""; auto_sel_off = "selected" if not state["auto"] else ""; auto_txt = "ON" if state["auto"] else "OFF"
    mode_txt = "REAL LIVE" if LIVE_MODE else "PAPER"
    html = """
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Future Bot</title></head>
    <body style="font-family:Arial;padding:10px;background:#fff;margin:0;">
    <h2 style="text-align:center;margin:5px 0;">🚀 Future Bot - 1m HIGH-LOW FIXED ✅ + Fee Cover + 24x7 AUTO 🤖</h2>
    <div style="text-align:center;margin:5px 0;">""" + coins_btn + """</div>
    <h3 style="text-align:center;">""" + coin + """ : ₹<span id="livePrice">""" + str(round(price, 2)) + """</span> | <span id="liveSignal">""" + signal + """</span> | 24h <span id="liveChg">""" + str(round(chg, 2)) + """%</span></h3>
    <p style="text-align:center;font-size:12px;">🔴 LIVE pe SHORT | Low pe LONG | Close = 0.25% andar | Fee +10 kat ke Net +15 Target | Chhoti Candle Skip</p>
    <p style="text-align:center;background:#fff8e1;padding:8px;border-radius:8px;">💰 Available Wallet: ₹""" + str(round(state["balance"], 2)) + """<br>Locked: ₹""" + str(round(state["locked"], 2)) + """ | Total: ₹""" + str(round(state["total"], 2)) + """</p>
    """ + open_html + """
    <div style="background:#f9f9f9;padding:10px;border-radius:8px;"><b>⚙️ SETTING</b><br>
    Leverage: <select id="lev"><option value="10" selected>10</option><option value="5">5</option><option value="20">20</option></select> x
    Amount ₹ <input id="amt" type="number" value=\"""" + str(state["amount"]) + """\" style="width:70px;">
    TP %: <input id="tp" type="number" step="0.1" value=\"""" + str(state["tp_pct"]) + """\" style="width:50px;">
    SL %: <input id="sl" type="number" step="0.1" value=\"""" + str(state["sl_pct"]) + """\" style="width:50px;">
    Trail %: <input id="trail" type="number" step="0.1" value=\"""" + str(state["trail_pct"]) + """\" style="width:50px;">
    </div>
    <div style="background:#f9f9f9;padding:10px;border-radius:8px;margin-top:8px;">
    🤖 AUTO: <select id="auto"><option value="ON" """ + auto_sel_on + """>ON</option><option value="OFF" """ + auto_sel_off + """>OFF</option></select>
    Auto % <input id="autopct" type="number" value=\"""" + str(state["auto_pct"]) + """\" style="width:50px;"> %
    Auto Coin: <select id="autocoin"><option>BTC</option><option>ETH</option><option>SOL</option><option>DOGE</option></select>
    <p style="margin:5px 0;"><b>AUTO """ + auto_txt + """ - """ + mode_txt + """</b></p></div>
    <div style="background:#fff3cd;padding:10px;border-radius:8px;margin-top:8px;"><b>📌 HIGH-LOW FIXED LOGIC - Live 1m Candle</b><br>
    Close %: <input id="close" type="number" step="0.05" value=\"""" + str(state["close_pct"]) + """\" style="width:50px;">
    Min Candle Range %: <input id="minrange" type="number" step="0.05" value=\"""" + str(state["min_range_pct"]) + """\" style="width:50px;"><br>
    <small>SHORT - High pe khulega, Low - 0.25% pe band. LONG - Low pe khulega, High - 0.25% pe band. Fee +10 kat ke Net +15 bachega. Candle Range 0.40% se chhota hua to trade nahi khulega.</small></div>
    <div style="background:#e8f5e9;padding:8px;border-radius:8px;margin-top:8px;">✅ TP Preview - Trade ₹""" + str(state["amount"]) + """ se<br>TP """ + str(state["tp_pct"]) + """% - Price """ + str(round(tp_price, 2)) + """ | Gross +₹""" + str(round(tp_gross, 2)) + """ - Fee ₹""" + str(round(tp_fee, 2)) + """ = Net +₹""" + str(round(tp_net, 2)) + """</div>
    <div style="background:#ffebee;padding:8px;border-radius:8px;margin-top:5px;">❌ SL Preview - Entry """ + str(round(price, 2)) + """ (""" + (str(t["side"]) if t else "LONG") + """) se<br>SL """ + str(state["sl_pct"]) + """% - Price """ + str(round(sl_price, 2)) + """ | Gross -₹""" + str(round(sl_gross, 2)) + """ - Fee ₹""" + str(round(tp_fee, 2)) + """ = Net -₹""" + str(round(sl_net, 2)) + """</div>
    <div style="display:flex;gap:8px;margin-top:10px;">
    <button onclick="trade('LONG')" style="flex:1;padding:12px;background:green;color:white;border:none;border-radius:8px;font-weight:bold;">LONG Lagao</button>
    <button onclick="trade('SHORT')" style="flex:1;padding:12px;background:red;color:white;border:none;border-radius:8px;font-weight:bold;">SHORT Lagao</button>
    <button onclick="trade('CLOSE')" style="flex:1;padding:12px;background:#333;color:white;border:none;border-radius:8px;">Band Karo</button>
    <button onclick="resetBal()" style="flex:1;padding:12px;background:#eee;border:none;border-radius:8px;">Wallet Reset ₹10,000</button>
    </div>
    <div style="text-align:center;margin-top:10px;">⏰ TIME FRAME: <button onclick="setTF('1m')">1m</button> <button onclick="setTF('5m')">5m</button> <button onclick="setTF('15m')">15m</button> <button onclick="setTF('1h')">1h</button> <button onclick="setTF('4h')">4h</button> <button onclick="setTF('1D')">1D</button></div>
    <div style="text-align:center;margin-top:5px;">📏 LINE TOOL: <button onclick="setLine('Normal')">Normal</button> <button onclick="setLine('H')">H-Line (Price)</button> <button onclick="setLine('V')">V-Line (Time)</button> <button onclick="clearLines()">Lines Clear</button></div>
    <div style="background:#fff;border:1px solid #ddd;border-radius:8px;margin-top:10px;overflow:hidden;">
    <div style="display:flex;justify-content:space-between;align-items:center;background:#f5f5f5;padding:8px 10px;">
    <div><b id="tvSymbol">""" + coin + """/USDT</b> <span style="color:#888;font-size:12px;">1m</span> <span style="background:#2962ff;color:white;padding:2px 6px;border-radius:3px;font-size:11px;">Chart</span></div>
    <div style="text-align:right;"><b id="tvInrPrice" style="color:#26a65b;font-size:15px;">Rs.""" + str(round(price)) + """</b><br><span id="tvUsdPrice" style="color:#888;font-size:11px;">$""" + str(round(usd_price, 2)) + """</span></div>
    </div>
    <div style="position:relative;"><canvas id="chart" width="360" height="280" style="width:100%;background:#fff;display:block;"></canvas>
    <div id="tvPriceTag" style="position:absolute;right:0;top:10px;background:#2962ff;color:white;font-size:11px;padding:2px 5px;border-radius:3px;">Rs.""" + str(round(price)) + """</div></div>
    <div style="display:flex;justify-content:space-between;color:#888;font-size:10px;padding:4px 10px;"><span>07:00</span><span>08:00</span><span>09:00</span><span>10:00</span><span>11:00</span></div>
    <p style="text-align:center;color:#888;font-size:11px;margin:0;padding:4px;">Green = Upar | Red = Neeche | Blue = MA7 | Orange = MA25 | Neeche Volume | INR Price Right Side</p>
    </div>
    <div style="text-align:center;margin-top:10px;"><h3>History</h3>""" + hist_html + """</div>
    <script>
    var curCoin='""" + coin + """';
    var curTF='1m';
    var candles=[];
    var lineMode='Normal'; var hLines=[]; var vLines=[];
    var livePrice=""" + str(price) + """;
    var priceHist=[];
    function loadCandles(){
        var sym=curCoin+'USDT';
        var interval=curTF;
        if(interval=='1D') interval='1d';
        var urls=[
            'https://api.binance.us/api/v3/klines?symbol='+sym+'&interval='+interval+'&limit=30',
            'https://api.binance.com/api/v3/klines?symbol='+sym+'&interval='+interval+'&limit=30'
        ];
        function tryUrl(i){
            if(i>=urls.length){ makeCandlesFromLive(); return; }
            fetch(urls[i]).then(r=>{ if(!r.ok) throw new Error('blocked'); return r.json(); }).then(d=>{
                if(!d || !d.length) throw new Error('empty');
                candles=d.map(k=>({o:parseFloat(k[1])*88000, h:parseFloat(k[2])*88000, l:parseFloat(k[3])*88000, c:parseFloat(k[4])*88000, vol:parseFloat(k[5])}));
                if(candles.length>0) candles[candles.length-1].c=livePrice;
                drawChart();
            }).catch(e=>{ tryUrl(i+1); });
        }
        tryUrl(0);
    }
    function makeCandlesFromLive(){
        if(priceHist.length<8){
            var base=livePrice; candles=[];
            for(var i=0;i<30;i++){
                var o=base + (Math.random()-0.5)*8000;
                var c=base + (Math.random()-0.5)*8000;
                var h=Math.max(o,c)+Math.random()*4000;
                var l=Math.min(o,c)-Math.random()*4000;
                candles.push({o:o,h:h,l:l,c:c,vol:100+Math.random()*500});
                base=c;
            }
        } else {
            var lastHist=priceHist.slice(-120); candles=[];
            for(var i=0;i<lastHist.length;i+=4){
                var chunk=lastHist.slice(i,i+4);
                if(chunk.length<2) continue;
                var o=chunk[0]; var cc=chunk[chunk.length-1];
                var h=Math.max.apply(null,chunk); var l=Math.min.apply(null,chunk);
                candles.push({o:o,h:h,l:l,c:cc,vol:100+Math.abs(cc-o)});
            }
            if(candles.length>30) candles=candles.slice(-30);
        }
        drawChart();
    }
    loadCandles();
    setInterval(loadCandles, 30000);
    function drawChart(){
        if(candles.length==0){ makeCandlesFromLive(); return; }
        var c=document.getElementById('chart'); var ctx=c.getContext('2d');
        var W=360, H=280, priceH=210, volH=50;
        var all=[]; for(var k=0;k<candles.length;k++){ all.push(candles[k].h); all.push(candles[k].l); }
        var min=Math.min.apply(null,all); var max=Math.max.apply(null,all);
        var range=(max-min)||1;
        min=min-range*0.15; max=max+range*0.15; range=(max-min)||1;
        var maxVol=0; for(var v=0;v<candles.length;v++) if(candles[v].vol>maxVol) maxVol=candles[v].vol;
        ctx.clearRect(0,0,W,H); ctx.fillStyle='#fff'; ctx.fillRect(0,0,W,H);
        ctx.strokeStyle='#f0f0f0'; ctx.lineWidth=1;
        for(var g=0;g<6;g++){ ctx.beginPath(); ctx.moveTo(0,g*35); ctx.lineTo(W-60,g*35); ctx.stroke(); }
        ctx.fillStyle='#888'; ctx.font='9px Arial'; ctx.textAlign='left';
        for(var p=0;p<6;p++){ var pv=max - (p/5)*range; ctx.fillText('Rs.'+pv.toLocaleString('en-IN',{maximumFractionDigits:0}), W-58, p*35+12); }
        var cw=(W-60)/30; var bw=Math.max(5, cw*0.65);
        var startX=(W-60) - candles.length*cw;
        for(var i=0;i<candles.length;i++){
            var cd=candles[i]; var isGreen=cd.c>=cd.o; var col=isGreen?'rgba(38,166,91,0.6)':'rgba(231,76,60,0.6)';
            var x=startX+i*cw+cw/2; var vh=(cd.vol/maxVol)*volH;
            ctx.fillStyle=col; ctx.fillRect(x-bw/2, priceH+10+(volH-vh), bw, vh);
        }
        for(var i=0;i<candles.length;i++){
            var cd=candles[i]; var x=startX+i*cw+cw/2;
            var yH=((max-cd.h)/range)*priceH; var yL=((max-cd.l)/range)*priceH;
            var yO=((max-cd.o)/range)*priceH; var yC=((max-cd.c)/range)*priceH;
            var isGreen=cd.c>=cd.o; var col=isGreen?'#26a65b':'#ef5350';
            ctx.strokeStyle=col; ctx.lineWidth=1.2; ctx.beginPath(); ctx.moveTo(x,yH); ctx.lineTo(x,yL); ctx.stroke();
            var top=Math.min(yO,yC); var hgt=Math.max(4, Math.abs(yC-yO));
            ctx.fillStyle=col; ctx.fillRect(x-bw/2, top, bw, hgt);
            if(i==candles.length-1){ ctx.beginPath(); ctx.arc(x,yC,3,0,2*Math.PI); ctx.fillStyle='black'; ct
