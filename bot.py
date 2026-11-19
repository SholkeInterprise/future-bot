import os
import time
import threading
import requests
import random
import hmac
import hashlib
import json
from collections import deque
from flask import Flask, jsonify, request

app = Flask(__name__)

WALLET_START = 10000.0

# ===== REAL TRADING CONFIG =====
API_KEY = os.environ.get("COINSWITCH_API_KEY", "")
API_SECRET = os.environ.get("COINSWITCH_API_SECRET", "")
# LIVE tabhi ON hoga jab Key + Secret + COINSWITCH_LIVE=true teeno honge
LIVE_MODE = bool(API_KEY and API_SECRET and os.environ.get("COINSWITCH_LIVE", "false").lower() == "true")
COINSWITCH_BASE = "https://api.coinswitch.co"

print(f"MODE: {'REAL LIVE' if LIVE_MODE else 'PAPER - Real OFF'}")

state = {
    "balance": 14911.48,
    "locked": 1000.0,
    "total": 15911.48,
    "open_trade": None,
    "history": [],
    "auto": True,
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
    "mode": "REAL LIVE" if LIVE_MODE else "PAPER",
    "real_order_id": None,
    "last_real_error": ""
}

price_history = deque(maxlen=200)
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

# ===== REAL COINSWITCH FUNCTIONS =====
def coinswitch_headers(method, path, body=""):
    # CoinSwitch Auth - HMAC Signature
    timestamp = str(int(time.time()*1000))
    payload = method + path + body + timestamp
    sig = hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Api-Key": API_KEY,
        "X-Api-Signature": sig,
        "X-Api-Timestamp": timestamp,
        "Content-Type": "application/json"
    }

def place_real_order(side, coin, amount_inr, leverage):
    """Real Order CoinSwitch pe - Future Order"""
    if not LIVE_MODE:
        return {"success": True, "paper": True, "order_id": "PAPER"}
    try:
        # CoinSwitch Futures Order Payload
        # side: BUY = LONG, SELL = SHORT
        cs_side = "BUY" if side == "LONG" else "SELL"
        # Amount INR se Coin Quantity nikalo
        price = get_price(coin)
        qty = (amount_inr * leverage) / price
        # CoinSwitch ke hisab se quantity 4 decimal tak
        qty = round(qty, 4)
        payload = {
            "symbol": f"{coin}/INR",
            "side": cs_side,
            "type": "MARKET",
            "quantity": qty,
            "leverage": int(leverage),
            "margin_type": "ISOLATED"
        }
        body = json.dumps(payload)
        path = "/v2/futures/order"
        headers = coinswitch_headers("POST", path, body)
        r = requests.post(COINSWITCH_BASE + path, headers=headers, data=body, timeout=10)
        print(f"REAL ORDER RESP: {r.status_code} {r.text[:500]}")
        if r.status_code in [200, 201]:
            d = r.json()
            oid = d.get("data", {}).get("order_id") or d.get("order_id") or str(time.time())
            return {"success": True, "order_id": oid, "qty": qty}
        else:
            # Agar endpoint alag hai to Spot try karo - kam se kam error to pata chale
            state["last_real_error"] = f"Real Order Fail: {r.text[:200]}"
            return {"success": False, "error": r.text[:300]}
    except Exception as e:
        state["last_real_error"] = str(e)
        print("REAL ORDER ERROR:", e)
        return {"success": False, "error": str(e)}

def close_real_order(trade):
    """Real Order Close - Opposite side se close"""
    if not LIVE_MODE:
        return {"success": True}
    try:
        # Opposite side se close - LONG tha to SELL, SHORT tha to BUY
        opp = "SELL" if trade["side"] == "LONG" else "BUY"
        qty = trade.get("real_qty", 0)
        if qty == 0:
            return {"success": True}
        payload = {
            "symbol": f"{trade['coin']}/INR",
            "side": opp,
            "type": "MARKET",
            "quantity": qty,
            "leverage": int(trade["leverage"]),
            "margin_type": "ISOLATED",
            "reduce_only": True
        }
        body = json.dumps(payload)
        path = "/v2/futures/order"
        headers = coinswitch_headers("POST", path, body)
        r = requests.post(COINSWITCH_BASE + path, headers=headers, data=body, timeout=10)
        print(f"REAL CLOSE RESP: {r.status_code} {r.text[:500]}")
        if r.status_code in [200, 201]:
            return {"success": True}
        else:
            state["last_real_error"] = f"Real Close Fail: {r.text[:200]}"
            return {"success": False, "error": r.text[:300]}
    except Exception as e:
        print("REAL CLOSE ERROR:", e)
        return {"success": False, "error": str(e)}

def get_real_balance():
    """Real Wallet Balance CoinSwitch se"""
    if not LIVE_MODE:
        return None
    try:
        path = "/v2/user/wallet"
        headers = coinswitch_headers("GET", path, "")
        r = requests.get(COINSWITCH_BASE + path, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print("Balance error", e)
    return None

def close_trade(reason):
    t = state["open_trade"]
    if not t:
        return
    # Pehle REAL Close karo, agar Real fail hua to Paper bhi mat band karo
    if LIVE_MODE:
        rc = close_real_order(t)
        if not rc.get("success"):
            print("REAL CLOSE FAILED, trade khula rakha")
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
        "coin": t["coin"], "side": t["side"], "reason": reason + (" [REAL]" if LIVE_MODE else " [PAPER]"),
        "entry": round(entry, 2), "exit": round(price, 2),
        "amount": amt, "leverage": lev,
        "gross": round(gross, 2), "fee": round(fee, 2), "net": round(net, 2),
        "high": round(t.get("high", entry), 2), "low": round(t.get("low", entry), 2),
        "time": time.strftime("%H:%M:%S")
    })
    state["open_trade"] = None
    state["real_order_id"] = None

def open_trade(side, coin="BTC"):
    if state["open_trade"]:
        return {"error": "Trade already open"}
    amt = float(state["amount"])
    if amt > state["balance"]:
        return {"error": "Low balance"}
    # SAFETY: Real me 500 se kam ka trade mat kholo
    if LIVE_MODE and amt < 500:
        return {"error": "Real me Min 500 Rs ka trade karo"}
    price = get_price(coin)
    fee = calc_fee(amt)
    # Pehle REAL Order lagao
    real = place_real_order(side, coin, amt, int(state["leverage"]))
    if not real.get("success"):
        return {"error": f"Real Order Fail: {real.get('error','Unknown')} - Paper trade nahi khola"}
    state["balance"] -= amt
    state["locked"] = amt
    state["total"] = state["balance"] + state["locked"]
    state["open_trade"] = {
        "side": side, "coin": coin, "entry": price, "amount": amt,
        "leverage": int(state["leverage"]), "fee": fee,
        "high": price, "low": price, "entry_high": price,
        "time": time.strftime("%H:%M:%S"),
        "real_order_id": real.get("order_id"),
        "real_qty": real.get("qty", 0)
    }
    state["real_order_id"] = real.get("order_id")
    state["total_trades"] += 1
    print(f"{'REAL' if LIVE_MODE else 'PAPER'} ORDER: {side} {coin} {amt} @ {price} ID:{real.get('order_id')}")
    return {"success": True, "mode": state["mode"]}

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
                        close_target = t["high"] * (1 - float(state["close_pct"])/100)
                        tp_price = t["entry"] * (1 + (float(state["tp_pct"]))/100)
                        sl_price = t["entry"] * (1 - (float(state["sl_pct"]))/100)
                        if price >= tp_price and price <= close_target:
                            close_trade("TP Hit")
                        elif price <= sl_price:
                            close_trade("SL Hit")
                        elif price <= close_target and price > t["entry"]:
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
                    prices = list(price_history)
                    if len(prices) > 10:
                        last = prices[-1]
                        prev = prices[-5]
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
    signal = "SHORT" if chg < 0 else "LONG"
    mode_badge = f'<span style="background:{"red" if LIVE_MODE else "green"};color:white;padding:4px 10px;border-radius:5px;font-weight:bold;">{"REAL LIVE - Real Paisa" if LIVE_MODE else "PAPER - Real OFF"}</span>'
    err_html = f'<p style="text-align:center;color:red;background:#ffebee;padding:6px;border-radius:5px;">Real Error: {state["last_real_error"]}</p>' if state["last_real_error"] else ""
    open_html = f'<p style="text-align:center;">Open: {coin} {t["side"] if t else "NONE"} @ {t["entry"]:.2f} | Amt {t["amount"]} Lev {t["leverage"]}x | Fee {t["fee"]:.2f} | Order ID: {t.get("real_order_id","-")}</p><p style="text-align:center;">Trailing: High {trailing_high:.2f} | Entry High {entry_high:.2f} | Low {trailing_low:.2f} | Close Target = High - {state["close_pct"]}% = {close_target:.2f}</p><p style="text-align:center;">Live PnL Net (Fee kat ke): <b>{live_net:.2f}</b> | Gross {live_gross:.2f}</p>' if t else "<p style='text-align:center;'>No Open Trade</p>"
    coins_btn = ""
    for c in ["BTC","ETH","SOL","DOGE","XRP","ADA","BNB"]:
        bg = "#007bff" if c==coin else "#e0e0e0"
        col = "white" if c==coin else "black"
        coins_btn += f'<button onclick="setCoin(\'{c}\')" style="padding:6px 10px;background:{bg};color:{col};border:none;border-radius:5px;">{c}</button>'
    usd_price = price/88000
    return f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Future Bot - Real Trading</title></head>
    <body style="font-family:Arial;padding:10px;background:#fff;margin:0;">
    <h2 style="text-align:center;margin:5px 0;">Future Bot - 1m HIGH-LOW FIXED + Fee Cover + 24x7 AUTO</h2>
    <div style="text-align:center;margin:5px 0;">{mode_badge}</div>
    {err_html}
    <div style="text-align:center;margin:5px 0;">{coins_btn}</div>
    <h3 style="text-align:center;">{coin} : Rs.<span id="livePrice">{price:,.2f}</span> | <span id="liveSignal">{signal}</span> | 24h <span id="liveChg">{chg:.2f}%</span></h3>
    <p style="text-align:center;font-size:12px;">LIVE pe SHORT | Low pe LONG | Close = 0.25% andar | Fee +10 kat ke Net +15 Target | Chhoti Candle Skip</p>
    <p style="text-align:center;background:#fff8e1;padding:8px;border-radius:8px;">Available Wallet: Rs.{state["balance"]:,.2f}<br>Locked: Rs.{state["locked"]:,.2f} | Total: Rs.{state["total"]:,.2f}</p>
    {open_html}
    <div style="background:#f9f9f9;padding:10px;border-radius:8px;">
    <b>SETTING - Real me yahi jayega</b><br>
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
    <p style="margin:5px 0;"><b>AUTO {'ON' if state["auto"] else 'OFF'} - {'REAL' if LIVE_MODE else 'PAPER'}</b></p>
    </div>
    <div style="background:#fff3cd;padding:10px;border-radius:8px;margin-top:8px;">
    <b>HIGH-LOW FIXED LOGIC - Live 1m Candle</b><br>
    Close %: <input id="close" type="number" step="0.05" value="{state["close_pct"]}" style="width:50px;">
    Min Candle Range %: <input id="minrange" type="number" step="0.05" value="{state["min_range_pct"]}" style="width:50px;"><br>
    </div>
    <div style="background:#e8f5e9;padding:8px;border-radius:8px;margin-top:8px;">TP Preview - Trade Rs.{state["amount"]:.0f} se<br>TP {state["tp_pct"]}% - Price {tp_price:,.2f} | Gross +Rs.{tp_gross:.2f} - Fee Rs.{tp_fee:.2f} = Net +Rs.{tp_net:.2f}</div>
    <div style="background:#ffebee;padding:8px;border-radius:8px;margin-top:5px;">SL Preview - Entry {price:,.2f} ({t["side"] if t else "LONG"}) se<br>SL {state["sl_pct"]}% - Price {sl_price:,.2f} | Gross -Rs.{sl_gross:.2f} - Fee Rs.{tp_fee:.2f} = Net -Rs.{sl_net:.2f}</div>
    <div style="display:flex;gap:8px;margin-top:10px;">
    <button onclick="trade('LONG')" style="flex:1;padding:12px;background:green;color:white;border:none;border-radius:8px;font-weight:bold;">LONG Lagao {'REAL' if LIVE_MODE else ''}</button>
    <button onclick="trade('SHORT')" style="flex:1;padding:12px;background:red;color:white;border:none;border-radius:8px;font-weight:bold;">SHORT Lagao {'REAL' if LIVE_MODE else ''}</button>
    <button onclick="trade('CLOSE')" style="flex:1;padding:12px;background:#333;color:white;border:none;border-radius:8px;">Band Karo</button>
    <button onclick="resetBal()" style="flex:1;padding:12px;background:#eee;border:none;border-radius:8px;">Wallet Reset Rs.10,000</button>
    </div>
    <div style="text-align:center;margin-top:10px;">TIME FRAME: <button onclick="setTF('1m')">1m</button> <button onclick="setTF('5m')">5m</button> <button onclick="setTF('15m')">15m</button> <button onclick="setTF('1h')">1h</button> <button onclick="setTF('4h')">4h</button> <button onclick="setTF('1D')">1D</button></div>
    <div style="text-align:center;margin-top:5px;">LINE TOOL: <button onclick="setLine('Normal')">Normal</button> <button onclick="setLine('H')">H-Line (Price)</button> <button onclick="setLine('V')">V-Line (Time)</button> <button onclick="clearLines()">Lines Clear</button></div>
    <div style="background:#131722;border-radius:8px;margin-top:10px;padding:0;overflow:hidden;">
    <div style="display:flex;justify-content:space-between;align-items:center;background:#1e222d;padding:8px 10px;color:white;">
    <div><b id="tvSymbol">{coin}/USDT</b> <span style="color:#787b86;font-size:12px;">1m</span> <span style="background:#2962ff;color:white;padding:2px 6px;border-radius:3px;font-size:11px;">Chart</span></div>
    <div style="text-align:right;"><b id="tvInrPrice" style="color:#26a65b;font-size:15px;">Rs.{price:,.0f}</b><br><span id="tvUsdPrice" style="color:#787b86;font-size:11px;">${usd_price:,.2f} USDT</span></div>
    </div>
    <div style="display:flex;justify-content:space-between;background:#1e222d;padding:4px 10px;color:#787b86;font-size:11px;border-top:1px solid #2a2e39;"><span>Timeframe: <b style="color:white;">1m</b> 5m 15m 1h 4h 1D</span><span>MA 7 | MA 25 | Vol</span></div>
    <div style="position:relative;">
    <canvas id="chart" width="360" height="320" style="width:100%;background:#131722;display:block;"></canvas>
    <div id="tvPriceTag" style="position:absolute;right:0;top:10px;background:#2962ff;color:white;font-size:11px;padding:2px 5px;border-radius:3px;">Rs.{price:,.0f}</div>
    </div>
    <div style="display:flex;justify-content:space-between;color:#787b86;font-size:10px;padding:4px 10px;background:#1e222d;"><span>07:00</span><span>08:00</span><span>09:00</span><span>10:00</span><span>11:00</span></div>
    <p style="text-align:center;color:#787b86;font-size:11px;margin:0;padding:4px;background:#1e222d;">Green = Upar | Red = Neeche | Blue = MA7 | Orange = MA25 | Neeche Volume | INR Price Right Side</p>
    </div>
    <div style="text-align:center;margin-top:10px;"><h3>History</h3>{hist_html}</div>
    <script>
    var curCoin='{coin}';
    var curTF='1m';
    var candles=[];
    var lineMode='Normal'; var hLines=[]; var vLines=[];
    var livePrice={price};
    function loadCandles(){{
        var sym=curCoin+'USDT';
        var interval=curTF;
        if(interval=='1D') interval='1d';
        fetch('https://api.binance.com/api/v3/klines?symbol='+sym+'&interval='+interval+'&limit=30')
        .then(r=>r.json()).then(d=>{{
            candles=d.map(k=>({{o:parseFloat(k[1])*88000, h:parseFloat(k[2])*88000, l:parseFloat(k[3])*88000, c:parseFloat(k[4])*88000, vol:parseFloat(k[5])}}));
            if(candles.length>0) candles[candles.length-1].c=livePrice;
            drawChart();
        }}).catch(e=>{{ console.log(e); }});
    }}
    loadCandles();
    setInterval(loadCandles, 30000);
    function drawChart(){{
        if(candles.length==0) return;
        var c=document.getElementById('chart'); var ctx=c.getContext('2d');
        var W=360, H=320, priceH=240, volH=60;
        var all=[]; for(var k=0;k<candles.length;k++){{ all.push(candles[k].h); all.push(candles[k].l); }}
        var min=Math.min.apply(null,all); var max=Math.max.apply(null,all);
        var range=(max-min)||1;
        min=min-range*0.15; max=max+range*0.15; range=(max-min)||1;
        var maxVol=0; for(var v=0;v<candles.length;v++) if(candles[v].vol>maxVol) maxVol=candles[v].vol;
        ctx.clearRect(0,0,W,H); ctx.fillStyle='#131722'; ctx.fillRect(0,0,W,H);
        ctx.strokeStyle='#1e222d'; ctx.lineWidth=1;
        for(var g=0;g<6;g++){{ ctx.beginPath(); ctx.moveTo(0,g*40); ctx.lineTo(W-40,g*40); ctx.stroke(); }}
        ctx.fillStyle='#787b86'; ctx.font='9px Arial'; ctx.textAlign='left';
        for(var p=0;p<6;p++){{ var pv=max - (p/5)*range; ctx.fillText('Rs.'+pv.toLocaleString('en-IN',{{maximumFractionDigits:0}}), W-58, p*40+12); }}
        var cw=(W-60)/30; var bw=Math.max(5, cw*0.65);
        var startX=(W-60) - candles.length*cw;
        for(var i=0;i<candles.length;i++){{
            var cd=candles[i]; var isGreen=cd.c>=cd.o; var col=isGreen?'rgba(38,166,91,0.7)':'rgba(231,76,60,0.7)';
            var x=startX+i*cw+cw/2; var vh=(cd.vol/maxVol)*volH;
            ctx.fillStyle=col; ctx.fillRect(x-bw/2, priceH+10+(volH-vh), bw, vh);
        }}
        for(var i=0;i<candles.length;i++){{
            var cd=candles[i]; var x=startX+i*cw+cw/2;
            var yH=((max-cd.h)/range)*priceH; var yL=((max-cd.l)/range)*priceH;
            var yO=((max-cd.o)/range)*priceH; var yC=((max-cd.c)/range)*priceH;
            var isGreen=cd.c>=cd.o; var col=isGreen?'#26a65b':'#ef5350';
            ctx.strokeStyle=col; ctx.lineWidth=1.2; ctx.beginPath(); ctx.moveTo(x,yH); ctx.lineTo(x,yL); ctx.stroke();
            var top=Math.min(yO,yC); var hgt=Math.max(4, Math.abs(yC-yO));
            ctx.fillStyle=col; ctx.fillRect(x-bw/2, top, bw, hgt);
            if(i==candles.length-1){{ ctx.beginPath(); ctx.arc(x,yC,3,0,2*Math.PI); ctx.fillStyle='white'; ctx.fill(); ctx.strokeStyle=col; ctx.stroke(); }}
        }}
        function drawMA(period, color){{
            ctx.beginPath(); ctx.strokeStyle=color; ctx.lineWidth=1.2; var started=false;
            for(var i=0;i<candles.length;i++){{
                if(i<period-1) continue;
                var sum=0; for(var j=i-period+1;j<=i;j++) sum+=candles[j].c;
                var ma=sum/period; var x=startX+i*cw+cw/2; var y=((max-ma)/range)*priceH;
                if(!started){{ ctx.moveTo(x,y); started=true; }} else ctx.lineTo(x,y);
            }} ctx.stroke();
        }}
        drawMA(7,'#2962ff'); drawMA(25,'#f9a825');
        var lastY=((max-livePrice)/range)*priceH;
        ctx.beginPath(); ctx.strokeStyle='#787b86'; ctx.setLineDash([4,4]); ctx.moveTo(0,lastY); ctx.lineTo(W-60,lastY); ctx.stroke(); ctx.setLineDash([]);
        for(var h=0;h<hLines.length;h++){{ ctx.beginPath(); ctx.strokeStyle='orange'; ctx.setLineDash([4,4]); ctx.moveTo(0,hLines[h]); ctx.lineTo(W-60,hLines[h]); ctx.stroke(); ctx.setLineDash([]); }}
        for(var v=0;v<vLines.length;v++){{ ctx.beginPath(); ctx.strokeStyle='#aa00ff'; ctx.setLineDash([4,4]); ctx.moveTo(vLines[v],0); ctx.lineTo(vLines[v],priceH); ctx.stroke(); ctx.setLineDash([]); }}
    }}
    document.getElementById('chart').addEventListener('click', function(e){{
        var rect=this.getBoundingClientRect();
        var x=(e.clientX-rect.left)/rect.width*360;
        var y=(e.clientY-rect.top)/rect.height*320;
        if(lineMode=='H') hLines.push(y);
        if(lineMode=='V') vLines.push(x);
        drawChart();
    }});
    function setLine(m){{ lineMode=m; alert(m+' Line Mode ON - Chart pe click karo'); }}
    function clearLines(){{ hLines=[]; vLines=[]; drawChart(); }}
    setInterval(function(){{
        fetch('/live_price').then(r=>r.json()).then(d=>{{
            livePrice=d.price;
            document.getElementById('livePrice').innerText=d.price.toLocaleString('en-IN',{{minimumFractionDigits:2}});
            document.getElementById('liveChg').innerText=d.change.toFixed(2)+'%';
            document.getElementById('liveSignal').innerText=d.change<0?'SHORT':'LONG';
            var usd=d.price/88000;
            document.getElementById('tvPriceTag').innerText='Rs.'+d.price.toLocaleString('en-IN',{{maximumFractionDigits:0}})+' | $'+usd.toLocaleString('en-US',{{maximumFractionDigits:2}});
            document.getElementById('tvInrPrice').innerText='Rs.'+d.price.toLocaleString('en-IN',{{maximumFractionDigits:0}});
            document.getElementById('tvUsdPrice').innerText='$'+usd.toLocaleString('en-US',{{maximumFractionDigits:2}}+' USDT');
            if(candles.length>0){{
                var last=candles[candles.length-1];
                last.c=d.price; if(d.price>last.h) last.h=d.price; if(d.price<last.l) last.l=d.price;
            }}
            drawChart();
        }});
    }}, 2000);
    function trade(s){{ if(confirm(s+' Lagao? ' + ('{ "REAL LIVE" if LIVE_MODE else "PAPER" }') + ' me trade hoga')) {{ fetch('/trade',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{side:s}})}}).then(r=>r.json()).then(d=>{{ if(d.error) alert(d.error); else location.reload(); }}); }} }}
    function resetBal(){{ fetch('/reset',{{method:'POST'}}).then(()=>location.reload()); }}
    function setCoin(c){{ curCoin=c; fetch('/settings',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{auto_coin:c, coin:c}})}}).then(()=>{{ loadCandles(); location.reload(); }}); }}
    function setTF(tf){{ curTF=tf; document.getElementById('tvSymbol').innerText=curCoin+'/USDT'; loadCandles(); fetch('/settings',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{timeframe:tf}})}}); }}
    function saveAll(){{ var d={{amount:document.getElementById('amt').value, leverage:document.getElementById('lev').value, tp_pct:document.getElementById('tp').value, sl_pct:document.getElementById('sl').value, trail_pct:document.getElementById('trail').value, close_pct:document.getElementById('close').value, min_range_pct:document.getElementById('minrange').value, auto:document.getElementById('auto').value=='ON', auto_pct:document.getElementById('autopct').value}}; fetch('/settings',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d)}}).then(()=>location.reload()); }}
    document.querySelectorAll('input,select').forEach(e=>e.addEventListener('change', saveAll));
    </script></body></html>
    """

@app.route("/live_price")
def live_price():
    p = get_price(state["auto_coin"])
    return jsonify({"price": p, "change": get_24h_change(), "coin": state["auto_coin"], "mode": state["mode"]})

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
    return jsonify({"state": state, "price": get_price(), "chart": list(price_history)[-50:], "live": LIVE_MODE})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
