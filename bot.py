import os
import time
import threading
import requests
import random
from collections import deque
from flask import Flask, jsonify, request

app = Flask(__name__)

# ===== SETTINGS =====
WALLET_START = 10000.0
API_KEY = os.environ.get("COINSWITCH_API_KEY", "")
API_SECRET = os.environ.get("COINSWITCH_API_SECRET", "")
LIVE_MODE = bool(API_KEY and API_SECRET)

state = {
    "balance": WALLET_START,
    "locked": 0.0,
    "open_trade": None,
    "history": [],
    "auto": False,
    "amount": 150.0,
    "max_amount": 150.0,
    "leverage": 10,
    "take_profit": 0.25,
    "stop_loss": 0.50,
    "strategy": "BALANCED",
    "coin": "BTC/INR",
    "total_trades": 0,
    "total_profit": 0.0,
    "win": 0,
    "loss": 0,
    "mode": "LIVE" if LIVE_MODE else "PAPER"
}

price_history = deque(maxlen=100)
for i in range(50):
    price_history.append(6600000 + random.randint(-5000, 5000))

def get_btc_price():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
        usdt = float(r.json()["price"])
        p = usdt * 88000
        price_history.append(p)
        return p
    except:
        try:
            r = requests.get("https://api.coinswitch.co/v2/ticker?symbol=BTC/INR", timeout=5)
            p = float(r.json()["data"]["last_price"])
            price_history.append(p)
            return p
        except:
            p = price_history[-1] + random.randint(-200, 200)
            price_history.append(p)
            return p

def calc_ema(prices, period):
    if len(prices) < period:
        return sum(prices)/len(prices) if prices else 0
    k = 2/(period+1)
    ema = prices[0]
    for pr in prices[1:]:
        ema = pr*k + ema*(1-k)
    return ema

def calc_rsi(prices, period=14):
    if len(prices) < period+1:
        return 50.0
    gains = 0
    losses = 0
    for i in range(1, period+1):
        d = prices[-i] - prices[-i-1]
        if d > 0:
            gains += d
        else:
            losses += -d
    if losses == 0:
        return 100.0
    rs = (gains/period)/(losses/period)
    return 100 - (100/(1+rs))

def decide_signal():
    prices = list(price_history)
    if len(prices) < 20:
        return "HOLD", 50, 0, 0
    ema9 = calc_ema(prices, 9)
    ema21 = calc_ema(prices, 21)
    rsi = calc_rsi(prices)
    last = prices[-1]
    momentum = (last - prices[-5])/prices[-5]*100
    # Strategy logic
    if state["strategy"] == "SAFE":
        if ema9 > ema21 and rsi < 65 and momentum > 0.01:
            return "LONG", rsi, ema9, ema21
        if ema9 < ema21 and rsi > 35 and momentum < -0.01:
            return "SHORT", rsi, ema9, ema21
    elif state["strategy"] == "AGGRESSIVE":
        if momentum > 0.005 or (ema9 > ema21 and rsi < 70):
            return "LONG", rsi, ema9, ema21
        if momentum < -0.005 or (ema9 < ema21 and rsi > 30):
            return "SHORT", rsi, ema9, ema21
    else: # BALANCED
        if ema9 > ema21 and rsi < 70 and momentum > 0:
            return "LONG", rsi, ema9, ema21
        if ema9 < ema21 and rsi > 30 and momentum < 0:
            return "SHORT", rsi, ema9, ema21
    return "HOLD", rsi, ema9, ema21

def close_trade(reason):
    t = state["open_trade"]
    if not t:
        return
    price = get_btc_price()
    entry = t["entry"]
    amt = t["amount"]
    lev = t["leverage"]
    if t["side"] == "LONG":
        pnl_pct = (price - entry)/entry*100*lev
    else:
        pnl_pct = (entry - price)/entry*100*lev
    pnl = amt * pnl_pct/100
    fee = amt * 0.001
    net = pnl - fee
    state["balance"] += amt + net
    state["locked"] = 0.0
    state["total_profit"] += net
    if net >= 0:
        state["win"] += 1
    else:
        state["loss"] += 1
    state["history"].insert(0, {
        "pair": state["coin"], "side": t["side"],
        "entry": round(entry, 2), "exit": round(price, 2),
        "amount": amt, "leverage": lev,
        "pnl": round(net, 2), "reason": reason,
        "time": time.strftime("%H:%M:%S")
    })
    state["open_trade"] = None

def open_trade(side):
    if state["open_trade"]:
        return {"error": "Trade already open"}
    amt = float(state["amount"])
    if amt > state["balance"]:
        return {"error": "Low balance"}
    price = get_btc_price()
    state["balance"] -= amt
    state["locked"] = amt
    state["open_trade"] = {"side": side, "entry": price, "amount": amt, "leverage": int(state["leverage"]), "time": time.strftime("%H:%M:%S"), "coin": state["coin"]}
    state["total_trades"] += 1
    return {"success": True, "entry": price}

def auto_loop():
    while True:
        try:
            if state["auto"]:
                price = get_btc_price()
                if state["open_trade"]:
                    t = state["open_trade"]
                    if t["side"] == "LONG":
                        ch = (price - t["entry"])/t["entry"]*100*t["leverage"]
                    else:
                        ch = (t["entry"] - price)/t["entry"]*100*t["leverage"]
                    if ch >= float(state["take_profit"]):
                        close_trade("TP Hit")
                    elif ch <= -float(state["stop_loss"]):
                        close_trade("SL Hit")
                else:
                    sig, rsi, e9, e21 = decide_signal()
                    if sig in ["LONG", "SHORT"]:
                        open_trade(sig)
            time.sleep(8)
        except Exception as e:
            print("Loop error", e)
            time.sleep(5)

threading.Thread(target=auto_loop, daemon=True).start()

@app.route("/")
def home():
    price = get_btc_price()
    sig, rsi, ema9, ema21 = decide_signal()
    t = state["open_trade"]
    live_pnl = 0
    live_pct = 0
    if t:
        if t["side"] == "LONG":
            live_pct = (price - t["entry"])/t["entry"]*100*t["leverage"]
        else:
            live_pct = (t["entry"] - price)/t["entry"]*100*t["leverage"]
        live_pnl = t["amount"] * live_pct/100
    win_rate = 0
    if state["total_trades"] > 0:
        win_rate = round(state["win"]/state["total_trades"]*100, 1)
    hist_html = ""
    for h in state["history"][:30]:
        col = "green" if h["pnl"] >= 0 else "red"
        hist_html += f'<div style="display:flex;justify-content:space-between;padding:7px;border-bottom:1px solid #eee;font-size:13px;"><span><b>{h["side"]}</b> {h["pair"]} {h["time"]} | {h["leverage"]}x</span><span style="color:{col};font-weight:bold;">{h["pnl"]} ({h["reason"]})</span></div>'
    if not hist_html:
        hist_html = "<p style='text-align:center;color:#888;'>Abhi koi trade nahi hua</p>"
    chart_data = ",".join([str(int(p)) for p in list(price_history)[-40:]])
    open_html = "<p style='text-align:center;color:#888;'>No Open Trade - AUTO ON karo</p>"
    if t:
        open_html = f'<div style="background:#fff3cd;padding:10px;border-radius:8px;"><b>{t["side"]} {t["coin"]}</b> | Entry {t["entry"]:.2f} | Amt {t["amount"]} | Lev {t["leverage"]}x<br>Live PnL: <b>{live_pnl:.2f}</b> ({live_pct:.2f}%) | Time {t["time"]}</div>'
    return f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Future Bot Pro</title></head>
    <body style="font-family:Arial;padding:12px;background:#f0f2f5;margin:0;">
    <h2 style="margin:5px 0;">Future Bot PRO - {state["mode"]} MODE</h2>
    <p style="margin:2px 0;">BTC Price: <b style="font-size:20px;">{price:.2f}</b> | Signal: <b style="color:{'green' if sig=='LONG' else 'red' if sig=='SHORT' else 'gray'};">{sig}</b> | RSI: {rsi:.1f} | EMA9: {ema9:.0f} EMA21: {ema21:.0f}</p>
    <div style="background:white;padding:12px;border-radius:10px;">
    <p style="margin:3px 0;">Available: <b>{state["balance"]:.2f}</b> | Locked: {state["locked"]:.2f} | Total Profit: <b>{state["total_profit"]:.2f}</b></p>
    <p style="margin:3px 0;">Trades: {state["total_trades"]} | Win: {state["win"]} Loss: {state["loss"]} WinRate: {win_rate}% | Auto: <b>{'ON' if state["auto"] else 'OFF'}</b></p>
    </div>
    <div style="background:white;padding:12px;border-radius:10px;margin-top:10px;">
    <h3 style="margin:0 0 8px 0;">Live Chart</h3>
    <canvas id="chart" width="340" height="120" style="width:100%;background:#f9f9f9;border:1px solid #ddd;border-radius:8px;"></canvas>
    </div>
    <div style="background:white;padding:12px;border-radius:10px;margin-top:10px;">
    <h3 style="margin:0 0 8px 0;">Settings / Options</h3>
    <label>Amount: <input id="amt" type="number" value="{state["amount"]}" style="width:70px;"></label>
    <label>Leverage: <select id="lev"><option value="5" {'selected' if state["leverage"]==5 else ''}>5x</option><option value="10" {'selected' if state["leverage"]==10 else ''}>10x</option><option value="20" {'selected' if state["leverage"]==20 else ''}>20x</option></select></label><br><br>
    <label>Take Profit %: <input id="tp" type="number" step="0.05" value="{state["take_profit"]}" style="width:60px;"></label>
    <label>Stop Loss %: <input id="sl" type="number" step="0.05" value="{state["stop_loss"]}" style="width:60px;"></label><br><br>
    <label>Strategy: <select id="strat"><option value="SAFE" {'selected' if state["strategy"]=='SAFE' else ''}>SAFE</option><option value="BALANCED" {'selected' if state["strategy"]=='BALANCED' else ''}>BALANCED</option><option value="AGGRESSIVE" {'selected' if state["strategy"]=='AGGRESSIVE' else ''}>AGGRESSIVE</option></select></label>
    <label>Coin: <select id="coin"><option>BTC/INR</option><option>ETH/INR</option><option>SOL/INR</option></select></label><br><br>
    <button onclick="saveSet()" style="width:100%;padding:10px;background:#007bff;color:white;border:none;border-radius:8px;">Settings Save Karo</button>
    </div>
    <div style="display:flex;gap:10px;margin-top:10px;">
    <button style="flex:1;padding:12px;background:green;color:white;border:none;border-radius:8px;font-weight:bold;" onclick="trade('LONG')">LONG Lagao</button>
    <button style="flex:1;padding:12px;background:red;color:white;border:none;border-radius:8px;font-weight:bold;" onclick="trade('SHORT')">SHORT Lagao</button>
    </div>
    <div style="display:flex;gap:10px;margin-top:10px;">
    <button style="flex:1;padding:10px;" onclick="trade('CLOSE')">Band Karo</button>
    <button style="flex:1;padding:10px;background:#ff9800;color:white;border:none;border-radius:8px;" onclick="toggleAuto()">AUTO ON/OFF</button>
    <button style="flex:1;padding:10px;" onclick="resetBal()">Reset 10000</button>
    </div>
    <div style="background:white;padding:10px;border-radius:10px;margin-top:10px;"><h3>Open Trade</h3>{open_html}</div>
    <div style="background:white;padding:10px;border-radius:10px;margin-top:10px;"><h3>History (Last 30)</h3>{hist_html}</div>
    <script>
    var data=[{chart_data}];
    var c=document.getElementById('chart'); var ctx=c.getContext('2d');
    var min=Math.min.apply(null,data); var max=Math.max.apply(null,data);
    ctx.beginPath(); ctx.strokeStyle='#007bff'; ctx.lineWidth=2;
    for(var i=0;i<data.length;i++){{ var x=i/(data.length-1)*340; var y=110-((data[i]-min)/(max-min||1))*100; if(i==0) ctx.moveTo(x,y); else ctx.lineTo(x,y); }} ctx.stroke();
    function trade(s){{ fetch('/trade',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{side:s}})}}).then(()=>location.reload()); }}
    function toggleAuto(){{ fetch('/auto',{{method:'POST'}}).then(()=>location.reload()); }}
    function resetBal(){{ fetch('/reset',{{method:'POST'}}).then(()=>location.reload()); }}
    function saveSet(){{ var d={{amount:document.getElementById('amt').value, leverage:document.getElementById('lev').value, take_profit:document.getElementById('tp').value, stop_loss:document.getElementById('sl').value, strategy:document.getElementById('strat').value, coin:document.getElementById('coin').value}}; fetch('/settings',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d)}}).then(()=>location.reload()); }}
    setTimeout(()=>location.reload(), 15000);
    </script></body></html>
    """

@app.route("/trade", methods=["POST"])
def do_trade():
    d = request.json
    side = d.get("side")
    if side == "CLOSE":
        if state["open_trade"]:
            close_trade("Manual Close")
            return jsonify({"success": True})
        return jsonify({"error": "No open trade"})
    return jsonify(open_trade(side))

@app.route("/auto", methods=["POST"])
def do_auto():
    state["auto"] = not state["auto"]
    return jsonify({"auto": state["auto"]})

@app.route("/reset", methods=["POST"])
def do_reset():
    state["balance"] = WALLET_START
    state["locked"] = 0.0
    state["open_trade"] = None
    state["history"] = []
    state["total_trades"] = 0
    state["total_profit"] = 0.0
    state["win"] = 0
    state["loss"] = 0
    return jsonify({"success": True})

@app.route("/settings", methods=["POST"])
def do_settings():
    d = request.json
    try:
        state["amount"] = float(d.get("amount", 150))
        state["max_amount"] = state["amount"]
        state["leverage"] = int(d.get("leverage", 10))
        state["take_profit"] = float(d.get("take_profit", 0.25))
        state["stop_loss"] = float(d.get("stop_loss", 0.50))
        state["strategy"] = d.get("strategy", "BALANCED")
        state["coin"] = d.get("coin", "BTC/INR")
    except Exception as e:
        print(e)
    return jsonify({"success": True})

@app.route("/status")
def status():
    sig, rsi, e9, e21 = decide_signal()
    return jsonify({"state": state, "price": get_btc_price(), "signal": sig, "rsi": rsi, "ema9": e9, "ema21": e21, "chart": list(price_history)[-50:]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
