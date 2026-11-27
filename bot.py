"""CoinSwitch PRO Futures V2 Dashboard & Auto Trading Bot (Render-Ready).

All HFT (dma.coinswitch.co) classes, endpoints, and UI blocks have been removed.
Uses CoinSwitch Futures V2 (/trade/api/v2/futures/*) exclusively.
Safety defaults: LIVE_TRADING and AUTO_TRADING_ALLOWED are OFF unless set to YES
in Render environment variables.
"""
import os
import time
import threading
import uuid
import urllib.parse
import hmac
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from flask import Flask, jsonify, render_template_string, request, Response
import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

BASE_URL = "https://coinswitch.co"
API_KEY = os.getenv("COINSWITCH_API_KEY", "").strip()
SECRET_KEY = os.getenv("COINSWITCH_SECRET_KEY", "").strip()

LIVE_TRADING = os.getenv("LIVE_TRADING", "NO").strip().upper() == "YES"
AUTO_TRADING_ALLOWED = os.getenv("AUTO_TRADING_ALLOWED", "NO").strip().upper() == "YES"

# Risk guards for auto mode (tunable from Render env)
MAX_DAILY_LOSS_USDT = float(os.getenv("MAX_DAILY_LOSS_USDT", "5"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "20"))
MAX_CONSEC_LOSSES = int(os.getenv("MAX_CONSEC_LOSSES", "3"))
AUTO_COOLDOWN_SEC = int(os.getenv("AUTO_COOLDOWN_SEC", "15"))
MAX_MARGIN_USDT = os.getenv("MAX_MARGIN_USDT", "50")
MIN_MARGIN_USDT = os.getenv("MIN_MARGIN_USDT", "1")
MIN_LEVERAGE = int(os.getenv("MIN_LEVERAGE", "1"))
MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "100"))
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "10"))
DEFAULT_MARGIN_USDT = os.getenv("DEFAULT_MARGIN_USDT", "").strip() or MIN_MARGIN_USDT
USDT_INR_RATE_FALLBACK = os.getenv("USDT_INR_RATE", "").strip()

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

app = Flask(__name__)
HTTP = requests.Session()
COINS = ["BTC", "ETH", "SOL", "DOGE", "XRP", "ADA", "BNB"]
INTERVALS = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "1440"}
LOCK = threading.RLock()

STATE = {
    "settings": {
        "auto": False,
        "leverage": max(MIN_LEVERAGE, min(MAX_LEVERAGE, DEFAULT_LEVERAGE)),
        "amount": float(DEFAULT_MARGIN_USDT),
        "tp": 10.0,            # 10% Take Profit
        "sl": 0.5,             # 0.5% Stop Loss
        "trail": 0.2,          # 0.2% Trailing Stop
        "close_pct": 0.05,     # Long: high - 0.05%, Short: low + 0.05%
        "min_range_pct": 0.40, # 1m candle min range 0.40%
        "auto_coin": "SOL",
        "one_trade_per_candle": True,
    },
    "history": [],
    "last_price": 0.0,
    "last_time": 0.0,
    "last_close_time": 0.0,
    "traded_candle_ts": None,
    "last_error": None,
    "fx_error": None,
    "pos_meta": None,
    "risk": {"day": "", "trades": 0, "pnl": 0.0, "consec_losses": 0, "halted": None},
}


class CoinSwitchError(RuntimeError):
    pass


class CoinSwitchFutures:
    """CoinSwitch PRO Futures V2 REST Client (EXCHANGE_2)."""

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
        headers = {
            "Content-Type": "application/json",
            "X-AUTH-APIKEY": self.api_key,
            "X-AUTH-SIGNATURE": signature,
            "X-AUTH-EPOCH": epoch,
        }
        resp = HTTP.request(
            method,
            BASE_URL + signed_path,
            headers=headers,
            json=body if body is not None else None,
            timeout=15,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text[:1000]}
        if not resp.ok:
            raise CoinSwitchError(f"CoinSwitch HTTP {resp.status_code}: {data}")
        if isinstance(data, dict) and data.get("error"):
            raise CoinSwitchError(str(data["error"]))
        return data

    def wallet(self):
        return self.request("GET", "/trade/api/v2/futures/wallet_balance")

    def spot_portfolio(self):
        return self.request("GET", "/trade/api/v2/user/portfolio")

    def usdt_inr_ticker(self):
        return self.request(
            "GET",
            "/trade/api/v2/24hr/ticker",
            {"exchange": "coinswitchx", "symbol": "USDT/INR"},
        )

    def ticker(self, symbol):
        return self.request(
            "GET",
            "/trade/api/v2/futures/ticker",
            {"exchange": "EXCHANGE_2", "symbol": symbol},
        )

    def positions(self, symbol):
        return self.request(
            "GET",
            "/trade/api/v2/futures/positions",
            {"exchange": "EXCHANGE_2", "symbol": symbol},
        )

    def klines(self, symbol, interval):
        return self.request(
            "GET",
            "/trade/api/v2/futures/klines",
            {"exchange": "EXCHANGE_2", "symbol": symbol, "interval": interval, "limit": 50},
        )

    def instruments(self):
        return self.request(
            "GET",
            "/trade/api/v2/futures/instrument_info",
            {"exchange": "EXCHANGE_2"},
        )

    def set_leverage(self, symbol, leverage):
        return self.request(
            "POST",
            "/trade/api/v2/futures/leverage",
            body={"exchange": "EXCHANGE_2", "symbol": symbol, "leverage": int(leverage)},
        )

    def order(self, symbol, side, order_type, quantity, **extra):
        body = {
            "exchange": "EXCHANGE_2",
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "quantity": float(quantity),
            "client_order_id": str(uuid.uuid4()),
        }
        body.update(extra)
        return self.request("POST", "/trade/api/v2/futures/order", body=body)

    def cancel_all_open_orders(self, symbol=None):
        body = {"exchange": "EXCHANGE_2"}
        if symbol:
            body["symbol"] = symbol
        return self.request("POST", "/trade/api/v2/futures/cancel-all", body=body)

    def open_orders(self, symbol=None, limit=None):
        body = {"exchange": "EXCHANGE_2"}
        if symbol:
            body["symbol"] = symbol
        if limit is not None:
            body["limit"] = limit
        return self.request("POST", "/trade/api/v2/futures/orders/open", body=body)


CLIENT = None
CLIENT_ERROR = None
try:
    CLIENT = CoinSwitchFutures(API_KEY, SECRET_KEY)
except Exception as exc:
    CLIENT_ERROR = str(exc)


def unwrap(obj):
    return obj.get("data", obj) if isinstance(obj, dict) else obj


def D(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise CoinSwitchError(f"Invalid number: {value}")


def find_num(obj, names):
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key.lower() in names:
                try:
                    return Decimal(str(val))
                except Exception:
                    pass
        for val in obj.values():
            found = find_num(val, names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for val in obj:
            found = find_num(val, names)
            if found is not None:
                return found
    return None


def require_client():
    if CLIENT is None:
        raise CoinSwitchError(CLIENT_ERROR or "CoinSwitch API client unavailable")
    return CLIENT


def get_instrument(symbol):
    data = unwrap(require_client().instruments())
    if isinstance(data, dict):
        item = data.get(symbol) or data.get(symbol.lower()) or data.get(symbol.upper())
        if item:
            return item
    raise CoinSwitchError(f"{symbol} ke liye CoinSwitch Futures V2 instrument info nahi mila.")


def get_usdt_inr_rate():
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
                return rate, "Render USDT_INR_RATE fallback"
        except Exception:
            pass
    return 0.0, "INR conversion unavailable"


def get_price(coin):
    symbol = coin + "USDT"
    data = unwrap(require_client().ticker(symbol))
    p = find_num(data, {"last_price", "lastprice", "mark_price", "price", "index_price"})
    if p is None or p <= 0:
        raise CoinSwitchError(f"Ticker me valid price nahi mila: {data}")
    change = find_num(data, {"price_change_percent", "change_24_hour", "change_percent", "pricechangepercent"}) or Decimal("0")
    return float(p), float(change)


def get_candles(coin, interval="1m"):
    symbol = coin + "USDT"
    data = unwrap(require_client().klines(symbol, INTERVALS.get(interval, "1")))
    if isinstance(data, dict):
        data = data.get("candles", data.get("list", []))
    candles = []
    for c in data if isinstance(data, list) else []:
        try:
            ts = int(Decimal(str(c.get("start_time", c.get("time", 0)))))
            if ts > 10_000_000_000:
                ts //= 1000
            candles.append({
                "time": ts,
                "open": float(c.get("o", c.get("open"))),
                "high": float(c.get("h", c.get("high"))),
                "low": float(c.get("l", c.get("low"))),
                "close": float(c.get("c", c.get("close"))),
            })
        except Exception:
            continue
    candles.sort(key=lambda x: x["time"])
    return candles


def get_position(coin):
    raw = unwrap(require_client().positions(coin + "USDT"))
    if isinstance(raw, dict):
        raw = raw.get("positions", raw.get("list", []))
    for p in raw if isinstance(raw, list) else []:
        if str(p.get("symbol", "")).upper() == coin + "USDT" and str(p.get("status", "OPEN")).upper() == "OPEN":
            try:
                if Decimal(str(p.get("position_size", "0"))) > 0:
                    return p
            except Exception:
                pass
    return None


def record(msg):
    STATE["history"].insert(0, time.strftime("%Y-%m-%d %H:%M:%S") + " | " + str(msg)[:700])
    del STATE["history"][50:]


def round_step(value, step):
    value, step = D(value), D(step)
    if step <= 0:
        raise CoinSwitchError("Exchange quantity/price step invalid")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def tick_step(info):
    precision = int(info.get("price_precision", 2))
    tick = D(info.get("tick_size", "1"))
    return tick * (Decimal(10) ** (-precision))


def wallet_balances():
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
        raise CoinSwitchError(f"Futures wallet response unexpected: {str(response)[:500]}")
    for item in base:
        if isinstance(item, dict) and str(item.get("base_asset", "")).upper() == "USDT":
            b = item.get("balances", {})
            if not isinstance(b, dict):
                raise CoinSwitchError("Futures wallet me USDT balances object nahi mila.")
            return {
                "total": float(D(b["total_balance"])),
                "available": float(D(b["total_available_balance"])),
                "blocked": float(D(b["total_blocked_balance"])),
                "source": "CoinSwitch Futures V2 wallet_balance",
            }
    raise CoinSwitchError(f"USDT balance row not found in Futures wallet: {str(response)[:500]}")


def protective_orders(coin, side, entry, info):
    symbol = coin + "USDT"
    s = STATE["settings"]
    tp_pct, sl_pct = D(str(s["tp"])) / 100, D(str(s["sl"])) / 100
    tick = tick_step(info)
    if side == "LONG":
        exit_side = "SELL"
        sl = entry * (1 - sl_pct)
        tp = entry * (1 + tp_pct)
    else:
        exit_side = "BUY"
        sl = entry * (1 + sl_pct)
        tp = entry * (1 - tp_pct)
    sl = (sl / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    tp = (tp / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    sl_result = require_client().order(
        symbol, exit_side, "STOP_MARKET", 0, trigger_price=float(sl), reduce_only=True
    )
    tp_result = require_client().order(
        symbol, exit_side, "TAKE_PROFIT_MARKET", 0, trigger_price=float(tp), reduce_only=True
    )
    return {"stop_loss": sl_result, "take_profit": tp_result, "sl_trigger": str(sl), "tp_trigger": str(tp)}


def open_live(coin, side, margin, leverage, auto=False, candle_low=0.0, candle_high=0.0, candle_ts=None):
    margin = D(margin)
    min_margin = D(MIN_MARGIN_USDT)
    if margin < min_margin:
        raise CoinSwitchError(f"Margin kam se kam {min_margin} USDT hona chahiye.")
    if margin > D(MAX_MARGIN_USDT):
        raise CoinSwitchError(f"Per-trade margin {D(MAX_MARGIN_USDT)} USDT cap se zyada hai.")
    if not LIVE_TRADING:
        raise CoinSwitchError("LIVE_TRADING=YES set nahi hai. Live order locked hai.")

    symbol = coin + "USDT"
    if get_position(coin):
        raise CoinSwitchError(f"{symbol} par pehle se position khuli hai.")

    price, _ = get_price(coin)
    info = get_instrument(symbol)
    if str(info.get("status", "TRADING")).upper() != "TRADING":
        raise CoinSwitchError(f"{symbol} abhi trading status me nahi hai.")

    lev_min, lev_max = D(info.get("min_leverage", 1)), D(info.get("max_leverage", 1))
    if D(leverage) < lev_min or D(leverage) > lev_max:
        raise CoinSwitchError(f"Leverage allowed range {lev_min}-{lev_max}x hai.")

    wallet = wallet_balances()
    if D(margin) > D(wallet["available"]):
        raise CoinSwitchError(f"Available wallet sirf {wallet['available']:.4f} USDT hai.")

    notional = D(margin) * D(leverage)
    qty_step = D(info.get("base_quantity_step_size", info.get("lot_size", "0.000001")))
    qty = round_step(notional / D(price), qty_step)
    min_qty = D(info.get("min_base_quantity", "0"))
    max_qty = D(info.get("max_market_base_quantity", "0"))
    if qty < min_qty:
        raise CoinSwitchError(f"Calculated quantity {qty} minimum {min_qty} se kam hai.")
    if max_qty > 0 and qty > max_qty:
        raise CoinSwitchError(f"Calculated quantity {qty} maximum {max_qty} se zyada hai.")

    require_client().set_leverage(symbol, leverage)
    order_result = require_client().order(symbol, "BUY" if side == "LONG" else "SELL", "MARKET", qty)

    pos = None
    for _ in range(8):
        time.sleep(1)
        pos = get_position(coin)
        if pos:
            break
    if not pos:
        raise CoinSwitchError("Order bheja gaya, lekin position confirm nahi hui. CoinSwitch app me check karein.")

    entry = D(pos.get("avg_entry_price", price))
    try:
        protections = protective_orders(coin, side, entry, info)
    except Exception as exc:
        record(f"CRITICAL: position open but TP/SL failed: {exc}")
        try:
            size = D(pos.get("position_size", qty))
            require_client().order(symbol, "SELL" if side == "LONG" else "BUY", "MARKET", size, reduce_only=True)
        except Exception as close_exc:
            record(f"CRITICAL: emergency close failed: {close_exc}")
        STATE["last_close_time"] = time.time()
        halt_auto("TP/SL setup failed after entry")
        raise CoinSwitchError(f"Position open hui par TP/SL fail hone se emergency close kiya: {exc}")

    STATE["pos_meta"] = {
        "coin": coin,
        "side": side,
        "entry": float(entry),
        "peak": float(entry),
        "trough": float(entry),
        "candle_low": float(candle_low) if candle_low else float(entry),
        "candle_high": float(candle_high) if candle_high else float(entry),
        "candle_ts": candle_ts,
        "auto": bool(auto),
    }
    if auto:
        STATE["risk"]["trades"] += 1
        if candle_ts is not None:
            STATE["traded_candle_ts"] = candle_ts

    record(
        f"{coin} {side} {'AUTO' if auto else 'MANUAL'} OPEN @ {entry} USDT | "
        f"Qty {qty} | Lev {leverage}x | SL {protections['sl_trigger']} | TP {protections['tp_trigger']}"
    )
    return {"order": order_result, "position": pos, "protection": protections}


def close_live(coin, reason="MANUAL CLOSE"):
    if not LIVE_TRADING:
        raise CoinSwitchError("LIVE_TRADING=YES set nahi hai. Live close locked hai.")
    pos = get_position(coin)
    if not pos:
        STATE["pos_meta"] = None
        return {"message": "Is coin par open position nahi mili."}
    size = D(pos.get("position_size", "0"))
    side = str(pos.get("position_side", "")).upper()
    if size <= 0 or side not in ("LONG", "SHORT"):
        raise CoinSwitchError(f"Invalid position data: {pos}")

    try:
        upnl = float(D(pos.get("unrealised_pnl", 0)))
    except Exception:
        upnl = 0.0

    result = require_client().order(
        coin + "USDT", "SELL" if side == "LONG" else "BUY", "MARKET", size, reduce_only=True
    )
    record_trade_result(upnl)
    try:
        require_client().cancel_all_open_orders(coin + "USDT")
    except Exception as exc:
        record(f"WARN: orphan TP/SL cancel failed: {exc}")

    record(f"{coin} {side} CLOSE ({reason}) | Est PnL: {upnl:+.4f} USDT")
    STATE["pos_meta"] = None
    STATE["last_close_time"] = time.time()
    return result


def _risk_roll():
    r = STATE["risk"]
    today = time.strftime("%Y-%m-%d")
    if r["day"] != today:
        r.update({"day": today, "trades": 0, "pnl": 0.0, "consec_losses": 0})
    return r


def halt_auto(reason):
    STATE["settings"]["auto"] = False
    STATE["risk"]["halted"] = reason
    record("AUTO HALTED: " + str(reason))


def risk_allows_new_trade():
    r = _risk_roll()
    if r["halted"]:
        return False
    if r["pnl"] <= -abs(MAX_DAILY_LOSS_USDT):
        halt_auto(f"Daily loss limit {MAX_DAILY_LOSS_USDT} USDT hit")
        return False
    if r["trades"] >= MAX_TRADES_PER_DAY:
        halt_auto("Max trades/day reached")
        return False
    if r["consec_losses"] >= MAX_CONSEC_LOSSES:
        halt_auto(f"{MAX_CONSEC_LOSSES} consecutive losses hit")
        return False
    if time.time() - STATE["last_close_time"] < AUTO_COOLDOWN_SEC:
        return False
    return True


def record_trade_result(pnl):
    r = _risk_roll()
    r["pnl"] += float(pnl)
    r["consec_losses"] = (r["consec_losses"] + 1) if pnl < 0 else 0


def check_auto_strategy(coin, price, candles):
    """1m Live Candle High/Low Scalping Strategy (Futures V2).

    Open Logic:
      - 1m candle range >= min_range_pct (default 0.40%)
      - Price touches/exceeds 1m live High -> SHORT
      - Price touches/drops below 1m live Low -> LONG
    Close Logic:
      - LONG closes when price >= candle_high * (1 - close_pct/100) [default high - 0.05%]
      - SHORT closes when price <= candle_low * (1 + close_pct/100) [default low + 0.05%]
      - Hard SL (0.5%), Hard TP (10%), Trailing SL (0.2%)
    """
    s = STATE["settings"]
    if not (s["auto"] and LIVE_TRADING and AUTO_TRADING_ALLOWED):
        return
    if not candles:
        return

    live_c = candles[-1]
    c_ts = live_c["time"]
    c_low = min(live_c["low"], price)
    c_high = max(live_c["high"], price)

    pos = get_position(coin)
    if not pos:
        if not risk_allows_new_trade():
            return
        if s.get("one_trade_per_candle", True) and STATE.get("traded_candle_ts") == c_ts:
            return
        if c_low <= 0:
            return

        range_pct = ((c_high - c_low) / c_low) * 100.0
        if range_pct < s["min_range_pct"]:
            return

        tol = price * 0.0001  # 0.01% execution tolerance for live high/low touch
        if price >= (c_high - tol):
            open_live(coin, "SHORT", s["amount"], s["leverage"], True, c_low, c_high, c_ts)
        elif price <= (c_low + tol):
            open_live(coin, "LONG", s["amount"], s["leverage"], True, c_low, c_high, c_ts)
        return

    # Manage open position
    side = str(pos.get("position_side", "")).upper()
    entry = float(D(pos.get("avg_entry_price", price)))
    meta = STATE.get("pos_meta") or {
        "coin": coin,
        "side": side,
        "entry": entry,
        "peak": entry,
        "trough": entry,
        "candle_low": c_low,
        "candle_high": c_high,
        "candle_ts": c_ts,
        "auto": True,
    }

    # Update live extremes & expanding candle bounds
    if meta.get("candle_ts") == c_ts:
        meta["candle_high"] = max(meta.get("candle_high", c_high), c_high)
        meta["candle_low"] = min(meta.get("candle_low", c_low), c_low)

    if side == "LONG":
        meta["peak"] = max(meta.get("peak", entry), price)
    else:
        meta["trough"] = min(meta.get("trough", entry), price)
    STATE["pos_meta"] = meta

    close_pct = s["close_pct"] / 100.0
    sl_pct = s["sl"] / 100.0
    tp_pct = s["tp"] / 100.0
    trail_pct = s["trail"] / 100.0

    if side == "LONG":
        candle_target = meta["candle_high"] * (1.0 - close_pct)
        trail_stop = meta["peak"] * (1.0 - trail_pct)
        hard_sl = entry * (1.0 - sl_pct)
        hard_tp = entry * (1.0 + tp_pct)

        if price <= hard_sl:
            close_live(coin, f"HARD SL ({s['sl']}%)")
        elif price <= trail_stop and trail_stop > entry:
            close_live(coin, f"TRAILING SL ({s['trail']}%)")
        elif price >= hard_tp:
            close_live(coin, f"HARD TP ({s['tp']}%)")
        elif meta["candle_high"] > entry and price >= candle_target and price > entry:
            close_live(coin, f"CANDLE EXIT (High - {s['close_pct']}%)")

    elif side == "SHORT":
        candle_target = meta["candle_low"] * (1.0 + close_pct)
        trail_stop = meta["trough"] * (1.0 + trail_pct)
        hard_sl = entry * (1.0 + sl_pct)
        hard_tp = entry * (1.0 - tp_pct)

        if price >= hard_sl:
            close_live(coin, f"HARD SL ({s['sl']}%)")
        elif price >= trail_stop and trail_stop < entry:
            close_live(coin, f"TRAILING SL ({s['trail']}%)")
        elif price <= hard_tp:
            close_live(coin, f"HARD TP ({s['tp']}%)")
        elif meta["candle_low"] < entry and price <= candle_target and price < entry:
            close_live(coin, f"CANDLE EXIT (Low + {s['close_pct']}%)")


def background_bot():
    while True:
        try:
            s = STATE["settings"]
            if not (s["auto"] and AUTO_TRADING_ALLOWED and LIVE_TRADING):
                time.sleep(2)
                continue
            coin = s.get("auto_coin", "SOL")
            with LOCK:
                price, _ = get_price(coin)
                candles = get_candles(coin, "1m")
                if price > 0:
                    STATE["last_price"] = price
                    STATE["last_time"] = time.time()
                    check_auto_strategy(coin, price, candles)
            STATE["last_error"] = None
        except Exception as exc:
            STATE["last_error"] = str(exc)
            record("AUTO error: " + str(exc))
        time.sleep(1.5)


def auth_required():
    if not DASHBOARD_PASSWORD:
        return Response("Set DASHBOARD_PASSWORD in Render Environment before using this app.", 503)
    auth = request.authorization
    if (
        not auth
        or not hmac.compare_digest(auth.username or "", DASHBOARD_USER)
        or not hmac.compare_digest(auth.password or "", DASHBOARD_PASSWORD)
    ):
        return Response("Login required", 401, {"WWW-Authenticate": "Basic realm=FutureV2Bot"})
    return None


@app.before_request
def protect_dashboard():
    if request.path == "/healthz":
        return None
    return auth_required()


HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CoinSwitch Futures V2 Bot</title>
<style>
body{font-family:Arial,sans-serif;text-align:center;padding:8px;background:#fafafa;color:#111}
button{padding:9px 14px;margin:3px;font-size:14px;border-radius:8px;border:1px solid #222;cursor:pointer}
.long{background:#16a34a;color:#fff;border-color:#15803d}
.short{background:#dc2626;color:#fff;border-color:#b91c1c}
.close{background:#111;color:#fff}
.tf{background:#e5e7eb}.tf.active{background:#111;color:#fff}
.tool{background:#fff;border:2px solid #7c3aed}.tool.active{background:#7c3aed;color:#fff}
.box{background:#fff;border:1px solid #d1d5db;border-radius:10px;padding:12px;margin:10px auto;max-width:860px;text-align:left}
input,select{padding:7px;margin:4px;font-size:14px;width:95px;border:1px solid #9ca3af;border-radius:6px}
.hlbox{background:#fffbeb;border:1px solid #f59e0b;border-radius:8px;padding:9px;margin-top:8px}
.tpbox{background:#f0fdf4;border:1px solid #22c55e;border-radius:8px;padding:8px;margin-top:8px}
.slbox{background:#fef2f2;border:1px solid #ef4444;border-radius:8px;padding:8px;margin-top:6px}
</style>
</head>
<body>
<h2>CoinSwitch PRO — Futures V2 Live Bot</h2>
<div>
  <button onclick="load('BTC')">BTC</button>
  <button onclick="load('ETH')">ETH</button>
  <button onclick="load('SOL')">SOL</button>
  <button onclick="load('DOGE')">DOGE</button>
  <button onclick="load('XRP')">XRP</button>
  <button onclick="load('ADA')">ADA</button>
  <button onclick="load('BNB')">BNB</button>
</div>

<h1 id="p">Loading...</h1>
<div>
  <b style="color:#15803d">● FUTURES V2 ONLY (EXCHANGE_2)</b> |
  <span style="font-size:13px">1m High pe SHORT · 1m Low pe LONG · Min Range 0.40%</span>
</div>

<h2 id="bal" style="color:#0a7">💰 Futures V2 Wallet: Available USDT -- | ₹ --</h2>
<h3 id="locked" style="color:#d97706">🔒 Locked USDT -- | Total USDT --</h3>
<h4 id="fx">USDT/INR rate: loading...</h4>

<div class="box" id="mainWallet">
  Main CoinSwitch Spot Portfolio (read-only): <button onclick="checkMainWallet()">Check Main INR Wallet</button>
</div>

<h3 id="pos">No Position</h3>
<h3 id="trailInfo" style="color:#7c3aed">Trailing / Candle Target: -</h3>
<h3 id="pnl" style="color:blue">Live Unrealised PnL: USDT 0</h3>

<div style="margin:10px 0">
  <button class="long" onclick="trade('LONG')">🟢 Manual LONG</button>
  <button class="short" onclick="trade('SHORT')">🔴 Manual SHORT</button>
  <button class="close" onclick="trade('CLOSE')">❌ Close Position</button>
  <button onclick="refreshWallet()">🔄 Refresh Wallet</button>
</div>

<div class="box">
  <b>⚙️ STRATEGY & RISK SETTINGS (Futures V2)</b><br>
  Coin: <select id="auto_coin" onchange="saveSet()">
    <option>BTC</option><option>ETH</option><option selected>SOL</option><option>DOGE</option><option>XRP</option><option>ADA</option><option>BNB</option>
  </select> &nbsp;
  Leverage (__MIN_LEV__–__MAX_LEV__x): <input id="lev" type="number" value="__DEF_LEV__" min="__MIN_LEV__" max="__MAX_LEV__" onchange="saveSet()"> &nbsp;
  Margin USDT (__MIN_MARGIN__–__MAX_MARGIN__): <input id="amt" type="number" value="__DEF_MARGIN__" min="__MIN_MARGIN__" max="__MAX_MARGIN__" step="0.1" onchange="saveSet()"><br>
  TP %: <input id="tp" type="number" value="10.0" step="0.5" onchange="saveSet()"> &nbsp;
  SL %: <input id="sl" type="number" value="0.5" step="0.1" onchange="saveSet()"> &nbsp;
  Trail %: <input id="trail" type="number" value="0.2" step="0.05" onchange="saveSet()"><br>

  <div class="hlbox">
    <b>⚡ 1m Live Candle Entry / Exit Parameters</b><br>
    Min Candle Range %: <input id="min_range_pct" type="number" value="0.40" step="0.05" onchange="saveSet()"> % &nbsp;
    Candle Close Offset %: <input id="close_pct" type="number" value="0.05" step="0.01" onchange="saveSet()"> % &nbsp;
    1 Trade / Candle: <select id="one_trade" onchange="saveSet()"><option value="1" selected>YES</option><option value="0">NO</option></select><br>
    <small>SHORT @ 1m High → Close @ Low + Close% | LONG @ 1m Low → Close @ High - Close%</small>
  </div>

  <div style="margin-top:10px">
    <b>🤖 AUTO TRADING:</b>
    <select id="auto" onchange="if(this.value=='1'&&!confirm('AUTO LIVE ON karein? Real Futures V2 orders lagेंगे.')){this.value='0';return;}saveSet()">
      <option value="0" selected>OFF</option>
      <option value="1">ON (Live)</option>
    </select>
    <span id="autoTxt" style="font-weight:bold">AUTO OFF</span>
    <button class="close" onclick="fetch('/api/kill?close=1',{method:'POST'}).then(()=>{document.getElementById('auto').value='0';document.getElementById('autoTxt').innerText='AUTO OFF';alert('KILL SWITCH: Auto stopped & close requested');})">🛑 KILL + CLOSE</button>
  </div>
  <div id="tpPreview"></div>
</div>

<div style="margin-top:10px">
  <b>⏱️ TIMEFRAME:</b>
  <button class="tf active" id="tf-1m" onclick="setTF('1m')">1m</button>
  <button class="tf" id="tf-5m" onclick="setTF('5m')">5m</button>
  <button class="tf" id="tf-15m" onclick="setTF('15m')">15m</button>
  <button class="tf" id="tf-1h" onclick="setTF('1h')">1h</button>
  <button class="tf" id="tf-4h" onclick="setTF('4h')">4h</button>
  <button class="tf" id="tf-1d" onclick="setTF('1d')">1D</button>
</div>

<div style="margin-top:8px">
  <b>📐 CHART LINES:</b>
  <button class="tool active" id="tool-none" onclick="setTool('none')">🖱️ Normal</button>
  <button class="tool" id="tool-h" onclick="setTool('h')">➖ H-Line</button>
  <button class="tool" id="tool-v" onclick="setTool('v')">➡️ V-Line</button>
  <button class="tool" onclick="clearLines()">🗑️ Clear</button>
</div>

<canvas id="c" width="860" height="340" style="border:1px solid #222;background:#fff;width:100%;max-width:860px;margin-top:10px;cursor:crosshair"></canvas>

<h3>📜 Trade & Bot Logs</h3>
<div class="box" id="h" style="max-height:260px;overflow-y:auto;font-family:monospace;font-size:12px"></div>

<script>
let cur='SOL', tf='1m', tool='none', hLines=[], vLines=[], lastCandles=[], lastPrice=0, curPos=null, curEntry=0;

function setTF(t){
  tf=t;
  document.querySelectorAll('.tf').forEach(b=>b.classList.remove('active'));
  document.getElementById('tf-'+t).classList.add('active');
  fetchData();
}
function setTool(t){
  tool=t;
  document.querySelectorAll('.tool').forEach(b=>b.classList.remove('active'));
  document.getElementById('tool-'+t).classList.add('active');
}
function clearLines(){hLines=[];vLines=[];drawChart();}
async function load(coin){
  cur=coin;
  document.getElementById('auto_coin').value=coin;
  await saveSet();
  fetchData();
}
async function saveSet(){
  let payload={
    leverage: document.getElementById('lev').value,
    amount: document.getElementById('amt').value,
    tp: document.getElementById('tp').value,
    sl: document.getElementById('sl').value,
    trail: document.getElementById('trail').value,
    auto: document.getElementById('auto').value,
    auto_coin: document.getElementById('auto_coin').value,
    close_pct: document.getElementById('close_pct').value,
    min_range_pct: document.getElementById('min_range_pct').value,
    one_trade_per_candle: document.getElementById('one_trade').value === '1'
  };
  let r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  let d=await r.json();
  if(!r.ok||!d.ok){alert(d.msg||'Invalid setting');return;}
  document.getElementById('autoTxt').innerText = payload.auto=='1'
    ? ('AUTO ON ('+payload.auto_coin+' | Range >= '+payload.min_range_pct+'%)')
    : 'AUTO OFF';
  updateTpSlPreview();
}
async function trade(side){
  if(side!=='CLOSE' && !confirm('REAL CoinSwitch Futures V2 order: '+side+' '+cur+'?')) return;
  let r=await fetch('/api/trade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({coin:cur,side:side})});
  let d=await r.json();
  alert(d.msg||JSON.stringify(d));
  fetchData();
}
async function refreshWallet(){
  try{
    let r=await fetch('/api/wallet',{cache:'no-store'});
    let d=await r.json();
    if(!r.ok||!d.ok) throw new Error(d.error||'Wallet error');
    alert('Futures V2 Wallet\nAvailable: '+d.available+' USDT\nTotal: '+d.total+' USDT');
    fetchData();
  }catch(e){alert('Wallet error: '+e.message);}
}
async function checkMainWallet(){
  let box=document.getElementById('mainWallet');
  box.innerText='Checking main CoinSwitch INR portfolio...';
  try{
    let r=await fetch('/api/main-wallet',{cache:'no-store'});
    let d=await r.json();
    if(!r.ok||!d.ok) throw new Error(d.error||'Error');
    box.innerText='Main Spot Wallet (Read-only): Available ₹'+Number(d.available_inr).toLocaleString('en-IN',{minimumFractionDigits:2})+' | Total ₹'+Number(d.total_inr).toLocaleString('en-IN',{minimumFractionDigits:2});
  }catch(e){box.innerText='Main INR check failed: '+e.message;}
}
function fmtUSDT(n){return 'USDT '+Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4});}
function fmtPrice(n){return Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4});}

function updateTpSlPreview(){
  let tp=parseFloat(document.getElementById('tp').value)||0;
  let sl=parseFloat(document.getElementById('sl').value)||0;
  let lev=parseFloat(document.getElementById('lev').value)||10;
  let amt=parseFloat(document.getElementById('amt').value)||1;
  let fee=amt*lev*0.0012;
  let tpGross=amt*lev*tp/100;
  let slGross=amt*lev*sl/100;
  let basePrice=(curPos&&curEntry>0)?curEntry:lastPrice;
  let side=(curPos&&curPos.side)?curPos.side:'LONG';
  let tpPrice=side=='LONG'?basePrice*(1+tp/100):basePrice*(1-tp/100);
  let slPrice=side=='LONG'?basePrice*(1-sl/100):basePrice*(1+sl/100);
  document.getElementById('tpPreview').innerHTML=
    '<div class="tpbox"><b>🎯 TP ('+tp+'%)</b> → Price <b>'+fmtPrice(tpPrice)+'</b> | Est Net: <b style="color:green">+'+(tpGross-fee).toFixed(3)+' USDT</b></div>'+
    '<div class="slbox"><b>🛑 SL ('+sl+'%)</b> → Price <b>'+fmtPrice(slPrice)+'</b> | Est Net: <b style="color:red">-'+ (slGross+fee).toFixed(3)+' USDT</b></div>';
}

function drawChart(){
  let cv=document.getElementById('c'), x=cv.getContext('2d');
  x.clearRect(0,0,860,340);
  if(!lastCandles.length) return;
  let allLow=[...lastCandles.map(a=>a.low),lastPrice];
  let allHigh=[...lastCandles.map(a=>a.high),lastPrice];
  let lo=Math.min(...allLow), hi=Math.max(...allHigh), rg=(hi-lo)||1;
  lo-=rg*0.08; hi+=rg*0.08; rg=hi-lo;
  let chartW=700, w=chartW/lastCandles.length;
  let yOf=p=>10+((hi-p)/rg)*300;
  let xOf=i=>i*w+w/2;

  x.font='11px Arial'; x.fillStyle='#555'; x.strokeStyle='#eee';
  for(let i=0;i<=5;i++){
    let p=lo+(rg*i/5), y=yOf(p);
    x.beginPath(); x.moveTo(0,y); x.lineTo(chartW,y); x.stroke();
    x.fillText(fmtUSDT(p),706,y+4);
  }
  let yL=yOf(lastPrice);
  x.strokeStyle='#2563eb'; x.setLineDash([4,3]);
  x.beginPath(); x.moveTo(0,yL); x.lineTo(chartW,yL); x.stroke();
  x.setLineDash([]);

  hLines.forEach(p=>{let y=yOf(p);x.strokeStyle='#7c3aed';x.setLineDash([6,4]);x.beginPath();x.moveTo(0,y);x.lineTo(chartW,y);x.stroke();x.setLineDash([]);});
  vLines.forEach(idx=>{let px=xOf(idx);x.strokeStyle='#f59e0b';x.setLineDash([6,4]);x.beginPath();x.moveTo(px,0);x.lineTo(px,310);x.stroke();x.setLineDash([]);});

  lastCandles.forEach((c,i)=>{
    let px=xOf(i), yH=yOf(c.high), yL2=yOf(c.low), yO=yOf(c.open), yC=yOf(c.close);
    let g=c.close>=c.open;
    x.strokeStyle=g?'#16a34a':'#dc2626'; x.fillStyle=g?'#16a34a':'#dc2626';
    x.beginPath(); x.moveTo(px,yH); x.lineTo(px,yL2); x.stroke();
    let bw=(i==lastCandles.length-1)?10:7;
    x.fillRect(px-bw/2,Math.min(yO,yC),bw,Math.max(3,Math.abs(yC-yO)));
  });

  x.fillStyle='#2563eb'; x.fillRect(706,yL-10,146,20);
  x.fillStyle='#fff'; x.fillText(fmtUSDT(lastPrice),710,yL+4);
}

document.getElementById('c').addEventListener('click',function(e){
  if(tool=='none'||!lastCandles.length) return;
  let rect=this.getBoundingClientRect();
  let cx=(e.clientX-rect.left)*(860/rect.width);
  let cy=(e.clientY-rect.top)*(340/rect.height);
  let lo=Math.min(...lastCandles.map(a=>a.low)), hi=Math.max(...lastCandles.map(a=>a.high)), rg=(hi-lo)||1;
  lo-=rg*0.08; hi+=rg*0.08; rg=hi-lo;
  if(tool=='h') hLines.push(hi-((cy-10)/300)*rg);
  if(tool=='v') vLines.push(Math.floor(cx/(700/lastCandles.length)));
  drawChart();
});

async function fetchData(){
  let r=await fetch('/api/data?coin='+cur+'&interval='+tf);
  let d=await r.json();
  if(!r.ok||d.error){
    document.getElementById('bal').innerText='⚠️ API Error: '+(d.error||('HTTP '+r.status));
    return;
  }
  lastCandles=d.candles; lastPrice=d.price_usdt; curPos=d.pos_obj; curEntry=d.entry_usdt;
  document.getElementById('p').innerText=cur+' : '+d.price_usdt.toFixed(4)+' USDT | '+(d.usdt_inr?'₹ '+(d.price_usdt*d.usdt_inr).toLocaleString('en-IN',{maximumFractionDigits:2}):'₹ --')+' | '+d.position+' | 24h '+d.change+'%';
  document.getElementById('bal').innerText='💰 Futures V2 Available: '+d.balance.toFixed(4)+' USDT | ₹ '+(d.balance_inr==null?'--':d.balance_inr.toLocaleString('en-IN',{maximumFractionDigits:2}));
  document.getElementById('locked').innerText='🔒 Locked: '+d.locked.toFixed(4)+' USDT | Total: '+d.total.toFixed(4)+' USDT';
  document.getElementById('fx').innerText='1 USDT = ₹ '+(d.usdt_inr||'Unavailable')+' | '+d.fx_source;
  document.getElementById('pos').innerText=d.pos_msg;
  document.getElementById('trailInfo').innerText=d.trail_msg;
  let p=document.getElementById('pnl');
  p.innerText='Live Unrealised PnL: '+d.live_pnl+' USDT ('+d.live_pct+'%) | ₹ '+(d.live_pnl_inr==null?'--':d.live_pnl_inr);
  p.style.color=d.live_pnl>=0?'green':'red';
  document.getElementById('h').innerHTML=d.history.map(x=>'<div>'+x+'</div>').join('');
  drawChart(); updateTpSlPreview();
}
setInterval(fetchData, 4000);
fetchData();
</script>
</body>
</html>
"""


@app.route("/")
def home():
    page = (
        HTML.replace("__MIN_MARGIN__", str(MIN_MARGIN_USDT))
        .replace("__MAX_MARGIN__", str(MAX_MARGIN_USDT))
        .replace("__DEF_MARGIN__", str(STATE["settings"]["amount"]))
        .replace("__MIN_LEV__", str(MIN_LEVERAGE))
        .replace("__MAX_LEV__", str(MAX_LEVERAGE))
        .replace("__DEF_LEV__", str(STATE["settings"]["leverage"]))
    )
    return render_template_string(page)


@app.route("/healthz")
def healthz():
    return jsonify({
        "ok": True,
        "engine": "futures_v2_only",
        "live_trading": LIVE_TRADING,
        "auto_allowed": AUTO_TRADING_ALLOWED,
    })


@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    s = STATE["settings"]
    backup = dict(s)
    try:
        data = request.get_json(silent=True) or request.args
        if "leverage" in data:
            s["leverage"] = int(data["leverage"])
        if "amount" in data:
            s["amount"] = float(data["amount"])
        if "tp" in data:
            s["tp"] = float(data["tp"])
        if "sl" in data:
            s["sl"] = float(data["sl"])
        if "trail" in data:
            s["trail"] = float(data["trail"])
        if "auto" in data:
            s["auto"] = str(data["auto"]).lower() in ("1", "true", "on", "yes")
        if "auto_coin" in data and str(data["auto_coin"]).upper() in COINS:
            s["auto_coin"] = str(data["auto_coin"]).upper()
        if "close_pct" in data:
            s["close_pct"] = float(data["close_pct"])
        if "min_range_pct" in data:
            s["min_range_pct"] = float(data["min_range_pct"])
        if "one_trade_per_candle" in data:
            s["one_trade_per_candle"] = str(data["one_trade_per_candle"]).lower() in ("1", "true", "yes")

        if s["amount"] <= 0 or min(s["tp"], s["sl"], s["trail"], s["close_pct"], s["min_range_pct"]) <= 0:
            raise ValueError("Amount, TP, SL, Trail, Close% aur Min Range% positive hone chahiye.")
        if not (MIN_LEVERAGE <= s["leverage"] <= MAX_LEVERAGE):
            raise ValueError(f"Leverage {MIN_LEVERAGE}–{MAX_LEVERAGE}x ke beech honi chahiye.")
        lo, hi = D(MIN_MARGIN_USDT), D(MAX_MARGIN_USDT)
        if not (lo <= D(str(s["amount"])) <= hi):
            raise ValueError(f"Margin {lo}–{hi} USDT ke beech hona chahiye.")
    except Exception as exc:
        s.clear()
        s.update(backup)
        return jsonify({"ok": False, "msg": str(exc)}), 400
    return jsonify({"ok": True, "settings": s, "live_trading": LIVE_TRADING, "auto_allowed": AUTO_TRADING_ALLOWED})


@app.route("/api/main-wallet")
def main_wallet_api():
    try:
        response = require_client().spot_portfolio()
        raw = unwrap(response)
        if isinstance(raw, dict):
            raw = raw.get("portfolio", raw.get("balances", raw.get("data", raw)))
        if not isinstance(raw, list):
            raise CoinSwitchError("Portfolio format unexpected")
        inr_row = next((x for x in raw if isinstance(x, dict) and str(x.get("currency", "")).upper() == "INR"), None)
        if inr_row is None:
            raise CoinSwitchError("INR row not found in portfolio")
        available = D(inr_row.get("main_balance", 0))
        blocked = D(inr_row.get("blocked_balance_order", 0))
        return jsonify({
            "ok": True,
            "available_inr": float(available),
            "blocked_inr": float(blocked),
            "total_inr": float(available + blocked),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/wallet")
def wallet_api():
    try:
        wallet = wallet_balances()
        rate, fx_source = get_usdt_inr_rate()
        return jsonify({
            "ok": True,
            **wallet,
            "usdt_inr": rate or None,
            "available_inr": round(wallet["available"] * rate, 2) if rate else None,
            "total_inr": round(wallet["total"] * rate, 2) if rate else None,
            "fx_source": fx_source,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/kill", methods=["POST"])
def kill_switch():
    halt_auto("Manual kill switch")
    out = {"ok": True, "auto": False}
    if request.args.get("close") == "1":
        try:
            with LOCK:
                out["close"] = close_live(STATE["settings"].get("auto_coin", "SOL"), "KILL SWITCH")
        except Exception as exc:
            out["close_error"] = str(exc)
    return jsonify(out)


@app.route("/api/auto/resume", methods=["POST"])
def auto_resume():
    if not (LIVE_TRADING and AUTO_TRADING_ALLOWED):
        return jsonify({"ok": False, "msg": "LIVE_TRADING / AUTO_TRADING_ALLOWED env YES nahi hai"}), 403
    STATE["risk"]["halted"] = None
    STATE["settings"]["auto"] = True
    record("AUTO resumed manually")
    return jsonify({"ok": True})


@app.route("/api/risk")
def risk_status():
    return jsonify({
        "live": LIVE_TRADING,
        "auto_allowed": AUTO_TRADING_ALLOWED,
        "auto_on": STATE["settings"]["auto"],
        "risk": _risk_roll(),
        "limits": {
            "daily_loss": MAX_DAILY_LOSS_USDT,
            "trades_per_day": MAX_TRADES_PER_DAY,
            "consec_losses": MAX_CONSEC_LOSSES,
            "cooldown_sec": AUTO_COOLDOWN_SEC,
        },
    })


@app.route("/api/trade", methods=["POST"])
def trade_api():
    if not LIVE_TRADING:
        return jsonify({"ok": False, "msg": "LIVE_TRADING=YES set nahi hai; live orders locked hain."}), 403
    data = request.get_json(silent=True) or {}
    coin = str(data.get("coin", "SOL")).upper()
    side = str(data.get("side", "")).upper()
    if coin not in COINS:
        return jsonify({"ok": False, "msg": "Unsupported coin"}), 400
    try:
        with LOCK:
            if side == "CLOSE":
                result = close_live(coin, "MANUAL CLOSE")
            elif side in ("LONG", "SHORT"):
                candles = get_candles(coin, "1m")
                c_low = candles[-1]["low"] if candles else 0.0
                c_high = candles[-1]["high"] if candles else 0.0
                c_ts = candles[-1]["time"] if candles else None
                result = open_live(
                    coin,
                    side,
                    STATE["settings"]["amount"],
                    STATE["settings"]["leverage"],
                    False,
                    c_low,
                    c_high,
                    c_ts,
                )
            else:
                raise CoinSwitchError("Side LONG, SHORT ya CLOSE hona chahiye.")
        return jsonify({"ok": True, "msg": f"{coin} {side} order Futures V2 par bhej diya gaya.", "result": result})
    except Exception as exc:
        record(f"ORDER ERROR {coin} {side}: {exc}")
        return jsonify({"ok": False, "msg": str(exc)}), 400


@app.route("/api/data")
def api_data():
    coin = request.args.get("coin", "SOL").upper()
    interval = request.args.get("interval", "1m")
    if coin not in COINS:
        coin = "SOL"
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

        pos_msg, trail_msg, pos_obj, entry, live_pct, live_gross, live_pnl = (
            "No Position",
            "Trailing / Candle Target: -",
            None,
            0.0,
            0.0,
            0.0,
            0.0,
        )
        if pos:
            side = str(pos.get("position_side", "?")).upper()
            entry = float(D(pos.get("avg_entry_price", 0)))
            live_pnl = float(D(pos.get("unrealised_pnl", 0)))
            margin = float(D(pos.get("position_margin", 0)))
            live_pct = (live_pnl / margin * 100) if margin else 0.0
            live_gross = live_pnl
            meta = STATE.get("pos_meta") or {}
            c_pct = STATE["settings"]["close_pct"] / 100.0
            if side == "LONG":
                c_tgt = meta.get("candle_high", entry) * (1.0 - c_pct)
                trail_msg = f"Peak: {meta.get('peak', entry):.4f} | Candle Target (High-{STATE['settings']['close_pct']}%): {c_tgt:.4f}"
            else:
                c_tgt = meta.get("candle_low", entry) * (1.0 + c_pct)
                trail_msg = f"Trough: {meta.get('trough', entry):.4f} | Candle Target (Low+{STATE['settings']['close_pct']}%): {c_tgt:.4f}"
            pos_msg = f"Open: {coin} {side} @ {entry:.4f} USDT | Size {pos.get('position_size')} | Lev {pos.get('leverage')}x | Liq {pos.get('liquidation_price')}"
            pos_obj = {"side": side, "coin": coin}

        pos_sig = "LONG 🟢" if change >= 0 else "SHORT 🔴"
        return jsonify({
            "price_usdt": price,
            "change": change,
            "position": pos_sig,
            "candles": candles,
            "history": STATE["history"][:30],
            "balance": wallet["available"],
            "locked": wallet["blocked"],
            "total": wallet["total"],
            "pos_msg": pos_msg,
            "trail_msg": trail_msg,
            "live_pnl": round(live_pnl, 6),
            "live_gross": round(live_gross, 6),
            "live_pct": round(live_pct, 3),
            "pos_obj": pos_obj,
            "entry_usdt": entry,
            "usdt_inr": usdt_inr,
            "fx_source": fx_source,
            "balance_inr": round(wallet["available"] * usdt_inr, 2) if usdt_inr else None,
            "locked_inr": round(wallet["blocked"] * usdt_inr, 2) if usdt_inr else None,
            "total_inr": round(wallet["total"] * usdt_inr, 2) if usdt_inr else None,
            "live_pnl_inr": round(live_pnl * usdt_inr, 2) if usdt_inr else None,
            "live_enabled": LIVE_TRADING,
            "auto_allowed": AUTO_TRADING_ALLOWED,
            "error": STATE.get("last_error"),
        })
    except Exception as exc:
        return jsonify({
            "error": str(exc),
            "price_usdt": 0,
            "change": 0,
            "candles": [],
            "history": STATE["history"][:30],
            "balance": 0,
            "locked": 0,
            "total": 0,
            "pos_msg": "Account/data unavailable",
            "trail_msg": "-",
            "live_pnl": 0,
            "live_gross": 0,
            "live_pct": 0,
            "pos_obj": None,
            "entry_usdt": 0,
            "usdt_inr": 0,
            "fx_source": "unavailable",
        }), 502


if (
    LIVE_TRADING
    and AUTO_TRADING_ALLOWED
    and DASHBOARD_PASSWORD
    and CLIENT is not None
    and os.getenv("WEB_CONCURRENCY", "1") == "1"
):
    threading.Thread(target=background_bot, daemon=True, name="futures-v2-bot-worker").start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)), debug=False)
