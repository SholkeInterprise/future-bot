"""Binance USDⓈ-M Futures bot with INR dashboard display.

TESTNET is the default. Live orders and auto trading are disabled by default.
This is a starter integration: verify testnet behavior before enabling live.
"""
import os, time, threading, uuid, urllib.parse, hmac, hashlib
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from functools import wraps
from flask import Flask, jsonify, render_template_string, request, Response
import requests

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "NO").strip().upper() == "YES"
BASE_URL = "https://demo-fapi.binance.com" if BINANCE_TESTNET else "https://fapi.binance.com"
LIVE_TRADING = os.getenv("LIVE_TRADING", "NO").strip().upper() == "YES"
AUTO_TRADING_ALLOWED = os.getenv("AUTO_TRADING", "NO").strip().upper() == "YES"
MAX_MARGIN_USDT = os.getenv("MAX_MARGIN_USDT", "25")
USDT_INR_FALLBACK = os.getenv("USDT_INR_RATE", "").strip()
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
app = Flask(__name__)
HTTP = requests.Session()
COINS = ["BTC", "ETH", "SOL", "DOGE", "XRP", "ADA", "BNB"]
INTERVALS = {"1m":"1m", "5m":"5m", "15m":"15m", "1h":"1h", "4h":"4h", "1d":"1d"}
LOCK = threading.RLock()
STATE = {
    "settings": {"auto": False, "leverage": 2, "amount": 900.0, "tp": 1.0,
                 "sl": 0.5, "trail": 0.5, "auto_pct": 0.5,
                 "auto_coin": "BTC", "close_pct": 0.25, "min_range_pct": 0.40},
    "history": [], "last_price": 0.0, "last_time": 0.0,
    "last_close_time": 0.0, "last_error": None, "pos_meta": None,
}

class BinanceError(RuntimeError):
    pass

class BinanceUSDMFutures:
    def __init__(self, api_key, api_secret):
        self.api_key, self.api_secret = api_key, api_secret

    def request(self, method, path, params=None, signed=False):
        params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}
        if signed:
            if not self.api_key or not self.api_secret:
                raise BinanceError("Render me BINANCE_API_KEY aur BINANCE_API_SECRET set karein.")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 10000
            query = urllib.parse.urlencode(params, doseq=True)
            params["signature"] = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        try:
            resp = HTTP.request(method.upper(), BASE_URL + path, params=params,
                                headers=headers, timeout=15)
            try: data = resp.json()
            except Exception: data = {"raw": resp.text[:1000]}
            if not resp.ok:
                raise BinanceError(f"Binance HTTP {resp.status_code}: {data}")
            if isinstance(data, dict) and "code" in data and int(data.get("code", 0)) < 0:
                raise BinanceError(str(data))
            return data
        except requests.RequestException as exc:
            raise BinanceError(f"Binance connection error: {exc}")

    def wallet(self):
        return self.request("GET", "/fapi/v2/account", signed=True)

    def ticker(self, symbol):
        return self.request("GET", "/fapi/v1/ticker/24hr", {"symbol":symbol})

    def mark_price(self, symbol):
        return self.request("GET", "/fapi/v1/premiumIndex", {"symbol":symbol})

    def positions(self, symbol):
        rows = self.request("GET", "/fapi/v2/positionRisk", {"symbol":symbol}, signed=True)
        out=[]
        for p in rows if isinstance(rows, list) else []:
            amt=Decimal(str(p.get("positionAmt", "0")))
            if amt == 0: continue
            side="LONG" if amt > 0 else "SHORT"
            out.append({"symbol":p.get("symbol"), "status":"OPEN", "position_size":str(abs(amt)),
                        "position_side":side, "avg_entry_price":p.get("entryPrice", "0"),
                        "mark_price":p.get("markPrice", "0"), "unrealised_pnl":p.get("unRealizedProfit", "0"),
                        "position_margin":p.get("isolatedWallet", "0"), "leverage":p.get("leverage", "1"),
                        "liquidation_price":p.get("liquidationPrice", "0")})
        return out

    def klines(self, symbol, interval):
        return self.request("GET", "/fapi/v1/klines", {"symbol":symbol,"interval":interval,"limit":50})

    def instruments(self):
        return self.request("GET", "/fapi/v1/exchangeInfo")

    def set_leverage(self, symbol, leverage):
        return self.request("POST", "/fapi/v1/leverage", {"symbol":symbol,"leverage":int(leverage)}, signed=True)

    def cancel_algo_orders(self, symbol):
        # Cancel all outstanding exchange-side conditional TP/SL/trailing orders.
        return self.request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol":symbol}, signed=True)

    def position_mode(self):
        return self.request("GET", "/fapi/v1/positionSide/dual", signed=True)

    def order(self, symbol, side, order_type, quantity, **extra):
        # USD-M Futures conditional orders must use the Algo Order service.
        if order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"):
            params = {"algoType":"CONDITIONAL", "symbol":symbol, "side":side,
                      "type":order_type, "workingType":"MARK_PRICE"}
            if order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
                # Close-All conditional orders must NOT include quantity/reduceOnly.
                params["triggerPrice"] = str(extra.pop("trigger_price"))
                params["closePosition"] = "true"
                extra.pop("reduce_only", None)
            else:
                qty = Decimal(str(quantity))
                if qty <= 0:
                    raise BinanceError("Trailing order quantity zero/invalid")
                params["quantity"] = format(qty, 'f')
                params["activatePrice"] = str(extra.pop("activate_price"))
                params["callbackRate"] = str(extra.pop("callback_rate"))
                if extra.pop("reduce_only", True):
                    params["reduceOnly"] = "true"
            params.update(extra)
            return self.request("POST", "/fapi/v1/algoOrder", params, signed=True)
        params={"symbol":symbol,"side":side,"type":order_type,"newOrderRespType":"RESULT"}
        if Decimal(str(quantity)) <= 0:
            raise BinanceError("Order quantity zero/invalid")
        params["quantity"] = format(Decimal(str(quantity)), 'f')
        if extra.pop("reduce_only", False):
            params["reduceOnly"] = "true"
        params.update(extra)
        return self.request("POST", "/fapi/v1/order", params, signed=True)

CLIENT = BinanceUSDMFutures(BINANCE_API_KEY, BINANCE_API_SECRET)
CLIENT_ERROR = None

def unwrap(obj):
    return obj.get("data", obj) if isinstance(obj, dict) else obj

def find_num(obj, names):
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key.lower() in names:
                try: return Decimal(str(val))
                except Exception: pass
        for val in obj.values():
            found = find_num(val, names)
            if found is not None: return found
    elif isinstance(obj, list):
        for val in obj:
            found = find_num(val, names)
            if found is not None: return found
    return None

def require_client():
    return CLIENT

def get_instrument(symbol):
    data = require_client().instruments()
    rows = data.get("symbols", []) if isinstance(data, dict) else []
    item = next((x for x in rows if x.get("symbol") == symbol), None)
    if not item or item.get("status") != "TRADING":
        raise BinanceError(f"{symbol} Binance USDⓈ-M Futures par TRADING status me nahi hai.")
    filters = {f.get("filterType"):f for f in item.get("filters", [])}
    lot = filters.get("LOT_SIZE", {})
    market_lot = filters.get("MARKET_LOT_SIZE", lot)
    price_filter = filters.get("PRICE_FILTER", {})
    return {"status":item.get("status"), "min_leverage":1, "max_leverage":125,
            "price_precision":item.get("pricePrecision", 2),
            "tick_size":price_filter.get("tickSize", "0.01"),
            "base_quantity_step_size":market_lot.get("stepSize", lot.get("stepSize", "0.001")),
            "min_base_quantity":market_lot.get("minQty", lot.get("minQty", "0")),
            "max_market_base_quantity":market_lot.get("maxQty", "0"),
            "quantity_precision":item.get("quantityPrecision", 3)}

def get_usdt_inr_rate():
    """Fetch a public indicative USDT/INR rate; never silently invent a rate."""
    try:
        rows = HTTP.get("https://api.coindcx.com/exchange/ticker", timeout=8,
                        headers={"User-Agent":"FutureBot/1.0"}).json()
        for row in rows if isinstance(rows, list) else []:
            if str(row.get("market", "")).upper() == "USDTINR":
                rate = D(row.get("last_price", 0))
                if rate > 0: return rate
    except Exception:
        pass
    if USDT_INR_FALLBACK:
        rate = D(USDT_INR_FALLBACK)
        if rate > 0: return rate
    raise BinanceError("USDT/INR conversion rate unavailable. Try again later or set USDT_INR_RATE in Render Environment.")

def get_price(coin):
    symbol = coin + "USDT"
    data = require_client().ticker(symbol)
    p = D(data.get("lastPrice", 0))
    if p <= 0: raise BinanceError(f"Binance ticker invalid: {data}")
    change = D(data.get("priceChangePercent", 0))
    return float(p), float(change)

def get_candles(coin, interval="1m"):
    rows = require_client().klines(coin + "USDT", INTERVALS.get(interval, "1m"))
    candles=[]
    for c in rows if isinstance(rows, list) else []:
        try:
            candles.append({"time":int(c[0]/1000), "open":float(c[1]), "high":float(c[2]),
                            "low":float(c[3]), "close":float(c[4])})
        except Exception: continue
    return candles

def get_position(coin):
    rows = require_client().positions(coin + "USDT")
    if not rows:
        return None
    if len(rows) > 1:
        raise BinanceError(f"{coin}USDT par multiple position sides mile. Binance account ko One-way Mode me set karein.")
    return rows[0]

def record(msg):
    STATE["history"].insert(0, time.strftime("%Y-%m-%d %H:%M:%S") + " | " + str(msg)[:700])
    del STATE["history"][50:]

def D(value):
    try: return Decimal(str(value))
    except (InvalidOperation, TypeError): raise BinanceError(f"Invalid number: {value}")

def round_step(value, step):
    value, step = D(value), D(step)
    if step <= 0: raise BinanceError("Exchange quantity/price step invalid")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

def tick_step(info):
    return D(info.get("tick_size", "0.01"))

def wallet_balances():
    raw = require_client().wallet()
    asset = next((x for x in raw.get("assets", []) if x.get("asset") == "USDT"), {})
    return {"total":float(D(asset.get("walletBalance", 0))),
            "available":float(D(asset.get("availableBalance", 0))),
            "blocked":float(max(Decimal("0"), D(asset.get("walletBalance", 0))-D(asset.get("availableBalance", 0))))}

def protective_orders(coin, side, entry, info, position_size):
    """Place exchange-hosted TP, SL and trailing stop Algo orders."""
    symbol = coin + "USDT"
    s = STATE["settings"]
    tp_pct, sl_pct, trail_pct = D(s["tp"])/100, D(s["sl"])/100, D(s["trail"])
    if trail_pct < Decimal("0.1") or trail_pct > Decimal("10"):
        raise BinanceError("Binance trailing callback 0.1% se 10% ke beech hona chahiye.")
    tick = tick_step(info)
    if side == "LONG":
        exit_side = "SELL"; sl = entry * (1-sl_pct); tp = entry * (1+tp_pct)
    else:
        exit_side = "BUY"; sl = entry * (1+sl_pct); tp = entry * (1-tp_pct)
    sl = (sl/tick).to_integral_value(rounding=ROUND_HALF_UP)*tick
    tp = (tp/tick).to_integral_value(rounding=ROUND_HALF_UP)*tick
    activation = (entry/tick).to_integral_value(rounding=ROUND_HALF_UP)*tick
    # closePosition=true closes the full one-way position; no quantity/reduceOnly is sent.
    sl_result = require_client().order(symbol, exit_side, "STOP_MARKET", 0, trigger_price=str(sl))
    tp_result = require_client().order(symbol, exit_side, "TAKE_PROFIT_MARKET", 0, trigger_price=str(tp))
    # Native Binance trailing stop is exchange-hosted and remains active if Render sleeps.
    trail_result = require_client().order(symbol, exit_side, "TRAILING_STOP_MARKET", position_size,
                                          activate_price=str(activation), callback_rate=str(trail_pct),
                                          reduce_only=True)
    return {"stop_loss":sl_result, "take_profit":tp_result, "trailing_stop":trail_result,
            "sl_trigger":str(sl), "tp_trigger":str(tp), "trail_activation":str(activation),
            "trail_callback":str(trail_pct)}

def open_live(coin, side, margin, leverage, auto=False, candle_low=0, candle_high=0):
    margin = D(margin)
    fx = get_usdt_inr_rate()
    margin = margin / fx  # Dashboard input is INR; exchange order margin is USDT.
    if margin < Decimal("5"):
        raise BinanceError(f"Minimum margin 5 USDT (lagbhag ₹{(Decimal(5)*fx):,.0f}) hai.")
    if margin > D(MAX_MARGIN_USDT):
        raise BinanceError(f"Per-trade limit ₹{(D(MAX_MARGIN_USDT)*fx):,.0f} (USDT limit) se zyada hai.")
    if not LIVE_TRADING: raise BinanceError("LIVE_TRADING=YES set nahi hai. Live order locked hai.")
    symbol = coin + "USDT"
    mode = require_client().position_mode()
    if bool(mode.get("dualSidePosition", False)):
        raise BinanceError("Binance Futures ko One-way Mode par set karein; Hedge Mode is bot version me supported nahi hai.")
    if get_position(coin): raise BinanceError(f"{symbol} par pehle se position khuli hai. Pehle usko manage/close karein.")
    # Clear stale protective orders from any earlier flat position before a new entry.
    require_client().cancel_algo_orders(symbol)
    price, _ = get_price(coin)
    info = get_instrument(symbol)
    if str(info.get("status", "TRADING")).upper() != "TRADING": raise BinanceError(f"{symbol} abhi trading status me nahi hai")
    lev_min, lev_max = D(info.get("min_leverage", 1)), D(info.get("max_leverage", 1))
    if D(leverage) < lev_min or D(leverage) > lev_max: raise BinanceError(f"Leverage allowed range {lev_min}-{lev_max}x hai")
    wallet = wallet_balances()
    if D(margin) > D(wallet["available"]): raise BinanceError(f"Available wallet lagbhag ₹{(D(wallet['available'])*fx):,.2f} hai")
    notional = D(margin) * D(leverage)
    qty_step = D(info.get("base_quantity_step_size", info.get("lot_size", "0.000001")))
    qty = round_step(notional / D(price), qty_step)
    min_qty = D(info.get("min_base_quantity", "0"))
    max_qty = D(info.get("max_market_base_quantity", "0"))
    if qty < min_qty: raise BinanceError(f"Calculated quantity {qty} minimum {min_qty} se kam hai")
    if max_qty > 0 and qty > max_qty: raise BinanceError(f"Calculated quantity {qty} maximum market quantity {max_qty} se zyada hai")
    require_client().set_leverage(symbol, leverage)
    order_result = require_client().order(symbol, "BUY" if side == "LONG" else "SELL", "MARKET", qty)
    # Wait for exchange position confirmation; do not retry blindly if exchange response is delayed.
    pos = None
    for _ in range(8):
        time.sleep(1)
        pos = get_position(coin)
        if pos: break
    if not pos:
        raise BinanceError("Market order API ne response diya, lekin position confirm nahi hui. Binance app me Orders/Positions check karein; dobara click na karein.")
    entry = D(pos.get("avg_entry_price", price))
    try:
        protections = protective_orders(coin, side, entry, info, D(pos.get("position_size", qty)))
    except Exception as exc:
        record(f"CRITICAL: position open but protective order setup failed: {exc}")
        # Best-effort emergency reduce-only close; never claim the position is protected.
        try:
            size = D(pos.get("position_size", qty))
            require_client().order(symbol, "SELL" if side == "LONG" else "BUY", "MARKET", size, reduce_only=True)
            require_client().cancel_algo_orders(symbol)
        except Exception as close_exc:
            record(f"CRITICAL: emergency close/cancel also failed: {close_exc}")
        raise BinanceError(f"Position open hui, lekin TP/SL set nahi hua: {exc}. Binance app me turant position check karein.")
    STATE["pos_meta"] = {"coin":coin,"side":side,"entry":float(entry),"peak":float(entry),
                         "trough":float(entry),"candle_low":float(candle_low),
                         "candle_high":float(candle_high),"auto":bool(auto)}
    record(f"{coin} {side} {'AUTO' if auto else 'MANUAL'} opened | entry {entry} USDT | qty {qty} | lev {leverage}x | SL {protections['sl_trigger']} | TP {protections['tp_trigger']}")
    return {"order":order_result,"position":pos,"protection":protections}

def close_live(coin, reason="MANUAL CLOSE"):
    if not LIVE_TRADING: raise BinanceError("LIVE_TRADING=YES set nahi hai. Live close order locked hai.")
    pos = get_position(coin)
    if not pos: return {"message":"Is coin par open position nahi mili."}
    size = D(pos.get("position_size", "0"))
    side = str(pos.get("position_side", "")).upper()
    if size <= 0 or side not in ("LONG", "SHORT"):
        raise BinanceError(f"Position details samajh nahi aaye; close order nahi bheja: {pos}")
    result = require_client().order(coin+"USDT", "SELL" if side == "LONG" else "BUY",
                                    "MARKET", size, reduce_only=True)
    try:
        require_client().cancel_algo_orders(coin+"USDT")
    except Exception as cancel_exc:
        record(f"WARNING: position close sent but conditional-order cancellation failed: {cancel_exc}")
    record(f"{coin} {side} reduce-only market close requested: {reason}")
    STATE["pos_meta"] = None
    STATE["last_close_time"] = time.time()
    return result

def check_auto_strategy(coin, price, candles):
    s = STATE["settings"]
    if not (s["auto"] and LIVE_TRADING and AUTO_TRADING_ALLOWED): return
    if not candles: return
    pos = get_position(coin)
    if not pos:
        if time.time() - STATE["last_close_time"] < 3: return
        low, high = candles[-1]["low"], candles[-1]["high"]
        tol = price * 0.0002
        close_off = price * (s["close_pct"] / 100)
        if high-low < price * (s["min_range_pct"] / 100): return
        if price >= high-tol and (high-(low+close_off)) >= close_off*0.5:
            open_live(coin, "SHORT", s["amount"], s["leverage"], True, low, high)
        elif price <= low+tol and ((high-close_off)-low) >= close_off*0.5:
            open_live(coin, "LONG", s["amount"], s["leverage"], True, low, high)
        return
    side = str(pos.get("position_side", "")).upper()
    entry = float(D(pos.get("avg_entry_price", price)))
    meta = STATE.get("pos_meta") or {"coin":coin,"side":side,"entry":entry,"peak":entry,"trough":entry,"candle_low":0,"candle_high":0,"auto":True}
    if side == "LONG": meta["peak"] = max(meta.get("peak",entry), price)
    else: meta["trough"] = min(meta.get("trough",entry), price)
    STATE["pos_meta"] = meta
    off = entry * (s["close_pct"] / 100)
    if meta.get("auto"):
        if side == "LONG" and meta.get("candle_high",0) and price >= meta["candle_high"]-off and price-entry >= off*0.5:
            close_live(coin, "HIGH-LOW TARGET")
        elif side == "SHORT" and meta.get("candle_low",0) and price <= meta["candle_low"]+off and entry-price >= off*0.5:
            close_live(coin, "HIGH-LOW TARGET")
    # TP/SL/trailing are now exchange-hosted Algo orders. Do not duplicate them
    # with an app-side trailing close, which could race the exchange order.

def background_bot():
    while True:
        try:
            s = STATE["settings"]
            if not (s["auto"] and AUTO_TRADING_ALLOWED and LIVE_TRADING):
                time.sleep(5)
                continue
            coin = s.get("auto_coin", "BTC")
            price, _ = get_price(coin)
            candles = get_candles(coin, "1m")
            if price > 0:
                STATE["last_price"] = price
                check_auto_strategy(coin, price, candles)
            STATE["last_error"] = None
        except Exception as exc:
            STATE["last_error"] = str(exc)
            record("AUTO/monitor error: " + str(exc))
        time.sleep(5)


def auth_required():
    if not DASHBOARD_PASSWORD:
        return Response("Set DASHBOARD_PASSWORD in Render Environment before using this app.", 503)
    auth = request.authorization
    if not auth or not hmac.compare_digest(auth.username or "", DASHBOARD_USER) or not hmac.compare_digest(auth.password or "", DASHBOARD_PASSWORD):
        return Response("Login required", 401, {"WWW-Authenticate":"Basic realm=FutureBot"})
    return None

@app.before_request
def protect_dashboard():
    if request.path == "/healthz": return None
    return auth_required()

HTML = '\n<style>body{font-family:Arial;text-align:center;padding:8px}button{padding:10px 14px;margin:3px;font-size:15px;border-radius:8px;border:1px solid #000}.long{background:#22c55e;color:#fff}.short{background:#ef4444;color:#fff}.close{background:#000;color:#fff}.tf{background:#eef}.tf.active{background:#000;color:#fff}.tool{background:#fff;border:2px solid #7c3aed}.tool.active{background:#7c3aed;color:#fff}.box{border:1px solid #ccc;border-radius:10px;padding:10px;margin:8px auto;max-width:860px;text-align:left}input,select{padding:8px;margin:4px;font-size:15px;width:110px}.on{color:green;font-weight:bold}.off{color:red;font-weight:bold}.live{color:#ef4444;font-weight:bold;animation:blink 1s infinite}@keyframes blink{50%{opacity:0.3}}.tpbox{background:#f0fdf4;border:1px solid #22c55e;border-radius:8px;padding:8px;margin-top:8px}.slbox{background:#fef2f2;border:1px solid #ef4444;border-radius:8px;padding:8px;margin-top:6px}.hlbox{background:#fff7ed;border:1px solid #f59e0b;border-radius:8px;padding:8px;margin-top:8px}</style>\n<h2>🚀 Future Bot — Binance USDⓈ-M Futures (₹ Display)</h2>\n<div style="background:#fff3cd;padding:9px;border-radius:8px;font-size:13px"><b>महत्वपूर्ण:</b> Dashboard की रकम USDT/INR रेट से ₹ में दिखाई जाती है। Binance USDⓈ-M Futures API में ऑर्डर और वास्तविक सेटलमेंट USDT में ही हैं; यह INR Futures API नहीं है।</div>\n<div><button onclick="load(\'BTC\')">BTC</button><button onclick="load(\'ETH\')">ETH</button><button onclick="load(\'SOL\')">SOL</button><button onclick="load(\'DOGE\')">DOGE</button><button onclick="load(\'XRP\')">XRP</button><button onclick="load(\'ADA\')">ADA</button><button onclick="load(\'BNB\')">BNB</button></div>\n<h1 id="p">Loading...</h1>\n<div><span class="live">● LIVE</span> <span style="font-size:14px">High pe SHORT | Low pe LONG | Close = 0.25% andar | Fees/funding vary — no fixed-profit guarantee | Chhoti Candle Skip</span></div>\n<h2 id="bal" style="color:#0a7">💰 Futures Wallet: ₹ --</h2>\n<h3 id="locked" style="color:#d97706">🔒 Locked: ₹ -- | Total: ₹ --</h3>\n<h3 id="pos">No Position</h3>\n<h3 id="trailInfo" style="color:#7c3aed">Trailing: -</h3>\n<h3 id="pnl" style="color:blue">Live PnL: ₹ 0</h3>\n<div class="box"><b>⚙️ SETTING</b><br>\nLeverage: <select id="lev" onchange="saveSet()"><option>1</option><option selected>2</option><option>5</option><option>10</option><option>20</option></select> x &nbsp;\nMargin ₹: <input id="amt" type="number" value="900" onchange="saveSet()"> &nbsp;\nTP %: <input id="tp" type="number" value="1.0" step="0.1" onchange="saveSet()"> &nbsp;\nSL %: <input id="sl" type="number" value="0.5" step="0.1" onchange="saveSet()"> &nbsp;\nTrail %: <input id="trail" type="number" value="0.5" step="0.1" onchange="saveSet()"><br>\n<b>🤖 AUTO:</b> <select id="auto" onchange="saveSet()"><option value="1">ON (server permission needed)</option><option value="0" selected>OFF ❌</option></select>\nAuto %: <input id="auto_pct" type="number" value="0.5" step="0.1" onchange="saveSet()"> % &nbsp;\nAuto Coin: <select id="auto_coin" onchange="saveSet()"><option>BTC</option><option>ETH</option><option>SOL</option><option>DOGE</option><option>XRP</option><option>ADA</option><option>BNB</option></select><br>\n<span id="autoTxt" class="on">AUTO ON</span>\n<div class="hlbox"><b>⚡ HIGH-LOW FIXED LOGIC - Live 1m Candle</b><br>\nClose %: <input id="close_pct" type="number" value="0.25" step="0.05" onchange="saveSet()"> % &nbsp;\nMin Candle Range %: <input id="min_range_pct" type="number" value="0.40" step="0.05" onchange="saveSet()"> %<br>\n<span style="font-size:13px">SHORT = High pe khulega, Low + 0.25% pe band. LONG = Low pe khulega, High - 0.25% pe band. Actual fees, funding and slippage apply; profit is not guaranteed. Candle Range 0.40% se chhota hua to trade nahi khulega - Fee bachega.</span>\n</div>\n<div id="tpPreview"></div>\n</div>\n<div><button class="long" onclick="trade(\'LONG\')">🟢 LONG Lagao</button><button class="short" onclick="trade(\'SHORT\')">🔴 SHORT Lagao</button><button class="close" onclick="trade(\'CLOSE\')">❌ Band Karo</button><button onclick="resetBal()">🔄 Refresh Wallet</button></div>\n<div style="margin-top:12px"><b>⏱️ TIME FRAME:</b>\n<button class="tf active" id="tf-1m" onclick="setTF(\'1m\')">1m</button><button class="tf" id="tf-5m" onclick="setTF(\'5m\')">5m</button><button class="tf" id="tf-15m" onclick="setTF(\'15m\')">15m</button><button class="tf" id="tf-1h" onclick="setTF(\'1h\')">1h</button><button class="tf" id="tf-4h" onclick="setTF(\'4h\')">4h</button><button class="tf" id="tf-1d" onclick="setTF(\'1d\')">1D</button>\n</div>\n<div style="margin-top:8px"><b>📐 LINE TOOL:</b>\n<button class="tool active" id="tool-none" onclick="setTool(\'none\')">🖱️ Normal</button><button class="tool" id="tool-h" onclick="setTool(\'h\')">➖ H-Line (Price)</button><button class="tool" id="tool-v" onclick="setTool(\'v\')">➡️ V-Line (Time)</button><button class="tool" onclick="clearLines()">🗑️ Lines Clear</button>\n</div>\n<canvas id="c" width="860" height="340" style="border:1px solid #000;width:100%;max-width:860px;margin-top:10px;cursor:crosshair"></canvas>\n<h3>📜 History</h3><div id="h"></div>\n<script>\nlet cur=\'BTC\'; let tf=\'1m\'; let tool=\'none\'; let hLines=[]; let vLines=[]; let lastCandles=[]; let lastPrice=0; let curPos=null; let curEntry=0;\nfunction setTF(t){tf=t;document.querySelectorAll(\'.tf\').forEach(b=>b.classList.remove(\'active\'));document.getElementById(\'tf-\'+t).classList.add(\'active\');fetchData();}\nfunction setTool(t){tool=t;document.querySelectorAll(\'.tool\').forEach(b=>b.classList.remove(\'active\'));if(t!=\'none\')document.getElementById(\'tool-\'+t).classList.add(\'active\');else document.getElementById(\'tool-none\').classList.add(\'active\');}\nfunction clearLines(){hLines=[];vLines=[];drawChart();}\nasync function load(coin){cur=coin;document.getElementById(\'auto_coin\').value=coin;saveSet();fetchData();}\nasync function saveSet(){let s={leverage:document.getElementById(\'lev\').value,amount:document.getElementById(\'amt\').value,tp:document.getElementById(\'tp\').value,sl:document.getElementById(\'sl\').value,trail:document.getElementById(\'trail\').value,auto:document.getElementById(\'auto\').value,auto_pct:document.getElementById(\'auto_pct\').value,auto_coin:document.getElementById(\'auto_coin\').value,close_pct:document.getElementById(\'close_pct\').value,min_range_pct:document.getElementById(\'min_range_pct\').value};await fetch(\'/api/settings?leverage=\'+s.leverage+\'&amount=\'+s.amount+\'&tp=\'+s.tp+\'&sl=\'+s.sl+\'&trail=\'+s.trail+\'&auto=\'+s.auto+\'&auto_pct=\'+s.auto_pct+\'&auto_coin=\'+s.auto_coin+\'&close_pct=\'+s.close_pct+\'&min_range_pct=\'+s.min_range_pct);document.getElementById(\'autoTxt\').innerText=s.auto==\'1\'?\'AUTO ON - High pe SHORT | Low pe LONG | Close \'+s.close_pct+\'%\':\'AUTO OFF\';updateTpSlPreview();}\nasync function trade(side){let q={coin:cur,side:side};if(side!==\'CLOSE\'&&!confirm(\'REAL Binance Futures order: \'+side+\' \'+cur+\'?\'))return;let r=await fetch(\'/api/trade\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(q)});let d=await r.json();alert(d.msg||JSON.stringify(d));fetchData();}\nasync function resetBal(){fetchData();alert(\'Live exchange wallet cannot be reset from this dashboard.\');}\nfunction fmtINR(n){return \'₹ \'+Number(n).toLocaleString(\'en-IN\',{minimumFractionDigits:2, maximumFractionDigits:2});}\nfunction fmtPrice(n){return Number(n).toLocaleString(\'en-IN\',{minimumFractionDigits:2, maximumFractionDigits:2});}\nfunction updateTpSlPreview(){\nlet tp=parseFloat(document.getElementById(\'tp\').value)||0;let sl=parseFloat(document.getElementById(\'sl\').value)||0;let lev=parseFloat(document.getElementById(\'lev\').value)||10;let amt=parseFloat(document.getElementById(\'amt\').value)||1000;let fee=amt*lev*0.0013;let tpGross=amt*lev*tp/100;let slGross=amt*lev*sl/100;let tpNet=tpGross-fee;let slNet=-(slGross+fee);let basePrice=(curPos&&curEntry>0)?curEntry:lastPrice;let side=(curPos&&curPos.side)?curPos.side:\'LONG\';let tpPrice,slPrice;if(side==\'LONG\'){tpPrice=basePrice*(1+tp/100);slPrice=basePrice*(1-sl/100);}else{tpPrice=basePrice*(1-tp/100);slPrice=basePrice*(1+sl/100);}let baseLabel=(curPos&&curEntry>0)?(\'Entry ₹ \'+fmtPrice(basePrice)+\' (\'+side+\') se\'):(\'Live ₹ \'+fmtPrice(basePrice)+\' pe \'+side+\' lagao to\');let html=\'<div class="tpbox"><b>🎯 TP Preview - \'+baseLabel+\'</b><br>✅ TP \'+tp+\'% → Price <b>₹ \'+fmtPrice(tpPrice)+\'</b> | Gross +₹ \'+tpGross.toFixed(2)+\' - Estimated fee ₹ \'+fee.toFixed(2)+\' = Net <b style="color:green">+₹ \'+tpNet.toFixed(2)+\'</b></div>\';html+=\'<div class="slbox"><b>🛑 SL Preview - \'+baseLabel+\'</b><br>❌ SL \'+sl+\'% → Price <b>₹ \'+fmtPrice(slPrice)+\'</b> | Gross -₹ \'+slGross.toFixed(2)+\' - Estimated fee ₹ \'+fee.toFixed(2)+\' = Net <b style="color:red">₹ \'+slNet.toFixed(2)+\'</b></div>\';document.getElementById(\'tpPreview\').innerHTML=html;}\nfunction drawChart(){let cv=document.getElementById(\'c\');let x=cv.getContext(\'2d\');x.clearRect(0,0,860,340);if(!lastCandles.length)return;let allLow=[...lastCandles.map(a=>a.low),lastPrice];let allHigh=[...lastCandles.map(a=>a.high),lastPrice];let lo=Math.min(...allLow);let hi=Math.max(...allHigh);let rg=hi-lo||1;lo-=rg*0.08;hi+=rg*0.08;rg=hi-lo;let chartW=700;let w=chartW/lastCandles.length;let yOf=p=>10+((hi-p)/rg)*300;let xOf=i=>i*w+w/2;x.font=\'12px Arial\';x.fillStyle=\'#666\';x.strokeStyle=\'#eee\';for(let i=0;i<=5;i++){let p=lo+(rg*i/5);let y=yOf(p);x.beginPath();x.moveTo(0,y);x.lineTo(chartW,y);x.stroke();x.fillText(fmtINR(p),708,y+4);}let yL=yOf(lastPrice);x.strokeStyle=\'#2563eb\';x.setLineDash([4,3]);x.lineWidth=1;x.beginPath();x.moveTo(0,yL);x.lineTo(chartW,yL);x.stroke();x.setLineDash([]);x.lineWidth=1;hLines.forEach(p=>{let y=yOf(p);x.strokeStyle=\'#7c3aed\';x.setLineDash([6,4]);x.lineWidth=1;x.beginPath();x.moveTo(0,y);x.lineTo(chartW,y);x.stroke();x.setLineDash([]);});vLines.forEach(idx=>{let px=xOf(idx);x.strokeStyle=\'#f59e0b\';x.setLineDash([6,4]);x.lineWidth=1;x.beginPath();x.moveTo(px,0);x.lineTo(px,310);x.stroke();x.setLineDash([]);});lastCandles.forEach((c,i)=>{let px=xOf(i);let yH=yOf(c.high);let yL2=yOf(c.low);let yO=yOf(c.open);let yC=yOf(c.close);let isLast=(i==lastCandles.length-1);let g=c.close>=c.open;x.strokeStyle=g?\'green\':\'red\';x.fillStyle=g?\'green\':\'red\';x.beginPath();x.moveTo(px,yH);x.lineTo(px,yL2);x.stroke();let bw=isLast?10:7;x.fillRect(px-bw/2,Math.min(yO,yC),bw,Math.max(3,Math.abs(yC-yO)));if(isLast){x.strokeStyle=\'#000\';x.lineWidth=2.5;x.setLineDash([]);x.strokeRect(px-bw/2-1,Math.min(yO,yC)-1,bw+2,Math.max(4,Math.abs(yC-yO))+2);x.lineWidth=1;x.fillStyle=\'#000\';x.font=\'bold 11px Arial\';x.fillText(\'● LIVE\',px-18,Math.min(yO,yC)-6);x.font=\'12px Arial\';}});x.fillStyle=\'#2563eb\';x.fillRect(708,yL-10,142,20);x.fillStyle=\'#fff\';x.fillText(fmtINR(lastPrice),712,yL+4);x.fillStyle=\'#666\';hLines.forEach(p=>{let y=yOf(p);x.fillStyle=\'#7c3aed\';x.fillRect(708,y-10,142,20);x.fillStyle=\'#fff\';x.fillText(fmtINR(p),712,y+4);x.fillStyle=\'#666\';});}\ndocument.getElementById(\'c\').addEventListener(\'click\',function(e){if(tool==\'none\')return;let rect=this.getBoundingClientRect();let cx=(e.clientX-rect.left)*(860/rect.width);let cy=(e.clientY-rect.top)*(340/rect.height);if(!lastCandles.length)return;let lo=Math.min(...lastCandles.map(a=>a.low));let hi=Math.max(...lastCandles.map(a=>a.high));let rg=hi-lo||1;lo-=rg*0.08;hi+=rg*0.08;rg=hi-lo;if(tool==\'h\'){let price=hi-((cy-10)/300)*rg;hLines.push(price);alert(\'H-Line Price: USDT \'+price.toFixed(2));}if(tool==\'v\'){let idx=Math.floor(cx/(700/lastCandles.length));vLines.push(idx);}drawChart();});\nasync function fetchData(){try{let r=await fetch(\'/api/data?coin=\'+cur+\'&interval=\'+tf);let d=await r.json();if(!r.ok||d.error){document.getElementById(\'p\').innerText=\'Data Error: \'+(d.error|| (\'HTTP \'+r.status));document.getElementById(\'bal\').innerText=\'Wallet data unavailable\';document.getElementById(\'locked\').innerText=\'API/data error\';return;}lastCandles=d.candles||[];lastPrice=d.price_inr;curPos=d.pos_obj;curEntry=d.entry_inr;document.getElementById(\'p\').innerText=cur+\' : ₹ \'+d.price_inr.toLocaleString(\'en-IN\',{maximumFractionDigits:2})+\' | \'+d.position+\' | 24h \'+d.change+\'%\';document.getElementById(\'bal\').innerText=\'💰 Available Wallet: ₹ \'+d.balance.toLocaleString(\'en-IN\',{maximumFractionDigits:2});document.getElementById(\'locked\').innerText=\'🔒 Locked: ₹ \'+d.locked.toLocaleString(\'en-IN\',{maximumFractionDigits:2})+\' | Total: ₹ \'+(d.balance+d.locked).toLocaleString(\'en-IN\',{maximumFractionDigits:2});document.getElementById(\'pos\').innerText=d.pos_msg;document.getElementById(\'trailInfo\').innerText=d.trail_msg;let p=document.getElementById(\'pnl\');p.innerText=\'Live PnL Net (Fee kat ke): ₹ \'+d.live_pnl+\' (\'+d.live_pct+\'%) | Gross ₹ \'+d.live_gross; p.style.color=d.live_pnl>=0?\'green\':\'red\';document.getElementById(\'h\').innerHTML=d.history.map(x=>\'<div>\'+x+\'</div>\').join(\'\');drawChart();updateTpSlPreview();}catch(e){document.getElementById(\'p\').innerText=\'Dashboard error: \'+e.message;}}\nsetInterval(fetchData,10000);fetchData();\n</script>\n'

@app.route("/")
def home(): return render_template_string(HTML)

@app.route("/healthz")
def healthz(): return jsonify({"ok":True,"live_trading":LIVE_TRADING,"auto_allowed":AUTO_TRADING_ALLOWED})

@app.route("/api/settings", methods=["GET","POST"])
def settings():
    s = STATE["settings"]
    try:
        data = request.get_json(silent=True) or request.args
        if "leverage" in data: s["leverage"] = int(data["leverage"])
        if "amount" in data: s["amount"] = float(data["amount"])
        if "tp" in data: s["tp"] = float(data["tp"])
        if "sl" in data: s["sl"] = float(data["sl"])
        if "trail" in data: s["trail"] = float(data["trail"])
        if "auto" in data:
            requested_auto = str(data["auto"]).lower() in ("1","true","on","yes")
            s["auto"] = bool(requested_auto and LIVE_TRADING and AUTO_TRADING_ALLOWED)
        if "auto_pct" in data: s["auto_pct"] = float(data["auto_pct"])
        if "auto_coin" in data and data["auto_coin"] in COINS: s["auto_coin"] = data["auto_coin"]
        if "close_pct" in data: s["close_pct"] = float(data["close_pct"])
        if "min_range_pct" in data: s["min_range_pct"] = float(data["min_range_pct"])
        if s["amount"] <= 0 or min(s["tp"],s["sl"],s["close_pct"],s["min_range_pct"]) <= 0:
            raise ValueError("Amount, TP, SL and range settings must be positive")
        if not 0.1 <= s["trail"] <= 10:
            raise ValueError("Binance trailing callback 0.1% se 10% ke beech hona chahiye")
    except Exception as exc:
        return jsonify({"ok":False,"msg":str(exc)}), 400
    return jsonify({"ok":True,"settings":s,"live_trading":LIVE_TRADING,"auto_allowed":AUTO_TRADING_ALLOWED})

@app.route("/api/reset")
def reset():
    return jsonify({"ok":False,"msg":"Live exchange wallet reset nahi kiya ja sakta."}), 403

@app.route("/api/trade", methods=["POST"])
def trade_api():
    if not LIVE_TRADING: return jsonify({"ok":False,"msg":"LIVE_TRADING=NO hai; live orders locked hain. Dashboard read-only hai."}), 403
    data = request.get_json(silent=True) or {}
    coin = str(data.get("coin", "BTC")).upper()
    side = str(data.get("side", "")).upper()
    if coin not in COINS: return jsonify({"ok":False,"msg":"Unsupported coin"}), 400
    try:
        with LOCK:
            if side == "CLOSE": result = close_live(coin)
            elif side in ("LONG","SHORT"):
                result = open_live(coin, side, STATE["settings"]["amount"], STATE["settings"]["leverage"], False)
            else: raise BinanceError("Side LONG, SHORT ya CLOSE hona chahiye")
        return jsonify({"ok":True,"msg":f"{coin} {side} Binance ko bhej di gayi. Binance app me fill aur TP/SL verify karein.","result":result})
    except Exception as exc:
        record(f"ORDER ERROR {coin} {side}: {exc}")
        return jsonify({"ok":False,"msg":str(exc)}), 400

@app.route("/api/data")
def api_data():
    coin = request.args.get("coin", "BTC").upper()
    interval = request.args.get("interval", "1m")
    if coin not in COINS: coin = "BTC"
    try:
        price, change = get_price(coin)
        fx = get_usdt_inr_rate()
        candles = get_candles(coin, interval)
        if candles:
            candles[-1]["close"] = price
            candles[-1]["high"] = max(candles[-1]["high"], price)
            candles[-1]["low"] = min(candles[-1]["low"], price)
        wallet = wallet_balances()
        pos = get_position(coin)
        pos_msg, trail_msg, pos_obj, entry, live_pct, live_gross, live_pnl = "No Position", "Trailing: -", None, 0, 0, 0, 0
        if pos:
            side = str(pos.get("position_side", "?")).upper()
            entry = float(D(pos.get("avg_entry_price", 0)))
            live_pnl = float(D(pos.get("unrealised_pnl", 0)))
            margin = float(D(pos.get("position_margin", 0)))
            live_pct = (live_pnl/margin*100) if margin else 0
            live_gross = live_pnl
            pos_msg = f"Exchange Position: {coin} {side} @ {entry:.8f} USDT | Size {pos.get('position_size')} | Lev {pos.get('leverage')}x | Liq {pos.get('liquidation_price')}"
            trail_msg = f"Exchange TP/SL should be checked in Binance app. Mark {pos.get('mark_price')} | Unrealised PnL {live_pnl:.4f} USDT"
            pos_obj = {"side":side,"coin":coin}
        pos_sig = "LONG 🟢" if change >= 0 else "SHORT 🔴"
        # Display-only conversion: exchange order/settlement remains USDT.
        fx_float = float(fx)
        candles_inr = [{k: (v * fx_float if k in ("open", "high", "low", "close") else v)
                        for k, v in candle.items()} for candle in candles]
        if pos:
            pos_msg = f"Exchange Position: {coin} {side} @ ₹{entry*fx_float:,.2f} | Size {pos.get('position_size')} | Lev {pos.get('leverage')}x"
            trail_msg = f"Mark ₹{float(D(pos.get('mark_price', 0)))*fx_float:,.2f} | Unrealised PnL ₹{live_pnl*fx_float:,.2f}"
        else:
            pos_msg, trail_msg = "No Position", "Trailing: -"
        return jsonify({"price_inr":price*fx_float,"fx_rate":fx_float,"change":change,"position":pos_sig,"candles":candles_inr,
                        "history":STATE["history"][:30],"balance":wallet["available"]*fx_float,"locked":wallet["blocked"]*fx_float,
                        "total":wallet["total"]*fx_float,"pos_msg":pos_msg,"trail_msg":trail_msg,
                        "live_pnl":round(live_pnl*fx_float,2),"live_gross":round(live_gross*fx_float,2),
                        "live_pct":round(live_pct,3),"pos_obj":pos_obj,"entry_inr":entry*fx_float,
                        "live_enabled":LIVE_TRADING,"auto_allowed":AUTO_TRADING_ALLOWED,
                        "error":STATE.get("last_error")})
    except Exception as exc:
        return jsonify({"error":str(exc),"price_inr":0,"change":0,"candles":[],"history":STATE["history"][:30],
                        "balance":0,"locked":0,"pos_msg":"Account/data unavailable","trail_msg":"-",
                        "live_pnl":0,"live_gross":0,"live_pct":0,"pos_obj":None,"entry_inr":0}), 502

if LIVE_TRADING and AUTO_TRADING_ALLOWED and DASHBOARD_PASSWORD and CLIENT is not None:
    threading.Thread(target=background_bot, daemon=True, name="future-bot-worker").start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",10000)), debug=False)
