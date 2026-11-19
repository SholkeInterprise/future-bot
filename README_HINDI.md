# Future Bot — CoinSwitch PRO Futures (USDT)

## सुरक्षा
- Default `LIVE_TRADING=NO` और `AUTO_TRADING=NO` है। पहले API/account read test और paper/demo validation करें।
- CoinSwitch API key को केवल आवश्यक trading permissions दें; withdrawal permission कभी न दें।
- API key/secret को GitHub में नहीं, Render → Environment में रखें।
- `DASHBOARD_PASSWORD` अनिवार्य है; इसके बिना dashboard 503 देगा।
- Auto strategy कोड में शामिल है, लेकिन `AUTO_TRADING=YES` और UI में AUTO ON दोनों जरूरी हैं। शुरुआत में इसे OFF रखें।
- Live futures में liquidation, slippage, funding और fees का जोखिम है। कोई fixed profit नहीं है।

## GitHub
1. इस folder की सभी files अपने `future-bot` repository में upload/replace करें।
2. `bot.py`, `requirements.txt`, `Procfile`, `render.yaml`, `.gitignore` शामिल हों।
3. `.env.example` में असली key न डालें।

## Render
1. Render में New → Web Service → अपना GitHub repository चुनें।
2. Build command: `pip install -r requirements.txt`
3. Start command: `gunicorn --workers 1 --threads 4 --timeout 120 bot:app`
4. Environment variables जोड़ें: `DASHBOARD_USER`, `DASHBOARD_PASSWORD`, `COINSWITCH_API_KEY`, `COINSWITCH_SECRET_KEY`।
5. पहले `LIVE_TRADING=NO`, `AUTO_TRADING=NO` रखें।
6. `/healthz` और dashboard खोलकर data/auth जांचें।

## Live enable करने से पहले
CoinSwitch API docs/account पर supported symbols, instrument precision, leverage limits, wallet balance, position response और attached reduce-only TP/SL की पुष्टि करें। `bot.py` अभी API schema के अनुसार तैयार starter है; इसे आपके CoinSwitch account पर end-to-end live execution के रूप में प्रमाणित नहीं किया गया है। पहले minimum size से भी कम जोखिम वाले test / exchange sandbox यदि उपलब्ध हो तो उसी में जाँचें। जब तक order fill, position sync और SL/TP verification सफल न हों, `LIVE_TRADING=NO` ही रखें।
