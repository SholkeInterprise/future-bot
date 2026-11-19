import os
import time
import threading
import requests
import random
from collections import deque
from flask import Flask
from flask import jsonify
from flask import request
app = Flask(__name__)
WALLET = 10000.0
LIVE = False
state = {}
state["balance"] = 14911.48
state["locked"] = 1000.0
state["total"] = 15911.48
state["open_trade"] = None
state["history"] = []
state["auto"] = True
state["amount"] = 1000.0
state["leverage"] = 10
state["tp"] = 1.0
state["sl"] = 0.5
state["trail"] = 0.5
state["close"] = 0.25
state["minr"] = 0.40
state["autop"] = 0.5
state["coin"] = "BTC"
state["tf"] = "1m"
state["trades"] = 0
state["profit"] = 0.0
state["mode"] = "PAPER"
PH = deque(maxlen=200)
for i in range(60):
    PH.append(6612877 + i * 10)
def get_price(coin="BTC"):
    try:
        u = "https://api.binance.com"
        u += "/api/v3/ticker/price"
        u += "?symbol=" + coin + "USDT"
        r = requests.get(u, timeout=5)
        p = float(r.json()["price"])
        p = p * 88000
        if coin == "BTC":
            PH.append(p)
        return p
    except Exception:
        return PH[-1]
def get_chg():
    try:
        u = "https://api.binance.com"
        u += "/api/v3/ticker/24hr"
        u += "?symbol=BTCUSDT"
        r = requests.get(u, timeout=5)
        return float(r.json()["priceChangePercent"])
    except Exception:
        return -1.48
def fee(a):
    return a * 0.01
def close_trade(reason):
    t = state["open_trade"]
    if not t:
        return
    price = get_price(t["coin"])
    e = t["entry"]
    amt = t["amount"]
    lev = t["leverage"]
    f = fee(amt)
    if t["side"] == "LONG":
        g = (price - e) / e * 100
        g = g * lev * amt / 100
    else:
        g = (e - price) / e * 100
        g = g * lev * amt / 100
    net = g - f
    state["balance"] += amt + net
    state["locked"] = 0.0
    state["total"] = state["balance"]
    state["profit"] += net
    h = {}
    h["coin"] = t["coin"]
    h["side"] = t["side"]
    h["reason"] = reason
    h["entry"] = round(e, 2)
    h["exit"] = round(price, 2)
    h["amount"] = amt
    h["leverage"] = lev
    h["gross"] = round(g, 2)
    h["fee"] = round(f, 2)
    h["net"] = round(net, 2)
    h["high"] = round(t["high"], 2)
    h["low"] = round(t["low"], 2)
    h["time"] = time.strftime("%H:%M:%S")
    state["history"].insert(0, h)
    state["open_trade"] = None
def open_trade(side, coin="BTC"):
    if state["open_trade"]:
        return {"error": "Trade open hai"}
    amt = float(state["amount"])
    if amt > state["balance"]:
        return {"error": "Low balance"}
    price = get_price(coin)
    f = fee(amt)
    state["balance"] -= amt
    state["locked"] = amt
    state["total"] = state["balance"]
    state["total"] += state["locked"]
    nt = {}
    nt["side"] = side
    nt["coin"] = coin
    nt["entry"] = price
    nt["amount"] = amt
    nt["leverage"] = int(state["leverage"])
    nt["fee"] = f
    nt["high"] = price
    nt["low"] = price
    nt["time"] = time.strftime("%H:%M:%S")
    state["open_trade"] = nt
    state["trades"] += 1
    return {"success": True}
    def auto_loop():
    while True:
        try:
            if state["auto"]:
                coin = state["coin"]
                price = get_price(coin)
                if state["open_trade"]:
                    t = state["open_trade"]
                    if price > t["high"]:
                        t["high"] = price
                    if price < t["low"]:
                        t["low"] = price
                    cp = float(state["close"])
                    tp = float(state["tp"])
                    sl = float(state["sl"])
                    mr = float(state["minr"])
                    if t["side"] == "LONG":
                        ct = t["high"] * (1 - cp / 100)
                        tp_p = t["entry"] * (1 + tp / 100)
                        sl_p = t["entry"] * (1 - sl / 100)
                        if price >= tp_p and price <= ct:
                            close_trade("TP Hit")
                        elif price <= sl_p:
                            close_trade("SL Hit")
                        elif price <= ct and price > t["entry"]:
                            rng = (t["high"] - t["low"])
                            rng = rng / t["entry"] * 100
                            if rng >= mr:
                                close_trade("HIGH-LOW Close")
                    else:
                        ct = t["low"] * (1 + cp / 100)
                        tp_p = t["entry"] * (1 - tp / 100)
                        sl_p = t["entry"] * (1 + sl / 100)
                        if price <= tp_p and price >= ct:
                            close_trade("TP Hit")
                        elif price >= sl_p:
                            close_trade("SL Hit")
                        elif price >= ct and price < t["entry"]:
                            rng = (t["high"] - t["low"])
                            rng = rng / t["entry"] * 100
                            if rng >= mr:
                                close_trade("HIGH-LOW Close")
                else:
                    pr = list(PH)
                    if len(pr) > 10:
                        last = pr[-1]
                        prev = pr[-5]
                        rng = max(pr[-10:]) - min(pr[-10:])
                        rng = rng / last * 100
                        if rng >= float(state["minr"]):
                            if last > prev:
                                open_trade("LONG", coin)
                            else:
                                open_trade("SHORT", coin)
            time.sleep(5)
        except Exception as e:
            print("Loop", e)
            time.sleep(5)
threading.Thread(target=auto_loop, daemon=True).start()
@app.route("/")
def home():
    coin = state["coin"]
    price = get_price(coin)
    chg = get_chg()
    t = state["open_trade"]
    net = 0
    gross = 0
    th = price
    tl = price
    ct = price
    if t:
        if t["side"] == "LONG":
            gross = (price - t["entry"])
            gross = gross / t["entry"] * 100
            gross = gross * t["leverage"]
            gross = gross * t["amount"] / 100
        else:
            gross = (t["entry"] - price)
            gross = gross / t["entry"] * 100
            gross = gross * t["leverage"]
            gross = gross * t["amount"] / 100
        net = gross - t["fee"]
        th = t["high"]
        tl = t["low"]
        cp = float(state["close"])
        if t["side"] == "LONG":
            ct = t["high"] * (1 - cp / 100)
        else:
            ct = t["low"] * (1 + cp / 100)
    tp_p = price * (1 + float(state["tp"]) / 100)
    sl_p = price * (1 - float(state["sl"]) / 100)
    if t:
        if t["side"] == "LONG":
            tp_p = t["entry"] * (1 + float(state["tp"]) / 100)
            sl_p = t["entry"] * (1 - float(state["sl"]) / 100)
        else:
            tp_p = t["entry"] * (1 - float(state["tp"]) / 100)
            sl_p = t["entry"] * (1 + float(state["sl"]) / 100)
    tg = float(state["amount"])
    tg = tg * float(state["tp"])
    tg = tg * float(state["leverage"]) / 100
    tf = fee(float(state["amount"]))
    tn = tg - tf
    sg = float(state["amount"])
    sg = sg * float(state["sl"])
    sg = sg * float(state["leverage"]) / 100
    sn = sg + tf
    H = []
    H.append("<html><head>")
    H.append("<meta name='viewport'")
    H.append(" content='width=device-width,")
    H.append(" initial-scale=1'>")
    H.append("<title>Future Bot</title></head>")
    H.append("<body style='font-family:Arial;")
    H.append("padding:10px;background:#fff;'>")
    H.append("<h2 style='text-align:center;'>")
    H.append("Future Bot - HIGH-LOW + AUTO</h2>")
    H.append("<div style='text-align:center;'>")
    for c in ["BTC", "ETH", "SOL", "DOGE"]:
        H.append("<button onclick=\"setCoin('")
        H.append(c)
        H.append("')\" style='padding:6px 10px;'>")
        H.append(c)
        H.append("</button> ")
    H.append("</div>")
    H.append("<h3 style='text-align:center;'>")
    H.append(coin)
    H.append(" : Rs.<span id='livePrice'>")
    H.append(str(round(price, 2)))
    H.append("</span> | <span id='liveSignal'>")
    H.append("SHORT" if chg < 0 else "LONG")
    H.append("</span> | 24h <span id='liveChg'>")
    H.append(str(round(chg, 2)))
    H.append("%</span></h3>")
    H.append("<p style='text-align:center;'>")
    H.append("Wallet: Rs.")
    H.append(str(round(state["balance"], 2)))
    H.append(" | Locked: Rs.")
    H.append(str(round(state["locked"], 2)))
    H.append(" | Total: Rs.")
    H.append(str(round(state["total"], 2)))
    H.append("</p>")
    if t:
        H.append("<p style='text-align:center;'>")
        H.append("Open: " + coin + " " + str(t["side"]))
        H.append(" @ " + str(round(t["entry"], 2)))
        H.append(" | Amt " + str(t["amount"]))
        H.append(" Lev " + str(t["leverage"]) + "x")
        H.append("</p><p style='text-align:center;'>")
        H.append("High " + str(round(th, 2)))
        H.append(" Low " + str(round(tl, 2)))
        H.append(" Target " + str(round(ct, 2)))
        H.append("</p><p style='text-align:center;'>")
        H.append("Live PnL Net: " + str(round(net, 2)))
        H.append("</p>")
    else:
        H.append("<p style='text-align:center;'>")
        H.append("No Open Trade</p>")
    H.append("<div style='background:#f9f9f9;")
    H.append("padding:10px;border-radius:8px;'>")
    H.append("<b>SETTING</b><br>")
    H.append("Lev: <select id='lev'>")
    H.append("<option value='10'>10</option>")
    H.append("<option value='5'>5</option>")
    H.append("<option value='20'>20</option>")
    H.append("</select> x ")
    H.append("Amt <input id='amt' type='number'")
    H.append(" value='" + str(state["amount"]) + "'")
    H.append(" style='width:70px;'> ")
    H.append("TP <input id='tp' type='number'")
    H.append(" value='" + str(state["tp"]) + "'")
    H.append(" style='width:50px;'> ")
    H.append("SL <input id='sl' type='number'")
    H.append(" value='" + str(state["sl"]) + "'")
    H.append(" style='width:50px;'>")
    H.append("</div>")
    H.append("<div style='background:#f9f9f9;")
    H.append("padding:10px;margin-top:8px;'>")
    H.append("AUTO: <select id='auto'>")
    H.append("<option value='ON'>ON</option>")
    H.append("<option value='OFF'>OFF</option>")
    H.append("</select></div>")
    H.append("<div style='background:#fff3cd;")
    H.append("padding:10px;margin-top:8px;'>")
    H.append("<b>HIGH-LOW LOGIC</b><br>")
    H.append("Close <input id='close' type='number'")
    H.append(" value='" + str(state["close"]) + "'")
    H.append(" style='width:50px;'> ")
    H.append("Min Range <input id='minr' type='number'")
    H.append(" value='" + str(state["minr"]) + "'")
    H.append(" style='width:50px;'></div>")
    H.append("<div style='background:#e8f5e9;")
    H.append("padding:8px;margin-top:8px;'>")
    H.append("TP Preview - Price " + str(round(tp_p, 2)))
    H.append(" | Net +" + str(round(tn, 2)))
    H.append("</div>")
    H.append("<div style='background:#ffebee;")
    H.append("padding:8px;margin-top:5px;'>")
    H.append("SL Preview - Price " + str(round(sl_p, 2)))
    H.append(" | Net -" + str(round(sn, 2)))
    H.append("</div>")
    H.append("<div style='display:flex;gap:8px;")
    H.append("margin-top:10px;'>")
    H.append("<button onclick=\"trade('LONG')\"")
    H.append(" style='flex:1;padding:12px;")
    H.append("background:green;color:white;'>")
    H.append("LONG Lagao</button>")
    H.append("<button onclick=\"trade('SHORT')\"")
    H.append(" style='flex:1;padding:12px;")
    H.append("background:red;color:white;'>")
    H.append("SHORT Lagao</button>")
    H.append("<button onclick=\"trade('CLOSE')\"")
    H.append(" style='flex:1;padding:12px;")
    H.append("background:#333;color:white;'>")
    H.append("Band Karo</button>")
    H.append("<button onclick='resetBal()'")
    H.append(" style='flex:1;padding:12px;'>")
    H.append("Reset</button></div>")
    H.append("<div style='text-align:center;")
    H.append("margin-top:10px;'>TIME FRAME: ")
    H.append("<button onclick=\"setTF('1m')\">1m</button> ")
    H.append("<button onclick=\"setTF('5m')\">5m</button> ")
    H.append("<button onclick=\"setTF('15m')\">15m</button> ")
    H.append("<button onclick=\"setTF('1h')\">1h</button> ")
    H.append("<button onclick=\"setTF('1D')\">1D</button></div>")
    H.append("<div style='text-align:center;")
    H.append("margin-top:5px;'>LINE TOOL: ")
    H.append("<button onclick=\"setLine('H')\">H-Line</button> ")
    H.append("<button onclick=\"setLine('V')\">V-Line</button> ")
    H.append("<button onclick='clearLines()'>Clear</button></div>")
