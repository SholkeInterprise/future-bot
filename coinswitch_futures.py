"""
CoinSwitch PRO — Futures (perpetual) trading client (Python).

Single-file client with every Futures REST endpoint as a method.
Symbols use the unseparated form `BTCUSDT`, `DOGEUSDT`. Exchange is always
`EXCHANGE_2`.

Requirements:
    pip install cryptography requests

Usage:
    from coinswitch_futures import FuturesClient, EXCHANGE

    cf = FuturesClient(api_key="<key>", secret_key="<secret>")

    print(cf.wallet_balance())
    print(cf.open_orders(symbol="DOGEUSDT"))

    placed = cf.place_order(
        symbol="DOGEUSDT", side="BUY",
        order_type="LIMIT", price=0.28, quantity=22,
        client_order_id="my-uuid",
    )
"""

from __future__ import annotations

import json
import time
import urllib.parse
import uuid
from typing import Any, Optional

import requests
from cryptography.hazmat.primitives.asymmetric import ed25519


BASE_URL_DEFAULT = "https://coinswitch.co"
EXCHANGE = "EXCHANGE_2"


class FuturesClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        base_url: str = BASE_URL_DEFAULT,
        request_timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.timeout = request_timeout
        self._secret = ed25519.Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(secret_key)
        )
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sign(self, method: str, path: str, params: Optional[dict] = None):
        method = method.upper()
        if params:
            sep = "&" if "?" in path else "?"
            path = path + sep + urllib.parse.urlencode(params)
        decoded_path = urllib.parse.unquote_plus(path)

        epoch = str(int(time.time() * 1000))
        message = method + decoded_path + epoch
        signature = self._secret.sign(message.encode("utf-8")).hex()

        headers = {
            "Content-Type": "application/json",
            "X-AUTH-APIKEY": self.api_key,
            "X-AUTH-SIGNATURE": signature,
            "X-AUTH-EPOCH": epoch,
        }
        return headers, decoded_path

    def _request(self, method: str, path: str, *,
                 params: Optional[dict] = None,
                 body: Optional[dict] = None) -> Any:
        headers, decoded_path = self._sign(method, path, params)
        url = self.base_url + decoded_path
        kwargs: dict = {"headers": headers, "timeout": self.timeout}
        if body is not None:
            kwargs["data"] = json.dumps(body)
        r = self._session.request(method.upper(), url, **kwargs)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def wallet_balance(self) -> Any:
        return self._request("GET", "/trade/api/v2/futures/wallet_balance")

    def transactions(
        self,
        *,
        symbol: Optional[str] = None,
        type: Optional[str] = None,
        limit: Optional[int] = None,
        from_time: Optional[int] = None,
        to_time: Optional[int] = None,
    ) -> Any:
        params: dict = {"exchange": EXCHANGE}
        if symbol: params["symbol"] = symbol
        if type:   params["type"] = type
        if limit is not None: params["limit"] = limit
        if from_time is not None: params["from_time"] = from_time
        if to_time   is not None: params["to_time"] = to_time
        return self._request("GET", "/trade/api/v2/futures/transactions", params=params)

    # ------------------------------------------------------------------
    # Reference data
    # ------------------------------------------------------------------

    def instrument_info(self, symbol: Optional[str] = None) -> Any:
        params: dict = {"exchange": EXCHANGE}
        if symbol: params["symbol"] = symbol
        return self._request("GET", "/trade/api/v2/futures/instrument_info", params=params)

    def order_book(self, symbol: str) -> Any:
        return self._request(
            "GET", "/trade/api/v2/futures/order_book",
            params={"exchange": EXCHANGE, "symbol": symbol},
        )

    def ticker(self, symbol: str) -> Any:
        return self._request(
            "GET", "/trade/api/v2/futures/ticker",
            params={"exchange": EXCHANGE, "symbol": symbol},
        )

    def all_pairs_ticker(self) -> Any:
        return self._request(
            "GET", "/trade/api/v2/futures/all-pairs/ticker",
            params={"exchange": EXCHANGE},
        )

    def klines(self, *, symbol: str, interval: str,
               start_time: int, end_time: int) -> Any:
        return self._request(
            "GET", "/trade/api/v2/futures/klines",
            params={
                "exchange": EXCHANGE, "symbol": symbol,
                "interval": interval,
                "start_time": start_time, "end_time": end_time,
            },
        )

    def market_trades(self, symbol: str) -> Any:
        return self._request(
            "GET", "/trade/api/v2/futures/trades",
            params={"exchange": EXCHANGE, "symbol": symbol},
        )

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        price: Optional[float] = None,
        quantity: float,
        trigger_price: Optional[float] = None,
        reduce_only: Optional[bool] = None,
        time_in_force: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> Any:
        """
        Place a futures order. order_type: LIMIT / MARKET / TAKE_PROFIT_MARKET /
        STOP_MARKET. For TP/SL, send quantity=0 and reduce_only=True.

        client_order_id is the idempotency key — re-send the same value on
        retry to avoid double-placement.
        """
        body: dict = {
            "exchange": EXCHANGE,
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "quantity": quantity,
        }
        if price is not None:          body["price"] = price
        if trigger_price is not None:  body["trigger_price"] = trigger_price
        if reduce_only is not None:    body["reduce_only"] = reduce_only
        if time_in_force is not None:  body["time_in_force"] = time_in_force
        body["client_order_id"] = client_order_id or str(uuid.uuid4())
        return self._request("POST", "/trade/api/v2/futures/order", body=body)

    def cancel_order(self, order_id: str) -> Any:
        return self._request(
            "DELETE", "/trade/api/v2/futures/order",
            body={"exchange": EXCHANGE, "order_id": order_id},
        )

    def cancel_all_open_orders(self, symbol: Optional[str] = None) -> Any:
        body: dict = {"exchange": EXCHANGE}
        if symbol: body["symbol"] = symbol
        return self._request(
            "POST", "/trade/api/v2/futures/cancel-all", body=body,
        )

    def get_order_status(self, order_id: str) -> Any:
        return self._request(
            "GET", "/trade/api/v2/futures/order",
            params={"exchange": EXCHANGE, "order_id": order_id},
        )

    def open_orders(self, *, symbol: Optional[str] = None,
                    limit: Optional[int] = None) -> Any:
        body: dict = {"exchange": EXCHANGE}
        if symbol: body["symbol"] = symbol
        if limit is not None: body["limit"] = limit
        return self._request("POST", "/trade/api/v2/futures/orders/open", body=body)

    def closed_orders(self, *, symbol: Optional[str] = None,
                      count: Optional[int] = None) -> Any:
        body: dict = {"exchange": EXCHANGE}
        if symbol: body["symbol"] = symbol
        if count is not None: body["count"] = count
        return self._request("POST", "/trade/api/v2/futures/orders/closed", body=body)

    # ------------------------------------------------------------------
    # Positions, leverage, margin
    # ------------------------------------------------------------------

    def positions(self, symbol: Optional[str] = None) -> Any:
        params: dict = {"exchange": EXCHANGE}
        if symbol: params["symbol"] = symbol
        return self._request("GET", "/trade/api/v2/futures/positions", params=params)

    def get_leverage(self, symbol: str) -> Any:
        return self._request(
            "GET", "/trade/api/v2/futures/leverage",
            params={"exchange": EXCHANGE, "symbol": symbol},
        )

    def update_leverage(self, *, symbol: str, leverage: int) -> Any:
        return self._request(
            "POST", "/trade/api/v2/futures/leverage",
            body={"exchange": EXCHANGE, "symbol": symbol, "leverage": leverage},
        )

    def add_margin(self, *, symbol: str, margin: float) -> Any:
        return self._request(
            "POST", "/trade/api/v2/futures/add_margin",
            body={"exchange": EXCHANGE, "symbol": symbol, "margin": margin},
        )


if __name__ == "__main__":
    cf = FuturesClient(api_key="<your api key>", secret_key="<your secret key>")
    # Replace with real keys for a live smoke test:
    # print(cf.wallet_balance())
