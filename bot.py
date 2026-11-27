with open("bot_v2_render.py", "r", encoding="utf-8") as f:
    src = f.read()

old_api_data = '''@app.route("/api/data")
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
        }), 502'''

new_api_data = '''@app.route("/api/data")
def api_data():
    coin = request.args.get("coin", "SOL").upper()
    interval = request.args.get("interval", "1m")
    if coin not in COINS:
        coin = "SOL"

    price, change = 0.0, 0.0
    candles = []
    errors = []

    try:
        price, change = get_price(coin)
    except Exception as exc:
        errors.append(f"Price: {exc}")

    try:
        candles = get_candles(coin, interval)
        if candles and price > 0:
            candles[-1]["close"] = price
            candles[-1]["high"] = max(candles[-1]["high"], price)
            candles[-1]["low"] = min(candles[-1]["low"], price)
        elif candles and price == 0:
            price = float(candles[-1]["close"])
    except Exception as exc:
        errors.append(f"Candles: {exc}")

    wallet = {"available": 0.0, "blocked": 0.0, "total": 0.0}
    try:
        wallet = wallet_balances()
    except Exception as exc:
        errors.append(f"Wallet: {exc}")

    usdt_inr, fx_source = 0.0, "unavailable"
    try:
        usdt_inr, fx_source = get_usdt_inr_rate()
    except Exception as exc:
        errors.append(f"FX: {exc}")

    pos = None
    try:
        pos = get_position(coin)
    except Exception as exc:
        errors.append(f"Position: {exc}")

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
    err_text = " | ".join(errors) if errors else STATE.get("last_error")

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
        "error": err_text,
    })'''

old_fetch_js = '''async function fetchData(){
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
}'''

new_fetch_js = '''async function fetchData(){
  try{
    let r=await fetch('/api/data?coin='+cur+'&interval='+tf,{cache:'no-store'});
    let d=await r.json();
    if(Array.isArray(d.candles) && d.candles.length) lastCandles=d.candles;
    if(typeof d.price_usdt==='number' && d.price_usdt>0) lastPrice=d.price_usdt;
    curPos=d.pos_obj||null;
    curEntry=d.entry_usdt||0;
    let inrStr=d.usdt_inr?'₹ '+(lastPrice*d.usdt_inr).toLocaleString('en-IN',{maximumFractionDigits:2}):'₹ --';
    document.getElementById('p').innerText=cur+' : '+lastPrice.toFixed(4)+' USDT | '+inrStr+' | '+(d.position||'')+' | 24h '+(d.change||0)+'%';
    document.getElementById('bal').innerText='💰 Futures V2 Available: '+Number(d.balance||0).toFixed(4)+' USDT | ₹ '+(d.balance_inr==null?'--':Number(d.balance_inr).toLocaleString('en-IN',{maximumFractionDigits:2}));
    document.getElementById('locked').innerText='🔒 Locked: '+Number(d.locked||0).toFixed(4)+' USDT | Total: '+Number(d.total||0).toFixed(4)+' USDT';
    document.getElementById('fx').innerText='1 USDT = ₹ '+(d.usdt_inr||'Unavailable')+' | '+(d.fx_source||'')+(d.error?(' | ⚠️ '+d.error):'');
    document.getElementById('pos').innerText=d.pos_msg||'No Position';
    document.getElementById('trailInfo').innerText=d.trail_msg||'Trailing / Candle Target: -';
    let p=document.getElementById('pnl');
    p.innerText='Live Unrealised PnL: '+(d.live_pnl||0)+' USDT ('+(d.live_pct||0)+'%) | ₹ '+(d.live_pnl_inr==null?'--':d.live_pnl_inr);
    p.style.color=(d.live_pnl||0)>=0?'green':'red';
    if(Array.isArray(d.history)) document.getElementById('h').innerHTML=d.history.map(x=>'<div>'+x+'</div>').join('');
    drawChart();
    updateTpSlPreview();
  }catch(e){
    document.getElementById('fx').innerText='⚠️ Fetch error: '+e.message;
  }
}'''

assert old_api_data in src, "old_api_data not found"
assert old_fetch_js in src, "old_fetch_js not found"

updated_src = src.replace(old_api_data, new_api_data, 1).replace(old_fetch_js, new_fetch_js, 1)

with open("bot_v2_render.py", "w", encoding="utf-8") as f:
    f.write(updated_src)

print("Updated bot_v2_render.py successfully!")
