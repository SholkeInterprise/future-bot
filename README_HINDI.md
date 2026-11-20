# Binance Futures Bot — CoinSwitch bot wali strategy

यह प्रोजेक्ट आपके पुराने CoinSwitch Futures bot का Binance USDⓈ-M Futures API पर migration है। Dashboard, BTC/ETH/SOL/DOGE/XRP/ADA/BNB selection, candle chart, timeframes, line tools, INR display, LONG/SHORT/CLOSE controls और High-Low auto strategy को रखा गया है।

## सुरक्षा — अभी Live Trading बंद है

डिफॉल्ट सेटिंग:
- `BINANCE_TESTNET=NO` — Binance का LIVE Futures account data पढ़ेगा।
- `LIVE_TRADING=NO` — कोई वास्तविक order नहीं भेजेगा।
- `AUTO_TRADING=NO` — auto trading बंद रहेगी।

इस मोड में Dashboard account/wallet/position/market data पढ़ने के लिए API credentials इस्तेमाल करेगा। Trade endpoints call नहीं होंगे। इसलिए अभी Manual LONG/SHORT/CLOSE और Auto strategy का वास्तविक execution locked है।

## GitHub / Render

Repository root में ये files रखें: `bot.py`, `requirements.txt`, `Procfile`, `render.yaml`, `.env.example`, `README_HINDI.md`.

Render Web Service:
- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn --workers 1 --threads 4 --timeout 120 bot:app`
- Health path: `/healthz`

## Render Environment Variables

| Variable | Value |
|---|---|
| `BINANCE_TESTNET` | `NO` |
| `LIVE_TRADING` | `NO` |
| `AUTO_TRADING` | `NO` |
| `BINANCE_API_KEY` | Binance API key |
| `BINANCE_API_SECRET` | Binance API secret |
| `DASHBOARD_USER` | अपना dashboard username |
| `DASHBOARD_PASSWORD` | मजबूत और अलग password |
| `MAX_MARGIN_USDT` | `25` |
| `USDT_INR_RATE` | Optional fallback rate; सामान्यतः खाली रखें |

**API key सुरक्षा:** Binance API में Futures account का Read permission enable करें ताकि wallet/positions पढ़े जा सकें। Withdrawals permission बंद रखें। API key/secret को GitHub code में या ChatGPT chat में न डालें; केवल Render Environment में डालें।

## Binance conditional TP / SL / Trailing

इस version में TP, SL और `TRAILING_STOP_MARKET` को Binance USDⓈ-M Futures Algo Order REST API (`POST /fapi/v1/algoOrder`) के जरिए place करने का implementation है। TP/SL `closePosition=true` के साथ position-level close-all conditional orders हैं। Trailing stop `activatePrice`, `callbackRate` और position quantity के साथ reduce-only है। Close होने पर symbol के pending Algo orders cancel किए जाते हैं।

Binance USDⓈ-M Futures API में TP/SL/trailing conditional order types Algo Service पर migrate हो चुके हैं; इन्हें पुराने `/fapi/v1/order` endpoint से नहीं भेजना चाहिए। API docs: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order

## INR display note

Binance USDⓈ-M Futures में account margin, order quantity और settlement USDT में होते हैं। Dashboard INR conversion केवल display/amount input के लिए है; यह INR-settled futures account नहीं है। USDT/INR conversion indicative rate है और बदल सकता है।

## जरूरी सावधानियाँ

- `LIVE_TRADING=NO` रखें जब तक read-only dashboard, balance, position, symbol precision, and strategy review पूरा न हो।
- जब भी बाद में live orders चालू करने का निर्णय लें, पहले Binance Demo/Testnet पर entry, reduce-only close, TP, SL और trailing stop को अलग-अलग verify करें। उसके बाद ही `LIVE_TRADING=YES` पर विचार करें; Auto के लिए अलग से `AUTO_TRADING=YES` चाहिए।
- यह profitability की guarantee नहीं है। Futures में leverage, liquidation, fees, funding और slippage का जोखिम है।
- इस version में account position mode One-way होना चाहिए; Hedge Mode को order भेजने से पहले reject किया जाता है।
- Render restart होने पर in-memory dashboard history reset हो सकती है। Binance exchange-side Algo orders exchange पर रहते हैं।
