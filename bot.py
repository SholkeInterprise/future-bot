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

