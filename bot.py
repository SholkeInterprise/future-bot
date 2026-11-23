import os, time, uuid, threading, logging
from decimal import Decimal, ROUND_DOWN
from collections import defaultdict, deque
from urllib.parse import urlencode, unquote
from datetime import datetime, timezone
from flask import Flask, jsonify, request, render_template_string
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# CoinSwitch HFT REST client. Candle OHLC is aggregated locally from ticker snapshots.
BASE_URL = os.getenv("HFT_BASE_URL", "https://dma.coinswitch.co").rstrip("/")
API_KEY = os.getenv("COINSWITCH_API_KEY", "").strip()
SECRET_HEX = os.getenv("COINSWITCH_SECRET_KEY", "").strip()
LIVE = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
LIVE_CONFIRM = os.getenv("LIVE_CONFIRMATION", "") == "I_UNDERSTAND_REAL_ORDERS"
CATEGORY = "linear"
POLL_SECONDS = max(1.0, float(os.getenv("POLL_SECONDS", "1")))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "80"))
DEFAULTS = {
    "timeframe": "1", "range_filter_pct": 40.0, "entry_mode": "both",
    "long_enabled": True, "short_enabled": True,
    "tp_range_pct": 10.0, "stop_loss_pct": 1.0,
    "trailing_pct": 5.0, "margin_usdt": 10.0, "leverage": 1.0,
    "max_positions": 1, "symbol": "BTCUSDT", "symbol_mode": "all", "auto_trade": os.getenv("AUTO_TRADE", "false").lower() == "true",
    "max_spread_pct": 0.25, "max_daily_loss_usdt": 1.0,
}
CONFIG = DEFAULTS.copy()
CONFIG_LOCK = threading.Lock()
app = Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cs-hft-candle")

state = {
    "running": False, "status": "Starting", "last_error": None,
    "symbols": [], "last_price": {}, "candles": {}, "last_signal": None,
    "open_position": None, "trades_today": 0, "pnl_today": 0.0,
    "last_update": None, "live_armed": bool(LIVE and LIVE_CONFIRM),
}
lock = threading.RLock()
candle_store = defaultdict(lambda: deque(maxlen=300))
current_candle = {}
last_closed_key = {}
trail_extreme = {}
day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
api = None

def D(x, default="0"):
    try: return Decimal(str(x))
    except Exception: return Decimal(default)

def unwrap_list(result):
    if isinstance(result, list): return result
    if isinstance(result, dict):
        for k in ("list", "items", "instruments", "tickers", "positions"):
            if isinstance(result.get(k), list): return result[k]
    return []

def round_step(v, step):
    step = D(step)
    if step <= 0: return Decimal("0")
    return (D(v) / step).to_integral_value(rounding=ROUND_DOWN) * step

class HFT:
    def __init__(self):
        raw = bytes.fromhex(SECRET_HEX)
        if len(raw) == 64: raw = raw[:32]
        if len(raw) != 32: raise ValueError("Secret must be 32-byte Ed25519 private key hex.")
        self.private = Ed25519PrivateKey.from_private_bytes(raw)
        self.session = requests.Session()

    def call(self, method, path, params=None, body=None):
        params = params or {}
        query = urlencode(sorted((k, str(v)) for k, v in params.items()))
        path_q = path + (("?" + query) if query else "")
        epoch = str(int(time.time() * 1000))
        sig = self.private.sign((method.upper() + unquote(path_q) + epoch).encode()).hex()
        headers = {"Content-Type":"application/json", "X-AUTH-APIKEY":API_KEY,
                   "X-AUTH-SIGNATURE":sig, "X-AUTH-EPOCH":epoch}
        r = self.session.request(method.upper(), BASE_URL + path_q, headers=headers,
                                 json=body, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("retCode") != 0:
            raise RuntimeError(f"HFT {data.get('retCode')}: {data.get('retMsg')}")
        return data.get("result", {})

    def instruments(self):
        return self.call("GET", "/v5/market/instruments-info",
                         {"category":CATEGORY, "limit":1000})
    def tickers(self):
        return self.call("GET", "/v5/market/tickers", {"category":CATEGORY})
    def positions(self):
        return self.call("GET", "/v5/position/list", {"category":CATEGORY})
    def set_leverage(self, symbol, lev):
        return self.call("POST", "/v5/position/set-leverage", body={
            "category":CATEGORY, "symbol":symbol,
            "buyLeverage":str(lev), "sellLeverage":str(lev)})
    def order(self, symbol, side, qty, reduce_only=False):
        return self.call("POST", "/v5/order/create", body={
            "category":CATEGORY, "symbol":symbol, "side":side,
            "orderType":"Market", "qty":str(qty), "positionIdx":0,
            "reduceOnly":bool(reduce_only), "orderLinkId":str(uuid.uuid4())})

def timeframe_seconds():
    mins = int(CONFIG["timeframe"])
    return mins * 60

def candle_update(symbol, price, ts):
    sec = timeframe_seconds()
    bucket = int(ts // sec) * sec
    c = current_candle.get(symbol)
    if c is None or c["bucket"] != bucket:
        if c is not None:
            candle_store[symbol].append(c.copy())
        c = {"bucket":bucket, "time":bucket, "open":price, "high":price,
             "low":price, "close":price}
        current_candle[symbol] = c
    else:
        c["high"] = max(c["high"], price)
        c["low"] = min(c["low"], price)
        c["close"] = price
    return c

def candle_signal(symbol, price, meta, ticker):
    # Use last fully closed locally-built candle; breakout trigger is the prior
    # candle's exact high/low. Market execution can slip from the trigger price.
    history = candle_store[symbol]
    if not history: return None
    c = history[-1]
    if last_closed_key.get(symbol) == c["bucket"]: return None
    low, high = D(c["low"]), D(c["high"])
    if low <= 0 or high <= low: return None
    range_pct = (high-low)/low*Decimal("100")
    if range_pct < D(CONFIG["range_filter_pct"]): return None
    px = D(price)
    bid, ask = D(ticker.get("bid1Price")), D(ticker.get("ask1Price"))
    if bid <= 0 or ask <= 0: return None
    spread_pct = (ask-bid)/px*Decimal("100")
    if spread_pct > D(CONFIG["max_spread_pct"]): return None
    # Prevent repeated signals for the same closed candle.
    if CONFIG["short_enabled"] and px >= high:
        last_closed_key[symbol] = c["bucket"]
        return {"side":"Sell", "entry_trigger":str(high),
                "target":str(low+(high-low)*D(CONFIG["tp_range_pct"])/100),
                "candle":c, "range_pct":str(range_pct)}
    if CONFIG["long_enabled"] and px <= low:
        last_closed_key[symbol] = c["bucket"]
        return {"side":"Buy", "entry_trigger":str(low),
                "target":str(high-(high-low)*D(CONFIG["tp_range_pct"])/100),
                "candle":c, "range_pct":str(range_pct)}
    return None

def current_positions():
    return [p for p in unwrap_list(api.positions()) if D(p.get("size")) > 0]

def place_entry(symbol, side, px, signal, meta):
    cfg = CONFIG.copy()
    lot = meta.get("lotSizeFilter", {})
    step, min_qty = D(lot.get("qtyStep")), D(lot.get("minOrderQty"))
    min_notional = D(lot.get("minNotionalValue"))
    # Margin * leverage is maximum position notional; size is rounded down.
    notional = D(cfg["margin_usdt"]) * D(cfg["leverage"])
    qty = round_step(notional / D(px), step)
    if qty <= 0 or qty < min_qty or qty * D(px) < min_notional:
        raise RuntimeError(f"{symbol}: calculated size below exchange minimum.")
    api.set_leverage(symbol, cfg["leverage"])
    result = api.order(symbol, side, qty, False)
    # Exchange acceptance is not fill confirmation; position is reconciled next loop.
    with lock:
        state["last_signal"] = {"symbol":symbol, **signal,
                                "submitted_order":result, "qty":str(qty)}
        state["trades_today"] += 1
    log.warning("Entry submitted %s %s qty=%s trigger=%s", symbol, side, qty,
                signal["entry_trigger"])

def manage_position(pos, ticker_map):
    symbol = pos.get("symbol")
    ticker = ticker_map.get(symbol, {})
    px = D(ticker.get("markPrice") or ticker.get("lastPrice"))
    entry = D(pos.get("avgPrice"))
    qty = D(pos.get("size"))
    if px <= 0 or entry <= 0 or qty <= 0: return
    side = str(pos.get("side", "Buy"))
    long = side.lower() == "buy"
    move_pct = ((px-entry)/entry*100) if long else ((entry-px)/entry*100)
    # trailing extreme is updated only in the favorable direction.
    old = D(trail_extreme.get(symbol, entry))
    extreme = max(old, px) if long else min(old, px)
    trail_extreme[symbol] = str(extreme)
    stop_pct = D(CONFIG["stop_loss_pct"])
    trail_pct = D(CONFIG["trailing_pct"])
    fixed_stop = entry*(1-stop_pct/100) if long else entry*(1+stop_pct/100)
    trailing_stop = extreme*(1-trail_pct/100) if long else extreme*(1+trail_pct/100)
    last_sig = state.get("last_signal", {}) or {}
    target = D(last_sig.get("target", "0")) if last_sig.get("symbol") == symbol else Decimal("0")
    reason = None
    if long:
        if px <= fixed_stop: reason = "STOP LOSS"
        elif trail_pct > 0 and px <= trailing_stop and extreme > entry: reason = "TRAILING STOP"
        elif target > 0 and px >= target: reason = "CANDLE RANGE TP"
        exit_side = "Sell"
    else:
        if px >= fixed_stop: reason = "STOP LOSS"
        elif trail_pct > 0 and px >= trailing_stop and extreme < entry: reason = "TRAILING STOP"
        elif target > 0 and px <= target: reason = "CANDLE RANGE TP"
        exit_side = "Buy"
    if reason and LIVE and LIVE_CONFIRM:
        api.order(symbol, exit_side, qty, True)
        trail_extreme.pop(symbol, None)
        log.warning("%s exit submitted for %s", reason, symbol)
        with lock: state["status"] = f"{reason} exit submitted; reconciling fill."
    with lock:
        state["open_position"] = {**pos, "markPrice":str(px),
                                  "unrealized_move_pct":str(move_pct),
                                  "fixed_stop":str(fixed_stop),
                                  "trailing_stop":str(trailing_stop),
                                  "target":str(target)}

def worker():
    global api, day_key
    with lock: state["running"] = True
    try:
        if not API_KEY or not SECRET_HEX:
            raise RuntimeError("Add COINSWITCH_API_KEY and COINSWITCH_SECRET_KEY in Render Environment.")
        api = HFT()
        instruments = unwrap_list(api.instruments())
        eligible = [x for x in instruments
                    if str(x.get("status","")).lower() == "trading"
                    and str(x.get("settleCoin","")).upper() == "USDT"
                    and x.get("symbol")]
        eligible = eligible[:MAX_SYMBOLS]
        if not eligible: raise RuntimeError("No active USDT-settled linear contracts returned.")
        meta_map = {x["symbol"]:x for x in eligible}
        with lock: state["symbols"] = list(meta_map)
        with lock: state["status"] = f"Loaded {len(meta_map)} USDT contracts."
        while True:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != day_key:
                day_key = today
                with lock: state["trades_today"] = 0
            tickers = unwrap_list(api.tickers())
            ticker_map = {t.get("symbol"):t for t in tickers if t.get("symbol")}
            now = time.time()
            prices = {}
            for symbol, t in ticker_map.items():
                if symbol not in meta_map: continue
                px = D(t.get("lastPrice"))
                if px <= 0: continue
                prices[symbol] = float(px)
                candle_update(symbol, float(px), now)
            positions = current_positions()
            if len(positions) > int(CONFIG["max_positions"]):
                raise RuntimeError("Position limit exceeded; bot halted for manual review.")
            if positions:
                # Single-position strategy; never add another position while one is open.
                manage_position(positions[0], ticker_map)
                with lock: state["status"] = "Managing open position."
            elif LIVE and LIVE_CONFIRM and CONFIG["auto_trade"]:
                selected = CONFIG["symbol"]
                scan = list(meta_map.keys()) if CONFIG.get("symbol_mode") == "all" else ([selected] if selected in meta_map else list(meta_map.keys()))
                submitted = False
                for symbol in scan:
                    t = ticker_map.get(symbol)
                    if not t: continue
                    px = D(t.get("lastPrice"))
                    signal = candle_signal(symbol, px, meta_map[symbol], t)
                    if signal:
                        place_entry(symbol, signal["side"], px, signal, meta_map[symbol])
                        submitted = True
                        break
                if not submitted:
                    with lock: state["status"] = "Auto ON: waiting for previous-candle high/low breakout."
            else:
                with lock:
                    state["status"] = "Monitoring only. Turn Auto Trading ON and arm live mode in Render to submit orders."
            with lock:
                state["last_price"] = prices
                state["candles"] = {s:list(candle_store[s])[-120:] + ([current_candle[s]] if s in current_candle else [])
                                    for s in set([CONFIG["symbol"]]) if s in current_candle or candle_store[s]}
                state["last_update"] = datetime.now(timezone.utc).isoformat()
            time.sleep(POLL_SECONDS)
    except Exception as e:
        log.exception("Trading worker halted")
        with lock:
            state["last_error"] = str(e)
            state["status"] = "HALTED: " + str(e)
    finally:
        with lock: state["running"] = False

PAGE = r"""
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CoinSwitch USDT Candle HFT</title>
<style>
body{font-family:system-ui;background:#0b1220;color:#e8eef9;margin:0;padding:16px}
.wrap{max-width:1100px;margin:auto}.card{background:#151f32;border:1px solid #2a3953;border-radius:14px;padding:14px;margin:10px 0}
h2{margin:4px 0 12px} .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}
label{display:block;font-size:12px;color:#aebbd0;margin-bottom:5px}
input,select{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #40516d;background:#0d1728;color:white}
button{padding:11px 15px;border:0;border-radius:9px;background:#2878f0;color:white;font-weight:700}
small,.muted{color:#9eacc2}.status{font-weight:700;color:#f5c96a}
canvas{width:100%;height:300px;background:#0d1728;border-radius:10px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.pill{padding:6px 9px;background:#263650;border-radius:20px;font-size:12px}
</style></head><body><div class="wrap">
<h2>CoinSwitch PRO · USDT Candle HFT</h2>
<div class="card"><div class="row"><span class="pill" id="run">Connecting…</span><span class="pill" id="live">LIVE LOCKED</span><span class="pill" id="updated">—</span></div>
<p class="status" id="status">Loading status…</p><div id="error" class="muted"></div></div>
<div class="card"><h3>Live candle chart</h3><div class="grid">
<div><label>Symbol</label><select id="symbol"></select></div>
<div><label>Timeframe</label><select id="timeframe"><option value="1">1 minute</option><option value="3">3 minutes</option><option value="5">5 minutes</option><option value="15">15 minutes</option><option value="30">30 minutes</option><option value="60">1 hour</option><option value="240">4 hours</option></select></div>
</div><p class="muted" id="ohlc"></p><canvas id="chart" width="1000" height="300"></canvas></div>
<div class="card"><h3>Strategy settings</h3><div class="grid">
<div><label>Range filter (%)</label><input id="range_filter_pct" type="number" step=".1"></div>
<div><label>TP inside candle range (%)</label><input id="tp_range_pct" type="number" step=".1"></div>
<div><label>Stop loss (%)</label><input id="stop_loss_pct" type="number" step=".1"></div>
<div><label>Trailing stop (%)</label><input id="trailing_pct" type="number" step=".1"></div>
<div><label>Margin per trade (USDT)</label><input id="margin_usdt" type="number" step=".1"></div>
<div><label>Leverage</label><input id="leverage" type="number" min="1" step="1"></div>
<div><label>Max spread (%)</label><input id="max_spread_pct" type="number" step=".01"></div>
<div><label>Max daily loss (USDT)</label><input id="max_daily_loss_usdt" type="number" step=".1"></div>
<div><label>Entry side</label><select id="entry_mode"><option value="both">Auto Long + Short</option><option value="long">Long only</option><option value="short">Short only</option></select></div>
<div><label>Market</label><select id="symbol_mode"><option value="selected">Selected symbol</option><option value="all">All available pairs</option></select></div>
</div><p><label><input type="checkbox" id="auto_trade" style="width:auto"> Auto trading enabled (requires Render live arm)</label></p>
<button onclick="save()">Save settings</button> <span id="saved" class="muted"></span></div>
<div class="card"><h3>Position</h3><pre id="position" style="white-space:pre-wrap">None</pre><h3>Last signal / order</h3><pre id="signal" style="white-space:pre-wrap">None</pre></div>
<p class="muted">Execution note: signals trigger when price crosses the previous completed candle's high/low. Entry is a market order at trigger detection, so slippage means the fill is not guaranteed at the exact high/low. Candle data is built from REST ticker snapshots, not exchange-native OHLC websocket candles.</p>
</div>
<script>
let statusData={};
async function load(){try{let r=await fetch('/api/status');statusData=await r.json();
document.getElementById('run').textContent=statusData.running?'BOT RUNNING':'BOT STOPPED';
document.getElementById('live').textContent=statusData.live_armed?'LIVE ARMED':'LIVE LOCKED';
document.getElementById('status').textContent=statusData.status;
document.getElementById('error').textContent=statusData.last_error||'';
document.getElementById('updated').textContent=statusData.last_update||'';
document.getElementById('position').textContent=JSON.stringify(statusData.open_position,null,2);
document.getElementById('signal').textContent=JSON.stringify(statusData.last_signal,null,2);
let syms=statusData.symbols||[], sel=document.getElementById('symbol'), old=sel.value;
if(sel.options.length!==syms.length){sel.innerHTML=syms.map(s=>`<option>${s}</option>`).join('');if(syms.includes(old))sel.value=old;}
let c=(statusData.candles||{})[sel.value]||[];draw(c);
if(c.length){let x=c[c.length-1];document.getElementById('ohlc').textContent=`O ${x.open} · H ${x.high} · L ${x.low} · C ${x.close}`;}
}catch(e){document.getElementById('status').textContent=e}}
function draw(cs){let cv=document.getElementById('chart'),ctx=cv.getContext('2d');ctx.clearRect(0,0,cv.width,cv.height);
if(!cs.length)return;let lo=Math.min(...cs.map(c=>c.low)),hi=Math.max(...cs.map(c=>c.high)),pad=(hi-lo)*.08||1;
lo-=pad;hi+=pad;let step=cv.width/cs.length;
cs.forEach((c,i)=>{let x=i*step+step/2,y=v=>cv.height-(v-lo)/(hi-lo)*cv.height;
let up=c.close>=c.open;ctx.strokeStyle=up?'#21c78a':'#f06472';ctx.fillStyle=ctx.strokeStyle;
ctx.beginPath();ctx.moveTo(x,y(c.high));ctx.lineTo(x,y(c.low));ctx.stroke();
let top=y(Math.max(c.open,c.close)),bot=y(Math.min(c.open,c.close));
ctx.fillRect(x-step*.28,top,Math.max(2,step*.56),Math.max(1,bot-top));});
}
async function save(){let data={};['timeframe','range_filter_pct','tp_range_pct','stop_loss_pct','trailing_pct','margin_usdt','leverage','max_spread_pct','max_daily_loss_usdt','entry_mode'].forEach(k=>data[k]=document.getElementById(k).value);
data.symbol=document.getElementById('symbol').value;data.symbol_mode=document.getElementById('symbol_mode').value;data.auto_trade=document.getElementById('auto_trade').checked;
let r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
let j=await r.json();document.getElementById('saved').textContent=j.ok?'Saved':'Error: '+j.error;}
async function init(){let r=await fetch('/api/config');let j=await r.json();Object.keys(j).forEach(k=>{let el=document.getElementById(k);if(el){if(el.type==='checkbox')el.checked=!!j[k];else el.value=j[k];}});await load();}
document.getElementById('symbol').addEventListener('change',load);document.getElementById('timeframe').addEventListener('change',async()=>{await save();});
init();setInterval(load,2000);
</script></body></html>
"""

@app.get("/")
def home(): return render_template_string(PAGE)

@app.get("/health")
def health(): return jsonify({"ok":True})

@app.get("/api/status")
def status():
    with lock: return jsonify(state)

@app.get("/api/config")
def get_config():
    with CONFIG_LOCK: return jsonify(CONFIG)

@app.post("/api/config")
def set_config():
    try:
        data=request.get_json(force=True)
        old_tf = CONFIG["timeframe"]
        with CONFIG_LOCK:
            for k in ("timeframe","range_filter_pct","tp_range_pct","stop_loss_pct",
                      "trailing_pct","margin_usdt","leverage","max_spread_pct",
                      "max_daily_loss_usdt"):
                if k in data: CONFIG[k]=float(data[k])
            if "entry_mode" in data:
                CONFIG["entry_mode"]=data["entry_mode"]
                CONFIG["long_enabled"]=data["entry_mode"] in ("both","long")
                CONFIG["short_enabled"]=data["entry_mode"] in ("both","short")
            if "symbol" in data: CONFIG["symbol"]=str(data["symbol"])
            if "symbol_mode" in data:
                if data["symbol_mode"] not in ("selected","all"): raise ValueError("Invalid symbol mode")
                CONFIG["symbol_mode"]=data["symbol_mode"]
            if "auto_trade" in data: CONFIG["auto_trade"]=bool(data["auto_trade"])
            if CONFIG["timeframe"] not in ("1","3","5","15","30","60","240"):
                raise ValueError("Unsupported timeframe")
            for k in ("range_filter_pct","tp_range_pct","stop_loss_pct","margin_usdt","leverage"):
                if CONFIG[k] <= 0: raise ValueError(f"{k} must be greater than zero")
            if CONFIG["tp_range_pct"] >= 100: raise ValueError("TP range percent must be below 100")
            if CONFIG["leverage"] > 20: raise ValueError("Leverage capped at 20x in dashboard")
        if old_tf != CONFIG["timeframe"]:
            current_candle.clear()
            candle_store.clear()
            last_closed_key.clear()
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),400

if __name__ == "__main__":
    threading.Thread(target=worker,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")))
else:
    threading.Thread(target=worker,daemon=True).start()
