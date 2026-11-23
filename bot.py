"""Merged CoinSwitch PRO Futures dashboard/bot + REST client methods.

SAFETY DEFAULTS: live orders and auto trading are both OFF unless explicitly
enabled with Render environment variables. This is a starter integration and
must be validated against the user's CoinSwitch account before live use.
"""
import os, time, threading, uuid, urllib.parse, hmac
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from functools import wraps
from flask import Flask, jsonify, render_template_string, request, Response
import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

BASE_URL = "https://coinswitch.co"
API_KEY = os.getenv("COINSWITCH_API_KEY", "").strip()
SECRET_KEY = os.getenv("COINSWITCH_SECRET_KEY", "").strip()
HFT_SETTLE_COIN = os.getenv("HFT_SETTLE_COIN", "USDT").strip().upper()
LIVE_TRADING = False  # Safety lock: live orders disabled in this build
AUTO_TRADING_ALLOWED = False  # Safety lock: auto trading disabled in this build
MAX_MARGIN_USDT = os.getenv("MAX_MARGIN_USDT", "25")
# Manually observed on the user's CoinSwitch PRO HFT screen; NOT API-verified.
# Update Render env HFT_MANUAL_BALANCE_USDT whenever the displayed HFT balance changes.
HFT_MANUAL_BALANCE_USDT = os.getenv("HFT_MANUAL_BALANCE_USDT", "0.23").strip()
USDT_INR_RATE_FALLBACK = os.getenv("USDT_INR_RATE", "").strip()
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
app = Flask(__name__)
HTTP = requests.Session()
COINS = ["BTC", "ETH", "SOL", "DOGE", "XRP", "ADA", "BNB"]
INTERVALS = {"1m":"1", "5m":"5", "15m":"15", "1h":"60", "4h":"240", "1d":"1440"}
LOCK = threading.RLock()
HFT_TEST_LOCK = threading.Lock()
HFT_TEST_ORDER_ATTEMPTED = False
STATE = {
    "settings": {"auto": False, "leverage": 80, "amount": 0.10, "tp": 1.0,
                 "sl": 0.5, "trail": 0.5, "auto_pct": 0.5,
                 "auto_coin": "SOL", "close_pct": 0.25, "min_range_pct": 0.40},
    "history": [], "last_price": 0.0, "last_time": 0.0,
    "last_close_time": 0.0, "last_error": None,
}

class CoinSwitchError(RuntimeError):
    pass


class CoinSwitchHFTRo:
    """Read-only HFT diagnostics. No order or transfer methods are provided."""
    BASE_URL = "https://dma.coinswitch.co"

    def __init__(self, api_key, secret_hex):
        if not api_key or not secret_hex:
            raise CoinSwitchError("COINSWITCH_API_KEY / COINSWITCH_SECRET_KEY missing.")
        try:
            seed = bytes.fromhex(secret_hex)
            if len(seed) != 32:
                raise ValueError("Secret hex must decode to 32 bytes.")
            self.private_key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        except Exception as exc:
            raise CoinSwitchError("Ed25519 private-key format invalid: " + str(exc))
        self.api_key = api_key

    def get(self, path, params=None):
        query = urllib.parse.urlencode(params or {})
        signed_path = path + (("?" + query) if query else "")
        epoch = str(int(time.time() * 1000))
        message = ("GET" + urllib.parse.unquote_plus(signed_path) + epoch).encode("utf-8")
        signature = self.private_key.sign(message).hex()
        headers = {
            "Content-Type": "application/json",
            "X-AUTH-APIKEY": self.api_key,
            "X-AUTH-SIGNATURE": signature,
            "X-AUTH-EPOCH": epoch,
        }
        response = HTTP.get(self.BASE_URL + signed_path, headers=headers, timeout=15)
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text[:1200]}
        if not response.ok:
            raise CoinSwitchError(f"HFT HTTP {response.status_code}: {str(payload)[:1200]}")
        if isinstance(payload, dict) and payload.get("retCode") not in (None, 0, "0"):
            raise CoinSwitchError(f"HFT retCode={payload.get('retCode')}: {payload.get('retMsg', 'API error')}")
        return payload

    def post(self, path, body):
        epoch = str(int(time.time() * 1000))
        message = ("POST" + path + epoch).encode("utf-8")
        signature = self.private_key.sign(message).hex()
        headers = {"Content-Type":"application/json", "X-AUTH-APIKEY":self.api_key,
                   "X-AUTH-SIGNATURE":signature, "X-AUTH-EPOCH":epoch}
        response = HTTP.post(self.BASE_URL + path, headers=headers, json=body, timeout=12)
        try: payload = response.json()
        except Exception: payload = {"raw":response.text[:1200]}
        if not response.ok: raise CoinSwitchError(f"HFT HTTP {response.status_code}: {str(payload)[:1000]}")
        if not isinstance(payload, dict): raise CoinSwitchError("Unexpected HFT response; check app before retrying.")
        code = payload.get("retCode")
        if code not in (None, 0, "0", 110043, "110043"):
            raise CoinSwitchError(f"HFT retCode={code}: {payload.get('retMsg','API error')}")
        return payload

    def set_leverage(self, symbol, leverage):
        return self.post("/v5/position/set-leverage", {"category":"linear", "symbol":symbol,
                         "buyLeverage":str(leverage), "sellLeverage":str(leverage)})

    def place_order(self, payload):
        return self.post("/v5/order/create", payload)

    def server_time(self):
        # Authenticated HFT endpoint; useful for confirming signed HFT connectivity.
        return self.get("/v5/market/time")

    def instruments(self, symbol="BTCUSDT"):
        # HFT docs show category as required; symbol is an optional filter.
        return self.get("/v5/market/instruments-info", {
            "category": "linear", "symbol": symbol
        })

    def positions(self, symbol="BTCUSDT"):
        # Positions endpoint requires category; symbol is an optional filter.
        return self.get("/v5/position/list", {
            "category": "linear", "symbol": symbol
        })

    def tickers(self, symbol="BTCUSDT"):
        # Read-only HFT futures ticker; includes last/mark and best bid/ask.
        return self.get("/v5/market/tickers", {
            "category": "linear", "symbol": symbol
        })

    def order_book(self, symbol="BTCUSDT", limit=5):
        # Read-only order-book snapshot; no order is submitted.
        return self.get("/v5/market/orderbook", {
            "category": "linear", "symbol": symbol, "limit": int(limit)
        })

    def realtime_orders(self, symbol="BTCUSDT"):
        # Read-only currently-open/recent orders.
        return self.get("/v5/order/realtime", {
            "category": "linear", "symbol": symbol
        })

    def executions(self, symbol="BTCUSDT"):
        # Read-only fills/executions.
        return self.get("/v5/execution/list", {
            "category": "linear", "symbol": symbol
        })



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

    def spot_portfolio(self):
        # Read-only main CoinSwitch wallet portfolio (includes the INR row).
        # This is NOT the Futures margin wallet.
        return self.request("GET", "/trade/api/v2/user/portfolio")

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


    # Additional methods merged from coinswitch_futures.py
    def wallet_balance(self):
        return self.wallet()

    def transactions(self, symbol=None, type=None, limit=None,
                     from_time=None, to_time=None):
        params = {"exchange": "EXCHANGE_2"}
        if symbol: params["symbol"] = symbol
        if type: params["type"] = type
        if limit is not None: params["limit"] = limit
        if from_time is not None: params["from_time"] = from_time
        if to_time is not None: params["to_time"] = to_time
        return self.request("GET", "/trade/api/v2/futures/transactions", params)

    def order_book(self, symbol):
        return self.request("GET", "/trade/api/v2/futures/order_book",
                            {"exchange": "EXCHANGE_2", "symbol": symbol})

    def all_pairs_ticker(self):
        return self.request("GET", "/trade/api/v2/futures/all-pairs/ticker",
                            {"exchange": "EXCHANGE_2"})

    def market_trades(self, symbol):
        return self.request("GET", "/trade/api/v2/futures/trades",
                            {"exchange": "EXCHANGE_2", "symbol": symbol})

    def cancel_order(self, order_id):
        return self.request("DELETE", "/trade/api/v2/futures/order",
                            body={"exchange": "EXCHANGE_2", "order_id": order_id})

    def cancel_all_open_orders(self, symbol=None):
        body = {"exchange": "EXCHANGE_2"}
        if symbol: body["symbol"] = symbol
        return self.request("POST", "/trade/api/v2/futures/cancel-all", body=body)

    def get_order_status(self, order_id):
        return self.request("GET", "/trade/api/v2/futures/order",
                            {"exchange": "EXCHANGE_2", "order_id": order_id})

    def open_orders(self, symbol=None, limit=None):
        body = {"exchange": "EXCHANGE_2"}
        if symbol: body["symbol"] = symbol
        if limit is not None: body["limit"] = limit
        return self.request("POST", "/trade/api/v2/futures/orders/open", body=body)

    def closed_orders(self, symbol=None, count=None):
        body = {"exchange": "EXCHANGE_2"}
        if symbol: body["symbol"] = symbol
        if count is not None: body["count"] = count
        return self.request("POST", "/trade/api/v2/futures/orders/closed", body=body)

    def get_leverage(self, symbol):
        return self.request("GET", "/trade/api/v2/futures/leverage",
                            {"exchange": "EXCHANGE_2", "symbol": symbol})

    def add_margin(self, symbol, margin):
        return self.request("POST", "/trade/api/v2/futures/add_margin",
                            body={"exchange": "EXCHANGE_2", "symbol": symbol,
                                  "margin": float(margin)})

    def place_order(self, *, symbol, side, order_type, quantity, price=None,
                    trigger_price=None, reduce_only=None, time_in_force=None,
                    client_order_id=None):
        # Compatibility wrapper matching the standalone FuturesClient API.
        extra = {}
        if price is not None: extra["price"] = price
        if trigger_price is not None: extra["trigger_price"] = trigger_price
        if reduce_only is not None: extra["reduce_only"] = reduce_only
        if time_in_force is not None: extra["time_in_force"] = time_in_force
        if client_order_id is not None: extra["client_order_id"] = client_order_id
        return self.order(symbol, side, order_type, quantity, **extra)


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
    """Read authenticated Futures wallet; do not hide malformed responses as zero."""
    response = require_client().wallet()
    raw = unwrap(response)
    node = raw
    for _ in range(3):
        if isinstance(node, dict) and "base_asset_balances" in node:
            break
        if isinstance(node, dict) and isinstance(node.get("data"), (dict, list)):
            node = node["data"]
        else:
            break
    base = node.get("base_asset_balances", []) if isinstance(node, dict) else []
    if not isinstance(base, list):
        raise CoinSwitchError(f"Futures wallet response format unexpected: {str(response)[:500]}")
    for item in base:
        if isinstance(item, dict) and str(item.get("base_asset", "")).upper() == "USDT":
            b = item.get("balances", {})
            if not isinstance(b, dict):
                raise CoinSwitchError("Futures wallet me USDT balances object nahi mila.")
            required = ("total_balance", "total_available_balance", "total_blocked_balance")
            missing = [k for k in required if k not in b]
            if missing:
                raise CoinSwitchError("Futures wallet response me balance fields missing: " + ", ".join(missing))
            return {"total":float(D(b["total_balance"])),
                    "available":float(D(b["total_available_balance"])),
                    "blocked":float(D(b["total_blocked_balance"])),
                    "source":"CoinSwitch Futures wallet_balance"}
    if not base:
        # Do not silently convert an unknown/changed API response into a real zero balance.
        # Surface the response shape so the wallet schema can be mapped correctly.
        try:
            diagnostic = str(response)[:1200]
        except Exception:
            diagnostic = repr(type(response))
        raise CoinSwitchError(
            "CoinSwitch API connected, but response has no base_asset_balances/USDT row. "
            "Wallet response diagnostic (no API credentials): " + diagnostic
        )
    raise CoinSwitchError(f"USDT balance row not found in Futures wallet response: {str(response)[:500]}")

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
            coin = s.get("auto_coin", "SOL")
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

HTML = '\n<style>body{font-family:Arial;text-align:center;padding:8px}button{padding:10px 14px;margin:3px;font-size:15px;border-radius:8px;border:1px solid #000}.long{background:#22c55e;color:#fff}.short{background:#ef4444;color:#fff}.close{background:#000;color:#fff}.tf{background:#eef}.tf.active{background:#000;color:#fff}.tool{background:#fff;border:2px solid #7c3aed}.tool.active{background:#7c3aed;color:#fff}.box{border:1px solid #ccc;border-radius:10px;padding:10px;margin:8px auto;max-width:860px;text-align:left}input,select{padding:8px;margin:4px;font-size:15px;width:110px}.on{color:green;font-weight:bold}.off{color:red;font-weight:bold}.live{color:#ef4444;font-weight:bold;animation:blink 1s infinite}@keyframes blink{50%{opacity:0.3}}.tpbox{background:#f0fdf4;border:1px solid #22c55e;border-radius:8px;padding:8px;margin-top:8px}.slbox{background:#fef2f2;border:1px solid #ef4444;border-radius:8px;padding:8px;margin-top:6px}.hlbox{background:#fff7ed;border:1px solid #f59e0b;border-radius:8px;padding:8px;margin-top:8px}</style>\n<h2>🚀 CoinSwitch PRO Futures Dashboard — HFT Market Monitor + One-Time HFT Test</h2>\n<div><button onclick="load(\'BTC\')">BTC</button><button onclick="load(\'ETH\')">ETH</button><button onclick="load(\'SOL\')">SOL</button><button onclick="load(\'DOGE\')">DOGE</button><button onclick="load(\'XRP\')">XRP</button><button onclick="load(\'ADA\')">ADA</button><button onclick="load(\'BNB\')">BNB</button></div>\n<h1 id="p">Loading...</h1>\n<div><b style="color:#b45309">● MARKET DATA LIVE | ORDERS LOCKED</b> <span style="font-size:14px">High pe SHORT | Low pe LONG | Close = 0.25% andar | Fees/funding vary — no fixed-profit guarantee | Chhoti Candle Skip</span></div>\n<h2 id="bal" style="color:#0a7">💰 Regular Futures Wallet: Available USDT -- | ₹ --</h2>\n<h3 id="locked" style="color:#d97706">🔒 Regular Futures Wallet: Locked USDT -- | Total USDT --</h3><h4 id="fx">USDT/INR rate: loading...</h4>\n<div class="box" id="mainWallet">Main CoinSwitch Wallet (read-only portfolio): INR balance not checked yet. Tap “Check Main INR Wallet”. This is not the Futures margin balance.</div>\n<div class="box"><b>CoinSwitch HFT — Live Market & Exchange Rules</b><br><span>Observed HFT balance: __HFT_MANUAL_BALANCE__ USDT (manual screenshot only; not live API verified). HFT order execution uses the separate dma.coinswitch.co API.</span><div id="coinAverageTable" class="hlbox">Loading live prices and leverage limits...</div></div>\n<div class="box" style="border:2px solid #b91c1c"><b style="color:#b91c1c">⚠️ ONE-TIME HFT LIVE TEST — SOLUSDT ONLY</b><br>\nDirection: <select id="hftSide"><option value="LONG">LONG (Buy)</option><option value="SHORT">SHORT (Sell)</option></select>\nLeverage: <input id="hftLev" type="number" value="80" min="1" max="100" step="0.01">\nPosition mode: <select id="hftMode"><option value="0">One-way (positionIdx 0)</option><option value="1">Hedge LONG (positionIdx 1)</option><option value="2">Hedge SHORT (positionIdx 2)</option></select><br>\n<button class="short" onclick="placeOneHFTOrder()">⚡ PLACE ONE SOL LIVE TEST ORDER</button>\n<div id="hftOrderStatus" class="hlbox">Locked by default. Render env HFT_LIVE_ORDERS=YES is required. Single order only; max notional 5 USDT.</div>\n<small>Confirm your HFT free USDT and position mode in CoinSwitch before pressing. HFT balance is not API-verified here. This uses a best-bid/ask IOC limit order, not a market order; it may not fill. No auto-repeat, auto-close, or TP/SL is sent. 80x/100x leverage can liquidate a position after a very small adverse move.</small></div>\n<h3 id="pos">No Position</h3>\n<h3 id="trailInfo" style="color:#7c3aed">Trailing: -</h3>\n<h3 id="pnl" style="color:blue">Live PnL: USDT 0</h3>\n<div class="box"><b>⚙️ SETTING</b><br>\nLeverage (exchange limit checked): <select id="lev" onchange="saveSet()"><option>80</option><option>100</option></select> x &nbsp;\nMargin USDT (preview only; max 0.23): <input id="amt" type="number" value="0.10" min="0.0001" max="0.23" step="0.01" onchange="saveSet()"> &nbsp;\nTP %: <input id="tp" type="number" value="1.0" step="0.1" onchange="saveSet()"> &nbsp;\nSL %: <input id="sl" type="number" value="0.5" step="0.1" onchange="saveSet()"> &nbsp;\nTrail %: <input id="trail" type="number" value="0.5" step="0.1" onchange="saveSet()"><br>\n<span class="off">AUTO TRADING PERMANENTLY OFF IN THIS BUILD</span><div style="display:none"><select id="auto"><option value="0" selected>OFF</option></select><input id="auto_pct" value="0.5"><select id="auto_coin"><option selected>SOL</option></select></div><br>\n<div class="hlbox"><b>⚡ HIGH-LOW FIXED LOGIC - Live 1m Candle</b><br>\nClose %: <input id="close_pct" type="number" value="0.25" step="0.05" onchange="saveSet()"> % &nbsp;\nMin Candle Range %: <input id="min_range_pct" type="number" value="0.40" step="0.05" onchange="saveSet()"> %<br>\n<span style="font-size:13px">SHORT = High pe khulega, Low + 0.25% pe band. LONG = Low pe khulega, High - 0.25% pe band. Actual fees, funding and slippage apply; profit is not guaranteed. Candle Range 0.40% se chhota hua to trade nahi khulega - Fee bachega.</span>\n</div>\n<div id="tpPreview"></div>\n</div>\n<div class="hlbox"><b>Note:</b> Old Futures V2 order buttons removed. Chart and wallet widgets below are reference-only; the HFT order panel is separate.</div>\n\n<div style="margin-top:12px"><b>⏱️ TIME FRAME:</b>\n<button class="tf active" id="tf-1m" onclick="setTF(\'1m\')">1m</button><button class="tf" id="tf-5m" onclick="setTF(\'5m\')">5m</button><button class="tf" id="tf-15m" onclick="setTF(\'15m\')">15m</button><button class="tf" id="tf-1h" onclick="setTF(\'1h\')">1h</button><button class="tf" id="tf-4h" onclick="setTF(\'4h\')">4h</button><button class="tf" id="tf-1d" onclick="setTF(\'1d\')">1D</button>\n</div>\n<div style="margin-top:8px"><b>📐 LINE TOOL:</b>\n<button class="tool active" id="tool-none" onclick="setTool(\'none\')">🖱️ Normal</button><button class="tool" id="tool-h" onclick="setTool(\'h\')">➖ H-Line (Price)</button><button class="tool" id="tool-v" onclick="setTool(\'v\')">➡️ V-Line (Time)</button><button class="tool" onclick="clearLines()">🗑️ Lines Clear</button>\n</div>\n<canvas id="c" width="860" height="340" style="border:1px solid #000;width:100%;max-width:860px;margin-top:10px;cursor:crosshair"></canvas>\n<h3>📜 History</h3><div id="h"></div>\n<script>\nlet cur=\'SOL\'; let tf=\'1m\'; let tool=\'none\'; let hLines=[]; let vLines=[]; let lastCandles=[]; let lastPrice=0; let curPos=null; let curEntry=0;\nfunction setTF(t){tf=t;document.querySelectorAll(\'.tf\').forEach(b=>b.classList.remove(\'active\'));document.getElementById(\'tf-\'+t).classList.add(\'active\');fetchData();}\nfunction setTool(t){tool=t;document.querySelectorAll(\'.tool\').forEach(b=>b.classList.remove(\'active\'));if(t!=\'none\')document.getElementById(\'tool-\'+t).classList.add(\'active\');else document.getElementById(\'tool-none\').classList.add(\'active\');}\nfunction clearLines(){hLines=[];vLines=[];drawChart();}\nasync function load(coin){cur=coin;document.getElementById(\'auto_coin\').value=coin;saveSet();fetchData();}\nasync function saveSet(){let s={leverage:document.getElementById(\'lev\').value,amount:document.getElementById(\'amt\').value,tp:document.getElementById(\'tp\').value,sl:document.getElementById(\'sl\').value,trail:document.getElementById(\'trail\').value,auto:document.getElementById(\'auto\').value,auto_pct:document.getElementById(\'auto_pct\').value,auto_coin:document.getElementById(\'auto_coin\').value,close_pct:document.getElementById(\'close_pct\').value,min_range_pct:document.getElementById(\'min_range_pct\').value};await fetch(\'/api/settings?leverage=\'+s.leverage+\'&amount=\'+s.amount+\'&tp=\'+s.tp+\'&sl=\'+s.sl+\'&trail=\'+s.trail+\'&auto=\'+s.auto+\'&auto_pct=\'+s.auto_pct+\'&auto_coin=\'+s.auto_coin+\'&close_pct=\'+s.close_pct+\'&min_range_pct=\'+s.min_range_pct);document.getElementById(\'autoTxt\').innerText=s.auto==\'1\'?\'AUTO ON - High pe SHORT | Low pe LONG | Close \'+s.close_pct+\'%\':\'AUTO OFF\';updateTpSlPreview();}\nasync function trade(side){let q={coin:cur,side:side};if(side!==\'CLOSE\'&&!confirm(\'REAL CoinSwitch Futures order: \'+side+\' \'+cur+\'?\'))return;let r=await fetch(\'/api/trade\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(q)});let d=await r.json();alert(d.msg||JSON.stringify(d));fetchData();}\nasync function resetBal(){let b=document.getElementById(\'bal\');b.innerText=\'🔄 CoinSwitch Futures wallet checking...\';try{let r=await fetch(\'/api/wallet\',{cache:\'no-store\'});let d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||\'Wallet API request failed\');b.innerText=\'💰 Futures v2 Wallet — Available: USDT \'+Number(d.available).toLocaleString(\'en-IN\',{maximumFractionDigits:4})+\' | ₹ \'+(d.available_inr==null?\'--\':Number(d.available_inr).toLocaleString(\'en-IN\',{maximumFractionDigits:2}));document.getElementById(\'locked\').innerText=\'🔒 Futures v2 Wallet — Locked: USDT \'+Number(d.blocked).toFixed(4)+\' | Total: USDT \'+Number(d.total).toFixed(4)+\' | Total INR: ₹ \'+(d.total_inr==null?\'--\':Number(d.total_inr).toLocaleString(\'en-IN\',{maximumFractionDigits:2}));alert(\'CoinSwitch Futures Wallet API OK\\nAvailable: \'+d.available+\' USDT\\nTotal: \'+d.total+\' USDT\\n\'+d.source);await fetchData();}catch(e){b.innerText=\'⚠️ Wallet API error — see message\';alert(\'Wallet API error: \'+e.message);}}\n\nasync function checkHFT(){\nlet box=document.getElementById(\'hftResult\');\nbox.innerText=\'Checking HFT instruments and positions (read-only)...\';\ntry{\n let r=await fetch(\'/api/hft-readonly-check\',{cache:\'no-store\'});\n let d=await r.json();\n box.innerText=JSON.stringify(d,null,2);\n if(!r.ok||!d.ok) alert(\'HFT read-only check failed: \'+(d.error||(\'HTTP \'+r.status)));\n else alert(\'HFT read-only API connected. No funds moved and no orders placed.\');\n}catch(e){box.innerText=\'HFT check error: \'+e.message;}\n}\nasync function checkHFTMarket(){\nlet box=document.getElementById(\'hftResult\');\nbox.innerText=\'Checking HFT ticker, order book, open/recent orders, executions and positions (read-only)...\';\ntry{\n let r=await fetch(\'/api/hft-market-check\',{cache:\'no-store\'});\n let d=await r.json(); box.innerText=JSON.stringify(d,null,2);\n let panel=document.getElementById(\'hftSummary\');\n if(!r.ok||!d.ok){\n   panel.innerText=\'HFT check failed: \'+(d.error||(\'HTTP \'+r.status));\n   alert(\'HFT market check failed: \'+(d.error||(\'HTTP \'+r.status)));\n } else {\n   let x=d.instrument_rules_summary||{};\n   if(x.status===\'calculated\'){\n     let required=Number(x.minimum_margin_estimate_usdt);\n     let configured=Number(document.getElementById(\'amt\').value||0);\n     let leverage=Number(document.getElementById(\'lev\').value||0);\n     let enough=Number.isFinite(required)&&configured>=required;\n     panel.innerHTML=\'<b>HFT PRE-TRADE CHECK — \'+(x.symbol||\'\')+\'</b><br>\'+\n       \'Mark Price: \'+x.mark_price+\' USDT | Min Qty: \'+x.minimum_valid_qty_estimate+\'<br>\'+\n       \'Estimated Order Value: \'+Number(x.minimum_order_notional_estimate).toFixed(4)+\' USDT<br>\'+\n       \'Tick Size: \'+(x.tick_size||\'--\')+\' | Qty Step: \'+x.qty_step+\'<br>\'+\n       \'Selected Margin: \'+configured.toFixed(2)+\' USDT × \'+leverage+\'x<br>\'+\n       \'Estimated minimum margin: \'+required.toFixed(4)+\' USDT (fees/slippage extra)<br>\'+\n       \'<b style="color:\'+(enough?\'#15803d\':\'#b91c1c\')+\'">\'+\n       (enough?\'Margin estimate meets minimum before fees; not an order approval.\':\'INSUFFICIENT MARGIN: configured amount is below the estimated minimum.\')+\n       \'</b><br><small>Read-only estimate only. Available HFT wallet margin is not verified; no order was sent.</small>\';\n   } else {\n     panel.innerText=\'HFT rules calculation incomplete: \'+(x.reason||\'Check raw API result below.\');\n   }\n   alert(\'HFT market/API read-only check successful. No order, transfer or leverage change was sent.\');\n }\n}catch(e){box.innerText=\'HFT market check error: \'+e.message;}\n}\nasync function loadCoinLiveAverages(){\n const panel=document.getElementById(\'coinAverageTable\');\n panel.innerText=\'Fetching live CoinSwitch HFT ticker for every supported coin...\';\n try{\n  const r=await fetch(\'/api/hft-live-coin-averages\',{cache:\'no-store\'});\n  const d=await r.json();\n  if(!r.ok||!d.ok) throw new Error(d.error||(\'HTTP \'+r.status));\n  const rows=d.coins||[];\n  let html=\'<b>CoinSwitch HFT — Live prices & exchange average</b>\'+\n    \'<small>Average = mean of available Last, Mark, Index, Best Bid and Best Ask. Trade sizing still uses Mark Price.</small>\'+\n    \'<table style="width:100%;border-collapse:collapse;margin-top:8px"><tr><th>Coin</th><th>Live Average</th><th>Mark</th><th>Last</th><th>Exchange Leverage</th><th>24h %</th><th>Status</th></tr>\';\n  for(const x of rows){\n    html+=\'<tr><td>\'+x.coin+\'</td><td>\'+x.average_price+\'</td><td>\'+x.mark_price+\'</td><td>\'+x.last_price+\'</td><td>\'+(x.min_leverage&&x.max_leverage?(x.min_leverage+\'x–\'+x.max_leverage+\'x\'):\'--\')+\'</td><td>\'+x.change_24h_pct+\'</td><td>\'+x.status+\'</td></tr>\';\n  }\n  html+=\'</table><small>Updated: \'+d.updated_at_utc+\' · Prices are USDT per coin. Average is a dashboard reference, not an executable price.</small>\';\n  html+=\'<small>Exchange leverage range is read from instrument rules (minLeverage–maxLeverage). It is not a leverage setting/change. Actual allowed leverage may depend on position tier and risk limits.</small>\';\n  panel.innerHTML=html;\n  document.getElementById(\'hftResult\').innerText=JSON.stringify(d,null,2);\n }catch(e){panel.innerText=\'Live coin average error: \'+e.message;}\n}\nasync function runAllHFTChecks(){\n let panel=document.getElementById(\'hftSummary\');\n let box=document.getElementById(\'hftResult\');\n panel.innerText=\'Running all three HFT checks (read-only)...\';\n box.innerText=\'Checking HFT account status, market rules, positions, open orders and executions...\';\n try{\n  let q=new URLSearchParams({coin:cur,margin:document.getElementById(\'amt\').value,\n                            leverage:document.getElementById(\'lev\').value});\n  let r=await fetch(\'/api/hft-combined-readiness?\'+q.toString(),{cache:\'no-store\'});\n  let d=await r.json();\n  if(!r.ok||!d.ok) throw new Error(d.error||(\'HTTP \'+r.status));\n  let x=d.readiness||{};\n  let wallet=x.hft_wallet||{};\n  let calc=x.order_sizing||{};\n  let rules=x.instrument_rules||{};\n  let balanceText=wallet.status===\'manual_observation\'\n    ? (\'Manually observed HFT Balance: \'+wallet.observed_balance+\' \'+wallet.currency+\' (not API-verified)\')\n    : (wallet.status===\'available\'\n       ? (\'Available HFT Balance: \'+wallet.available+\' \'+wallet.currency)\n       : \'HFT Available Balance: NOT EXPOSED by the published HFT REST endpoint list\');\n  let marginText=calc.minimum_margin_estimate_usdt\n    ? (\'Estimated Minimum Margin: \'+calc.minimum_margin_estimate_usdt+\' USDT\')\n    : \'Minimum margin: unavailable (rules incomplete)\';\n  let budgetText=calc.calculated_qty!==undefined\n    ? (\'Your Budget: \'+calc.margin_budget_usdt+\' USDT × \'+calc.leverage+\'x | Qty: \'+calc.calculated_qty+\n       \' | Order Value: \'+calc.order_value_usdt+\' USDT\')\n    : \'Quantity preview unavailable\';\n  panel.innerHTML=\'<b>ALL 3 HFT CHECKS — \'+(x.symbol||cur+\'USDT\')+\'</b><br>\'+\n   \'<b>1. Account Balance:</b> \'+balanceText+\'<br>\'+\n   \'<small>Screenshot balance is manually recorded, not a live free-balance reading. Regular Futures v2 balance is separate.</small><br>\'+\n   \'<b>2. Exchange Minimum:</b> \'+marginText+\' | Min Qty: \'+(rules.minimum_valid_qty_estimate||\'--\')+\n   \' | Min Value: \'+(rules.minimum_order_notional_estimate||\'--\')+\' USDT<br>\'+\n   \'<b>Budget shortfall:</b> \'+(calc.budget_shortfall_usdt||\'0\')+\' USDT | <b>Observed balance shortfall:</b> \'+(calc.observed_balance_shortfall_usdt||\'--\')+\' USDT<br>\'+\n   \'<b>3. Your Trade Sizing:</b> \'+budgetText+\'<br>\'+\n   \'<b style="color:\'+(calc.eligible_by_rules?\'#15803d\':\'#b91c1c\')+\'">\'+\n   (calc.rule_check||\'Rules not verified\')+\'</b><br>\'+\n   \'<small>Read-only only. Manual screenshot balance is a conservative cap, not live API verification. No order, transfer or leverage change was sent.</small>\';\n  box.innerText=JSON.stringify(d,null,2);\n }catch(e){panel.innerText=\'Combined HFT check error: \'+e.message;}\n}\nasync function previewHFTOrder(){\n let panel=document.getElementById(\'hftSummary\');\n panel.innerText=\'Calculating HFT order size from current exchange rules (read-only)...\';\n try{\n  let q=new URLSearchParams({coin:cur,margin:document.getElementById(\'amt\').value,\n                            leverage:document.getElementById(\'lev\').value});\n  let r=await fetch(\'/api/hft-order-preview?\'+q.toString(),{cache:\'no-store\'});\n  let d=await r.json();\n  if(!r.ok||!d.ok) throw new Error(d.error||(\'HTTP \'+r.status));\n  let x=d.preview||{};\n  panel.innerHTML=\'<b>HFT ORDER PREVIEW — \'+(x.symbol||\'\')+\'</b><br>\'+\n   \'Budget: \'+x.margin_budget_usdt+\' USDT × \'+x.leverage+\'x = \'+x.target_notional_usdt+\' USDT target notional<br>\'+\n   \'Calculated Qty: <b>\'+x.calculated_qty+\'</b> | Estimated Order Value: \'+x.order_value_usdt+\' USDT<br>\'+\n   \'Minimum Qty: \'+x.minimum_qty+\' | Minimum Value: \'+x.minimum_notional_usdt+\' USDT<br>\'+\n   \'Minimum Margin: \'+x.minimum_margin_estimate_usdt+\' USDT | Budget gap: \'+(x.budget_shortfall_usdt||\'0\')+\' USDT | Observed balance gap: \'+(x.observed_balance_shortfall_usdt||\'--\')+\' USDT<br>\'+\n   \'<b style="color:\'+(x.eligible_by_rules?\'#15803d\':\'#b91c1c\')+\'">\'+x.rule_check+\'</b><br>\'+\n   \'<small>Read-only preview. HFT balance is manually observed, not API-verified; position mode, fees and slippage are not verified. No order was sent.</small>\';\n  document.getElementById(\'hftResult\').innerText=JSON.stringify(d,null,2);\n }catch(e){panel.innerText=\'HFT preview error: \'+e.message;}\n}\nasync function checkMainWallet(){let box=document.getElementById(\'mainWallet\');box.innerText=\'Checking main CoinSwitch INR portfolio (read-only)...\';try{let r=await fetch(\'/api/main-wallet\',{cache:\'no-store\'});let d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||(\'HTTP \'+r.status));box.innerText=\'Main CoinSwitch Wallet (read-only): Available ₹\'+Number(d.available_inr).toLocaleString(\'en-IN\',{minimumFractionDigits:2,maximumFractionDigits:2})+\' | Blocked ₹\'+Number(d.blocked_inr).toLocaleString(\'en-IN\',{minimumFractionDigits:2,maximumFractionDigits:2})+\' | Total ₹\'+Number(d.total_inr).toLocaleString(\'en-IN\',{minimumFractionDigits:2,maximumFractionDigits:2})+\'\\n\'+d.note+\'\\nSource: \'+d.source;}catch(e){box.innerText=\'Main INR wallet check failed: \'+e.message+\'\\nThis does not change the Futures wallet balance.\';}}\nfunction fmtINR(n){return \'USDT \'+Number(n).toLocaleString(\'en-US\',{minimumFractionDigits:2, maximumFractionDigits:4});}\nfunction fmtPrice(n){return Number(n).toLocaleString(\'en-IN\',{minimumFractionDigits:2, maximumFractionDigits:2});}\nfunction updateTpSlPreview(){\nlet tp=parseFloat(document.getElementById(\'tp\').value)||0;let sl=parseFloat(document.getElementById(\'sl\').value)||0;let lev=parseFloat(document.getElementById(\'lev\').value)||10;let amt=parseFloat(document.getElementById(\'amt\').value)||1000;let fee=amt*lev*0.0013;let tpGross=amt*lev*tp/100;let slGross=amt*lev*sl/100;let tpNet=tpGross-fee;let slNet=-(slGross+fee);let basePrice=(curPos&&curEntry>0)?curEntry:lastPrice;let side=(curPos&&curPos.side)?curPos.side:\'LONG\';let tpPrice,slPrice;if(side==\'LONG\'){tpPrice=basePrice*(1+tp/100);slPrice=basePrice*(1-sl/100);}else{tpPrice=basePrice*(1-tp/100);slPrice=basePrice*(1+sl/100);}let baseLabel=(curPos&&curEntry>0)?(\'Entry USDT \'+fmtPrice(basePrice)+\' (\'+side+\') se\'):(\'Live USDT \'+fmtPrice(basePrice)+\' pe \'+side+\' lagao to\');let html=\'<div class="tpbox"><b>🎯 TP Preview - \'+baseLabel+\'</b><br>✅ TP \'+tp+\'% → Price <b>USDT \'+fmtPrice(tpPrice)+\'</b> | Gross +USDT \'+tpGross.toFixed(2)+\' - Estimated fee USDT \'+fee.toFixed(2)+\' = Net <b style="color:green">+USDT \'+tpNet.toFixed(2)+\'</b></div>\';html+=\'<div class="slbox"><b>🛑 SL Preview - \'+baseLabel+\'</b><br>❌ SL \'+sl+\'% → Price <b>USDT \'+fmtPrice(slPrice)+\'</b> | Gross -USDT \'+slGross.toFixed(2)+\' - Estimated fee USDT \'+fee.toFixed(2)+\' = Net <b style="color:red">USDT \'+slNet.toFixed(2)+\'</b></div>\';document.getElementById(\'tpPreview\').innerHTML=html;}\nfunction drawChart(){let cv=document.getElementById(\'c\');let x=cv.getContext(\'2d\');x.clearRect(0,0,860,340);if(!lastCandles.length)return;let allLow=[...lastCandles.map(a=>a.low),lastPrice];let allHigh=[...lastCandles.map(a=>a.high),lastPrice];let lo=Math.min(...allLow);let hi=Math.max(...allHigh);let rg=hi-lo||1;lo-=rg*0.08;hi+=rg*0.08;rg=hi-lo;let chartW=700;let w=chartW/lastCandles.length;let yOf=p=>10+((hi-p)/rg)*300;let xOf=i=>i*w+w/2;x.font=\'12px Arial\';x.fillStyle=\'#666\';x.strokeStyle=\'#eee\';for(let i=0;i<=5;i++){let p=lo+(rg*i/5);let y=yOf(p);x.beginPath();x.moveTo(0,y);x.lineTo(chartW,y);x.stroke();x.fillText(fmtINR(p),708,y+4);}let yL=yOf(lastPrice);x.strokeStyle=\'#2563eb\';x.setLineDash([4,3]);x.lineWidth=1;x.beginPath();x.moveTo(0,yL);x.lineTo(chartW,yL);x.stroke();x.setLineDash([]);x.lineWidth=1;hLines.forEach(p=>{let y=yOf(p);x.strokeStyle=\'#7c3aed\';x.setLineDash([6,4]);x.lineWidth=1;x.beginPath();x.moveTo(0,y);x.lineTo(chartW,y);x.stroke();x.setLineDash([]);});vLines.forEach(idx=>{let px=xOf(idx);x.strokeStyle=\'#f59e0b\';x.setLineDash([6,4]);x.lineWidth=1;x.beginPath();x.moveTo(px,0);x.lineTo(px,310);x.stroke();x.setLineDash([]);});lastCandles.forEach((c,i)=>{let px=xOf(i);let yH=yOf(c.high);let yL2=yOf(c.low);let yO=yOf(c.open);let yC=yOf(c.close);let isLast=(i==lastCandles.length-1);let g=c.close>=c.open;x.strokeStyle=g?\'green\':\'red\';x.fillStyle=g?\'green\':\'red\';x.beginPath();x.moveTo(px,yH);x.lineTo(px,yL2);x.stroke();let bw=isLast?10:7;x.fillRect(px-bw/2,Math.min(yO,yC),bw,Math.max(3,Math.abs(yC-yO)));if(isLast){x.strokeStyle=\'#000\';x.lineWidth=2.5;x.setLineDash([]);x.strokeRect(px-bw/2-1,Math.min(yO,yC)-1,bw+2,Math.max(4,Math.abs(yC-yO))+2);x.lineWidth=1;x.fillStyle=\'#000\';x.font=\'bold 11px Arial\';x.fillText(\'● LIVE\',px-18,Math.min(yO,yC)-6);x.font=\'12px Arial\';}});x.fillStyle=\'#2563eb\';x.fillRect(708,yL-10,142,20);x.fillStyle=\'#fff\';x.fillText(fmtINR(lastPrice),712,yL+4);x.fillStyle=\'#666\';hLines.forEach(p=>{let y=yOf(p);x.fillStyle=\'#7c3aed\';x.fillRect(708,y-10,142,20);x.fillStyle=\'#fff\';x.fillText(fmtINR(p),712,y+4);x.fillStyle=\'#666\';});}\ndocument.getElementById(\'c\').addEventListener(\'click\',function(e){if(tool==\'none\')return;let rect=this.getBoundingClientRect();let cx=(e.clientX-rect.left)*(860/rect.width);let cy=(e.clientY-rect.top)*(340/rect.height);if(!lastCandles.length)return;let lo=Math.min(...lastCandles.map(a=>a.low));let hi=Math.max(...lastCandles.map(a=>a.high));let rg=hi-lo||1;lo-=rg*0.08;hi+=rg*0.08;rg=hi-lo;if(tool==\'h\'){let price=hi-((cy-10)/300)*rg;hLines.push(price);alert(\'H-Line Price: USDT \'+price.toFixed(2));}if(tool==\'v\'){let idx=Math.floor(cx/(700/lastCandles.length));vLines.push(idx);}drawChart();});\nasync function fetchData(){let r=await fetch(\'/api/data?coin=\'+cur+\'&interval=\'+tf);let d=await r.json();if(!r.ok||d.error){document.getElementById(\'bal\').innerText=\'⚠️ API Error: \'+(d.error||(\'HTTP \'+r.status));document.getElementById(\'locked\').innerText=\'Wallet data unavailable — check Render Logs\';return;}lastCandles=d.candles;lastPrice=d.price_usdt;curPos=d.pos_obj;curEntry=d.entry_usdt;document.getElementById(\'p\').innerText=cur+\' : USDT \'+d.price_usdt.toLocaleString(\'en-IN\',{maximumFractionDigits:4})+\' | \'+(d.usdt_inr?\'₹ \'+(d.price_usdt*d.usdt_inr).toLocaleString(\'en-IN\',{maximumFractionDigits:2}):\'₹ --\')+\' | \'+d.position+\' | 24h \'+d.change+\'%\';document.getElementById(\'bal\').innerText=\'💰 Futures v2 Wallet — Available: USDT \'+d.balance.toLocaleString(\'en-IN\',{maximumFractionDigits:2})+\' | ₹ \'+(d.balance_inr==null?\'--\':d.balance_inr.toLocaleString(\'en-IN\',{maximumFractionDigits:2}));document.getElementById(\'locked\').innerText=\'🔒 Futures v2 Wallet — Locked: USDT \'+d.locked.toLocaleString(\'en-IN\',{maximumFractionDigits:2})+\' | Total: USDT \'+d.total.toLocaleString(\'en-IN\',{maximumFractionDigits:2})+\' | Total INR: ₹ \'+(d.total_inr==null?\'--\':d.total_inr.toLocaleString(\'en-IN\',{maximumFractionDigits:2}));document.getElementById(\'fx\').innerText=\'1 USDT = ₹ \'+(d.usdt_inr||\'Unavailable\')+\' | \'+d.fx_source;document.getElementById(\'pos\').innerText=d.pos_msg;document.getElementById(\'trailInfo\').innerText=d.trail_msg;let p=document.getElementById(\'pnl\');p.innerText=\'Live Unrealised PnL: USDT \'+d.live_pnl+\' (\'+d.live_pct+\'%) | INR ₹ \'+(d.live_pnl_inr==null?\'--\':d.live_pnl_inr)+\' | Gross USDT \'+d.live_gross; p.style.color=d.live_pnl>=0?\'green\':\'red\';document.getElementById(\'h\').innerHTML=d.history.map(x=>\'<div>\'+x+\'</div>\').join(\'\');drawChart();updateTpSlPreview();}\nasync function placeOneHFTOrder(){\n const side=document.getElementById(\'hftSide\').value;\n const lev=Number(document.getElementById(\'hftLev\').value||0);\n const idx=Number(document.getElementById(\'hftMode\').value);\n if(cur!==\'SOL\'){alert(\'Live test is restricted to SOLUSDT. Select SOL first.\');return;}\n if(!Number.isFinite(lev)||lev<=0){alert(\'Valid leverage enter karein.\');return;}\n const modeText=idx===0?\'One-way\':(idx===1?\'Hedge LONG\':\'Hedge SHORT\');\n const warning=\'REAL HFT ORDER\\nSOLUSDT \'+side+\'\\nLeverage \'+lev+\'x\\nPosition mode \'+modeText+\'\\nMaximum notional 5 USDT (exchange minimum may block it).\\n\\nOrder live submit hoga. Fees, slippage and liquidation risk apply. Continue?\';\n if(!confirm(warning))return;\n if(prompt(\'Final confirmation: type PLACE SOL LIVE\')!==\'PLACE SOL LIVE\')return;\n const box=document.getElementById(\'hftOrderStatus\');box.innerText=\'HFT preflight running. Do not press again.\';\n try{\n  const r=await fetch(\'/api/hft/place-one-sol-test\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({side:side,leverage:lev,position_idx:idx,confirm:\'PLACE SOL LIVE\'})});\n  const d=await r.json();box.innerText=JSON.stringify(d,null,2);\n  if(!r.ok||!d.ok)alert(\'HFT order not confirmed: \'+(d.error||\'Check CoinSwitch HFT Orders.\'));\n  else alert(\'Exchange accepted order request; it may still be unfilled/cancelled. Check HFT Orders/Positions. ID: \'+(d.order_id||\'not returned\'));\n }catch(e){box.innerText=\'Outcome unknown. Do NOT click again. Check CoinSwitch HFT Orders first.\';alert(\'Network outcome unknown. Do not retry until HFT Orders are checked.\');}\n}\nsetInterval(fetchData,10000);fetchData();\n</script>\n'

@app.route("/")
def home(): return render_template_string(HTML.replace("__HFT_MANUAL_BALANCE__", HFT_MANUAL_BALANCE_USDT))

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
        observed_cap = _decimal_or_none(HFT_MANUAL_BALANCE_USDT) or D("0")
        if D(str(s["amount"])) > observed_cap:
            raise ValueError(f"Preview margin cannot exceed manually observed HFT balance ({observed_cap} USDT).")
    except Exception as exc:
        return jsonify({"ok":False,"msg":str(exc)}), 400
    return jsonify({"ok":True,"settings":s,"live_trading":LIVE_TRADING,"auto_allowed":AUTO_TRADING_ALLOWED})

@app.route("/api/main-wallet")
def main_wallet_api():
    """Read-only CoinSwitch main/spot portfolio; never treat it as futures margin."""
    try:
        response = require_client().spot_portfolio()
        raw = unwrap(response)
        if isinstance(raw, dict):
            raw = raw.get("portfolio", raw.get("balances", raw.get("data", raw)))
        if not isinstance(raw, list):
            raise CoinSwitchError("Portfolio response format unexpected: " + str(response)[:700])
        inr_row = next((x for x in raw if isinstance(x, dict) and
                        str(x.get("currency", "")).upper() == "INR"), None)
        if inr_row is None:
            raise CoinSwitchError("Portfolio API connected, but INR currency row was not returned: " + str(response)[:700])
        available = D(inr_row.get("main_balance", 0))
        blocked = D(inr_row.get("blocked_balance_order", 0))
        return jsonify({"ok": True, "wallet_type": "main_coin_switch_portfolio_not_futures_margin",
                        "currency": "INR", "available_inr": float(available),
                        "blocked_inr": float(blocked), "total_inr": float(available + blocked),
                        "source": "GET /trade/api/v2/user/portfolio",
                        "note": "Read-only main portfolio balance. Not proof of Futures margin or order eligibility."})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "read_only": True}), 502


@app.route("/api/hft-live-coin-averages", methods=["GET"])
def hft_live_coin_averages():
    """Read live HFT ticker values coin-by-coin; no state-changing API calls."""
    try:
        hft = CoinSwitchHFTRo(API_KEY, SECRET_KEY)
        rows = []
        for coin in COINS:
            symbol = coin + "USDT"
            try:
                payload = hft.tickers(symbol)
                if not isinstance(payload, dict) or payload.get("retCode") not in (0, "0", None):
                    raise CoinSwitchError(str(payload.get("retMsg", "Ticker API error")) if isinstance(payload, dict) else "Invalid ticker response")
                ticker_rows = _hft_result_list(payload)
                ticker = next((x for x in ticker_rows if isinstance(x, dict) and x.get("symbol") == symbol), None)
                if ticker is None and ticker_rows:
                    ticker = next((x for x in ticker_rows if isinstance(x, dict)), None)
                if not ticker:
                    raise CoinSwitchError("No ticker returned for " + symbol)

                fields = {
                    "last": _decimal_or_none(ticker.get("lastPrice")),
                    "mark": _decimal_or_none(ticker.get("markPrice")),
                    "index": _decimal_or_none(ticker.get("indexPrice")),
                    "bid": _decimal_or_none(ticker.get("bid1Price")),
                    "ask": _decimal_or_none(ticker.get("ask1Price")),
                }
                usable = [v for v in fields.values() if v is not None and v > 0]
                if not usable:
                    raise CoinSwitchError("No valid live price fields for " + symbol)
                average = sum(usable, D("0")) / D(len(usable))
                pct = _decimal_or_none(ticker.get("price24hPcnt"))
                # Fetch each contract's own exchange leverage filter (read-only).
                leverage_min = leverage_max = leverage_step = None
                leverage_error = None
                try:
                    instrument_payload = hft.instruments(symbol)
                    instrument_rows = _hft_result_list(instrument_payload)
                    instrument = next((x for x in instrument_rows if isinstance(x, dict) and x.get("symbol") == symbol), None)
                    if instrument is None and instrument_rows:
                        instrument = next((x for x in instrument_rows if isinstance(x, dict)), None)
                    lf = instrument.get("leverageFilter", {}) if isinstance(instrument, dict) else {}
                    if isinstance(lf, dict):
                        leverage_min = lf.get("minLeverage")
                        leverage_max = lf.get("maxLeverage")
                        leverage_step = lf.get("leverageStep")
                    if leverage_min is None or leverage_max is None:
                        leverage_error = "Exchange leverageFilter not returned"
                except Exception as lev_exc:
                    leverage_error = str(lev_exc)
                rows.append({
                    "coin": coin, "symbol": symbol, "status": "LIVE",
                    "min_leverage": str(leverage_min) if leverage_min is not None else None,
                    "max_leverage": str(leverage_max) if leverage_max is not None else None,
                    "leverage_step": str(leverage_step) if leverage_step is not None else None,
                    "leverage_status": "LIVE" if leverage_max is not None else "UNAVAILABLE",
                    "leverage_error": leverage_error,
                    "average_price": str(average),
                    "average_method": "arithmetic mean of available lastPrice, markPrice, indexPrice, bid1Price, ask1Price",
                    "average_components": {k: (str(v) if v is not None else None) for k, v in fields.items()},
                    "mark_price": str(fields["mark"]) if fields["mark"] is not None else "--",
                    "last_price": str(fields["last"]) if fields["last"] is not None else "--",
                    "index_price": str(fields["index"]) if fields["index"] is not None else "--",
                    "best_bid": str(fields["bid"]) if fields["bid"] is not None else "--",
                    "best_ask": str(fields["ask"]) if fields["ask"] is not None else "--",
                    "change_24h_pct": str(pct * D("100")) if pct is not None else "--",
                    "exchange_timestamp": payload.get("time")
                })
            except Exception as coin_exc:
                rows.append({
                    "coin": coin, "symbol": symbol, "status": "UNAVAILABLE",
                    "average_price": "--", "mark_price": "--", "last_price": "--",
                    "min_leverage": None, "max_leverage": None, "leverage_status": "UNAVAILABLE",
                    "change_24h_pct": "--", "error": str(coin_exc)
                })
        live_count = sum(1 for x in rows if x["status"] == "LIVE")
        return jsonify({
            "ok": True, "read_only": True, "source": "CoinSwitch HFT GET /v5/market/tickers",
            "quote_currency": "USDT", "updated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "average_definition": "Arithmetic mean of available last, mark, index, best bid and best ask. This is a dashboard reference only.",
            "leverage_source": "CoinSwitch HFT GET /v5/market/instruments-info leverageFilter minLeverage/maxLeverage per symbol",
            "sizing_reference": "Use markPrice for margin/quantity calculations; do not use the average as an executable order price.",
            "live_coin_count": live_count, "total_coins": len(COINS), "coins": rows,
            "note": "GET-only market data and instrument rules. No order, transfer, leverage change or cancellation was sent."
        })
    except Exception as exc:
        return jsonify({"ok": False, "read_only": True, "error": str(exc)}), 502


@app.route("/api/hft-readonly-check", methods=["GET"])
def hft_readonly_check():
    """Read-only HFT API check. No wallet transfer or order endpoint is called."""
    try:
        hft = CoinSwitchHFTRo(API_KEY, SECRET_KEY)
        symbol = os.getenv("HFT_TEST_SYMBOL", "SOLUSDT").strip().upper() or "SOLUSDT"
        server_time = hft.server_time()
        instruments = hft.instruments(symbol)
        positions = hft.positions(symbol)
        return jsonify({
            "ok": True,
            "read_only": True,
            "base_url": CoinSwitchHFTRo.BASE_URL,
            "category": "linear",
            "symbol_requested": symbol,
            "server_time": server_time,
            "instruments": instruments,
            "positions": positions,
            "wallet_balance": {
                "status": "not_available_in_published_HFT_REST_endpoint_list",
                "note": "No HFT wallet-balance REST endpoint is listed in the official HFT Client endpoint inventory. The B2B master-balance endpoint is a separate B2B API and is not assumed to be this user's HFT wallet."
            },
            "note": "Read-only test only. No transfer, order, cancellation, or leverage change was sent."
        })
    except Exception as exc:
        return jsonify({
            "ok": False, "read_only": True, "error": str(exc),
            "note": "No transfer or order was sent. Check key permissions, Ed25519 secret format, and HFT API access."
        }), 502


def _hft_result_list(payload):
    """Extract a Bybit-style result.list safely without assuming a response shape."""
    if not isinstance(payload, dict):
        return []
    result = payload.get("result", {})
    if isinstance(result, dict) and isinstance(result.get("list"), list):
        return result["list"]
    return []


def _decimal_or_none(value):
    try:
        if value in (None, ""):
            return None
        return D(str(value))
    except Exception:
        return None


def _hft_pretrade_summary(instrument_payload, ticker_payload, leverage):
    """Calculate constraints from exchange-returned rules; never submits an order."""
    instruments = _hft_result_list(instrument_payload)
    tickers = _hft_result_list(ticker_payload)
    if not instruments or not tickers:
        return {
            "status": "incomplete",
            "reason": "Instrument rules or ticker result.list is empty; no quantity estimate made."
        }

    instrument = next((x for x in instruments if isinstance(x, dict)), None)
    ticker = next((x for x in tickers if isinstance(x, dict)), None)
    if not instrument or not ticker:
        return {"status": "incomplete", "reason": "Unexpected instrument/ticker row format."}

    lot = instrument.get("lotSizeFilter") or {}
    price_filter = instrument.get("priceFilter") or {}
    lev_filter = instrument.get("leverageFilter") or {}

    mark = _decimal_or_none(ticker.get("markPrice"))
    if mark is None or mark <= 0:
        mark = _decimal_or_none(ticker.get("lastPrice"))
    min_qty = _decimal_or_none(lot.get("minOrderQty"))
    qty_step = _decimal_or_none(lot.get("qtyStep"))
    min_notional = _decimal_or_none(lot.get("minNotionalValue")) or D("0")
    min_lev = _decimal_or_none(lev_filter.get("minLeverage"))
    max_lev = _decimal_or_none(lev_filter.get("maxLeverage"))
    tick_size = _decimal_or_none(price_filter.get("tickSize"))
    min_price = _decimal_or_none(price_filter.get("minPrice"))

    if mark is None or min_qty is None or qty_step is None or qty_step <= 0:
        return {
            "status": "incomplete",
            "symbol": instrument.get("symbol"),
            "reason": "Missing mark price, minOrderQty, or qtyStep; quantity estimate withheld."
        }

    # Quantity must satisfy BOTH min quantity and min notional, rounded UP to qtyStep.
    required_by_notional = (min_notional / mark) if min_notional > 0 else D("0")
    raw_qty = max(min_qty, required_by_notional)
    steps = (raw_qty / qty_step).to_integral_value(rounding=ROUND_UP)
    min_valid_qty = steps * qty_step
    min_valid_notional = min_valid_qty * mark

    lev = _decimal_or_none(leverage)
    min_margin = (min_valid_notional / lev) if lev and lev > 0 else None
    leverage_valid = bool(
        lev is not None and
        (min_lev is None or lev >= min_lev) and
        (max_lev is None or lev <= max_lev)
    )
    return {
        "status": "calculated",
        "symbol": instrument.get("symbol"),
        "settlement_coin": instrument.get("settleCoin"),
        "quote_coin": instrument.get("quoteCoin"),
        "contract_status": instrument.get("status"),
        "mark_price": str(mark),
        "min_order_qty": str(min_qty),
        "qty_step": str(qty_step),
        "min_notional_value": str(min_notional),
        "tick_size": str(tick_size) if tick_size is not None else None,
        "min_price": str(min_price) if min_price is not None else None,
        "minimum_valid_qty_estimate": str(min_valid_qty),
        "minimum_order_notional_estimate": str(min_valid_notional),
        "selected_leverage": str(lev) if lev is not None else None,
        "allowed_leverage_min": str(min_lev) if min_lev is not None else None,
        "allowed_leverage_max": str(max_lev) if max_lev is not None else None,
        "selected_leverage_valid": leverage_valid,
        "minimum_margin_estimate_usdt": str(min_margin) if min_margin is not None else None,
        "fee_slippage_funding_included": False,
        "warning": "Estimate only. Actual market fill, fees, slippage, risk limits and available HFT margin can change order eligibility. No order was sent."
    }


@app.route("/api/hft-combined-readiness", methods=["GET"])
def hft_combined_readiness():
    """Run balance-status, minimum-rule and budget-sizing checks without state changes."""
    try:
        coin = request.args.get("coin", "SOL").strip().upper()
        if coin not in COINS:
            return jsonify({"ok": False, "error": "Unsupported coin"}), 400
        symbol = coin + "USDT"
        margin = _decimal_or_none(request.args.get("margin"))
        leverage = _decimal_or_none(request.args.get("leverage"))
        if margin is None or leverage is None or margin <= 0 or leverage <= 0:
            return jsonify({"ok": False, "error": "Margin and leverage must be positive numbers."}), 400
        if margin > D(MAX_MARGIN_USDT):
            return jsonify({"ok": False, "error": f"Margin exceeds configured safety cap of {MAX_MARGIN_USDT} USDT."}), 400

        hft = CoinSwitchHFTRo(API_KEY, SECRET_KEY)
        instrument_data = hft.instruments(symbol)
        ticker_data = hft.tickers(symbol)
        positions_data = hft.positions(symbol)
        orders_data = hft.realtime_orders(symbol)
        executions_data = hft.executions(symbol)
        rules = _hft_pretrade_summary(instrument_data, ticker_data, leverage)

        sizing = {"status": "incomplete", "rule_check": "Instrument rules unavailable."}
        if rules.get("status") == "calculated":
            mark = _decimal_or_none(rules.get("mark_price"))
            qty_step = _decimal_or_none(rules.get("qty_step"))
            min_qty = _decimal_or_none(rules.get("min_order_qty"))
            min_notional = _decimal_or_none(rules.get("min_notional_value")) or D("0")
            max_qty = None
            instrument_rows = _hft_result_list(instrument_data)
            if instrument_rows and isinstance(instrument_rows[0], dict):
                lot = instrument_rows[0].get("lotSizeFilter") or {}
                max_qty = (_decimal_or_none(lot.get("maxMktOrderQty")) or
                           _decimal_or_none(lot.get("maxOrderQty")))
            if mark and qty_step and min_qty:
                target_notional = margin * leverage
                raw_qty = target_notional / mark
                qty = (raw_qty / qty_step).to_integral_value(rounding=ROUND_DOWN) * qty_step
                order_value = qty * mark
                min_required_qty = max(min_qty, min_notional / mark if min_notional > 0 else D("0"))
                min_steps = (min_required_qty / qty_step).to_integral_value(rounding=ROUND_UP)
                min_valid_qty = min_steps * qty_step
                min_margin = (min_valid_qty * mark) / leverage
                manual_balance = _decimal_or_none(HFT_MANUAL_BALANCE_USDT) or D("0")
                if manual_balance < 0:
                    manual_balance = D("0")
                # Compare both the configured budget and the observed HFT balance
                # against the exchange-derived minimum margin, not just against each other.
                budget_covers_minimum = margin >= min_margin
                observed_balance_covers_minimum = manual_balance >= min_margin
                within_observed_balance = margin <= manual_balance
                max_qty_ok = (max_qty is None or max_qty <= 0 or qty <= max_qty)
                eligible = bool(
                    rules.get("selected_leverage_valid") and budget_covers_minimum and
                    observed_balance_covers_minimum and within_observed_balance and
                    qty >= min_valid_qty and max_qty_ok
                )
                blockers = []
                if not rules.get("selected_leverage_valid"):
                    blockers.append("Selected leverage is outside the instrument's allowed range")
                if not budget_covers_minimum:
                    blockers.append(f"Budget short by {(min_margin - margin):.8f} USDT versus estimated minimum margin")
                if not observed_balance_covers_minimum:
                    blockers.append(f"Observed HFT balance short by {(min_margin - manual_balance):.8f} USDT; balance is not API-verified")
                if not within_observed_balance:
                    blockers.append("Requested margin exceeds the manually observed HFT balance")
                if qty < min_valid_qty:
                    blockers.append("Calculated quantity is below exchange minimum")
                if not max_qty_ok:
                    blockers.append("Quantity exceeds exchange maximum")
                reason = "; ".join(blockers) if blockers else "Published quantity rules and manually observed balance estimate pass; NOT approval to trade."
                sizing = {
                    "status": "calculated", "margin_budget_usdt": str(margin),
                    "leverage": str(leverage), "target_notional_usdt": str(target_notional),
                    "calculated_qty": str(qty), "order_value_usdt": str(order_value),
                    "minimum_valid_qty": str(min_valid_qty),
                    "minimum_margin_estimate_usdt": str(min_margin),
                    "budget_shortfall_usdt": str(max(D("0"), min_margin - margin)),
                    "observed_balance_shortfall_usdt": str(max(D("0"), min_margin - manual_balance)),
                    "budget_covers_minimum": budget_covers_minimum,
                    "observed_balance_covers_minimum": observed_balance_covers_minimum,
                    "eligible_by_rules": eligible, "rule_check": reason,
                    "hft_manual_observed_balance_usdt": str(manual_balance),
                    "hft_balance_source": "user screenshot; manually recorded, not API-verified",
                    "requested_margin_within_observed_balance": within_observed_balance,
                    "hft_available_balance_verified": False
                }

        return jsonify({
            "ok": True, "read_only": True,
            "readiness": {
                "symbol": symbol,
                "hft_wallet": {
                    "status": "manual_observation",
                    "observed_balance": HFT_MANUAL_BALANCE_USDT,
                    "available": None, "currency": "USDT",
                    "source": "User-provided CoinSwitch PRO HFT screenshot; not fetched from API",
                    "note": "This is a manually recorded screen value, not an API-verified available/free balance. Refresh HFT_MANUAL_BALANCE_USDT in Render after any transfer or balance change."
                },
                "instrument_rules": rules,
                "order_sizing": sizing,
                "positions": positions_data,
                "open_orders": orders_data,
                "executions": executions_data
            },
            "note": "GET-only checks. No order, transfer, cancellation, leverage change or other state-changing request was sent."
        })
    except Exception as exc:
        return jsonify({"ok": False, "read_only": True, "error": str(exc),
                        "note": "No state-changing HFT request was sent."}), 502


@app.route("/api/hft-order-preview", methods=["GET"])
def hft_order_preview():
    """Calculate a proposed HFT quantity; this route is strictly GET-only."""
    try:
        coin = request.args.get("coin", "SOL").strip().upper()
        if coin not in COINS:
            return jsonify({"ok": False, "error": "Unsupported coin"}), 400
        symbol = coin + "USDT"
        margin = _decimal_or_none(request.args.get("margin"))
        leverage = _decimal_or_none(request.args.get("leverage"))
        if margin is None or leverage is None or margin <= 0 or leverage <= 0:
            return jsonify({"ok": False, "error": "Margin and leverage must be positive numbers."}), 400
        if margin > D(MAX_MARGIN_USDT):
            return jsonify({"ok": False, "error": f"Margin exceeds configured safety cap of {MAX_MARGIN_USDT} USDT."}), 400

        hft = CoinSwitchHFTRo(API_KEY, SECRET_KEY)
        instrument_data = hft.instruments(symbol)
        ticker_data = hft.tickers(symbol)
        instruments = _hft_result_list(instrument_data)
        tickers = _hft_result_list(ticker_data)
        if not instruments or not tickers:
            raise CoinSwitchError("HFT instrument/ticker list empty; quantity withheld.")
        instrument = next((x for x in instruments if isinstance(x, dict)), None)
        ticker = next((x for x in tickers if isinstance(x, dict)), None)
        if not instrument or not ticker:
            raise CoinSwitchError("Unexpected HFT instrument/ticker response.")
        lot = instrument.get("lotSizeFilter") or {}
        lev_filter = instrument.get("leverageFilter") or {}
        mark = _decimal_or_none(ticker.get("markPrice")) or _decimal_or_none(ticker.get("lastPrice"))
        min_qty = _decimal_or_none(lot.get("minOrderQty"))
        qty_step = _decimal_or_none(lot.get("qtyStep"))
        max_qty = _decimal_or_none(lot.get("maxMktOrderQty")) or _decimal_or_none(lot.get("maxOrderQty"))
        min_notional = _decimal_or_none(lot.get("minNotionalValue")) or D("0")
        min_lev = _decimal_or_none(lev_filter.get("minLeverage"))
        max_lev = _decimal_or_none(lev_filter.get("maxLeverage"))
        if mark is None or min_qty is None or qty_step is None or mark <= 0 or qty_step <= 0:
            raise CoinSwitchError("Required live mark/minQty/qtyStep missing.")
        if (min_lev is not None and leverage < min_lev) or (max_lev is not None and leverage > max_lev):
            raise CoinSwitchError(f"Leverage outside instrument range {min_lev}-{max_lev}.")
        target_notional = margin * leverage
        raw_qty = target_notional / mark
        steps = (raw_qty / qty_step).to_integral_value(rounding=ROUND_DOWN)
        qty = steps * qty_step
        order_value = qty * mark
        minimum_qty_needed = max(min_qty, (min_notional / mark if min_notional > 0 else D("0")))
        min_steps = (minimum_qty_needed / qty_step).to_integral_value(rounding=ROUND_UP)
        min_qty_rounded = min_steps * qty_step
        min_margin = (min_qty_rounded * mark) / leverage
        manual_balance = _decimal_or_none(HFT_MANUAL_BALANCE_USDT) or D("0")
        if manual_balance < 0:
            manual_balance = D("0")
        budget_covers_minimum = margin >= min_margin
        observed_balance_covers_minimum = manual_balance >= min_margin
        within_observed_balance = margin <= manual_balance
        max_qty_ok = (max_qty is None or max_qty <= 0 or qty <= max_qty)
        eligible = bool(qty >= min_qty and order_value >= min_notional and max_qty_ok and
                        budget_covers_minimum and observed_balance_covers_minimum and within_observed_balance)
        blockers = []
        if not budget_covers_minimum:
            blockers.append(f"Budget short by {(min_margin - margin):.8f} USDT versus estimated minimum margin")
        if not observed_balance_covers_minimum:
            blockers.append(f"Observed HFT balance short by {(min_margin - manual_balance):.8f} USDT; balance is not API-verified")
        if not within_observed_balance:
            blockers.append("Requested margin exceeds the manually observed HFT balance")
        if qty < min_qty:
            blockers.append("Calculated quantity is below exchange minimum")
        if order_value < min_notional:
            blockers.append("Calculated order value is below exchange minimum notional")
        if not max_qty_ok:
            blockers.append("Quantity exceeds exchange maximum market order quantity")
        reason = "; ".join(blockers) if blockers else "Quantity meets published rules and manual balance estimate only; NOT approval to trade."
        return jsonify({
            "ok": True, "read_only": True, "symbol": symbol,
            "preview": {
                "symbol": symbol, "mark_price": str(mark),
                "margin_budget_usdt": str(margin), "leverage": str(leverage),
                "target_notional_usdt": str(target_notional),
                "calculated_qty": str(qty), "order_value_usdt": str(order_value),
                "minimum_qty": str(min_qty), "minimum_notional_usdt": str(min_notional),
                "qty_step": str(qty_step), "eligible_by_rules": eligible,
                "rule_check": reason,
                "minimum_margin_estimate_usdt": str(min_margin),
                "budget_shortfall_usdt": str(max(D("0"), min_margin - margin)),
                "observed_balance_shortfall_usdt": str(max(D("0"), min_margin - manual_balance)),
                "budget_covers_minimum": budget_covers_minimum,
                "observed_balance_covers_minimum": observed_balance_covers_minimum,
                "hft_manual_observed_balance_usdt": str(manual_balance),
                "hft_balance_source": "user screenshot; manually recorded, not API-verified",
                "requested_margin_within_observed_balance": within_observed_balance,
                "hft_available_balance_verified": False,
                "position_mode_verified": False,
                "order_sent": False
            },
            "note": "GET-only calculation. No order, leverage change, cancellation or fund transfer was sent."
        })
    except Exception as exc:
        return jsonify({"ok": False, "read_only": True, "error": str(exc),
                        "note": "No state-changing HFT request was sent."}), 502


@app.route("/api/hft-market-check", methods=["GET"])
def hft_market_check():
    """Read-only HFT pre-trade diagnostics; deliberately has no POST/order path."""
    try:
        hft = CoinSwitchHFTRo(API_KEY, SECRET_KEY)
        symbol = os.getenv("HFT_TEST_SYMBOL", "SOLUSDT").strip().upper() or "SOLUSDT"
        instrument_data = hft.instruments(symbol)
        ticker_data = hft.tickers(symbol)
        leverage = STATE["settings"].get("leverage", 2)
        return jsonify({
            "ok": True,
            "read_only": True,
            "base_url": CoinSwitchHFTRo.BASE_URL,
            "settlement_currency_config": HFT_SETTLE_COIN,
            "category": "linear",
            "symbol_requested": symbol,
            "instruments": instrument_data,
            "instrument_rules_summary": _hft_pretrade_summary(instrument_data, ticker_data, leverage),
            "tickers": ticker_data,
            "order_book": hft.order_book(symbol, limit=5),
            "realtime_orders": hft.realtime_orders(symbol),
            "executions": hft.executions(symbol),
            "positions": hft.positions(symbol),
            "note": "GET-only diagnostic. No funds transfer, order placement/cancellation, or leverage change was sent."
        })
    except Exception as exc:
        return jsonify({"ok": False, "read_only": True, "error": str(exc),
                        "note": "No state-changing HFT request was sent."}), 502


@app.route("/api/wallet")
def wallet_api():
    try:
        wallet = wallet_balances()
        rate, fx_source = get_usdt_inr_rate()
        return jsonify({"ok":True, **wallet, "usdt_inr":rate or None,
                        "available_inr":round(wallet["available"]*rate,2) if rate else None,
                        "total_inr":round(wallet["total"]*rate,2) if rate else None,
                        "source":wallet.get("source", "CoinSwitch Futures wallet_balance"),
                        "wallet_type":"regular_futures_v2_usdt_margined",
                        
                        "fx_source":fx_source})
    except Exception as exc:
        return jsonify({"ok":False,"error":str(exc)}), 502

@app.route("/api/reset")
def reset():
    return jsonify({"ok":False,"msg":"Live exchange wallet reset nahi kiya ja sakta."}), 403

@app.route("/api/hft/place-one-sol-test", methods=["POST"])
def hft_place_one_sol_test():
    if os.getenv("HFT_LIVE_ORDERS", "NO").strip().upper() != "YES":
        return jsonify({"ok":False,"error":"Live HFT order locked. Render env HFT_LIVE_ORDERS=YES is required."}), 403
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != "PLACE SOL LIVE":
        return jsonify({"ok":False,"error":"Explicit confirmation missing."}), 400
    side = str(data.get("side","")).upper()
    if side not in ("LONG","SHORT"):
        return jsonify({"ok":False,"error":"Only SOL LONG/SHORT is allowed."}), 400
    try:
        lev = D(str(data.get("leverage","0")))
        idx = int(data.get("position_idx",-1))
        if lev <= 0 or idx not in (0,1,2): raise CoinSwitchError("Leverage/position mode invalid.")
        if idx not in (0, 1 if side == "LONG" else 2):
            raise CoinSwitchError("PositionIdx does not match side. Verify one-way/hedge mode in CoinSwitch.")
        hft = CoinSwitchHFTRo(API_KEY, SECRET_KEY)
        symbol = "SOLUSDT"
        ip = hft.instruments(symbol); tp = hft.tickers(symbol)
        ir = _hft_result_list(ip); tr = _hft_result_list(tp)
        ins = next((x for x in ir if isinstance(x,dict) and x.get("symbol")==symbol),None)
        tick = next((x for x in tr if isinstance(x,dict) and x.get("symbol")==symbol),None)
        if not ins or not tick or str(ins.get("status","")).lower() != "trading":
            raise CoinSwitchError("SOLUSDT instrument/ticker unavailable or not Trading; no order sent.")
        lf=ins.get("leverageFilter") or {}; lot=ins.get("lotSizeFilter") or {}; pf=ins.get("priceFilter") or {}
        mnlev=D(str(lf.get("minLeverage","1"))); mxlev=D(str(lf.get("maxLeverage","1"))); step=D(str(lf.get("leverageStep","1")))
        if lev < mnlev or lev > mxlev: raise CoinSwitchError(f"Exchange leverage range {mnlev}x–{mxlev}x; no order sent.")
        if step>0 and (lev-mnlev)/step != ((lev-mnlev)/step).to_integral_value(): raise CoinSwitchError(f"Leverage step must be {step}x.")
        mark=D(str(tick.get("markPrice") or tick.get("lastPrice") or "0")); bid=D(str(tick.get("bid1Price") or "0")); ask=D(str(tick.get("ask1Price") or "0"))
        ticksize=D(str(pf.get("tickSize") or "0")); qstep=D(str(lot.get("qtyStep") or "0")); minqty=D(str(lot.get("minOrderQty") or "0")); minval=D(str(lot.get("minNotionalValue") or "0"))
        maxqty=D(str(lot.get("maxOrderQty") or lot.get("maxMktOrderQty") or "0"))
        if min(mark,bid,ask,ticksize,qstep,minqty)<=0: raise CoinSwitchError("Live mark/bid/ask or size rules missing; no order sent.")
        qtyraw=max(minqty, minval/mark if minval>0 else D("0"))
        qty=(qtyraw/qstep).to_integral_value(rounding=ROUND_UP)*qstep
        notional=qty*mark; cap=D(os.getenv("HFT_TEST_MAX_NOTIONAL_USDT","5"))
        if notional>cap: raise CoinSwitchError(f"Minimum valid order {notional:.6f} USDT exceeds test cap {cap} USDT; no order sent.")
        if maxqty>0 and qty>maxqty: raise CoinSwitchError("Minimum order exceeds exchange max quantity; no order sent.")
        margin=notional/lev; observed=D(HFT_MANUAL_BALANCE_USDT or "0")
        if margin*D("1.20")>observed: raise CoinSwitchError(f"Estimated margin plus 20% buffer {margin*D('1.20'):.6f} USDT exceeds manually observed {observed} USDT. Balance not API verified.")
        positions=_hft_result_list(hft.positions(symbol)); orders=_hft_result_list(hft.realtime_orders(symbol))
        if any(isinstance(x,dict) and D(str(x.get("size","0") or "0"))>0 for x in positions): raise CoinSwitchError("SOL position already open; no new order sent.")
        active={"new","partiallyfilled","created"}
        if any(isinstance(x,dict) and str(x.get("orderStatus","")).lower() in active for x in orders): raise CoinSwitchError("SOL open order already exists; no new order sent.")
        raw=ask if side=="LONG" else bid; rounding=ROUND_UP if side=="LONG" else ROUND_DOWN
        price=(raw/ticksize).to_integral_value(rounding=rounding)*ticksize
        hft.set_leverage(symbol,lev)
        link="soltest-"+uuid.uuid4().hex[:24]
        body={"category":"linear","symbol":symbol,"side":"Buy" if side=="LONG" else "Sell","orderType":"Limit","qty":format(qty,"f"),"price":format(price,"f"),"positionIdx":idx,"timeInForce":"IOC","orderLinkId":link}
        global HFT_TEST_ORDER_ATTEMPTED
        with HFT_TEST_LOCK:
            if HFT_TEST_ORDER_ATTEMPTED:
                raise CoinSwitchError("One HFT SOL test order has already been attempted by this server. Check CoinSwitch Orders; no repeat allowed.")
            HFT_TEST_ORDER_ATTEMPTED = True
        try: placed=hft.place_order(body)
        except requests.RequestException:
            raise CoinSwitchError("Order request outcome UNKNOWN. Do NOT retry. Check HFT Orders using orderLinkId "+link)
        result=placed.get("result") or {}
        return jsonify({"ok":True,"exchange":"CoinSwitch HFT","symbol":symbol,"side":side,"order_type":"Limit IOC","order_id":result.get("orderId"),"order_link_id":result.get("orderLinkId",link),"qty":format(qty,"f"),"limit_price":format(price,"f"),"mark_price":format(mark,"f"),"notional_usdt":format(notional,"f"),"estimated_margin_usdt":format(margin,"f"),"leverage":format(lev,"f"),"position_idx":idx,"exchange_response":placed,"note":"retCode 0 means accepted, not necessarily filled. IOC can cancel unfilled quantity. No automatic close, TP/SL, retry or repeat was sent."})
    except Exception as exc:
        return jsonify({"ok":False,"error":str(exc),"note":"Do not retry blindly. Check HFT Orders if the response was lost."}), 400


@app.route("/api/trade", methods=["POST"])
def trade_api():
    if not LIVE_TRADING: return jsonify({"ok":False,"msg":"LIVE_TRADING=YES nahi hai; live orders locked hain."}), 403
    data = request.get_json(silent=True) or {}
    coin = str(data.get("coin", "SOL")).upper()
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
    coin = request.args.get("coin", "SOL").upper()
    interval = request.args.get("interval", "1m")
    if coin not in COINS: coin = "SOL"
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
                        "total":wallet["total"],"wallet_type":"regular_futures_v2_usdt_margined",
                        
                        "pos_msg":pos_msg,"trail_msg":trail_msg,
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
