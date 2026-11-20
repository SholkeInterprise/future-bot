"""Future Bot: CoinSwitch PRO USDT-margined futures.

SAFETY DEFAULTS: live orders and auto trading are both OFF unless explicitly
enabled with Render environment variables. This is a starter integration and
must be validated against the user's CoinSwitch account before live use.
"""
import os, time, threading, uuid, urllib.parse, hmac
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from functools import wraps
from flask import Flask, jsonify, render_template_string, request, Response
import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

BASE_URL = "https://coinswitch.co"
API_KEY = os.getenv("COINSWITCH_API_KEY", "").strip()
SECRET_KEY = os.getenv("COINSWITCH_SECRET_KEY", "").strip()
LIVE_TRADING = os.getenv("LIVE_TRADING", "NO").strip().upper() == "YES"
AUTO_TRADING_ALLOWED = os.getenv("AUTO_TRADING", "NO").strip().upper() == "YES"
MAX_MARGIN_USDT = os.getenv("MAX_MARGIN_USDT", "25")
USDT_INR_RATE_FALLBACK = os.getenv("USDT_INR_RATE", "").strip()
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
app = Flask(__name__)
HTTP = requests.Session()
COINS = ["BTC", "ETH", "SOL", "DOGE", "XRP", "ADA", "BNB"]
INTERVALS = {"1m":"1", "5m":"5", "15m":"15", "1h":"60", "4h":"240", "1d":"1440"}
LOCK = threading.RLock()
STATE = {
    "settings": {"auto": False, "leverage": 2, "amount": 10.0, "tp": 1.0,
                 "sl": 0.5, "trail": 0.5, "auto_pct": 0.5,
                 "auto_coin": "BTC", "close_pct": 0.25, "min_range_pct": 0.40},
    "history": [], "last_price": 0.0, "last_time": 0.0,
    "last_close_time": 0.0, "last_error": None,
}

class CoinSwitchError(RuntimeError):
    pass

class CoinSwitchFutures:
    def __init__(self, api_key, secret_hex):
        if not api_key or not secret_hex:
            raise CoinSwitchError("Render Environment Variables me COINSWITCH_API_KEY aur COINSWITCH_SECRET_KEY set karein.")
        try:
            seed = bytes.fromhex(secret_hex)
            if len(seed) != 32:
                raise ValueError("Secret key hex decode ke baad 32 bytes honi chahiye")
            self.private_key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        except Exception as exc:
            raise CoinSwitchError(f"CoinSwitch secret key format invalid: {exc}")
        self.api_key = api_key

    def request(self, method, path, params=None, body=None):
        method = method.upper()
        if params:
            query = urllib.parse.urlencode(params)
            path += ("&" if "?" in path else "?") + query
        signed_path = urllib.parse.unquote_plus(path)
        epoch = str(int(time.time() * 1000))
        message = (method + signed_path + epoch).encode("utf-8")
        signature = self.private_key.sign(message).hex()
        headers = {"Content-Type":"application/json", "X-AUTH-APIKEY":self.api_key,
                   "X-AUTH-SIGNATURE":signature, "X-AUTH-EPOCH":epoch}
        resp = HTTP.request(method, BASE_URL + signed_path, headers=headers,
                            json=body if body is not None else None, timeout=15)
        try: data = resp.json()
        except Exception: data = {"raw": resp.text[:1000]}
        if not resp.ok:
            raise CoinSwitchError(f"CoinSwitch HTTP {resp.status_code}: {data}")
        if isinstance(data, dict) and data.get("error"):
            raise CoinSwitchError(str(data["error"]))
        return data

    def wallet(self):
        return self.request("GET", "/trade/api/v2/futures/wallet_balance")

    def usdt_inr_ticker(self):
        # CoinSwitch Spot INR market ticker; used only for display conversion.
        return self.request("GET", "/trade/api/v2/24hr/ticker",
                            {"exchange":"coinswitchx", "symbol":"USDT/INR"})

    def ticker(self, symbol):
        return self.request("GET", "/trade/api/v2/futures/ticker",
                            {"exchange":"EXCHANGE_2", "symbol":symbol})

    def positions(self, symbol):
        return self.request("GET", "/trade/api/v2/futures/positions",
                            {"exchange":"EXCHANGE_2", "symbol":symbol})

    def klines(self, symbol, interval):
        return self.request("GET", "/trade/api/v2/futures/klines",
                            {"exchange":"EXCHANGE_2", "symbol":symbol,
                             "interval":interval, "limit":50})

    def instruments(self):
        return self.request("GET", "/trade/api/v2/futures/instrument_info",
                            {"exchange":"EXCHANGE_2"})

    def set_leverage(self, symbol, leverage):
        return self.request("POST", "/trade/api/v2/futures/leverage",
                            body={"exchange":"EXCHANGE_2", "symbol":symbol,
                                  "leverage":int(leverage)})

    def order(self, symbol, side, order_type, quantity, **extra):
        body = {"exchange":"EXCHANGE_2", "symbol":symbol, "side":side,
                "order_type":order_type, "quantity":float(quantity),
                "client_order_id":str(uuid.uuid4())}
        body.update(extra)
        return self.request("POST", "/trade/api/v2/futures/order", body=body)

CLIENT = None
CLIENT_ERROR = None
try:
    CLIENT = CoinSwitchFutures(API_KEY, SECRET_KEY)
except Exception as exc:
    CLIENT_ERROR = str(exc)


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
    if CLIENT is None: raise CoinSwitchError(CLIENT_ERROR or "CoinSwitch API client unavailable")
    return CLIENT

def get_instrument(symbol):
    data = unwrap(require_client().instruments())
    if isinstance(data, dict):
        item = data.get(symbol) or data.get(symbol.lower()) or data.get(symbol.upper())
        if item: return item
    raise CoinSwitchError(f"{symbol} ke liye CoinSwitch instrument info nahi mila. Contract listed/trading hona chahiye.")

def get_usdt_inr_rate():
    """Fetch CoinSwitch USDT/INR spot quote; fallback is explicitly configured."""
    try:
        raw = unwrap(require_client().usdt_inr_ticker())
        item = raw.get("USDT/INR", raw) if isinstance(raw, dict) else raw
        rate = find_num(item, {"lastprice", "last_price", "price"})
        if rate and rate > 0:
            return float(rate), "CoinSwitch spot ticker"
    except Exception as exc:
        STATE["fx_error"] = str(exc)[:300]
    if USDT_INR_RATE_FALLBACK:
        try:
            rate = float(D(USDT_INR_RATE_FALLBACK))
            if rate > 0:
                return rate, "Render USDT_INR_RATE fallback (manual; may be stale)"
        except Exception:
            pass
    return 0.0, "INR conversion unavailable"

def get_price(coin):
    symbol = coin + "USDT"
    data = unwrap(require_client().ticker(symbol))
    p = find_num(data, {"mark_price", "last_price", "lastprice", "price", "index_price"})
    if p is None or p <= 0: raise CoinSwitchError(f"Ticker me valid price nahi mila: {data}")
    change = find_num(data, {"price_change_percent", "change_24_hour", "change_percent", "pricechangepercent"}) or Decimal("0")
    return float(p), float(change)

def get_candles(coin, interval="1m"):
    symbol = coin + "USDT"
    data = unwrap(require_client().klines(symbol, INTERVALS.get(interval, "1")))
    if isinstance(data, dict): data = data.get("candles", data.get("list", []))
    candles = []
    for c in data if isinstance(data, list) else []:
        try:
            candles.append({"time":int(Decimal(str(c.get("start_time", c.get("time", 0))))/1000),
                            "open":float(c.get("o", c.get("open"))),
                            "high":float(c.get("h", c.get("high"))),
                            "low":float(c.get("l", c.get("low"))),
                            "close":float(c.get("c", c.get("close")))})
        except Exception: continue
    candles.sort(key=lambda x:x["time"])
    return candles

def get_position(coin):
    raw = unwrap(require_client().positions(coin + "USDT"))
    if isinstance(raw, dict): raw = raw.get("positions", raw.get("list", []))
    for p in raw if isinstance(raw, list) else []:
        if str(p.get("symbol", "")).upper() == coin + "USDT" and str(p.get("status", "OPEN")).upper() == "OPEN":
            try:
                if Decimal(str(p.get("position_size", "0"))) > 0: return p
            except Exception: pass
    return None

def record(msg):
    STATE["history"].insert(0, time.strftime("%Y-%m-%d %H:%M:%S") + " | " + str(msg)[:700])
    del STATE["history"][50:]

def D(value):
    try: return Decimal(str(value))
    except (InvalidOperation, TypeError): raise CoinSwitchError(f"Invalid number: {value}")

def round_step(value, step):
    value, step = D(value), D(step)
    if step <= 0: raise CoinSwitchError("Exchange quantity/price step invalid")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

def tick_step(info):
    precision = int(info.get("price_precision", 2))
    tick = D(info.get("tick_size", "1"))
    return tick * (Decimal(10) ** (-precision))

def wallet_balances():
    raw = unwrap(require_client().wallet())
    base = raw.get("base_asset_balances", []) if isinstance(raw, dict) else []
    for item in base:
        if str(item.get("base_asset", "")).upper() == "USDT":
            b = item.get("balances", {})
            return {"total":float(D(b.get("total_balance", 0))),
                    "available":float(D(b.get("total_available_balance", 0))),
                    "blocked":float(D(b.get("total_blocked_balance", 0)))}
    return {"total":0.0,"available":0.0,"blocked":0.0}

def protective_orders(coin, side, entry, info):
    symbol = coin + "USDT"
    s = STATE["settings"]
    tp_pct, sl_pct = D(s["tp"])/100, D(s["sl"])/100
    tick = tick_step(info)
    if side == "LONG":
        exit_side = "SELL"; sl = entry * (1-sl_pct); tp = entry * (1+tp_pct)
    else:
        exit_side = "BUY"; sl = entry * (1+sl_pct); tp = entry * (1-tp_pct)
    sl = (sl/tick).to_integral_value(rounding=ROUND_HALF_UP)*tick
    tp = (tp/tick).to_integral_value(rounding=ROUND_HALF_UP)*tick
    # Exchange position-level TP/SL orders use quantity=0 and reduce_only=true.
    sl_result = require_client().order(symbol, exit_side, "STOP_MARKET", 0,
                                       trigger_price=float(sl), reduce_only=True)
    tp_result = require_client().order(symbol, exit_side, "TAKE_PROFIT_MARKET", 0,
                                       trigger_price=float(tp), reduce_only=True)
    return {"stop_loss":sl_result, "take_profit":tp_result, "sl_trigger":str(sl), "tp_trigger":str(tp)}

def open_live(coin, side, margin, leverage, auto=False, candle_low=0, candle_high=0):
    margin = D(margin)
    if margin < Decimal("5"):
        raise CoinSwitchError("Starter safety limit: margin kam se kam 5 USDT hona chahiye.")
    if margin > D(MAX_MARGIN_USDT):
        raise CoinSwitchError(f"Starter safety limit: per-trade margin {D(MAX_MARGIN_USDT)} USDT se zyada nahi ho sakta. MAX_MARGIN_USDT env badalne se pehle risk samjhein.")
    if not LIVE_TRADING: raise CoinSwitchError("LIVE_TRADING=YES set nahi hai. Live order locked hai.")
    symbol = coin + "USDT"
    if get_position(coin): raise CoinSwitchError(f"{symbol} par pehle se position khuli hai. Pehle usko manage/close karein.")
    price, _ = get_price(coin)
    info = get_instrument(symbol)
    if str(info.get("status", "TRADING")).upper() != "TRADING": raise CoinSwitchError(f"{symbol} abhi trading status me nahi hai")
    lev_min, lev_max = D(info.get("min_leverage", 1)), D(info.get("max_leverage", 1))
    if D(leverage) < lev_min or D(leverage) > lev_max: raise CoinSwitchError(f"Leverage allowed range {lev_min}-{lev_max}x hai")
    wallet = wallet_balances()
    if D(margin) > D(wallet["available"]): raise CoinSwitchError(f"Available wallet sirf {wallet['available']:.4f} USDT hai")
    notional = D(margin) * D(leverage)
    qty_step = D(info.get("base_quantity_step_size", info.get("lot_size", "0.000001")))
    qty = round_step(notional / D(price), qty_step)
    min_qty = D(info.get("min_base_quantity", "0"))
    max_qty = D(info.get("max_market_base_quantity", "0"))
    if qty < min_qty: raise CoinSwitchError(f"Calculated quantity {qty} minimum {min_qty} se kam hai")
    if max_qty > 0 and qty > max_qty: raise CoinSwitchError(f"Calculated quantity {qty} maximum market quantity {max_qty} se zyada hai")
    require_client().set_leverage(symbol, leverage)
    order_result = require_client().order(symbol, "BUY" if side == "LONG" else "SELL", "MARKET", qty)
    # Wait for exchange position confirmation; do not retry blindly if exchange response is delayed.
    pos = None
    for _ in range(8):
        time.sleep(1)
        pos = get_position(coin)
        if pos: break
    if not pos:
        raise CoinSwitchError("Market order API ne response diya, lekin position confirm nahi hui. CoinSwitch app me Orders/Positions check karein; dobara click na karein.")
    entry = D(pos.get("avg_entry_price", price))
    try:
        protections = protective_orders(coin, side, entry, info)
    except Exception as exc:
        record(f"CRITICAL: position open but protective order setup failed: {exc}")
        # Best-effort emergency reduce-only close; never claim the position is protected.
        try:
            size = D(pos.get("position_size", qty))
            require_client().order(symbol, "SELL" if side == "LONG" else "BUY", "MARKET", size, reduce_only=True)
        except Exception as close_exc:
            record(f"CRITICAL: emergency close also failed: {close_exc}")
        raise CoinSwitchError(f"Position open hui, lekin TP/SL set nahi hua: {exc}. CoinSwitch app me turant position check karein.")
    STATE["pos_meta"] = {"coin":coin,"side":side,"entry":float(entry),"peak":float(entry),
                         "trough":float(entry),"candle_low":float(candle_low),
                         "candle_high":float(candle_high),"auto":bool(auto)}
    record(f"{coin} {side} {'AUTO' if auto else 'MANUAL'} opened | entry {entry} USDT | qty {qty} | lev {leverage}x | SL {protections['sl_trigger']} | TP {protections['tp_trigger']}")
    return {"order":order_result,"position":pos,"protection":protections}

def close_live(coin, reason="MANUAL CLOSE"):
    if not LIVE_TRADING: raise CoinSwitchError("LIVE_TRADING=YES set nahi hai. Live close order locked hai.")
    pos = get_position(coin)
    if not pos: return {"message":"Is coin par open position nahi mili."}
    size = D(pos.get("position_size", "0"))
    side = str(pos.get("position_side", "")).upper()
    if size <= 0 or side not in ("LONG", "SHORT"):
        raise CoinSwitchError(f"Position details samajh nahi aaye; close order nahi bheja: {pos}")
    result = require_client().order(coin+"USDT", "SELL" if side == "LONG" else "BUY",
                                    "MARKET", size, reduce_only=True)
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
    if side == "LONG" and price <= meta["peak"]*(1-s["trail"]/100) and price > entry:
        close_live(coin, "TRAILING STOP")
    elif side == "SHORT" and price >= meta["trough"]*(1+s["trail"]/100) and price < entry:
        close_live(coin, "TRAILING STOP")

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

HTML = '\n<style>body{font-family:Arial;text-align:center;padding:8px}button{padding:10px 14px;margin:3px;font-size:15px;border-radius:8px;border:1px solid #000}.long{background:#22c55e;color:#fff}.short{background:#ef4444;color:#fff}.close{background:#000;color:#fff}.tf{background:#eef}.tf.active{background:#000;color:#fff}.tool{background:#fff;border:2px solid #7c3aed}.tool.active{background:#7c3aed;color:#fff}.box{border:1px solid #ccc;border-radius:10px;padding:10px;margin:8px auto;max-width:860px;text-align:left}input,select{padding:8px;margin:4px;font-size:15px;width:110px}.on{color:green;font-weight:bold}.off{color:red;font-weight:bold}.live{color:#ef4444;font-weight:bold;animation:blink 1s infinite}@keyframes blink{50%{opacity:0.3}}.tpbox{background:#f0fdf4;border:1px solid #22c55e;border-radius:8px;padding:8px;margin-top:8px}.slbox{background:#fef2f2;border:1px solid #ef4444;border-radius:8px;padding:8px;margin-top:6px}.hlbox{background:#fff7ed;border:1px solid #f59e0b;border-radius:8px;padding:8px;margin-top:8px}</style>\n<h2>🚀 Future Bot — CoinSwitch PRO Futures (USDT Execution / INR Display)</h2>\n<div><button onclick="load(\'BTC\')">BTC</button><button onclick="load(\'ETH\')">ETH</button><button onclick="load(\'SOL\')">SOL</button><button onclick="load(\'DOGE\')">DOGE</button><button onclick="load(\'XRP\')">XRP</button><button onclick="load(\'ADA\')">ADA</button><button onclick="load(\'BNB\')">BNB</button></div>\n<h1 id="p">Loading...</h1>\n<div><span class="live">● LIVE</span> <span style="font-size:14px">High pe SHORT | Low pe LONG | Close = 0.25% andar | Fees/funding vary — no fixed-profit guarantee | Chhoti Candle Skip</span></div>\n<h2 id="bal" style="color:#0a7">💰 Available Wallet: USDT -- | ₹ --</h2>\n<h3 id="locked" style="color:#d97706">🔒 Locked: USDT -- | Total: USDT --</h3><h4 id="fx">USDT/INR rate: loading...</h4>\n<h3 id="pos">No Position</h3>\n<h3 id="trailInfo" style="color:#7c3aed">Trailing: -</h3>\n<h3 id="pnl" style="color:blue">Live PnL: USDT 0</h3>\n<div class="box"><b>⚙️ SETTING</b><br>\nLeverage: <select id="lev" onchange="saveSet()"><option>1</option><option>2</option><option>5</option><option selected>2</option><option>10</option><option>20</option></select> x &nbsp;\nMargin USDT: <input id="amt" type="number" value="10" onchange="saveSet()"> &nbsp;\nTP %: <input id="tp" type="number" value="1.0" step="0.1" onchange="saveSet()"> &nbsp;\nSL %: <input id="sl" type="number" value="0.5" step="0.1" onchange="saveSet()"> &nbsp;\nTrail %: <input id="trail" type="number" value="0.5" step="0.1" onchange="saveSet()"><br>\n<b>🤖 AUTO:</b> <select id="auto" onchange="saveSet()"><option value="1">ON (server permission needed)</option><option value="0" selected>OFF ❌</option></select>\nAuto %: <input id="auto_pct" type="number" value="0.5" step="0.1" onchange="saveSet()"> % &nbsp;\nAuto Coin: <select id="auto_coin" onchange="saveSet()"><option>BTC</option><option>ETH</option><option>SOL</option><option>DOGE</option><option>XRP</option><option>ADA</option><option>BNB</option></select><br>\n<span id="autoTxt" class="on">AUTO ON</span>\n<div class="hlbox"><b>⚡ HIGH-LOW FIXED LOGIC - Live 1m Candle</b><br>\nClose %: <input id="close_pct" type="number" value="0.25" step="0.05" onchange="saveSet()"> % &nbsp;\nMin Candle Range %: <input id="min_range_pct" type="number" value="0.40" step="0.05" onchange="saveSet()"> %<br>\n<span style="font-size:13px">SHORT = High pe khulega, Low + 0.25% pe band. LONG = Low pe khulega, High - 0.25% pe band. Actual fees, funding and slippage apply; profit is not guaranteed. Candle Range 0.40% se chhota hua to trade nahi khulega - Fee bachega.</span>\n</div>\n<div id="tpPreview"></div>\n</div>\n<div><button class="long" onclick="trade(\'LONG\')">🟢 LONG Lagao</button><button class="short" onclick="trade(\'SHORT\')">🔴 SHORT Lagao</button><button class="close" onclick="trade(\'CLOSE\')">❌ Band Karo</button><button onclick="resetBal()">🔄 Refresh Wallet</button></div>\n<div style="margin-top:12px"><b>⏱️ TIME FRAME:</b>\n<button class="tf active" id="tf-1m" onclick="setTF(\'1m\')">1m</button><button class="tf" id="tf-5m" onclick="setTF(\'5m\')">5m</button><button class="tf" id="tf-15m" onclick="setTF(\'15m\')">15m</button><button class="tf" id="tf-1h" onclick="setTF(\'1h\')">1h</button><button class="tf" id="tf-4h" onclick="setTF(\'4h\')">4h</button><button class="tf" id="tf-1d" onclick="setTF(\'1d\')">1D</button>\n</div>\n<div style="margin-top:8px"><b>📐 LINE TOOL:</b>\n<button class="tool active" id="tool-none" onclick="setTool(\'none\')">🖱️ Normal</button><button class="tool" id="tool-h" onclick="setTool(\'h\')">➖ H-Line (Price)</button><button class="tool" id="tool-v" onclick="setTool(\'v\')">➡️ V-Line (Time)</button><button class="tool" onclick="clearLines()">🗑️ Lines Clear</button>\n</div>\n<canvas id="c" width="860" height="340" style="border:1px solid #000;width:100%;max-width:860px;margin-top:10px;cursor:crosshair"></canvas>\n<h3>📜 History</h3><div id="h"></div>\n<script>\nlet cur=\'BTC\'; let tf=\'1m\'; let tool=\'none\'; let hLines=[]; let vLines=[]; let lastCandles=[]; let lastPrice=0; let curPos=null; let curEntry=0;\nfunction setTF(t){tf=t;document.querySelectorAll(\'.tf\').forEach(b=>b.classList.remove(\'active\'));document.getElementById(\'tf-\'+t).classList.add(\'active\');fetchData();}\nfunction setTool(t){tool=t;document.querySelectorAll(\'.tool\').forEach(b=>b.classList.remove(\'active\'));if(t!=\'none\')document.getElementById(\'tool-\'+t).classList.add(\'active\');else document.getElementById(\'tool-none\').classList.add(\'active\');}\nfunction clearLines(){hLines=[];vLines=[];drawChart();}\nasync function load(coin){cur=coin;document.getElementById(\'auto_coin\').value=coin;saveSet();fetchData();}\nasync function saveSet(){let s={leverage:document.getElementById(\'lev\').value,amount:document.getElementById(\'amt\').value,tp:document.getElementById(\'tp\').value,sl:document.getElementById(\'sl\').value,trail:document.getElementById(\'trail\').value,auto:document.getElementById(\'auto\').value,auto_pct:document.getElementById(\'auto_pct\').value,auto_coin:document.getElementById(\'auto_coin\').value,close_pct:document.getElementById(\'close_pct\').value,min_range_pct:document.getElementById(\'min_range_pct\').value};await fetch(\'/api/settings?leverage=\'+s.leverage+\'&amount=\'+s.amount+\'&tp=\'+s.tp+\'&sl=\'+s.sl+\'&trail=\'+s.trail+\'&auto=\'+s.auto+\'&auto_pct=\'+s.auto_pct+\'&auto_coin=\'+s.auto_coin+\'&close_pct=\'+s.close_pct+\'&min_range_pct=\'+s.min_range_pct);document.getElementById(\'autoTxt\').innerText=s.auto==\'1\'?\'AUTO ON - High pe SHORT | Low pe LONG | Close \'+s.close_pct+\'%\':\'AUTO OFF\';updateTpSlPreview();}\nasync function trade(side){let q={coin:cur,side:side};if(side!==\'CLOSE\'&&!confirm(\'REAL CoinSwitch Futures order: \'+side+\' \'+cur+\'?\'))return;let r=await fetch(\'/api/trade\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(q)});let d=await r.json();alert(d.msg||JSON.stringify(d));fetchData();}\nasync function resetBal(){fetchData();alert(\'Live exchange wallet cannot be reset from this dashboard.\');}\nfunction fmtINR(n){return \'USDT \'+Number(n).toLocaleString(\'en-US\',{minimumFractionDigits:2, maximumFractionDigits:4});}\nfunction fmtPrice(n){return Number(n).toLocaleString(\'en-IN\',{minimumFractionDigits:2, maximumFractionDigits:2});}\nfunction updateTpSlPreview(){\nlet tp=parseFloat(document.getElementById(\'tp\').value)||0;let sl=parseFloat(document.getElementById(\'sl\').value)||0;let lev=parseFloat(document.getElementById(\'lev\').value)||10;let amt=parseFloat(document.getElementById(\'amt\').value)||1000;let fee=amt*lev*0.0013;let tpGross=amt*lev*tp/100;let slGross=amt*lev*sl/100;let tpNet=tpGross-fee;let slNet=-(slGross+fee);let basePrice=(curPos&&curEntry>0)?curEntry:lastPrice;let side=(curPos&&curPos.side)?curPos.side:\'LONG\';let tpPrice,slPrice;if(side==\'LONG\'){tpPrice=basePrice*(1+tp/100);slPrice=basePrice*(1-sl/100);}else{tpPrice=basePrice*(1-tp/100);slPrice=basePrice*(1+sl/100);}let baseLabel=(curPos&&curEntry>0)?(\'Entry USDT \'+fmtPrice(basePrice)+\' (\'+side+\') se\'):(\'Live USDT \'+fmtPrice(basePrice)+\' pe \'+side+\' lagao to\');let html=\'<div class="tpbox"><b>🎯 TP Preview - \'+baseLabel+\'</b><br>✅ TP \'+tp+\'% → Price <b>USDT \'+fmtPrice(tpPrice)+\'</b> | Gross +USDT \'+tpGross.toFixed(2)+\' - Estimated fee USDT \'+fee.toFixed(2)+\' = Net <b style="color:green">+USDT \'+tpNet.toFixed(2)+\'</b></div>\';html+=\'<div class="slbox"><b>🛑 SL Preview - \'+baseLabel+\'</b><br>❌ SL \'+sl+\'% → Price <b>USDT \'+fmtPrice(slPrice)+\'</b> | Gross -USDT \'+slGross.toFixed(2)+\' - Estimated fee USDT \'+fee.toFixed(2)+\' = Net <b style="color:red">USDT \'+slNet.toFixed(2)+\'</b></div>\';document.getElementById(\'tpPreview\').innerHTML=html;}\nfunction drawChart(){let cv=document.getElementById(\'c\');let x=cv.getContext(\'2d\');x.clearRect(0,0,860,340);if(!lastCandles.length)return;let allLow=[...lastCandles.map(a=>a.low),lastPrice];let allHigh=[...lastCandles.map(a=>a.high),lastPrice];let lo=Math.min(...allLow);let hi=Math.max(...allHigh);let rg=hi-lo||1;lo-=rg*0.08;hi+=rg*0.08;rg=hi-lo;let chartW=700;let w=chartW/lastCandles.length;let yOf=p=>10+((hi-p)/rg)*300;let xOf=i=>i*w+w/2;x.font=\'12px Arial\';x.fillStyle=\'#666\';x.strokeStyle=\'#eee\';for(let i=0;i<=5;i++){let p=lo+(rg*i/5);let y=yOf(p);x.beginPath();x.moveTo(0,y);x.lineTo(chartW,y);x.stroke();x.fillText(fmtINR(p),708,y+4);}let yL=yOf(lastPrice);x.strokeStyle=\'#2563eb\';x.setLineDash([4,3]);x.lineWidth=1;x.beginPath();x.moveTo(0,yL);x.lineTo(chartW,yL);x.stroke();x.setLineDash([]);x.lineWidth=1;hLines.forEach(p=>{let y=yOf(p);x.strokeStyle=\'#7c3aed\';x.setLineDash([6,4]);x.lineWidth=1;x.beginPath();x.moveTo(0,y);x.lineTo(chartW,y);x.stroke();x.setLineDash([]);});vLines.forEach(idx=>{let px=xOf(idx);x.strokeStyle=\'#f59e0b\';x.setLineDash([6,4]);x.lineWidth=1;x.beginPath();x.moveTo(px,0);x.lineTo(px,310);x.stroke();x.setLineDash([]);});lastCandles.forEach((c,i)=>{let px=xOf(i);let yH=yOf(c.high);let yL2=yOf(c.low);let yO=yOf(c.open);let yC=yOf(c.close);let isLast=(i==lastCandles.length-1);let g=c.close>=c.open;x.strokeStyle=g?\'green\':\'red\';x.fillStyle=g?\'green\':\'red\';x.beginPath();x.moveTo(px,yH);x.lineTo(px,yL2);x.stroke();let bw=isLast?10:7;x.fillRect(px-bw/2,Math.min(yO,yC),bw,Math.max(3,Math.abs(yC-yO)));if(isLast){x.strokeStyle=\'#000\';x.lineWidth=2.5;x.setLineDash([]);x.strokeRect(px-bw/2-1,Math.min(yO,yC)-1,bw+2,Math.max(4,Math.abs(yC-yO))+2);x.lineWidth=1;x.fillStyle=\'#000\';x.font=\'bold 11px Arial\';x.fillText(\'● LIVE\',px-18,Math.min(yO,yC)-6);x.font=\'12px Arial\';}});x.fillStyle=\'#2563eb\';x.fillRect(708,yL-10,142,20);x.fillStyle=\'#fff\';x.fillText(fmtINR(lastPrice),712,yL+4);x.fillStyle=\'#666\';hLines.forEach(p=>{let y=yOf(p);x.fillStyle=\'#7c3aed\';x.fillRect(708,y-10,142,20);x.fillStyle=\'#fff\';x.fillText(fmtINR(p),712,y+4);x.fillStyle=\'#666\';});}\ndocument.getElementById(\'c\').addEventListener(\'click\',function(e){if(tool==\'none\')return;let rect=this.getBoundingClientRect();let cx=(e.clientX-rect.left)*(860/rect.width);let cy=(e.clientY-rect.top)*(340/rect.height);if(!lastCandles.length)return;let lo=Math.min(...lastCandles.map(a=>a.low));let hi=Math.max(...lastCandles.map(a=>a.high));let rg=hi-lo||1;lo-=rg*0.08;hi+=rg*0.08;rg=hi-lo;if(tool==\'h\'){let price=hi-((cy-10)/300)*rg;hLines.push(price);alert(\'H-Line Price: USDT \'+price.toFixed(2));}if(tool==\'v\'){let idx=Math.floor(cx/(700/lastCandles.length));vLines.push(idx);}drawChart();});\nasync function fetchData(){let r=await fetch(\'/api/data?coin=\'+cur+\'&interval=\'+tf);let d=await r.json();lastCandles=d.candles;lastPrice=d.price_usdt;curPos=d.pos_obj;curEntry=d.entry_usdt;document.getElementById(\'p\').innerText=cur+\' : USDT \'+d.price_usdt.toLocaleString(\'en-IN\',{maximumFractionDigits:4})+\' | \'+(d.usdt_inr?\'₹ \'+(d.price_usdt*d.usdt_inr).toLocaleString(\'en-IN\',{maximumFractionDigits:2}):\'₹ --\')+\' | \'+d.position+\' | 24h \'+d.change+\'%\';document.getElementById(\'bal\').innerText=\'💰 Available: USDT \'+d.balance.toLocaleString(\'en-IN\',{maximumFractionDigits:2})+\' | ₹ \'+(d.balance_inr==null?\'--\':d.balance_inr.toLocaleString(\'en-IN\',{maximumFractionDigits:2}));document.getElementById(\'locked\').innerText=\'🔒 Locked: USDT \'+d.locked.toLocaleString(\'en-IN\',{maximumFractionDigits:2})+\' | Total: USDT \'+d.total.toLocaleString(\'en-IN\',{maximumFractionDigits:2})+\' | Total INR: ₹ \'+(d.total_inr==null?\'--\':d.total_inr.toLocaleString(\'en-IN\',{maximumFractionDigits:2}));document.getElementById(\'fx\').innerText=\'1 USDT = ₹ \'+(d.usdt_inr||\'Unavailable\')+\' | \'+d.fx_source;document.getElementById(\'pos\').innerText=d.pos_msg;document.getElementById(\'trailInfo\').innerText=d.trail_msg;let p=document.getElementById(\'pnl\');p.innerText=\'Live Unrealised PnL: USDT \'+d.live_pnl+\' (\'+d.live_pct+\'%) | INR ₹ \'+(d.live_pnl_inr==null?\'--\':d.live_pnl_inr)+\' | Gross USDT \'+d.live_gross; p.style.color=d.live_pnl>=0?\'green\':\'red\';document.getElementById(\'h\').innerHTML=d.history.map(x=>\'<div>\'+x+\'</div>\').join(\'\');drawChart();updateTpSlPreview();}\nsetInterval(fetchData,10000);fetchData();\n</script>\n'

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
        if "auto" in data: s["auto"] = str(data["auto"]).lower() in ("1","true","on","yes")
        if "auto_pct" in data: s["auto_pct"] = float(data["auto_pct"])
        if "auto_coin" in data and data["auto_coin"] in COINS: s["auto_coin"] = data["auto_coin"]
        if "close_pct" in data: s["close_pct"] = float(data["close_pct"])
        if "min_range_pct" in data: s["min_range_pct"] = float(data["min_range_pct"])
        if s["amount"] <= 0 or min(s["tp"],s["sl"],s["trail"],s["close_pct"],s["min_range_pct"]) <= 0:
            raise ValueError("Amount, TP, SL, Trail and range settings must be positive")
    except Exception as exc:
        return jsonify({"ok":False,"msg":str(exc)}), 400
    return jsonify({"ok":True,"settings":s,"live_trading":LIVE_TRADING,"auto_allowed":AUTO_TRADING_ALLOWED})

@app.route("/api/reset")
def reset():
    return jsonify({"ok":False,"msg":"Live exchange wallet reset nahi kiya ja sakta."}), 403

@app.route("/api/trade", methods=["POST"])
def trade_api():
    if not LIVE_TRADING: return jsonify({"ok":False,"msg":"LIVE_TRADING=YES nahi hai; live orders locked hain."}), 403
    data = request.get_json(silent=True) or {}
    coin = str(data.get("coin", "BTC")).upper()
    side = str(data.get("side", "")).upper()
    if coin not in COINS: return jsonify({"ok":False,"msg":"Unsupported coin"}), 400
    try:
        with LOCK:
            if side == "CLOSE": result = close_live(coin)
            elif side in ("LONG","SHORT"):
                result = open_live(coin, side, STATE["settings"]["amount"], STATE["settings"]["leverage"], False)
            else: raise CoinSwitchError("Side LONG, SHORT ya CLOSE hona chahiye")
        return jsonify({"ok":True,"msg":f"{coin} {side} request exchange ko bhej di gayi. CoinSwitch app me fill aur TP/SL verify karein.","result":result})
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
        usdt_inr, fx_source = get_usdt_inr_rate()
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
            trail_msg = f"Exchange TP/SL should be checked in CoinSwitch app. Mark {pos.get('mark_price')} | Unrealised PnL {live_pnl:.4f} USDT"
            pos_obj = {"side":side,"coin":coin}
        pos_sig = "LONG 🟢" if change >= 0 else "SHORT 🔴"
        return jsonify({"price_usdt":price,"change":change,"position":pos_sig,"candles":candles,
                        "history":STATE["history"][:30],"balance":wallet["available"],"locked":wallet["blocked"],
                        "total":wallet["total"],"pos_msg":pos_msg,"trail_msg":trail_msg,
                        "live_pnl":round(live_pnl,6),"live_gross":round(live_gross,6),
                        "live_pct":round(live_pct,3),"pos_obj":pos_obj,"entry_usdt":entry,
                        "usdt_inr":usdt_inr,"fx_source":fx_source,
                        "balance_inr":round(wallet["available"]*usdt_inr,2) if usdt_inr else None,
                        "locked_inr":round(wallet["blocked"]*usdt_inr,2) if usdt_inr else None,
                        "total_inr":round(wallet["total"]*usdt_inr,2) if usdt_inr else None,
                        "live_pnl_inr":round(live_pnl*usdt_inr,2) if usdt_inr else None,
                        "live_enabled":LIVE_TRADING,"auto_allowed":AUTO_TRADING_ALLOWED,
                        "error":STATE.get("last_error")})
    except Exception as exc:
        return jsonify({"error":str(exc),"price_usdt":0,"change":0,"candles":[],"history":STATE["history"][:30],
                        "balance":0,"locked":0,"pos_msg":"Account/data unavailable","trail_msg":"-",
                        "live_pnl":0,"live_gross":0,"live_pct":0,"pos_obj":None,"entry_usdt":0,"usdt_inr":0,"fx_source":"unavailable"}), 502

if LIVE_TRADING and AUTO_TRADING_ALLOWED and DASHBOARD_PASSWORD and CLIENT is not None:
    threading.Thread(target=background_bot, daemon=True, name="future-bot-worker").start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",10000)), debug=False)
