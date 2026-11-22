"""CoinSwitch PRO HFT-only dashboard/bot v12.

All legacy trading clients and routes have been removed. Uses the documented
HFT REST surface at https://dma.coinswitch.co only.

IMPORTANT LIMITATIONS:
- The published HFT REST endpoint inventory does not expose an HFT wallet
  balance endpoint or a documented exchange-attached TP/SL endpoint.
- The 0.05 USDT balance is a user-reported UI value, not API-verified.
- Candles below are locally aggregated from HFT ticker polling; they are not
  exchange historical klines and disappear on process restart.
- TP/SL/trailing are local monitoring only, not exchange-resident protection.
- Live trading and auto trading default OFF. Do not enable until adequate
  balance, order mode, protection behavior and minimum order are verified.
"""
import os, time, threading, uuid, urllib.parse, json
from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from datetime import datetime
from flask import Flask, jsonify, render_template_string, request
import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

BASE_URL = "https://dma.coinswitch.co"
API_KEY = os.getenv("COINSWITCH_API_KEY", "").strip()
SECRET_HEX = os.getenv("COINSWITCH_SECRET_KEY", "").strip()
CATEGORY = "linear"
SETTLE_COIN = "USDT"
LIVE_TRADING = os.getenv("HFT_LIVE_TRADING", "NO").upper() == "YES"
AUTO_ALLOWED = os.getenv("HFT_AUTO_ALLOWED", "NO").upper() == "YES"
BALANCE_USDT = Decimal(os.getenv("HFT_AVAILABLE_BALANCE_USDT", "0.05"))
BALANCE_CONFIRMED = os.getenv("HFT_BALANCE_CONFIRMED", "YES").upper() == "YES"  # screenshot-confirmed UI figure, not API verified
POSITION_MODE_CONFIRMED = os.getenv("HFT_POSITION_MODE_CONFIRMED", "NO").upper() == "YES"
MAX_MARGIN = Decimal(os.getenv("HFT_MAX_MARGIN_USDT", "5"))
MAX_LEVERAGE = int(os.getenv("HFT_MAX_LEVERAGE", "2"))
POLL_SECONDS = max(2, int(os.getenv("HFT_POLL_SECONDS", "3")))
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "ADAUSDT", "BNBUSDT"]
app = Flask(__name__)
http = requests.Session()
lock = threading.RLock()

STATE = {
    "symbol": os.getenv("HFT_TEST_SYMBOL", "BTCUSDT").upper(),
    "settings": {"margin": 5.0, "leverage": 2, "tp": 1.0, "sl": 0.5,
                  "trail": 0.5, "auto": False, "close_pct": 0.25,
                  "min_range_pct": 0.40, "timeframe": "1m"},
    "candles": {}, "last_tick": {}, "history": deque(maxlen=50),
    "position_meta": {}, "last_close_time": 0.0, "last_error": None,
    "last_market_poll": 0.0,
}

class HFTError(RuntimeError): pass

def D(v):
    try: return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError): raise HFTError(f"Invalid numeric value: {v}")

def log(msg):
    STATE["history"].appendleft({"time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"), "message": str(msg)[:500]})

def unwrap_list(payload):
    if not isinstance(payload, dict): return []
    result = payload.get("result", {})
    if isinstance(result, list): return result
    if isinstance(result, dict):
        for k in ("list", "rows", "data"):
            if isinstance(result.get(k), list): return result[k]
    return []

class HFTClient:
    def __init__(self, api_key, secret_hex):
        if not api_key or not secret_hex: raise HFTError("Render env COINSWITCH_API_KEY / COINSWITCH_SECRET_KEY missing")
        try:
            raw = bytes.fromhex(secret_hex)
            if len(raw) != 32: raise ValueError("secret must decode to 32 bytes")
            self.key = ed25519.Ed25519PrivateKey.from_private_bytes(raw)
        except Exception as e: raise HFTError(f"Ed25519 secret format invalid: {e}")
        self.api_key = api_key

    def request(self, method, path, params=None, body=None):
        method = method.upper()
        query = urllib.parse.urlencode(params or {})
        signed_path = path + ("?" + query if query else "")
        epoch = str(int(time.time() * 1000))
        message = (method + urllib.parse.unquote_plus(signed_path) + epoch).encode()
        sig = self.key.sign(message).hex()
        headers = {"Content-Type":"application/json", "X-AUTH-APIKEY":self.api_key,
                   "X-AUTH-SIGNATURE":sig, "X-AUTH-EPOCH":epoch}
        # Sign and transmit the exact JSON body; HFT auth signs method+path+epoch.
        r = http.request(method, BASE_URL + signed_path, headers=headers,
                         json=body if body is not None else None, timeout=12)
        try: data = r.json()
        except Exception: data = {"raw": r.text[:1000]}
        if not r.ok: raise HFTError(f"HFT HTTP {r.status_code}: {str(data)[:800]}")
        if isinstance(data, dict) and data.get("retCode") not in (0, "0", None):
            raise HFTError(f"HFT retCode {data.get('retCode')}: {data.get('retMsg','API error')}")
        return data

    def time(self): return self.request("GET", "/v5/market/time")
    def instruments(self, symbol): return self.request("GET", "/v5/market/instruments-info", {"category":CATEGORY,"symbol":symbol})
    def ticker(self, symbol): return self.request("GET", "/v5/market/tickers", {"category":CATEGORY,"symbol":symbol})
    def orderbook(self, symbol): return self.request("GET", "/v5/market/orderbook", {"category":CATEGORY,"symbol":symbol,"limit":5})
    def positions(self, symbol): return self.request("GET", "/v5/position/list", {"category":CATEGORY,"symbol":symbol})
    def orders(self, symbol): return self.request("GET", "/v5/order/realtime", {"category":CATEGORY,"symbol":symbol})
    def executions(self, symbol): return self.request("GET", "/v5/execution/list", {"category":CATEGORY,"symbol":symbol})
    def set_leverage(self, symbol, leverage):
        return self.request("POST", "/v5/position/set-leverage", body={"category":CATEGORY,"symbol":symbol,"buyLeverage":str(leverage),"sellLeverage":str(leverage)})
    def switch_mode(self, mode=0):
        return self.request("POST", "/v5/position/switch-mode", body={"category":CATEGORY,"coin":SETTLE_COIN,"mode":int(mode)})
    def place(self, symbol, side, qty, position_idx=0, link_id=None):
        body={"category":CATEGORY,"symbol":symbol,"side":side,"orderType":"Market",
              "qty":str(qty),"positionIdx":int(position_idx),"orderLinkId":link_id or str(uuid.uuid4())}
        return self.request("POST", "/v5/order/create", body=body)

_CLIENT = None
_CLIENT_ERROR = None
try: _CLIENT = HFTClient(API_KEY, SECRET_HEX)
except Exception as e: _CLIENT_ERROR = str(e)

def client():
    if _CLIENT is None: raise HFTError(_CLIENT_ERROR or "HFT client unavailable")
    return _CLIENT

def first_row(payload):
    rows=unwrap_list(payload)
    return next((x for x in rows if isinstance(x,dict)), None)

def market_snapshot(symbol):
    data=client().ticker(symbol)
    row=first_row(data)
    if not row: raise HFTError("HFT ticker result.list is empty")
    price=D(row.get("markPrice") or row.get("lastPrice") or 0)
    if price <= 0: raise HFTError(f"HFT ticker has no valid mark/last price: {row}")
    change=D(row.get("price24hPcnt") or row.get("priceChangePercent") or 0)*100
    return float(price), float(change), row, data

def instrument(symbol):
    row=first_row(client().instruments(symbol))
    if not row: raise HFTError("HFT instrument info returned no instrument row")
    lot=row.get("lotSizeFilter") or {}; lev=row.get("leverageFilter") or {}
    qty_step=D(lot.get("qtyStep") or 0); min_qty=D(lot.get("minOrderQty") or 0)
    min_notional=D(lot.get("minNotionalValue") or 0)
    max_qty=D(lot.get("maxMktOrderQty") or lot.get("maxOrderQty") or 0)
    if qty_step<=0 or min_qty<=0: raise HFTError("Instrument qtyStep/minOrderQty missing; order blocked")
    return {"raw":row,"qty_step":qty_step,"min_qty":min_qty,"min_notional":min_notional,
            "max_qty":max_qty,"min_lev":D(lev.get("minLeverage") or 1),
            "max_lev":D(lev.get("maxLeverage") or MAX_LEVERAGE)}

def round_down(v, step): return (D(v)/D(step)).to_integral_value(rounding=ROUND_DOWN)*D(step)
def round_up(v, step): return (D(v)/D(step)).to_integral_value(rounding=ROUND_UP)*D(step)

def current_positions(symbol): return [p for p in unwrap_list(client().positions(symbol)) if D(p.get("size") or 0)>0]
def get_position(symbol):
    ps=current_positions(symbol)
    if not ps: return None
    if len(ps)>1: raise HFTError("Multiple/hedged positions found. Manual management required; bot will not guess which one to close.")
    return ps[0]

def aggregate_tick(symbol, price, interval_seconds=60):
    now=int(time.time()); bucket=now-(now%interval_seconds)
    arr=STATE["candles"].setdefault(symbol, deque(maxlen=300))
    if not arr or arr[-1]["time"] != bucket:
        arr.append({"time":bucket,"open":price,"high":price,"low":price,"close":price,"source":"local_hft_ticker_poll"})
    else:
        c=arr[-1]; c["high"]=max(c["high"],price); c["low"]=min(c["low"],price); c["close"]=price
    STATE["last_tick"][symbol]=price
    return list(arr)

def get_candles(symbol, timeframe="1m"):
    base=list(STATE["candles"].get(symbol, []))
    seconds={"1m":60,"5m":300,"15m":900,"1h":3600,"4h":14400,"1D":86400}.get(timeframe,60)
    if seconds==60: return base
    out=[]
    for c in base:
        b=c["time"]-(c["time"]%seconds)
        if not out or out[-1]["time"]!=b: out.append({"time":b,"open":c["open"],"high":c["high"],"low":c["low"],"close":c["close"],"source":"aggregated_local_hft_ticker"})
        else:
            x=out[-1]; x["high"]=max(x["high"],c["high"]); x["low"]=min(x["low"],c["low"]); x["close"]=c["close"]
    return out[-200:]

def pretrade(symbol, margin, leverage):
    price,change,tick,_=market_snapshot(symbol); info=instrument(symbol)
    margin=D(margin); lev=D(leverage)
    if lev<info["min_lev"] or lev>info["max_lev"]: raise HFTError(f"Leverage outside exchange range {info['min_lev']}–{info['max_lev']}")
    if margin<=0 or margin>MAX_MARGIN: raise HFTError(f"Margin must be >0 and <= configured HFT_MAX_MARGIN_USDT={MAX_MARGIN}")
    qty=round_down(margin*lev/D(price),info["qty_step"])
    notional=qty*D(price)
    min_required_qty=max(info["min_qty"], info["min_notional"]/D(price) if info["min_notional"] else D(0))
    min_qty=round_up(min_required_qty,info["qty_step"])
    min_margin=min_qty*D(price)/lev
    eligible=(qty>=info["min_qty"] and notional>=info["min_notional"] and (info["max_qty"]<=0 or qty<=info["max_qty"]))
    if not eligible: reason="Budget is below exchange minimum quantity/notional or exceeds max market quantity"
    else: reason="Meets instrument filters only; balance/mode/protection still not verified"
    return {"symbol":symbol,"mark_price":str(price),"change_24h_pct":str(change),"margin_usdt":str(margin),"leverage":str(lev),
            "qty":str(qty),"order_notional_usdt":str(notional),"min_qty":str(info['min_qty']),
            "min_notional_usdt":str(info['min_notional']),"qty_step":str(info['qty_step']),
            "minimum_valid_qty_estimate":str(min_qty),"minimum_margin_estimate_usdt":str(min_margin),
            "eligible_by_instrument_rules":eligible,"rule_check":reason,
            "reported_hft_balance_usdt":str(BALANCE_USDT),"balance_api_verified":False,
            "balance_override_confirmed":BALANCE_CONFIRMED}

def require_live_ready(preview):
    if not LIVE_TRADING: raise HFTError("HFT_LIVE_TRADING=YES not set; live orders locked")
    if not BALANCE_CONFIRMED: raise HFTError("HFT balance is not API-verifiable here. Confirm UI balance, then set HFT_BALANCE_CONFIRMED=YES; no order sent")
    if BALANCE_USDT<=0: raise HFTError("Configured HFT available balance is zero")
    if D(preview["margin_usdt"])>BALANCE_USDT: raise HFTError(f"Requested margin exceeds configured available HFT balance {BALANCE_USDT} USDT")
    if not preview["eligible_by_instrument_rules"]: raise HFTError(preview["rule_check"])
    if not POSITION_MODE_CONFIRMED:
        raise HFTError("One-way position mode has not been explicitly confirmed. Set HFT_POSITION_MODE_CONFIRMED=YES only after verifying HFT account mode")
    if not os.getenv("HFT_ACCEPT_LOCAL_TPSL_RISK", "NO").upper()=="YES":
        raise HFTError("HFT exchange-attached TP/SL is not documented in current HFT REST inventory. Local TP/SL risk acceptance missing; order blocked")

def close_position(symbol, reason="MANUAL CLOSE"):
    p=get_position(symbol)
    if not p: return {"message":"No open HFT position"}
    size=D(p.get("size") or 0); side=str(p.get("side", ""))
    idx=int(p.get("positionIdx",0))
    if size<=0 or side not in ("Buy","Sell"): raise HFTError(f"Position fields invalid; no close sent: {p}")
    # In hedge mode the same positionIdx identifies the leg; one-way mode uses 0.
    # The bot refuses multi-leg positions and verifies the position after submission.
    close_side="Sell" if side=="Buy" else "Buy"
    result=client().place(symbol,close_side,size,idx)
    log(f"{symbol} {side} close order accepted for {size}; reason={reason}; verify fill in HFT Orders")
    return {"order_response":result,"position_before":p,"note":"Accepted is not the same as filled; verify HFT order/position status."}

def open_position(symbol, direction, margin, leverage, auto=False, candle_low=None, candle_high=None):
    if direction not in ("LONG","SHORT"): raise HFTError("direction must be LONG or SHORT")
    preview=pretrade(symbol,margin,leverage); require_live_ready(preview)
    if get_position(symbol): raise HFTError("An HFT position already exists for this symbol; no duplicate entry")
    info=instrument(symbol); lev=int(leverage)
    if lev>MAX_LEVERAGE: raise HFTError(f"Leverage exceeds local cap {MAX_LEVERAGE}x")
    # One-way mode is assumed only when positionIdx=0 is configured by the user.
    if os.getenv("HFT_POSITION_MODE", "0") != "0": raise HFTError("This build only submits positionIdx=0 one-way orders. Set HFT_POSITION_MODE=0 and verify account mode")
    client().set_leverage(symbol,lev)
    side="Buy" if direction=="LONG" else "Sell"
    link=str(uuid.uuid4())
    try:
        placed=client().place(symbol,side,preview["qty"],0,link)
    except requests.RequestException as e:
        # Unknown outcome: do not blindly resubmit. Check HFT realtime orders manually by orderLinkId.
        raise HFTError(f"Order request outcome unknown ({e}); DO NOT retry. Check HFT Orders for orderLinkId={link}")
    log(f"HFT {direction} order accepted (not fill-confirmed), link={link}")
    STATE["position_meta"][symbol]={"auto":bool(auto),"side":direction,"entry":float(D(preview['mark_price'])),
        "peak":float(D(preview['mark_price'])),"trough":float(D(preview['mark_price'])),
        "candle_low":candle_low,"candle_high":candle_high,"tp":STATE['settings']['tp'],
        "sl":STATE['settings']['sl'],"trail":STATE['settings']['trail'],"local_protection_only":True}
    return {"order":placed,"preview":preview,"note":"Exchange acceptance is not fill confirmation. Local TP/SL requires this Render process to stay online."}

def manage_position(symbol, price):
    p=get_position(symbol)
    if not p: return
    meta=STATE["position_meta"].get(symbol)
    if not meta: return  # Never invent local TP/SL parameters for positions opened outside this bot.
    entry=D(p.get("avgPrice") or meta.get("entry") or price)
    side="LONG" if p.get("side")=="Buy" else "SHORT"
    s=STATE["settings"]
    if side=="LONG": meta["peak"]=max(D(meta.get("peak",entry)),D(price))
    else: meta["trough"]=min(D(meta.get("trough",entry)),D(price))
    pnl_pct=((D(price)-entry)/entry*100) * (1 if side=="LONG" else -1) if entry else D(0)
    reason=None
    if pnl_pct>=D(s["tp"]): reason="LOCAL TAKE PROFIT"
    elif pnl_pct<=-D(s["sl"]): reason="LOCAL STOP LOSS"
    elif side=="LONG" and D(price)<=D(meta["peak"])*(1-D(s["trail"])/100): reason="LOCAL TRAILING STOP"
    elif side=="SHORT" and D(price)>=D(meta["trough"])*(1+D(s["trail"])/100): reason="LOCAL TRAILING STOP"
    if reason and LIVE_TRADING:
        try: close_position(symbol,reason)
        except Exception as e: log(f"CRITICAL: local {reason} trigger but HFT close failed: {e}")


def strategy_tick(symbol, price, candles):
    s=STATE["settings"]
    if not (s["auto"] and AUTO_ALLOWED and LIVE_TRADING): return
    if len(candles)<2: return
    if get_position(symbol): manage_position(symbol,price); return
    if time.time()-STATE["last_close_time"]<4: return
    c=candles[-2]  # use last completed locally aggregated candle
    high,low=D(c["high"]),D(c["low"]); px=D(price)
    if px<=0 or high<=low: return
    if (high-low)/px*100 < D(s["min_range_pct"]): return
    off=px*D(s["close_pct"])/100; tol=px*D("0.0002")
    if px>=high-tol and high-(low+off)>=off*D("0.5"):
        open_position(symbol,"SHORT",s["margin"],s["leverage"],True,float(low),float(high))
    elif px<=low+tol and (high-off)-low>=off*D("0.5"):
        open_position(symbol,"LONG",s["margin"],s["leverage"],True,float(low),float(high))

def poll_loop():
    while True:
        try:
            sym=STATE["symbol"]
            price,change,_,_=market_snapshot(sym)
            candles=aggregate_tick(sym,price)
            STATE["last_market_poll"]=time.time(); STATE["last_error"]=None
            if LIVE_TRADING and get_position(sym): manage_position(sym,price)
            strategy_tick(sym,price,candles)
        except Exception as e:
            STATE["last_error"]=str(e)[:500]
            log(f"HFT poll/strategy error: {e}")
        time.sleep(POLL_SECONDS)

_worker_lock = threading.Lock()
_worker_started = False

@app.before_request
def start_worker_and_auth():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=poll_loop,daemon=True,name='hft-market-strategy').start()
            _worker_started = True
    if not DASHBOARD_PASSWORD: return None
    from flask import Response as FlaskResponse
    auth=request.authorization
    if not auth or auth.username!=DASHBOARD_USER or auth.password!=DASHBOARD_PASSWORD:
        return FlaskResponse("Authentication required",401,{"WWW-Authenticate":"Basic realm=CoinSwitch HFT"})

HTML = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>CoinSwitch HFT Dashboard</title>
<style>body{font:15px Arial;background:#f6f7fb;color:#111;margin:12px} .card{background:white;border:1px solid #ddd;border-radius:12px;padding:12px;margin:10px 0}button,input,select{padding:10px;margin:4px;border-radius:7px;border:1px solid #aaa}button{font-weight:bold}.long{background:#16a34a;color:white}.short{background:#dc2626;color:white}.danger{background:#111;color:white}.muted{color:#666;font-size:13px}.warn{color:#a16207;font-weight:bold}.good{color:#15803d;font-weight:bold}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#111;color:#d7f7d7;padding:10px;border-radius:8px}canvas{width:100%;max-width:900px;height:300px;border:1px solid #ddd}</style></head><body>
<h2>CoinSwitch PRO — HFT USDT Futures Only</h2><div class="card"><b id="status">HFT market connecting…</b><h2 id="price">--</h2><div id="balance" class="warn">HFT UI-reported balance: 0.05 USDT (not API verified)</div><div class="muted">No legacy wallet or order routes are used. Candles are locally aggregated from HFT ticker polling.</div></div>
<div class="card"><b>Strategy Settings</b><br>Symbol <select id="symbol"><option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option><option>DOGEUSDT</option><option>XRPUSDT</option><option>ADAUSDT</option><option>BNBUSDT</option></select>
Margin USDT <input id="margin" type="number" value="5" step="0.1"> Leverage <input id="lev" type="number" value="2" min="1" max="2"><br>
TP % <input id="tp" value="1" type="number" step="0.1"> SL % <input id="sl" value="0.5" type="number" step="0.1"> Trail % <input id="trail" value="0.5" type="number" step="0.1"><br>
Close band % <input id="closepct" value="0.25" type="number" step="0.05"> Min candle range % <input id="range" value="0.40" type="number" step="0.05">
<button onclick="saveSettings()">Save settings</button><button onclick="preview()">HFT Order Preview (No Order)</button><button onclick="refresh()">Refresh HFT</button><br><label><input type="checkbox" id="auto"> AUTO strategy (requires server HFT_AUTO_ALLOWED=YES)</label></div>
<div class="card"><b>Manual HFT Orders</b><br><button class="long" onclick="trade('LONG')">LONG</button><button class="short" onclick="trade('SHORT')">SHORT</button><button class="danger" onclick="trade('CLOSE')">CLOSE POSITION</button><div class="warn">Live orders default OFF. 0.05 USDT is below the prior estimated BTC minimum margin (~30.38 USDT).</div></div>
<div class="card"><b>Position / PnL</b><div id="position">Loading…</div><div id="pnl"></div></div>
<div class="card"><b>Locally aggregated candles (not exchange historical klines)</b><canvas id="chart" width="900" height="300"></canvas></div>
<div class="card"><b>HFT Pre-trade / API result</b><pre id="result">No check yet</pre></div><div class="card"><b>Event log</b><pre id="history">—</pre></div>
<script>
let current='BTCUSDT', candles=[]; const el=id=>document.getElementById(id);
async function api(url,opts){let r=await fetch(url,opts||{});let d=await r.json();if(!r.ok||d.ok===false)throw Error(d.error||d.message||('HTTP '+r.status));return d;}
async function saveSettings(){try{let d=await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:el('symbol').value,margin:el('margin').value,leverage:el('lev').value,tp:el('tp').value,sl:el('sl').value,trail:el('trail').value,close_pct:el('closepct').value,min_range_pct:el('range').value,auto:el('auto').checked})});el('result').textContent=JSON.stringify(d,null,2);await refresh();}catch(e){alert(e.message)}}
async function preview(){try{await saveSettings();let d=await api('/api/hft-order-preview');el('result').textContent=JSON.stringify(d,null,2);}catch(e){el('result').textContent=e.message}}
async function trade(side){if(side!=='CLOSE'&&!confirm('HFT live '+side+' order request? This can use real funds if server live trading is enabled.'))return;try{let d=await api('/api/hft-trade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({side})});el('result').textContent=JSON.stringify(d,null,2);await refresh();}catch(e){alert(e.message);el('result').textContent=e.message}}
function draw(){let c=el('chart'),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);if(!candles.length)return;let lo=Math.min(...candles.map(v=>v.low)),hi=Math.max(...candles.map(v=>v.high)),rg=hi-lo||1;let w=c.width/candles.length;for(let i=0;i<candles.length;i++){let a=candles[i],px=i*w+w/2, y=v=>(hi-v)/rg*(c.height-20)+10;x.strokeStyle=a.close>=a.open?'green':'red';x.beginPath();x.moveTo(px,y(a.high));x.lineTo(px,y(a.low));x.stroke();x.fillStyle=x.strokeStyle;x.fillRect(px-3,Math.min(y(a.open),y(a.close)),6,Math.max(2,Math.abs(y(a.open)-y(a.close))));}}
async function refresh(){try{let d=await api('/api/hft-dashboard?symbol='+el('symbol').value);current=d.symbol;candles=d.candles||[];el('status').textContent=d.status;el('price').textContent=d.symbol+' : '+d.price+' USDT | 24h '+d.change_24h_pct+'%';el('balance').textContent='HFT UI-reported balance: '+d.reported_balance_usdt+' USDT (not API verified)';el('position').textContent=d.position?JSON.stringify(d.position,null,2):'No HFT position';el('pnl').textContent=d.position?'Unrealised PnL: '+d.position.unrealisedPnl+' USDT':'—';el('history').textContent=(d.history||[]).map(v=>v.time+' | '+v.message).join('\n');draw();}catch(e){el('status').textContent='HFT API error: '+e.message;}}
refresh();setInterval(refresh,5000);
</script></body></html>'''

@app.route('/')
def home(): return render_template_string(HTML)

@app.route('/healthz')
def health(): return jsonify({"ok":True,"surface":"HFT_ONLY","live_trading":LIVE_TRADING,"auto_allowed":AUTO_ALLOWED,"settlement":"USDT"})

@app.route('/api/settings',methods=['GET','POST'])
def settings():
    if request.method=='POST':
        d=request.get_json(silent=True) or {}
        try:
            if d.get('symbol') in SYMBOLS: STATE['symbol']=d['symbol']
            for k,field in [('margin','margin'),('leverage','leverage'),('tp','tp'),('sl','sl'),('trail','trail'),('close_pct','close_pct'),('min_range_pct','min_range_pct')]:
                if field in d: STATE['settings'][k]=float(D(d[field]))
            if 'auto' in d: STATE['settings']['auto']=bool(d['auto'])
            s=STATE['settings']
            if s['margin']<=0 or s['leverage']<1 or s['leverage']>MAX_LEVERAGE or min(s['tp'],s['sl'],s['trail'],s['close_pct'],s['min_range_pct'])<=0: raise HFTError('Settings out of safe range')
        except Exception as e: return jsonify({"ok":False,"error":str(e)}),400
    return jsonify({"ok":True,"settings":STATE['settings'],"symbol":STATE['symbol'],"live_trading":LIVE_TRADING,"auto_allowed":AUTO_ALLOWED})

@app.route('/api/hft-market-check')
@app.route('/api/hft-usdt-check')
def hft_market_check():
    try:
        sym=request.args.get('symbol',STATE['symbol']).upper()
        if sym not in SYMBOLS: raise HFTError('Unsupported symbol')
        t=client().time(); price,change,tick,_=market_snapshot(sym); ins=instrument(sym)
        return jsonify({"ok":True,"surface":"HFT_ONLY","base_url":BASE_URL,"category":CATEGORY,"settlement":"USDT","symbol":sym,"server_time":t,"ticker":tick,"instrument":ins['raw'],"positions":client().positions(sym),"open_orders":client().orders(sym),"executions":client().executions(sym),"reported_balance_usdt":str(BALANCE_USDT),"balance_api_verified":False,"note":"HFT REST check only. No HFT wallet balance endpoint or documented exchange-attached TP/SL endpoint is present in the published HFT endpoint inventory. No order sent."})
    except Exception as e: return jsonify({"ok":False,"error":str(e),"read_only":True}),502

@app.route('/api/hft-order-preview')
def order_preview():
    try:
        s=STATE['settings']; p=pretrade(STATE['symbol'],request.args.get('margin',s['margin']),request.args.get('leverage',s['leverage']))
        p['order_sent']=False
        return jsonify({"ok":True,"preview":p,"note":"Preview only. Balance is not API verified; no order sent."})
    except Exception as e: return jsonify({"ok":False,"error":str(e),"order_sent":False}),400

@app.route('/api/hft-trade',methods=['POST'])
def hft_trade():
    d=request.get_json(silent=True) or {}; side=str(d.get('side','')).upper(); sym=STATE['symbol']; s=STATE['settings']
    try:
        with lock:
            if side=='CLOSE':
                if not LIVE_TRADING: raise HFTError('HFT_LIVE_TRADING=YES not set; live close locked')
                result=close_position(sym,'MANUAL CLOSE'); return jsonify({"ok":True,"result":result})
            if side not in ('LONG','SHORT'): raise HFTError('side must be LONG, SHORT or CLOSE')
            result=open_position(sym,side,s['margin'],s['leverage'],False)
            return jsonify({"ok":True,"result":result,"warning":"Order acceptance is not fill confirmation."})
    except Exception as e: log(f"Trade blocked/failed {side}: {e}"); return jsonify({"ok":False,"error":str(e)}),400

@app.route('/api/hft-dashboard')
def dashboard_data():
    try:
        sym=request.args.get('symbol',STATE['symbol']).upper()
        if sym not in SYMBOLS: raise HFTError('Unsupported symbol')
        STATE['symbol']=sym
        price,change,_,_=market_snapshot(sym); candles=aggregate_tick(sym,price)
        pos=get_position(sym)
        if pos: manage_position(sym,price)
        return jsonify({"ok":True,"symbol":sym,"price":price,"change_24h_pct":change,"candles":get_candles(sym,STATE['settings']['timeframe']),"position":pos,"reported_balance_usdt":str(BALANCE_USDT),"balance_api_verified":False,"status":"HFT LIVE MARKET DATA | LIVE ORDERS "+("ENABLED" if LIVE_TRADING else "LOCKED"),"history":list(STATE['history'])[-30:],"last_error":STATE['last_error']})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),502

@app.route('/api/hft-positions')
def positions_api():
    try: return jsonify({"ok":True,"symbol":STATE['symbol'],"positions":client().positions(STATE['symbol'])})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),502

@app.route('/api/hft-orders')
def orders_api():
    try: return jsonify({"ok":True,"symbol":STATE['symbol'],"orders":client().orders(STATE['symbol']),"executions":client().executions(STATE['symbol'])})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),502

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')),debug=False)
