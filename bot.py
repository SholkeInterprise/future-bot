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
LIVE_MODE = False
if API_KEY and API_SECRET:
    if os.environ.get("COINSWITCH_LIVE", "false").lower() == "true":
        LIVE_MODE = True
state = {}
state["balance"] = 14911.48
state["locked"] = 1000.0
state["total"] = 15911.48
state["open_trade"] = None
state["history"] = []
state["auto"] = True
state["amount"] = 1000.0
state["leverage"] = 10
state["tp_pct"] = 1.0
state["sl_pct"] = 0.5
state["trail_pct"] = 0.5
state["close_pct"] = 0.25
state["min_range_pct"] = 0.40
state["auto_pct"] = 0.5
state["auto_coin"] = "BTC"
state["timeframe"] = "1m"
state["coin"] = "BTC"
state["total_trades"] = 0
state["total_profit"] = 0.0
state["mode"] = "PAPER"
if LIVE_MODE:
    state["mode"] = "REAL LIVE"
price_history = deque(maxlen=200)
for i in range(60):
    price_history.append(6612877 + random.randint(-5000, 5000))
def get_price(coin="BTC"):
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=" + coin + "USDT"
        r = requests.get(url, timeout=5)
        p = float(r.json()["price"]) * 88000
        if coin == "BTC":
            price_history.append(p)
        return p
    except Exception:
        return price_history[-1]
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
    entry = t["entry"]
    amt = t["amount"]
    lev = t["leverage"]
    fee = calc_fee(amt)
    if t["side"] == "LONG":
        gross = (price - entry) / entry * 100 * lev * amt / 100
    else:
        gross = (entry - price) / entry * 100 * lev * amt / 100
    net = gross - fee
    state["balance"] += amt + net
    state["locked"] = 0.0
    state["total"] = state["balance"] + state["locked"]
    state["total_profit"] += net
    h = {}
    h["coin"] = t["coin"]
    h["side"] = t["side"]
    h["reason"] = reason
    h["entry"] = round(entry, 2)
    h["exit"] = round(price, 2)
    h["amount"] = amt
    h["leverage"] = lev
    h["gross"] = round(gross, 2)
    h["fee"] = round(fee, 2)
    h["net"] = round(net, 2)
    h["high"] = round(t.get("high", entry), 2)
    h["low"] = round(t.get("low", entry), 2)
    h["time"] = time.strftime("%H:%M:%S")
    state["history"].insert(0, h)
    state["open_trade"] = None
def open_trade(side, coin="BTC"):
    if state["open_trade"]:
        return {"error": "Trade already open"}
    amt = float(state["amount"])
    if amt > state["balance"]:
        return {"error": "Low balance"}
    price = get_price(coin)
    fee = calc_fee(amt)
    state["balance"] -= amt
    state["locked"] = amt
    state["total"] = state["balance"] + state["locked"]
    nt = {}
    nt["side"] = side
    nt["coin"] = coin
    nt["entry"] = price
    nt["amount"] = amt
    nt["leverage"] = int(state["leverage"])
    nt["fee"] = fee
    nt["high"] = price
    nt["low"] = price
    nt["entry_high"] = price
    nt["time"] = time.strftime("%H:%M:%S")
    state["open_trade"] = nt
    state["total_trades"] += 1
    return {"success": True}
    def auto_loop():
    while True:
        try:
            if state["auto"]:
                coin = state["auto_coin"]
                price = get_price(coin)
                if state["open_trade"]:
                    t = state["open_trade"]
                    if price > t["high"]:
                        t["high"] = price
                    if price < t["low"]:
                        t["low"] = price
                    if t["side"] == "LONG":
                        ct = t["high"] * (1 - float(state["close_pct"]) / 100)
                        tp = t["entry"] * (1 + float(state["tp_pct"]) / 100)
                        sl = t["entry"] * (1 - float(state["sl_pct"]) / 100)
                        if price >= tp and price <= ct:
                            close_trade("TP Hit")
                        elif price <= sl:
                            close_trade("SL Hit")
                        elif price <= ct and price > t["entry"]:
                            rng = (t["high"] - t["low"]) / t["entry"] * 100
                            if rng >= float(state["min_range_pct"]):
                                close_trade("HIGH-LOW Close")
                    else:
                        ct = t["low"] * (1 + float(state["close_pct"]) / 100)
                        tp = t["entry"] * (1 - float(state["tp_pct"]) / 100)
                        sl = t["entry"] * (1 + float(state["sl_pct"]) / 100)
                        if price <= tp and price >= ct:
                            close_trade("TP Hit")
                        elif price >= sl:
                            close_trade("SL Hit")
                        elif price >= ct and price < t["entry"]:
                            rng = (t["high"] - t["low"]) / t["entry"] * 100
                            if rng >= float(state["min_range_pct"]):
                                close_trade("HIGH-LOW Close")
                else:
                    prices = list(price_history)
                    if len(prices) > 10:
                        last = prices[-1]
                        prev = prices[-5]
                        rng = (max(prices[-10:]) - min(prices[-10:])) / last * 100
                        if rng >= float(state["min_range_pct"]):
                            if last > prev:
                                open_trade("LONG", coin)
                            else:
                                open_trade("SHORT", coin)
            time.sleep(5)
        except Exception as e:
            print("Loop error", e)
            time.sleep(5)
threading.Thread(target=auto_loop, daemon=True).start()
@app.route("/")
def home():
    coin = state["auto_coin"]
    price = get_price(coin)
    chg = get_24h_change()
    t = state["open_trade"]
    live_net = 0
    live_gross = 0
    th = price
    tl = price
    ct = price
    if t:
        if t["side"] == "LONG":
            live_gross = (price - t["entry"]) / t["entry"] * 100 * t["leverage"] * t["amount"] / 100
        else:
            live_gross = (t["entry"] - price) / t["entry"] * 100 * t["leverage"] * t["amount"] / 100
        live_net = live_gross - t["fee"]
        th = t["high"]
        tl = t["low"]
        if t["side"] == "LONG":
            ct = t["high"] * (1 - float(state["close_pct"]) / 100)
        else:
            ct = t["low"] * (1 + float(state["close_pct"]) / 100)
    tp_price = price * (1 + float(state["tp_pct"]) / 100)
    sl_price = price * (1 - float(state["sl_pct"]) / 100)
    if t:
        if t["side"] == "LONG":
            tp_price = t["entry"] * (1 + float(state["tp_pct"]) / 100)
            sl_price = t["entry"] * (1 - float(state["sl_pct"]) / 100)
        else:
            tp_price = t["entry"] * (1 - float(state["tp_pct"]) / 100)
            sl_price = t["entry"] * (1 + float(state["sl_pct"]) / 100)
    tp_gross = float(state["amount"]) * float(state["tp_pct"]) * float(state["leverage"]) / 100
    tp_fee = calc_fee(float(state["amount"]))
    tp_net = tp_gross - tp_fee
    sl_gross = float(state["amount"]) * float(state["sl_pct"]) * float(state["leverage"]) / 100
    sl_net = sl_gross + tp_fee
    hist_html = ""
    for h in state["history"][:20]:
        col = "green" if h["net"] >= 0 else "red"
        hist_html += "<div style='border-bottom:1px solid #eee;padding:6px;font-size:12px;text-align:center;'>"
        hist_html += str(h["coin"]) + " " + str(h["side"]) + " | " + str(h["reason"])
        hist_html += "<br>Entry " + str(h["entry"]) + " Exit " + str(h["exit"])
        hist_html += "<br><span style='color:" + col + ";font-weight:bold;'>Net " + str(h["net"]) + "</span></div>"
    if hist_html == "":
        hist_html = "<p style='text-align:center;color:#888;'>No History Yet</p>"
    signal = "SHORT" if chg < 0 else "LONG"
    if t:
        open_html = "<p style='text-align:center;'>Open: " + coin + " " + str(t["side"])
        open_html += " @ " + str(round(t["entry"], 2))
        open_html += " | Amt " + str(t["amount"]) + " Lev " + str(t["leverage"]) + "x"
        open_html += "</p><p style='text-align:center;'>High " + str(round(th, 2))
        open_html += " | Low " + str(round(tl, 2))
        open_html += " | Close Target " + str(round(ct, 2))
        open_html += "</p><p style='text-align:center;'>Live PnL Net: <b>" + str(round(live_net, 2)) + "</b></p>"
    else:
        open_html = "<p style='text-align:center;'>No Open Trade</p>"
    coins_btn = ""
    for c in ["BTC", "ETH", "SOL", "DOGE", "XRP", "ADA", "BNB"]:
        bg = "#007bff" if c == coin else "#e0e0e0"
        col2 = "white" if c == coin else "black"
        coins_btn += "<button onclick=\"setCoin('" + c + "')\" "
        coins_btn += "style=\"padding:6px 10px;background:" + bg + ";color:" + col2 + ";border:none;border-radius:5px;\">"
        coins_btn += c + "</button> "
    usd_price = price / 88000
    auto_on = "selected" if state["auto"] else ""
    auto_off = "selected" if not state["auto"] else ""
    auto_txt = "ON" if state["auto"] else "OFF"
    mode_txt = "REAL LIVE" if LIVE_MODE else "PAPER"
    side_txt = str(t["side"]) if t else "LONG"
    html = ""
    html += "<html><head><meta name='viewport' content='width=device-width, initial-scale=1'><title>Future Bot</title></head>"
    html += "<body style='font-family:Arial;padding:10px;background:#fff;margin:0;'>"
    html += "<h2 style='text-align:center;margin:5px 0;'>Future Bot - 1m HIGH-LOW FIXED + Fee Cover + AUTO</h2>"
    html += "<div style='text-align:center;margin:5px 0;'>" + coins_btn + "</div>"
    html += "<h3 style='text-align:center;'>" + coin + " : Rs.<span id='livePrice'>" + str(round(price, 2)) + "</span>"
    html += " | <span id='liveSignal'>" + signal + "</span>"
    html += " | 24h <span id='liveChg'>" + str(round(chg, 2)) + "%</span></h3>"
    html += "<p style='text-align:center;font-size:12px;'>LIVE pe SHORT | Low pe LONG | Close = 0.25% andar | Fee +10 kat ke Net +15 Target</p>"
    html += "<p style='text-align:center;background:#fff8e1;padding:8px;border-radius:8px;'>Available: Rs." + str(round(state["balance"], 2))
    html += "<br>Locked: Rs." + str(round(state["locked"], 2)) + " | Total: Rs." + str(round(state["total"], 2)) + "</p>"
    html += open_html
    html += "<div style='background:#f9f9f9;padding:10px;border-radius:8px;'><b>SETTING</b><br>"
    html += "Leverage: <select id='lev'><option value='10' selected>10</option><option value='5'>5</option><option value='20'>20</option></select> x "
    html += "Amount Rs. <input id='amt' type='number' value='" + str(state["amount"]) + "' style='width:70px;'> "
    html += "TP %: <input id='tp' type='number' step='0.1' value='" + str(state["tp_pct"]) + "' style='width:50px;'> "
    html += "SL %: <input id='sl' type='number' step='0.1' value='" + str(state["sl_pct"]) + "' style='width:50px;'> "
    html += "Trail %: <input id='trail' type='number' step='0.1' value='" + str(state["trail_pct"]) + "' style='width:50px;'>"
    html += "</div>"
    html += "<div style='background:#f9f9f9;padding:10px;border-radius:8px;margin-top:8px;'>"
    html += "AUTO: <select id='auto'><option value='ON' " + auto_on + ">ON</option><option value='OFF' " + auto_off + ">OFF</option></select> "
    html += "Auto % <input id='autopct' type='number' value='" + str(state["auto_pct"]) + "' style='width:50px;'> % "
    html += "<p><b>AUTO " + auto_txt + " - " + mode_txt + "</b></p></div>"
    html += "<div style='background:#fff3cd;padding:10px;border-radius:8px;margin-top:8px;'><b>HIGH-LOW FIXED LOGIC</b><br>"
    html += "Close %: <input id='close' type='number' step='0.05' value='" + str(state["close_pct"]) + "' style='width:50px;'> "
    html += "Min Range %: <input id='minrange' type='number' step='0.05' value='" + str(state["min_range_pct"]) + "' style='width:50px;'></div>"
    html += "<div style='background:#e8f5e9;padding:8px;border-radius:8px;margin-top:8px;'>TP Preview - TP " + str(state["tp_pct"]) + "% - Price " + str(round(tp_price, 2))
    html += " | Gross +" + str(round(tp_gross, 2)) + " - Fee " + str(round(tp_fee, 2)) + " = Net +" + str(round(tp_net, 2)) + "</div>"
    html += "<div style='background:#ffebee;padding:8px;border-radius:8px;margin-top:5px;'>SL Preview - SL " + str(state["sl_pct"]) + "% - Price " + str(round(sl_price, 2))
    html += " | Net -" + str(round(sl_net, 2)) + " | Entry " + str(round(price, 2)) + " (" + side_txt + ")</div>"
    html += "<div style='display:flex;gap:8px;margin-top:10px;'>"
    html += "<button onclick=\"trade('LONG')\" style='flex:1;padding:12px;background:green;color:white;border:none;border-radius:8px;font-weight:bold;'>LONG Lagao</button>"
    html += "<button onclick=\"trade('SHORT')\" style='flex:1;padding:12px;background:red;color:white;border:none;border-radius:8px;font-weight:bold;'>SHORT Lagao</button>"
    html += "<button onclick=\"trade('CLOSE')\" style='flex:1;padding:12px;background:#333;color:white;border:none;border-radius:8px;'>Band Karo</button>"
    html += "<button onclick='resetBal()' style='flex:1;padding:12px;background:#eee;border:none;border-radius:8px;'>Reset</button>"
    html += "</div>"
    html += "<div style='text-align:center;margin-top:10px;'>TIME FRAME: <button onclick=\"setTF('1m')\">1m</button> <button onclick=\"setTF('5m')\">5m</button> <button onclick=\"setTF('15m')\">15m</button> <button onclick=\"setTF('1h')\">1h</button> <button onclick=\"setTF('4h')\">4h</button> <button onclick=\"setTF('1D')\">1D</button></div>"
    html += "<div style='text-align:center;margin-top:5px;'>LINE TOOL: <button onclick=\"setLine('Normal')\">Normal</button> <button onclick=\"setLine('H')\">H-Line</button> <button onclick=\"setLine('V')\">V-Line</button> <button onclick='clearLines()'>Clear</button></div>"
        html += "<div style='background:#fff;border:1px solid #ddd;border-radius:8px;margin-top:10px;overflow:hidden;'>"
    html += "<div style='display:flex;justify-content:space-between;align-items:center;background:#f5f5f5;padding:8px 10px;'>"
    html += "<div><b id='tvSymbol'>" + coin + "/USDT</b> <span style='color:#888;font-size:12px;'>1m</span>"
    html += " <span style='background:#2962ff;color:white;padding:2px 6px;border-radius:3px;font-size:11px;'>Chart</span></div>"
    html += "<div style='text-align:right;'><b id='tvInrPrice' style='color:#26a65b;font-size:15px;'>Rs." + str(round(price)) + "</b>"
    html += "<br><span id='tvUsdPrice' style='color:#888;font-size:11px;'>$" + str(round(usd_price, 2)) + "</span></div>"
    html += "</div>"
    html += "<div style='position:relative;'><canvas id='chart' width='360' height='280' style='width:100%;background:#fff;display:block;'></canvas>"
    html += "<div id='tvPriceTag' style='position:absolute;right:0;top:10px;background:#2962ff;color:white;font-size:11px;padding:2px 5px;border-radius:3px;'>Rs." + str(round(price)) + "</div></div>"
    html += "<div style='display:flex;justify-content:space-between;color:#888;font-size:10px;padding:4px 10px;'><span>07:00</span><span>08:00</span><span>09:00</span><span>10:00</span><span>11:00</span></div>"
    html += "<p style='text-align:center;color:#888;font-size:11px;margin:0;padding:4px;'>Green = Upar | Red = Neeche | Blue = MA7 | Orange = MA25 | Neeche Volume | INR Right Side</p>"
    html += "</div>"
    html += "<div style='text-align:center;margin-top:10px;'><h3>History</h3>" + hist_html + "</div>"
    html += "<script>"
    html += "var curCoin='" + coin + "';"
    html += "var curTF='1m';"
    html += "var candles=[];"
    html += "var lineMode='Normal'; var hLines=[]; var vLines=[];"
    html += "var livePrice=" + str(price) + ";"
    html += "var priceHist=[];"
    html += "function loadCandles(){"
    html += "var sym=curCoin+'USDT'; var interval=curTF; if(interval=='1D') interval='1d';"
    html += "var urls=['https://api.binance.us/api/v3/klines?symbol='+sym+'&interval='+interval+'&limit=30','https://api.binance.com/api/v3/klines?symbol='+sym+'&interval='+interval+'&limit=30'];"
    html += "function tryUrl(i){ if(i>=urls.length){ makeCandlesFromLive(); return; }"
    html += "fetch(urls[i]).then(r=>{ if(!r.ok) throw new Error('blocked'); return r.json(); }).then(d=>{"
    html += "if(!d || !d.length) throw new Error('empty');"
    html += "candles=d.map(k=>({o:parseFloat(k[1])*88000, h:parseFloat(k[2])*88000, l:parseFloat(k[3])*88000, c:parseFloat(k[4])*88000, vol:parseFloat(k[5])}));"
    html += "if(candles.length>0) candles[candles.length-1].c=livePrice; drawChart();"
    html += "}).catch(e=>{ tryUrl(i+1); }); } tryUrl(0); }"
    html += "function makeCandlesFromLive(){"
    html += "if(priceHist.length<8){ var base=livePrice; candles=[];"
    html += "for(var i=0;i<30;i++){ var o=base + (Math.random()-0.5)*8000; var c=base + (Math.random()-0.5)*8000;"
    html += "var h=Math.max(o,c)+Math.random()*4000; var l=Math.min(o,c)-Math.random()*4000;"
    html += "candles.push({o:o,h:h,l:l,c:c,vol:100+Math.random()*500}); base=c; } } else {"
    html += "var lastHist=priceHist.slice(-120); candles=[];"
    html += "for(var i=0;i<lastHist.length;i+=4){ var chunk=lastHist.slice(i,i+4); if(chunk.length<2) continue;"
    html += "var o=chunk[0]; var cc=chunk[chunk.length-1]; var h=Math.max.apply(null,chunk); var l=Math.min.apply(null,chunk);"
    html += "candles.push({o:o,h:h,l:l,c:cc,vol:100+Math.abs(cc-o)}); }"
    html += "if(candles.length>30) candles=candles.slice(-30); } drawChart(); }"
    html += "loadCandles(); setInterval(loadCandles, 30000);"
    html += "function drawChart(){"
    html += "if(candles.length==0){ makeCandlesFromLive(); return; }"
    html += "var c=document.getElementById('chart'); var ctx=c.getContext('2d');"
    html += "var W=360, H=280, priceH=210, volH=50;"
    html += "var all=[]; for(var k=0;k<candles.length;k++){ all.push(candles[k].h); all.push(candles[k].l); }"
    html += "var min=Math.min.apply(null,all); var max=Math.max.apply(null,all); var range=(max-min)||1;"
    html += "min=min-range*0.15; max=max+range*0.15; range=(max-min)||1;"
    html += "var maxVol=0; for(var v=0;v<candles.length;v++) if(candles[v].vol>maxVol) maxVol=candles[v].vol;"
    html += "ctx.clearRect(0,0,W,H); ctx.fillStyle='#fff'; ctx.fillRect(0,0,W,H);"
    html += "ctx.strokeStyle='#f0f0f0'; ctx.lineWidth=1;"
    html += "for(var g=0;g<6;g++){ ctx.beginPath(); ctx.moveTo(0,g*35); ctx.lineTo(W-60,g*35); ctx.stroke(); }"
    html += "ctx.fillStyle='#888'; ctx.font='9px Arial'; ctx.textAlign='left';"
    html += "for(var p=0;p<6;p++){ var pv=max - (p/5)*range; ctx.fillText('Rs.'+pv.toLocaleString('en-IN',{maximumFractionDigits:0}), W-58, p*35+12); }"
    html += "var cw=(W-60)/30; var bw=Math.max(5, cw*0.65); var startX=(W-60) - candles.length*cw;"
    html += "for(var i=0;i<candles.length;i++){ var cd=candles[i]; var isGreen=cd.c>=cd.o; var col=isGreen?'rgba(38,166,91,0.6)':'rgba(231,76,60,0.6)';"
    html += "var x=startX+i*cw+cw/2; var vh=(cd.vol/maxVol)*volH; ctx.fillStyle=col; ctx.fillRect(x-bw/2, priceH+10+(volH-vh), bw, vh); }"
    html += "for(var i=0;i<candles.length;i++){ var cd=candles[i]; var x=startX+i*cw+cw/2;"
    html += "var yH=((max-cd.h)/range)*priceH; var yL=((max-cd.l)/range)*priceH; var yO=((max-cd.o)/range)*priceH; var yC=((max-cd.c)/range)*priceH;"
    html += "var isGreen=cd.c>=cd.o; var col=isGreen?'#26a65b':'#ef5350';"
    html += "ctx.strokeStyle=col; ctx.lineWidth=1.2; ctx.beginPath(); ctx.moveTo(x,yH); ctx.lineTo(x,yL); ctx.stroke();"
    html += "var top=Math.min(yO,yC); var hgt=Math.max(4, Math.abs(yC-yO)); ctx.fillStyle=col; ctx.fillRect(x-bw/2, top, bw, hgt);"
    html += "if(i==candles.length-1){ ctx.beginPath(); ctx.arc(x,yC,3,0,2*Math.PI); ctx.fillStyle='black'; ctx.fill(); } }"
    html += "function drawMA(period, color){ ctx.beginPath(); ctx.strokeStyle=color; ctx.lineWidth=1.2; var started=false;"
    html += "for(var i=0;i<candles.length;i++){ if(i<period-1) continue; var sum=0; for(var j=i-period+1;j<=i;j++) sum+=candles[j].c;"
    html += "var ma=sum/period; var x=startX+i*cw+cw/2; var y=((max-ma)/range)*priceH;"
    html += "if(!started){ ctx.moveTo(x,y); started=true; } else ctx.lineTo(x,y); } ctx.stroke(); }"
    html += "drawMA(7,'#2962ff'); drawMA(25,'#f9a825');"
    html += "var lastY=((max-livePrice)/range)*priceH;"
    html += "ctx.beginPath(); ctx.strokeStyle='#e74c3c'; ctx.setLineDash([4,4]); ctx.moveTo(0,lastY); ctx.lineTo(W-60,lastY); ctx.stroke(); ctx.setLineDash([]);"
        html += "for(var h=0;h<hLines.length;h++){"
    html += "ctx.beginPath();"
    html += "ctx.strokeStyle='orange';"
    html += "ctx.moveTo(0,hLines[h]);"
    html += "ctx.lineTo(W-60,hLines[h]);"
    html += "ctx.stroke(); }"
    html += "for(var v=0;v<vLines.length;v++){"
    html += "ctx.beginPath();"
    html += "ctx.strokeStyle='#aa00ff';"
    html += "ctx.moveTo(vLines[v],0);"
    html += "ctx.lineTo(vLines[v],priceH);"
    html += "ctx.stroke(); } }"
