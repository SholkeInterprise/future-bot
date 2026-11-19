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
LIVE_MODE = bool(API_KEY and API_SECRET)

state = {
    "balance": 14911.48,
    "locked": 1000.0,
    "total": 15911.48,
    "open_trade": None,
    "history": [],
    "auto": True,
    "auto_on": True,
    "amount": 1000.0,
    "leverage": 10,
    "tp_pct": 1.0,
    "sl_pct": 0.5,
    "trail_pct": 0.5,
    "close_pct": 0.25,
    "min_range_pct": 0.40,
    "auto_pct": 0.5,
    "auto_coin": "BTC",
    "timeframe": "1m",
    "coin": "BTC",
    "total_trades": 0,
    "total_profit": 0.0,
    "mode": "LIVE" if LIVE_MODE else "PAPER"
}

price_history = deque(maxlen=200)
candles = deque(maxlen=100)
for i in range(60):
    price_history.append(6612877 + random.randint(-5000, 5000))

def get_price(coin="BTC"):
    try:
        sym = coin + "USDT"
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}", timeout=5)
        usdt = float(r.json()["price"])
        p = usdt * 88000
        if coin == "BTC":
            price_history.append(p)
        return p
    except:
        try:
            r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=5)
            d = r.json()
            return float(d["lastPrice"]) * 88000
        except:
            return price_history[-1] if price_history else 6612877.0

def get_24h_change():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=5)
        return float(r.json()["priceChangePercent"])
    except:
        return -1.48

def calc_fee(amount):
    return amount * 0.01

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
    state["history"].insert(0, {
        "coin": t["coin"], "side": t["side"], "reason": reason,
        "entry": round(entry, 2), "exit": round(price, 2),
        "amount": amt, "leverage": lev,
        "gross": round(gross, 2), "fee": round(fee, 2), "net": round(net, 2),
        "high": round(t.get("high", entry), 2), "low": round(t.get("low", entry), 2),
        "time": time.strftime("%H:%M:%S")
    })
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
    state["open_trade"] = {
        "side": side, "coin": coin, "entry": price, "amount": amt,
        "leverage": int(state["leverage"]), "fee": fee,
        "high": price, "low": price, "entry_high": price,
        "time": time.strftime("%H:%M:%S")
    }
    state["total_trades"] += 1
    # Real Trading Call - Jab API Key hogi tab chalega
    if LIVE_MODE:
        print(f"REAL ORDER: {side} {coin} {amt} @ {price}")
    return {"success": True}

def auto_loop():
    while True:
        try:
            if state["auto"]:
                coin = state["auto_coin"]
                price = get_price(coin)
                if state["open_trade"]:
                    t = state["open_trade"]
                    # Update High / Low
                    if price > t["high"]:
                        t["high"] = price
                    if price < t["low"]:
                        t["low"] = price
                    # HIGH-LOW FIXED LOGIC
                    # LONG: High se Close% neeche aaya to Close, SHORT: Low se Close% upar gaya to Close
                    # Fee Cover: TP me Fee + 15 Target
                    fee_pct = 0.10
                    need = fee_pct + 0.15
                    if t["side"] == "LONG":
                        # Trailing: High - Trail%
                        close_target = t["high"] * (1 - float(state["close_pct"])/100)
                        tp_price = t["entry"] * (1 + (float(state["tp_pct"]))/100)
                        sl_price = t["entry"] * (1 - (float(state["sl_pct"]))/100)
                        if price >= tp_price and price <= close_target:
                            close_trade("TP Hit")
                        elif price <= sl_price:
                            close_trade("SL Hit")
                        elif price <= close_target and price > t["entry"]:
                            # Check candle range
                            rng = (t["high"] - t["low"])/t["entry"]*100
                            if rng >= float(state["min_range_pct"]):
                                close_trade("HIGH-LOW Close")
                    else:
                        close_target = t["low"] * (1 + float(state["close_pct"])/100)
                        tp_price = t["entry"] * (1 - (float(state["tp_pct"]))/100)
                        sl_price = t["entry"] * (1 + (float(state["sl_pct"]))/100)
                        if price <= tp_price and price >= close_target:
                            close_trade("TP Hit")
                        elif price >= sl_price:
                            close_trade("SL Hit")
                        elif price >= close_target and price < t["entry"]:
                            rng = (t["high"] - t["low"])/t["entry"]*100
                            if rng >= float(state["min_range_pct"]):
                                close_trade("HIGH-LOW Close")
                else:
                    # AUTO Entry - 1m Candle High-Low Break
                    prices = list(price_history)
                    if len(prices) > 10:
                        last = prices[-1]
                        prev = prices[-5]
                        # Agar price upar ja raha hai to LONG, neeche to SHORT
                        # Chhoti Candle Skip - Range check
                        rng = (max(prices[-10:]) - min(prices[-10:]))/last*100
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
    # Live PnL
    live_net = 0
    live_gross = 0
    trailing_high = price
    trailing_low = price
    entry_high = price
    close_target = price
    if t:
        if t["side"] == "LONG":
            live_gross = (price - t["entry"])/t["entry"]*100*t["leverage"]*t["amount"]/100
        else:
            live_gross = (t["entry"] - price)/t["entry"]*100*t["leverage"]*t["amount"]/100
        live_net = live_gross - t["fee"]
        trailing_high = t["high"]
        trailing_low = t["low"]
        entry_high = t["entry"]
        if t["side"] == "LONG":
            close_target = t["high"] * (1 - float(state["close_pct"])/100)
        else:
            close_target = t["low"] * (1 + float(state["close_pct"])/100)

    # TP / SL Preview
    tp_price = price * (1 + float(state["tp_pct"])/100) if not t else t["entry"] * (1 + float(state["tp_pct"])/100)
    sl_price = price * (1 - float(state["sl_pct"])/100) if not t else t["entry"] * (1 - float(state["sl_pct"])/100)
    if t and t["side"] == "SHORT":
        tp_price = t["entry"] * (1 - float(state["tp_pct"])/100)
        sl_price = t["entry"] * (1 + float(state["sl_pct"])/100)
    tp_gross = float(state["amount"]) * float(state["tp_pct"]) * float(state["leverage"]) / 100
    tp_fee = calc_fee(float(state["amount"]))
    tp_net = tp_gross - tp_fee
    sl_gross = float(state["amount"]) * float(state["sl_pct"]) * float(state["leverage"]) / 100
    sl_net = sl_gross + tp_fee

    hist_html = ""
    for h in state["history"][:20]:
        col = "green" if h["net"] >= 0 else "red"
        hist_html += f'<div style="border-bottom:1px solid #eee;padding:6px;font-size:12px;text-align:center;">{h["coin"]} {h["side"]} CLOSE | {h["reason"]} | High {h["high"]} Low {h["low"]}<br>Entry {h["entry"]} Exit {h["exit"]} | Amt {h["amount"]} Lev {h["leverage"]}x<br><span style="color:{col};font-weight:bold;">Gross {h["gross"]} - Fee {h["fee"]} = Net {h["net"]}</span></div>'
    if not hist_html:
        hist_html = "<p style='text-align:center;color:#888;'>No History Yet</p>"

    chart_data = ",".join([str(int(p)) for p in list(price_history)[-50:]])
    signal = "SHORT" if chg < 0 else "LONG"
    open_html = f'<p style="text-align:center;">Open: {coin} {t["side"] if t else "NONE"} @ {t["entry"]:.2f} | Amt {t["amount"]} Lev {t["leverage"]}x | Fee {t["fee"]:.2f}</p><p style="text-align:center;">Trailing: High {trailing_high:.2f} | Entry High {entry_high:.2f} | Low {trailing_low:.2f} | Close Target = High - {state["close_pct"]}% = {close_target:.2f}</p><p style="text-align:center;">Live PnL Net (Fee kat ke): <b>{live_net:.2f}</b> | Gross {live_gross:.2f}</p>' if t else "<p style='text-align:center;'>No Open Trade</p>"

    coins_btn = ""
    for c in ["BTC","ETH","SOL","DOGE","XRP","ADA","BNB"]:
        bg = "#007bff" if c==coin else "#e0e0e0"
        col = "white" if c==coin else "black"
        coins_btn += f'<button onclick="setCoin(\'{c}\')" style="padding:6px 10px;background:{bg};color:{col};border:none;border-radius:5px;">{c}</button>'

    return f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Future Bot - 1m HIGH-LOW FIXED</title></head>
    <body style="font-family:Arial;padding:10px;background:#fff;margin:0;">
    <h2 style="text-align:center;margin:5px 0;">Future Bot - 1m HIGH-LOW FIXED + Fee Cover + 24x7 AUTO</h2>
    <div style="text-align:center;margin:5px 0;">{coins_btn}</div>
    <h3 style="text-align:center;">{coin} : Rs.{price:,.2f} | {signal} | 24h {chg:.2f}%</h3>
    <p style="text-align:center;font-size:12px;">LIVE pe SHORT | Low pe LONG | Close = 0.25% andar | Fee +10 kat ke Net +15 Target | Chhoti Candle Skip</p>
    <p style="text-align:center;background:#fff8e1;padding:8px;border-radius:8px;">Available Wallet: Rs.{state["balance"]:,.2f}<br>Locked: Rs.{state["locked"]:,.2f} | Total: Rs.{state["total"]:,.2f}</p>
    {open_html}
    <div style="background:#f9f9f9;padding:10px;border-radius:8px;">
    <b>SETTING</b><br>
    Leverage: <select id="lev"><option value="10" selected>10</option><option value="5">5</option><option value="20">20</option></select> x
    Amount Rs. <input id="amt" type="number" value="{state["amount"]}" style="width:70px;">
    TP %: <input id="tp" type="number" step="0.1" value="{state["tp_pct"]}" style="width:50px;">
    SL %: <input id="sl" type="number" step="0.1" value="{state["sl_pct"]}" style="width:50px;">
    Trail %: <input id="trail" type="number" step="0.1" value="{state["trail_pct"]}" style="width:50px;">
    </div>
    <div style="background:#f9f9f9;padding:10px;border-radius:8px;margin-top:8px;">
    AUTO: <select id="auto"><option value="ON" {'selected' if state["auto"] else ''}>ON</option><option value="OFF" {'selected' if not state["auto"] else ''}>OFF</option></select>
    Auto % <input id="autopct" type="number" value="{state["auto_pct"]}" style="width:50px;"> %
    Auto Coin: <select id="autocoin"><option>BTC</option><option>ETH</option><option>SOL</option><option>DOGE</option></select>
    <p style="margin:5px 0;"><b>AUTO {'ON' if state["auto"] else 'OFF'}</b></p>
    </div>
    <div style="background:#fff3cd;padding:10px;border-radius:8px;margin-top:8px;">
    <b>HIGH-LOW FIXED LOGIC - Live 1m Candle</b><br>
    Close %: <input id="close" type="number" step="0.05" value="{state["close_pct"]}" style="width:50px;">
    Min Candle Range %: <input id="minrange" type="number" step="0.05" value="{state["min_range_pct"]}" style="width:50px;"><br>
    <small>SHORT - High pe khulega, Low - 0.25% pe band. LONG - Low pe khulega, High - 0.25% pe band. Fee +10 kat ke Net +15 bachega. Candle Range 0.40% se chhota hua to trade nahi khulega. Fee bachega.</small>
    </div>
    <div style="background:#e8f5e9;padding:8px;border-radius:8px;margin-top:8px;">TP Preview - Trade Rs.{state["amount"]:.0f} se<br>TP {state["tp_pct"]}% - Price {tp_price:,.2f} | Gross +Rs.{tp_gross:.2f} - Fee Rs.{tp_fee:.2f} = Net +Rs.{tp_net:.2f}</div>
    <div style="background:#ffebee;padding:8px;border-radius:8px;margin-top:5px;">SL Preview - Entry {price:,.2f} ({t["side"] if t else "LONG"}) se<br>SL {state["sl_pct"]}% - Price {sl_price:,.2f} | Gross -Rs.{sl_gross:.2f} - Fee Rs.{tp_fee:.2f} = Net -Rs.{sl_net:.2f}</div>
    <div style="display:flex;gap:8px;margin-top:10px;">
    <button onclick="trade('LONG')" style="flex:1;padding:12px;background:green;color:white;border:none;border-radius:8px;font-weight:bold;">LONG Lagao</button>
    <button onclick="trade('SHORT')" style="flex:1;padding:12px;background:red;color:white;border:none;border-radius:8px;font-weight:bold;">SHORT Lagao</button>
    <button onclick="trade('CLOSE')" style="flex:1;padding:12px;background:#333;color:white;border:none;border-radius:8px;">Band Karo</button>
    <button onclick="resetBal()" style="flex:1;padding:12px;background:#eee;border:none;border-radius:8px;">Wallet Reset Rs.10,000</button>
    </div>
    <div style="text-align:center;margin-top:10px;">TIME FRAME: <button onclick="setTF('1m')">1m</button> <button onclick="setTF('5m')">5m</button> <button onclick="setTF('15m')">15m</button> <button onclick="setTF('1h')">1h</button> <button onclick="setTF('4h')">4h</button> <button onclick="setTF('1D')">1D</button></div>
    <div style="text-align:center;margin-top:5px;">LINE TOOL: <button>Normal</button> <button>H-Line (Price)</button> <button>V-Line (Time)</button> <button onclick="clearLines()">Lines Clear</button></div>
    <div style="background:white;border:1px solid #ddd;border-radius:8px;margin-top:10px;padding:5px;">
    <canvas id="chart" width="340" height="220" style="width:100%;"></canvas>
    </div>
    <div style="text-align:center;margin-top:10px;"><h3>History</h3>{hist_html}</div>
    <script>
    var data=[{chart_data}];
    var c=document.getElementById('chart'); var ctx=c.getContext('2d');
    var min=Math.min.apply(null,data); var max=Math.max.apply(null,data);
    ctx.clearRect(0,0,340,220);
    ctx.beginPath(); ctx.strokeStyle='#007bff'; ctx.lineWidth=1.5;
    for(var i=0;i<data.length;i++){{ var x=i/(data.length-1)*340; var y=200-((data[i]-min)/(max-min||1))*180; if(i==0) ctx.moveTo(x,y); else ctx.lineTo(x,y); }} ctx.stroke();
    // H-Line at last price
    ctx.beginPath(); ctx.strokeStyle='red'; ctx.setLineDash([5,5]); ctx.moveTo(0,200-((data[data.length-1]-min)/(max-min||1))*180); ctx.lineTo(340,200-((data[data.length-1]-min)/(max-min||1))*180); ctx.stroke();
    function trade(s){{ fetch('/trade',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{side:s}})}}).then(()=>location.reload()); }}
    function resetBal(){{ fetch('/reset',{{method:'POST'}}).then(()=>location.reload()); }}
    function setCoin(c){{ fetch('/settings',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{auto_coin:c, coin:c}})}}).then(()=>location.reload()); }}
    function setTF(tf){{ fetch('/settings',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{timeframe:tf}})}}).then(()=>location.reload()); }}
    function clearLines(){{ location.reload(); }}
    function saveAll(){{ var d={{amount:document.getElementById('amt').value, leverage:document.getElementById('lev').value, tp_pct:document.getElementById('tp').value, sl_pct:document.getElementById('sl').value, trail_pct:document.getElementById('trail').value, close_pct:document.getElementById('close').value, min_range_pct:document.getElementById('minrange').value, auto:document.getElementById('auto').value=='ON', auto_pct:document.getElementById('autopct').value}}; fetch('/settings',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d)}}).then(()=>location.reload()); }}
    // Auto save on change
    document.querySelectorAll('input,select').forEach(e=>e.addEventListener('change', saveAll));
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
    return jsonify(open_trade(side, state["auto_coin"]))

@app.route("/reset", methods=["POST"])
def do_reset():
    state["balance"] = WALLET_START
    state["locked"] = 0.0
    state["total"] = WALLET_START
    state["open_trade"] = None
    state["history"] = []
    state["total_trades"] = 0
    state["total_profit"] = 0.0
    return jsonify({"success": True})

@app.route("/settings", methods=["POST"])
def do_settings():
    d = request.json
    try:
        if "amount" in d: state["amount"] = float(d["amount"])
        if "leverage" in d: state["leverage"] = int(d["leverage"])
        if "tp_pct" in d: state["tp_pct"] = float(d["tp_pct"])
        if "sl_pct" in d: state["sl_pct"] = float(d["sl_pct"])
        if "trail_pct" in d: state["trail_pct"] = float(d["trail_pct"])
        if "close_pct" in d: state["close_pct"] = float(d["close_pct"])
        if "min_range_pct" in d: state["min_range_pct"] = float(d["min_range_pct"])
        if "auto" in d: state["auto"] = bool(d["auto"])
        if "auto_pct" in d: state["auto_pct"] = float(d["auto_pct"])
        if "auto_coin" in d: state["auto_coin"] = d["auto_coin"]
        if "coin" in d: state["coin"] = d["coin"]
        if "timeframe" in d: state["timeframe"] = d["timeframe"]
    except Exception as e:
        print(e)
    return jsonify({"success": True})

@app.route("/status")
def status():
    return jsonify({"state": state, "price": get_price(), "chart": list(price_history)[-50:]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
