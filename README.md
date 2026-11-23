# CoinSwitch USDT Candle HFT Bot

## Strategy implemented
- USDT-settled `linear` HFT contracts only.
- Locally aggregates REST ticker snapshots into candles; supported chart/timeframes: 1m, 3m, 5m, 15m, 30m, 1h, 4h.
- Candle range filter: `(high-low)/low*100 >= configured threshold` (default 40%).
- SAME ACTIVE CANDLE entry: while the candle is forming, after its running high-low range passes the configured filter, a fresh print at the running high triggers market SHORT; a fresh print at the running low triggers market LONG.
- Candle-range TP is recalculated from the active candle: SHORT target = current low + (current high-current low)*TP%; LONG target = current high - (current high-current low)*TP%. Default TP%=10.
- Adjustable fixed SL (default 1%), trailing stop (default 5%), margin, leverage, timeframe, range filter, spread filter, selected symbol / all symbols, and long/short/both mode.
- Single open position guard; no averaging/martingale.

## Important execution semantics
The bot cannot guarantee an exact fill at the candle high/low using a market order. It detects a fresh extreme in sampled ticker data and submits a market order; actual fill can slip. Candles are reconstructed from periodic ticker snapshots, not native exchange OHLC data. Missed ticker updates can omit wicks. A 40% range filter means 40% of the candle low, an unusually large movement filter; if you intended 40% of some other measure, change it in dashboard.

## Live safety
This deployment template sets `ENABLE_LIVE_TRADING=true` and `LIVE_CONFIRMATION=I_UNDERSTAND_REAL_ORDERS`; however `AUTO_TRADE=false` by default. After adding credentials and deploying, switch Auto Trading ON in the dashboard to submit real orders. You can set `AUTO_TRADE=true` in Render if you explicitly want it to start trading on boot. The bot does not transfer funds. It does not provide exchange-hosted protective stop orders; SL/TP/trailing exits are monitored by this running process. If Render sleeps, restarts, loses network, or API fails, software exits may not execute. Keep position size small and do not leave unattended.

## Deploy
1. Upload this folder to a GitHub repository.
2. Create a Render Web Service from the repo or use the included `render.yaml`.
3. Add API key and Ed25519 private secret as Render Environment variables; never put keys in source or share them in chat.
4. Confirm account is HFT USDT settlement and available USDT margin.
5. Keep live switches off for initial launch; inspect `/`, `/api/status`, and Render logs.
6. To arm real orders, explicitly set both live variables and enable Auto Trading on dashboard.

This is a strategy implementation, not financial advice or a profit guarantee. HFT order acceptance is not proof of fill; positions are reconciled from the HFT positions endpoint.
