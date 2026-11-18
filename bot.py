import os
import time
import threading
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# ===== SETTINGS =====
WALLET_START = 10000.0
DEFAULT_AMOUNT = 150.0
DEFAULT_LEVERAGE = 10
TAKE_PROFIT_PCT = 0.25
STOP_LOSS_PCT = 0.50

# CoinSwitch API - Render Environment se aayega
API_KEY = os.environ.get("COINSWITCH_API_KEY", "")
API_SECRET = os.environ.get("COINSWITCH_API_SECRET", "")
LIVE_MODE = bool(API_KEY and API_SECRET)

# ===== STATE =====
state = {
    "balance": WALLET_START,
    "locked": 0.0,
    "open_trade": None,
    "history": [],
    "auto": False,
    "amount": DEFAULT_AMOUNT,
    "max_amount": DEFAULT_AMOUNT,
    "total_trades": 0,
    "total_profit": 0.0,
    "mode": "LIVE" if LIVE_MODE else "PAPER"
}

def get_btc_price():
    try:
        r = requests.get("https://api.coinswitch.co/v2/ticker?symbol=BTC/INR", timeout=5)
        data = r.json()
        if "data" in data and "last_price" in data["data"]:
            return float(data["data"]["last_price"])
        r2 = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
        usdt = float(r2.json()["price"])
        return usdt * 88000
    except:
        return 6600000.0

def close_trade(reason):
    t = state["open_trade"]
    if not t:
        return
    price = get_btc_price()
    entry = t["entry"]
    amt = t["amount"]
    lev = t["leverage"]
    if t["side"] == "LONG":
        pnl_pct = (price - entry) / entry * 100 * lev
    else:
        pnl_pct = (entry - price) / entry * 100 * lev
    pnl = amt * pnl_pct / 100
    fee = amt * 0.001
    net = pnl - fee
    state["balance"] += amt + net
    state["locked"] = 0.0
    state["total_profit"] += net
    state["history"].insert(0, {
        "pair": "BTC/INR",
        "side": t["side"],
        "entry": round(entry, 2),
        "exit": round(price, 2),
        "amount": amt,
        "pnl": round(net, 2),
        "reason": reason,
        "time": time.strftime("%H:%M:%S")
    })
    state["open_trade"] = None
    print(f"CLOSED {t['side']} pnl={net} reason={reason}")

def open_trade(side):
    if state["open_trade"]:
        return {"error": "Trade already open"}
    amt = float(state["amount"])
    if amt > state["balance"]:
        return {"error": "Insufficient balance"}
    price = get_btc_price()
    state["balance"] -= amt
    state["locked"] = amt
    state["open_trade"] = {
        "side": side,
        "entry": price,
        "amount": amt,
        "leverage": DEFAULT_LEVERAGE,
        "time": time.strftime("%H:%M:%S")
    }
    state["total_trades"] += 1
    print(f"OPENED {side} at {price} amt {amt}")
    return {"success": True, "entry": price}

def auto_loop():
    last_price = 0
    while True:
        try:
            if state["auto"]:
                price = get_btc_price()
                if state["open_trade"]:
                    t = state["open_trade"]
                    entry = t["entry"]
                    if t["side"] == "LONG":
                        change = (price - entry) / entry * 100 * DEFAULT_LEVERAGE
                    else:
                        change = (entry - price) / entry * 100 * DEFAULT_LEVERAGE
                    if change >= TAKE_PROFIT_PCT:
                        close_trade("TP Hit")
                    elif change <= -STOP_LOSS_PCT:
                        close_trade("SL Hit")
                else:
                    if last_price != 0:
                        if price > last_price:
                            open_trade("LONG")
                        elif price < last_price:
                            open_trade("SHORT")
                    last_price = price
            time.sleep(10)
        except Exception as e:
            print("Auto loop error:", e)
            time.sleep(5)

threading.Thread(target=auto_loop, daemon=True).start()

@app.route("/")
def home():
    price = get_btc_price()
    t = state["open_trade"]
    live_pnl = 0
    if t:
        if t["side"] == "LONG":
            live_pnl = (price - t["entry"]) / t["entry"] * 100 * DEFAULT_LEVERAGE * t["amount"] / 100
        else:
            live_pnl = (t["entry"] - price) / t["entry"] * 100 * DEFAULT_LEVERAGE * t["amount"] / 100

    hist_html = ""
    for h in state["history"][:20]:
        color = "green" if h["pnl"] >= 0 else "red"
        hist_html += f'<div style="border-bottom:1px solid #ddd;padding:8px;display:flex;justify-content:space-between;"><span>{h["side"]} BTC {h["time"]}</span><span style="color:{color};font-weight:bold;">{h["pnl"]}</span></div>'

    open_html = "No Open Trade"
    if t:
        open_html = f'<div style="background:#fff3cd;padding:10px;border-radius:8px;"><b>{t["side"]} BTC</b> Entry: {t["entry"]:.2f} Amt: {t["amount"]} Lev: {t["leverage"]}x<br>Live PnL: {live_pnl:.2f}</div>'

    return f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Future Bot</title></head>
    <body style="font-family:Arial;padding:15px;background:#f5f5f5;">
    <h2>Future Bot - {state["mode"]} MODE</h2>
    <p>BTC Price: <b>{price:.2f}</b></p>
    <div style="background:white;padding:12px;border-radius:10px;">
    <p>Available Wallet: <b>{state["balance"]:.2f}</b> | Locked: {state["locked"]:.2f}</p>
    <p>Total Trades: {state["total_trades"]} | Total Profit: {state["total_profit"]:.2f}</p>
    <p>Auto: <b>{'ON' if state["auto"] else 'OFF'}</b></p>
    <label>Amount: <input id="amt" type="number" value="{state["amount"]}" style="width:80px;"></label>
    <button onclick="setAmt()">Set Amount</button>
    </div><br>
    <div style="display:flex;gap:10px;">
    <button style="flex:1;padding:12px;background:green;color:white;border:none;border-radius:8px;" onclick="trade('LONG')">LONG Lagao</button>
    <button style="flex:1;padding:12px;background:red;color:white;border:none;border-radius:8px;" onclick="trade('SHORT')">SHORT Lagao</button>
    </div><br>
    <div style="display:flex;gap:10px;">
    <button style="flex:1;padding:10px;" onclick="trade('CLOSE')">Band Karo</button>
    <button style="flex:1;padding:10px;" onclick="toggleAuto()">AUTO ON/OFF</button>
    <button style="flex:1;padding:10px;" onclick="resetBal()">Wallet Reset 10000</button>
    </div><br>
    <div style="background:white;padding:10px;border-radius:10px;"><h3>Open Trade</h3>{open_html}</div><br>
    <div style="background:white;padding:10px;border-radius:10px;"><h3>History</h3>{hist_html}</div>
    <script>
    function trade(s){{ fetch('/trade',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{side:s}})}}).then(()=>location.reload()); }}
    function toggleAuto(){{ fetch('/auto',{{method:'POST'}}).then(()=>location.reload()); }}
    function resetBal(){{ fetch('/reset',{{method:'POST'}}).then(()=>location.reload()); }}
    function setAmt(){{ var v=document.getElementById('amt').value; fetch('/set_amount',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{amount:v}})}}).then(()=>location.reload()); }}
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
    return jsonify({"success": True})

@app.route("/set_amount", methods=["POST"])
def do_set():
    try:
        state["amount"] = float(request.json.get("amount", 150))
        state["max_amount"] = state["amount"]
    except:
        pass
    return jsonify({"amount": state["amount"]})

@app.route("/status")
def status():
    return jsonify(state)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
