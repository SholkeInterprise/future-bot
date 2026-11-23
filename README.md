# CoinSwitch PRO HFT Candle Bot — guarded test build

## Included in this build
- CoinSwitch HFT `linear` USDT perpetual REST client.
- Chart intervals: 1m, 3m, 5m, 15m, 30m, 1h, 4h and 1d.
- Timeframe config bug fixed (timeframe remains a string; `1.0` no longer fails validation).
- Live open-position data from `/v5/position/list` and recent fills from `/v5/execution/list` (latest 50 executions for selected symbol).
- Manual Long, Short and Close controls. Manual entry uses market orders and checks existing positions. Margin, leverage and the other strategy risk settings remain adjustable in the dashboard; the starting defaults are 10 USDT margin and 1x leverage, and leverage validation retains the previous 20x ceiling. Set a small margin and low leverage yourself for the first live test.
- Dashboard P/L uses the exchange-reported unrealised PnL in USDT. Enter a current USDT/INR conversion rate in settings to display the INR equivalent and INR chart/ohlc prices. This rate is manually supplied; it is not a live FX feed.
- Live order submission is locked by default in `render.yaml`.

## Important safety limitation: automatic OCO is disabled
The strategy request requires two resting limit orders (short at the active candle high and long at the active candle low), followed by cancelling the sibling as soon as one side fills. The currently published CoinSwitch HFT reference confirms limit order creation, order-status polling, positions and execution history, but does not document an HFT cancel-order endpoint. This build therefore **does not submit automatic OCO entry orders**. The Auto Trading checkbox is deliberately rejected by the API. Do not bypass this guard by editing code or guessing an endpoint; obtain the current HFT cancel-order specification from CoinSwitch first.

## Manual live test steps
1. Deploy with `ENABLE_LIVE_TRADING=false` first. Add API credentials in Render Environment and verify dashboard/status, selected symbol and execution-history feed.
2. Set a long random `MANUAL_TRADE_TOKEN` in Render. Enter the same token in the dashboard before using a manual button. The token prevents unauthorised visitors from triggering manual orders.
3. Confirm the CoinSwitch HFT account is funded in the correct settlement currency and check the exchange minimum order notional for the chosen contract. The bot does not transfer funds.
4. Enter the current USDT/INR rate in the dashboard if INR display is desired. It is a manual conversion input, not an exchange-sourced live rate.
5. Only when ready for a manual live test, set `ENABLE_LIVE_TRADING=true` and `LIVE_CONFIRMATION=I_UNDERSTAND_REAL_ORDERS` in Render and redeploy. Keep Auto Trading OFF.
6. Select one symbol. The manual button asks for confirmation and calculates size from the configured margin and leverage. Choose a small margin and low leverage for the first test; there is no forced 5-USDT margin cap. Exchange minimum notional/quantity still applies and may make a symbol ineligible for such a small test.
7. After entry, verify the open position and execution history in the dashboard and CoinSwitch. Use Close Position to send a reduce-only market close. Confirm the position is actually flat at the exchange; an accepted order response is not proof of a fill.
8. After testing, set `ENABLE_LIVE_TRADING=false` again if you do not want further live manual orders.

## Execution caveats
- Manual Long/Short/Close are market orders and can slip.
- The candle chart is reconstructed from REST ticker snapshots, not exchange-native candle websocket data; intrabar wicks can be missed.
- Position P/L shown is the exchange's unrealised P/L in USDT, converted using the manually entered FX rate when provided.
- Recent fill history is sourced from HFT Execution List; it is execution/fill history, not a fully reconstructed round-trip position ledger.
- Software-managed exits are not exchange-hosted protective stops. Render restarts, outages, API errors or network loss can interrupt software monitoring. Do not leave a position unattended.

## Deploy
1. Upload this folder to GitHub and create/update the Render Web Service.
2. Add `COINSWITCH_API_KEY` and `COINSWITCH_SECRET_KEY` as Render environment variables. Never put credentials in source or chat.
3. Keep live order variables false until dashboard and API connectivity are checked.
4. See the manual live test steps above. Do not turn Auto Trading on; automatic OCO is intentionally locked in this build.


## Exchange-driven quantity and leverage rules

On startup the bot reads each USDT linear contract's live `instruments-info` metadata. Before a manual order (and any enabled entry path), it uses the symbol's `minOrderQty`, `qtyStep`, `minNotionalValue`, `minLeverage`, `maxLeverage`, and `leverageStep`. If the configured leverage exceeds that coin's maximum, it is clamped down. If the configured margin and leverage would produce less than the exchange minimum, leverage is raised only as far as needed (within the coin's max and step). If even maximum leverage cannot meet the exchange minimum, the order is blocked and the dashboard/API returns the reason.

The dashboard shows the selected coin's minimum quantity, quantity step, minimum notional and leverage range. Exchange rules are fetched from CoinSwitch HFT `/v5/market/instruments-info`; values can change, so the bot re-fetches metadata for manual orders before sizing.


## Fix in this revision

- Fixed the worker startup failure `HFT 10001: Missing some parameters ... symbol or settleCoin`. The `/v5/position/list` request now includes `settleCoin=USDT` together with `category=linear`, allowing the worker to query all USDT-settled positions.
- This fixes the specific missing-parameter error shown in the Render logs. It does not prove that the account credentials, settlement configuration, or every other HFT endpoint are valid; verify the status endpoint and logs after redeployment before enabling live orders.
