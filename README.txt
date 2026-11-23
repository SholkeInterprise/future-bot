CoinSwitch HFT SOL one-order test dashboard

Render settings required: COINSWITCH_API_KEY, COINSWITCH_SECRET_KEY, DASHBOARD_PASSWORD. Keep HFT_LIVE_ORDERS=NO while reviewing the dashboard. After verifying HFT account free USDT and the account position mode, set HFT_LIVE_ORDERS=YES to enable the manually confirmed single SOLUSDT IOC limit test. HFT_MANUAL_BALANCE_USDT is only a manually observed value and is not live-verified. Default test notional cap is 5 USDT (HFT_TEST_MAX_NOTIONAL_USDT). No automatic trading, retry, TP/SL or auto-close. Start command: gunicorn bot:app
