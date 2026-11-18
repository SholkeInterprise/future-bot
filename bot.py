import os
import time
import threading
import requests
import random
from collections import deque
from flask import Flask, jsonify, request

app = Flask(__name__)

# ===== SETTINGS =====
WALLET_START = 10000.0
API_KEY = os.environ.get("COINSWITCH_API_KEY", "")
API_SECRET = os.environ.get("COINSWITCH_API_SECRET", "")
LIVE_MODE = bool(API_KEY and API_SECRET)

state = {
    "balance": WALLET_START,
    "locked": 0.0,
    "open_trade": None,
    "history": [],
    "auto": False,
    "amount": 150.0,
    "max_amount": 150.0,
    "leverage": 10,
    "take_profit": 0.25,
    "stop_loss": 0.50,
    "strategy": "BALANCED",
    "coin": "BTC/INR",
    "total_trades": 0,
    "total_profit": 0.0,
    "win": 0,
    "loss": 0,
    "mode": "LIVE" if LIVE_MODE else "PAPER"
}

price_history = deque(maxlen=100)
for i in range(50):
    price_history.append(6600000 + random.randint(-5000, 5000))

def get_btc_price():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
        usdt = float(r.json()["price"])
        p = usdt * 88000
        price_history.append(p)
        return p
    except:
        try:
            r = requests.get("https://api.coinswitch.co/v2/ticker?symbol=BTC/INR", timeout=5)
            p = float(r.json()["data"]["last_price"])
            price_history.append(p)
            return p
        except:
            p = price_history[-1] + random.randint(-200, 200)
            price_history.append(p)
            return p

def calc_ema(prices, period):
    if len(prices) < period:
        return sum(prices)/len(prices) if prices else 0
    k = 2/(period+1)
    ema = prices[0]
    for pr in prices[1:]:
        ema = pr*k + ema*(1-k)
    return ema

def calc_rsi(prices, period=14):
    if len(prices) < period+1:
        return 50.0
    gains = 0
    losses = 0
    for i in range(1, period+1):
        d = prices[-i] - prices[-i-1]
        if d > 0:
            gains += d
        else:
            losses += -d
    if losses == 0:
        return 100.0
    rs = (gains/period)/(losses/period)
    return 100 - (100/(1+rs))

def decide_signal():
    prices = list(price_history)
    if len(prices) < 20:
        return "HOLD", 50, 0, 0
    ema9 = calc_ema(prices, 9)
    ema21 = calc_ema(prices, 21)
    rsi = calc_rsi(prices)
    last = prices[-1]
    momentum = (last - prices[-5])/prices[-5]*100
    # Strategy logic
    if state["strategy"] == "SAFE":
        if ema9 > ema21 and rsi < 65 and momentum > 0.01:
            return "LONG", rsi, ema9, ema21
        if ema9 < ema21 and rsi > 35 and momentum < -0.01:
            return "SHORT", rsi, ema9, ema21
    elif state["strategy"] == "AGGRESSIVE":
        if momentum > 0.005 or (ema9 > ema21 and rsi < 70):
            return "LONG", rsi, ema9, ema21
        if momentum < -0.005 or (ema9 < ema21 and rsi > 30):
            return "SHORT", rsi, ema9, ema21
    else: # BALANCED
        if ema9 > ema21 and rsi < 70 and momentum > 0:
            return "LONG", rsi, ema9, ema21
        if ema9 < ema21 and rsi > 30 and momentum < 0:
            return "SHORT", rsi, ema9, ema21
    return "HOLD", rsi, ema9, ema21

def close_trade(reason):
    t = state["open_trade"]
    if not t:
        return
    price = get_btc_price()
    entry = t["entry"]
    amt = t["amount"]
    lev = t["leverage"]
    if t["side"] == "LONG":
        pnl_pct = (price - entry)/entry*100*lev
    else:
        pnl_pct = (entry - price)/entry*100*lev
    pnl = amt * pnl_pct/100
    fee = amt * 0.001
    net = pnl - fee
    state["balance"] += amt + net
    state["locked"] = 0.0
    state["total_profit"] += net
    if net >= 0:
        state["win"] += 1
    else:
        state["loss"] += 1
    state["history"].insert(0, {
        "pair": state["coin"], "side": t["side"],
        "entry": round(entry, 2), "exit": round(price, 2),
        "amount": amt, "leverage": lev,
        "pnl": round(net, 2), "reason": reason,
        "time": time.strftime("%H:%M:%S")
    })
    state["open_trade"] = None

def open_trade(side):
    if state["open_trade"]:
        return {"error": "Trade already open"}
    amt = float(state["amount"])
    if amt > state["balance"]:
        return {"error": "Low balance"}
    price = get_btc_price()
    state["balance"] -= amt
    state["locked"] = amt
    state["open_trade"] = {"side": side, "entry": price, "amount": amt, "leverage": int(state["leverage"]), "time": time.strftime("%H:%M:%S"), "coin": state["coin"]}
    state["total_trades"] += 1
    return {"success": True, "entry": price}

def auto_loop():
    while True:
        try:
            if state["auto"]:
                price = get_btc_price()
                if state["open_trade"]:
                    t = state["open_trade"]
                    if t["side"] == "LONG":
                        ch = (price - t["entry"])/t["entry"]*100*t["leverage"]
                    else:
                        ch = (t["entry"] - price)/t["entry"]*100*t["leverage"]
                    if ch >= float(state["take_profit"]):
                        close_trade("TP Hit")
                    elif ch <= -float(state["stop_loss"]):
                        close_trade("SL Hit")
                else:
                    sig, rsi, e9, e21 = decide_signal()
                    if sig in ["LONG", "SHORT"]:
                        open_trade(sig)
            time.sleep(8)
        except Exception as e:
            print("Loop error", e)
            time.sleep(5)

threading.Thread(target=auto_loop, daemon=True).start()
