import requests, hmac, hashlib, time, json, os, threading
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# ===== REAL TRADING - Key yaha mat likhna, Render > Environment me daalna =====
API_KEY = os.environ.get("COINSWITCH_API_KEY", "")
API_SECRET = os.environ.get("COINSWITCH_API_SECRET", "")
BASE = "https://api.coinswitch.co"

STATE = {
    "mode": "PAPER",  # PAPER = Nakli, LIVE = Asli
    "balance_inr": 10000.0, "locked_inr": 0.0,
    "pos": None, "history": [],
    "daily_pnl": 0.0, "daily_trades": 0, "last_reset_day": time.strftime("%Y-%m-%d"),
    "settings": {
        "auto": True, "leverage": 10, "amount": 1000,
        "tp": 1.0, "sl": 0.5, "trail": 0.5,
        "auto_coin": "BTC", "close_pct": 0.25, "min_range_pct": 0.40,
        "max_amount": 1000
        # NO max_daily_loss - Lock Hata Hua
        # NO max_daily_trades - Unlimited
    },
    "last_price": 0, "last_close_time": 0, "emergency_stop": False,
}

# ===== COINSWITCH REAL API =====
def cs_headers(method, endpoint, payload=""):
    ts = str(int(time.time()*1000))
    msg = method + endpoint + payload + ts
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {"X-Api-Key": API_KEY, "X-Api-Signature": sig, "X-Api-Timestamp": ts, "Content-Type": "application/json"}

def get_real_balance():
    if STATE["mode"] != "LIVE" or not API_KEY: return None
    try:
        r = requests.get(BASE + "/v2/futures/balance", headers=cs_headers("GET", "/v2/futures/balance"), timeout=10).json()
        for b in r.get("data", []):
            if b.get("currency") == "USDT":
                return float(b.get("available", 0)) * 88
    except Exception as e: print("Balance Error:", e)
    return None

def place_real_order(coin, side, qty):
    if STATE["mode"] != "LIVE": return {"paper": True}
    if not API_KEY: return {"error": "API Key nahi hai - Render Environment me daalo"}
    payload = json.dumps({"pair": f"{coin}/USDT", "side": side.lower(), "type": "market", "quantity": qty, "leverage": STATE["settings"]["leverage"]})
    try:
        r = requests.post(BASE + "/v2/futures/order", headers=cs_headers("POST", "/v2/futures/order", payload), data=payload, timeout=15).json()
        print("REAL ORDER:", r); return r
    except Exception as e: return {"error": str(e)}

def get_live_price(coin):
    try:
        t = requests.get("https://api.coindcx.com/exchange/ticker", timeout=8, headers={"User-Agent":"Mozilla/5.0"}).json()
        for m in t:
            if m.get("market") == f"{coin}INR":
                return float(m.get("last_price", 0)), float(m.get("change_24_hour", 0))
    except: pass
    try:
        r = requests.get(f"https://data-api.binance.vision/api/v3/ticker/24hr?symbol={coin}USDT", timeout=10).json()
        return float(r["lastPrice"])*88, float(r["priceChangePercent"])
    except: return 0, 0

def get_data_coin(coin, interval="1m"):
    pair = f"I-{coin}_INR"
    price_inr, ch24 = get_live_price(coin)
    candles_inr = []
    try:
        r = requests.get(f"https://public.coindcx.com/market_data/candles?pair={pair}&interval={interval}&limit=50", timeout=10, headers={"User-Agent":"Mozilla/5.0"}).json()
        raw = r.get("data", r) if isinstance(r, dict) else r
        for k in raw:
            if isinstance(k, dict):
                candles_inr.append({"time": int(k.get("time",0)), "open": float(k.get("open",0)), "high": float(k.get("high",0)), "low": float(k.get("low",0)), "close": float(k.get("close",0))})
            else:
                ts = int(k[0]); ts = int(ts/1000) if ts>10000000000 else ts
                candles_inr.append({"time": ts, "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4])})
        candles_inr = sorted(candles_inr, key=lambda x: x["time"])
        if not candles_inr: raise Exception("no candles")
        if price_inr == 0: price_inr = candles_inr[-1]["close"]
        last = candles_inr[-1]
        last["close"] = price_inr
        if price_inr > last["high"]: last["high"] = price_inr
        if price_inr < last["low"]: last["low"] = price_inr
        return price_inr, ch24, candles_inr
    except:
        try:
            sym = coin + "USDT"
            bi = {"1m":"1m","5m":"5m","15m":"15m","1h":"1h","4h":"4h","1d":"1d"}.get(interval,"1m")
            r2 = requests.get(f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval={bi}&limit=50", timeout=10).json()
            candles = [{"time":int(int(k[0])/1000),"open":float(k[1])*88,"high":float(k[2])*88,"low":float(k[3])*88,"close":float(k[4])*88} for k in r2]
            if candles:
                candles[-1]["close"] = price_inr if price_inr>0 else candles[-1]["close"]
                if price_inr>0:
                    if price_inr > candles[-1]["high"]: candles[-1]["high"] = price_inr
                    if price_inr < candles[-1]["low"]: candles[-1]["low"] = price_inr
            return price_inr, ch24, candles
        except: return 0, 0, []

def calc_pnl(pos, curr_inr):
    if not pos: return 0, 0, 0
    pct = ((curr_inr - pos["entry_inr"]) / pos["entry_inr"] * 100) if pos["side"]=="LONG" else ((pos["entry_inr"] - curr_inr) / pos["entry_inr"] * 100)
    gross = pos["amount"] * (pos["lev"] * pct / 100)
    fee = pos["amount"] * pos["lev"] * 0.001116  # Asli CoinSwitch Fee 0.1116%
    net = gross - fee
    return round(pct,3), round(gross,2), round(net,2)

def check_daily_reset():
    today = time.strftime("%Y-%m-%d")
    if STATE["last_reset_day"] != today:
        STATE["daily_pnl"] = 0; STATE["daily_trades"] = 0; STATE["last_reset_day"] = today

def close_pos(reason, curr_inr):
    pos = STATE["pos"]
    if not pos: return
    pct, gross, net = calc_pnl(pos, curr_inr)
    fee = pos["amount"] * pos["lev"] * 0.001116
    if STATE["mode"] == "LIVE":
        qty = (pos["amount"] * pos["lev"]) / pos["entry_inr"]
        close_side = "SELL" if pos["side"] == "LONG" else "BUY"
        res = place_real_order(pos["coin"], close_side, round(qty, 6))
        STATE["history"].insert(0, f"🔴 LIVE ORDER CLOSE: {res}")
    STATE["locked_inr"] -= pos["amount"]
    STATE["balance_inr"] += pos["amount"] + net
    STATE["daily_pnl"] += net; STATE["daily_trades"] += 1
    STATE["history"].insert(0, f"{pos['coin']} {pos['side']} {reason} @ ₹{curr_inr:,.2f} | Gross ₹{gross:.2f} - Fee ₹{fee:.2f} = Net ₹{net:.2f} ({pct}%) | [{STATE['mode']}] | 1m High ₹{pos.get('c_high',0):,.2f} Low ₹{pos.get('c_low',0):,.2f}")
    STATE["history"] = STATE["history"][:50]
    STATE["pos"] = None
    STATE["last_close_time"] = time.time()

def open_pos(coin, side, entry_inr, auto=False, c_low=0, c_high=0):
    s = STATE["settings"]
    check_daily_reset()
    if STATE["emergency_stop"]: return False
    if STATE["pos"]: return False
    # NO Max Trades Check - UNLIMITED
    # NO Daily Loss Check - Lock Hata Hua
    if s["amount"] > s["max_amount"]: s["amount"] = s["max_amount"]
    # Paise Khatam To Trade Band - Yehi Asli Lock Hai
    if STATE["balance_inr"] < s["amount"]: return False
    if time.time() - STATE["last_close_time"] < 2: return False
    if STATE["mode"] == "LIVE":
        qty = (s["amount"] * s["leverage"]) / entry_inr
        res = place_real_order(coin, side, round(qty, 6))
        if "error" in res:
            STATE["history"].insert(0, f"❌ LIVE ORDER FAIL: {res}")
            return False
        STATE["history"].insert(0, f"🟢 LIVE ORDER PLACED: {coin} {side} Qty {qty:.6f}")
    STATE["balance_inr"] -= s["amount"]
    STATE["locked_inr"] += s["amount"]
    STATE["pos"] = {"coin":coin,"side":side,"entry_inr":entry_inr,"amount":s["amount"],"lev":s["leverage"],"time":time.time(),"auto":auto,"peak":entry_inr,"trough":entry_inr,"c_low":c_low,"c_high":c_high}
    STATE["history"].insert(0, f"{coin} {side} {'HIGH-LOW' if auto else 'MANUAL'} Open @ ₹{entry_inr:,.2f} | Amt ₹{s['amount']} Lev {s['leverage']}x [{STATE['mode']}]")
    return True

def check_trade_logic(coin, price_inr, candles):
    s = STATE["settings"]
    if STATE["emergency_stop"]: return
    if len(candles) < 1: return
    c_low = candles[-1]["low"]; c_high = candles[-1]["high"]
    tol = price_inr * 0.0002  # Entry Tolerance 0.02%
    close_off = price_inr * (s.get("close_pct", 0.25) / 100)  # Close 0.25%
    candle_range = c_high - c_low
    min_range = price_inr * (s.get("min_range_pct", 0.40) / 100)  # Min Range 0.40%
    if not STATE["pos"]:
        # Chhoti Candle Skip - Fee Bachao
        if candle_range < min_range:
            STATE["last_price"] = price_inr; return
        if price_inr >= (c_high - tol):
            if (c_high - (c_low + close_off)) < close_off * 0.5: return
            open_pos(coin, "SHORT", price_inr, auto=True, c_low=c_low, c_high=c_high)
            return
        if price_inr <= (c_low + tol):
            if ((c_high - close_off) - c_low) < close_off * 0.5: return
            open_pos(coin, "LONG", price_inr, auto=True, c_low=c_low, c_high=c_high)
            return
    STATE["last_price"] = price_inr
    if STATE["pos"]:
        pos = STATE["pos"]; curr = price_inr
        if pos["side"]=="LONG":
            if curr > pos["peak"]: pos["peak"]=curr
        else:
            if curr < pos["trough"]: pos["trough"]=curr
        live_pct, gross, net = calc_pnl(pos, curr)
        off = pos["entry_inr"] * (s.get("close_pct", 0.25) / 100)
        if pos["side"]=="SHORT":
            target = pos["c_low"] + off
            if curr <= target and (pos["entry_inr"] - curr) >= off * 0.5:
                close_pos(f"CLOSE ✅ Low + 0.25% pe aaya", curr); return
        if pos["side"]=="LONG":
            target = pos["c_high"] - off
            if curr >= target and (curr - pos["entry_inr"]) >= off * 0.5:
                close_pos(f"CLOSE ✅ High - 0.25% pe aaya", curr); return
        if live_pct >= s["tp"]: close_pos("TP HIT ✅", curr)
        elif live_pct <= -s["sl"]: close_pos("SL HIT ❌", curr)
        elif pos["side"]=="LONG" and curr <= pos["peak"] * (1 - s["trail"]/100) and net > 0: close_pos(f"TRAIL HIT 📈", curr)
        elif pos["side"]=="SHORT" and curr >= pos["trough"] * (1 + s["trail"]/100) and net > 0: close_pos(f"TRAIL HIT 📉", curr)

def background_bot():
    print("🤖 FINAL BOT START - ALL LOGICS - NO LOCK - UNLIMITED")
    while True:
        try:
            check_daily_reset()
            s = STATE["settings"]
            if STATE["mode"] == "LIVE":
                rb = get_real_balance()
                if rb is not None and not STATE["pos"]:
                    STATE["balance_inr"] = rb
            coin_to_check = STATE["pos"]["coin"] if STATE["pos"] else s.get("auto_coin","BTC")
            price_inr, _, candles = get_data_coin(coin_to_check, "1m")
            live_p, _ = get_live_price(coin_to_check)
            if live_p>0: price_inr = live_p
            if price_inr>0:
                check_trade_logic(coin_to_check, price_inr, candles)
        except Exception as e:
            print("BG Error:", e)
        time.sleep(1)

threading.Thread(target=background_bot, daemon=True).start()

HTML = """
<style>body{font-family:Arial;text-align:center;padding:8px}button{padding:10px 14px;margin:3px;font-size:15px;border-radius:8px;border:1px solid #000}.long{background:#22c55e;color:#fff}.short{background:#ef4444;color:#fff}.close{background:#000;color:#fff}.tf{background:#eef}.tf.active{background:#000;color:#fff}.tool{background:#fff;border:2px solid #7c3aed}.tool.active{background:#7c3aed;color:#fff}.box{border:1px solid #ccc;border-radius:10px;padding:10px;margin:8px auto;max-width:860px;text-align:left}input,select{padding:8px;margin:4px;font-size:15px;width:110px}.on{color:green;font-weight:bold}.live{color:#ef4444;font-weight:bold;animation:blink 1s infinite}@keyframes blink{50%{opacity:0.3}}.tpbox{background:#f0fdf4;border:1px solid #22c55e;border-radius:8px;padding:8px;margin-top:8px}.slbox{background:#fef2f2;border:1px solid #ef4444;border-radius:8px;padding:8px;margin-top:6px}.hlbox{background:#fff7ed;border:1px solid #f59e0b;border-radius:8px;padding:8px;margin-top:8px}.live-badge{background:#ef4444;color:#fff;padding:6px 12px;border-radius:20px;font-weight:bold}.paper-badge{background:#22c55e;color:#fff;padding:6px 12px;border-radius:20px;font-weight:bold}.danger{background:#ef4444;color:#fff;font-weight:bold;padding:12px}</style>
<h2>🚀 Future Bot - FINAL - ALL LOGICS ✅</h2>
<div id="modeBadge"></div>
<div><button onclick="load('BTC')">BTC</button><button onclick="load('ETH')">ETH</button><button onclick="load('SOL')">SOL</button><button onclick="load('DOGE')">DOGE</button><button onclick="load('XRP')">XRP</button><button onclick="load('ADA')">ADA</button><button onclick="load('BNB')">BNB</button></div>
<h1 id="p">Loading...</h1>
<div><span class="live">● LIVE</span> <span style="font-size:14px">High pe SHORT | Low pe LONG | Close 0.25% | Fee 0.1116% | NO Daily Loss Lock | ♾️ Unlimited Trades | Paise Khatam To Trade Band</span></div>
<h2 id="bal" style="color:#0a7">💰 Wallet: ₹10,000</h2>
<h3 id="locked" style="color:#d97706">🔒 Locked: ₹0 | Total: ₹10,000</h3>
<h3 id="daily" style="color:#7c3aed">📊 Aaj Ka PnL: ₹0 | Trades: 0 | ♾️ Unlimited</h3>
<h3 id="pos">No Position</h3>
<h3 id="trailInfo" style="color:#7c3aed">Trailing: -</h3>
<h3 id="pnl" style="color:blue">Live PnL: ₹0</h3>
<div class="box"><b>⚙️ SETTING - SABHI OPTIONS</b><br>
Leverage: <select id="lev" onchange="saveSet()"><option>1</option><option>2</option><option>5</option><option selected>10</option><option>20</option></select> x &nbsp;
Amount ₹: <input id="amt" type="number" value="1000" onchange="saveSet()"> &nbsp;
TP %: <input id="tp" type="number" value="1.0" step="0.1" onchange="saveSet()"> &nbsp;
SL %: <input id="sl" type="number" value="0.5" step="0.1" onchange="saveSet()"> &nbsp;
Trail %: <input id="trail" type="number" value="0.5" step="0.1" onchange="saveSet()"><br>
<b>🛡️ SAFETY:</b> Max Amount / Trade ₹: <input id="max_amount" type="number" value="1000" onchange="saveSet()"> &nbsp; Max Trades / Din: <b>♾️ UNLIMITED</b> &nbsp; Daily Loss Lock: <b>❌ HATA HUA</b><br>
<b>🤖 AUTO:</b> <select id="auto" onchange="saveSet()"><option value="1" selected>ON ✅</option><option value="0">OFF ❌</option></select>
Auto Coin: <select id="auto_coin" onchange="saveSet()"><option>BTC</option><option>ETH</option><option>SOL</option><option>DOGE</option><option>XRP</option><option>ADA</option><option>BNB</option></select><br>
<span id="autoTxt" class="on">AUTO ON</span>
<div class="hlbox"><b>⚡ HIGH-LOW LOGIC - Live 1m Candle</b><br>
Close %: <input id="close_pct" type="number" value="0.25" step="0.05" onchange="saveSet()"> % &nbsp;
Min Candle Range %: <input id="min_range_pct" type="number" value="0.40" step="0.05" onchange="saveSet()"> %<br>
<span style="font-size:13px">SHORT = High pe khulega, Low + 0.25% pe band. LONG = Low pe khulega, High - 0.25% pe band. Fee 0.1116% kat ke Net Profit. Candle Range 0.40% se chhota hua to trade nahi khulega.</span>
</div>
<div id="tpPreview"></div>
</div>
<div class="box" style="border:2px solid #ef4444"><b>🔴 REAL TRADING CONTROL</b><br>
<span style="font-size:13px">PAPER = Nakli paisa. LIVE = Asli paisa. Daily Loss Lock HATA HUA hai. Unlimited Trades hai. Sirf Paise Khatam hone pe ya tu Emergency dabayega tabhi Bot rukega.</span><br><br>
<button onclick="setMode('PAPER')" style="background:#22c55e;color:#fff">🟢 PAPER Mode (Safe)</button>
<button onclick="setMode('LIVE')" style="background:#ef4444;color:#fff">🔴 LIVE Mode (Real Paisa)</button>
<button onclick="emergencyStop()" class="danger">🛑 EMERGENCY STOP - Sab Band Karo</button>
<button onclick="clearStop()">✅ Stop Hatayo</button>
</div>
"""
