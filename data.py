"""
Language: Python3

Step1: Install Requirements
pip3 install pynacl
pip3 install requests
pip3 install python-dotenv

Step2: Create .env file with contents
API_KEY=apikey
API_SECRET=secretkey

"""

import json
import os
import time
import urllib.parse
import uuid
from urllib.parse import urlparse, urlencode

import requests
from dotenv import load_dotenv

# Conditional import for pynacl (which provides ed25519 functionality)
try:
    from nacl.signing import SigningKey
except ImportError:
    print("pynacl library not found. Please install it with: pip install pynacl")
    SigningKey = None


class DMABybit:
    def __init__(self, api_key, api_secret, symbol, category="linear"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.coinswitch_url = "https://dma.coinswitch.co"
        self.category = category
        self.symbol = symbol

    def _generate_coinswitch_signature(self, method, endpoint, params=None):
        """Generate signature for Coinswitch API requests using pynacl"""
        if not SigningKey:
            raise ImportError("pynacl library is required for Coinswitch signature generation")

        params = params or {}
        epoch_time = str(int(time.time()))

        # Modify endpoint for GET requests with params
        unquote_endpoint = endpoint
        if method == "GET" and params:
            endpoint += ('&', '?')[urlparse(endpoint).query == ''] + urlencode(params)
            unquote_endpoint = urllib.parse.unquote_plus(endpoint)

        signature_msg = method + unquote_endpoint + epoch_time

        # Generate signature using pynacl
        secret_key_bytes = bytes.fromhex(self.api_secret)
        signing_key = SigningKey(secret_key_bytes)
        signed = signing_key.sign(signature_msg.encode('utf-8'))
        signature = signed.signature.hex()

        return {
            'X-AUTH-SIGNATURE': signature,
            'X-AUTH-APIKEY': self.api_key,
            'X-AUTH-EPOCH': epoch_time
        }

    def _prepare_request(self, endpoint, method='GET', params=None, body=None):
        """Prepare and send Coinswitch API request"""
        # Prepare full URL
        full_url = f"{self.coinswitch_url}{endpoint}"

        # Get Coinswitch signature headers
        signature_headers = self._generate_coinswitch_signature(method, endpoint, params)

        # Prepare headers
        headers = {
            'Content-Type': 'application/json',
            **signature_headers
        }

        try:
            if method == 'GET':
                response = requests.get(full_url, headers=headers, params=params)
            elif method == 'POST':
                response = requests.post(full_url, headers=headers, json=body)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            # Check and return response
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            print(f"Request error: {e}")
            print(f"Response content: {e.response.text if hasattr(e, 'response') else 'No response'}")
            return None

    def transfer_funds(self, direction, amount= 4000):
        """Funds Transfer"""
        endpoint = "/dma/api/v1/funds/transfer"
        body = {
            "client_txn_id": str(uuid.uuid4()),
            "direction": "OUT",
            "amount": amount,
            "quote_asset":"INR"
        }
        return self._prepare_request(endpoint, method='POST', body=body)

    def set_leverage(self, leverage):
        """Set leverage for the symbol"""
        endpoint = "/v5/position/set-leverage"
        body = {
            "symbol": self.symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
            "category": self.category
        }
        return self._prepare_request(endpoint, method='POST', body=body)

    def switch_isolated(self, trade_mode, leverage):
        """switch_isolated for the symbol"""
        endpoint = "/v5/position/switch-isolated"
        body = {
            "symbol": self.symbol,
            "tradeMode": trade_mode,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
            "category": self.category
        }
        return self._prepare_request(endpoint, method='POST', body=body)

    def switch_position_mode(self, mode, switch_all_symbols=True, coin="USDT"):
        """switch_position_mode for the symbol"""
        endpoint = "/v5/position/switch-mode"
        body = {
            "symbol": self.symbol,
            "mode": mode,
            "coin": coin,
            "category": self.category
        }

        if switch_all_symbols:
            body.pop('symbol')

        return self._prepare_request(endpoint, method='POST', body=body)

    def place_order(self, body):
        """Place a market order"""
        endpoint = "/v5/order/create"

        body['category'] = self.category
        if not body.get('symbol'):
            body['symbol'] = self.symbol
        if not body.get('orderLinkId'):
            body['orderLinkId'] = str(uuid.uuid4())

        print("place order body", body)
        return self._prepare_request(endpoint, method='POST', body=body)

    def get_order_details(self, order_link_id):
        """Retrieve specific order details"""
        endpoint = "/v5/order/realtime"
        params = {
            'symbol': self.symbol,
            'orderLinkId': order_link_id,
            'category': self.category
        }
        return self._prepare_request(endpoint, params=params)

    def get_open_positions(self):
        """Get open positions for the symbol"""
        endpoint = "/v5/position/list"
        params = {
            'symbol': self.symbol,
            'category': self.category
        }
        return self._prepare_request(endpoint, params=params)

    def add_margin(self, margin_amount=1):
        """Add margin to the position"""
        endpoint = "/v5/position/add-margin"
        body = {
            "symbol": self.symbol,
            "margin": str(margin_amount),
            "category": self.category
        }
        return self._prepare_request(endpoint, method='POST', body=body)

    def remove_margin(self, margin_amount=1):
        """Remove margin from the position"""
        endpoint = "/v5/position/add-margin"
        body = {
            "symbol": self.symbol,
            "margin": str(-margin_amount),
            "category": self.category
        }
        return self._prepare_request(endpoint, method='POST', body=body)

    def get_trades(self, order_link_id):
        """Get trades for a specific order"""
        endpoint = "/v5/execution/list"
        params = {
            'symbol': self.symbol,
            'orderLinkId': order_link_id,
            'category': self.category
        }
        return self._prepare_request(endpoint, params=params)

    def get_account_info(self):
        """Get account information from /v5/account/info"""
        endpoint = "/v5/account/info"
        return self._prepare_request(endpoint, method='GET')

    def get_wallet_balance(self, account_type="UNIFIED", coin=None):
        """Get wallet balance from /v5/account/wallet-balance

        Docs: https://bybit-exchange.github.io/docs/v5/account/wallet-balance

        :param account_type: "UNIFIED" (required by Bybit v5; the funding wallet
                             has its own endpoint and is not covered here).
        :param coin: optional coin filter, uppercase. One ("USDT") or several
                     comma-separated ("USDT,USDC"). Omit for all non-zero assets.
                     A list/tuple is accepted and joined for you.
        """
        endpoint = "/v5/account/wallet-balance"
        params = {"accountType": account_type}
        if coin:
            if isinstance(coin, (list, tuple, set)):
                coin = ",".join(coin)
            # No spaces around the comma -- they would break the signed query string.
            params["coin"] = ",".join(c.strip().upper() for c in coin.split(",") if c.strip())
        return self._prepare_request(endpoint, method='GET', params=params)

    def get_coin_balance(self, coin, account_type="UNIFIED"):
        """Return just the per-coin dict for one coin, or None if absent.

        Convenience wrapper over get_wallet_balance for the common
        "how much USDT do I have" case.
        """
        response = self.get_wallet_balance(account_type=account_type, coin=coin) or {}
        for account in response.get("result", {}).get("list", []):
            for entry in account.get("coin", []):
                if entry.get("coin", "").upper() == coin.strip().upper():
                    return entry
        return None

    def set_margin_mode(self, margin_mode):
        """
        Set margin mode for the account.
        :param margin_mode: "REGULAR_MARGIN" | "PORTFOLIO_MARGIN" | "ISOLATED_MARGIN"
        """
        endpoint = "/v5/account/set-margin-mode"
        body = {
            "setMarginMode": margin_mode
        }
        return self._prepare_request(endpoint, method='POST', body=body)

    def generate_socket_signature(self):
        """Funds Transfer"""
        endpoint = "/dma/api/v1/socket/signature"
        return self._prepare_request(endpoint, method='GET')


def print_wallet_balance(response, hide_zero=True):
    """Pretty-print the /v5/account/wallet-balance response."""
    if not response:
        print("no response")
        return
    if response.get("retCode") not in (0, None):
        print(f"error {response.get('retCode')}: {response.get('retMsg')}")
        return

    for account in response.get("result", {}).get("list", []):
        print(
            f"\naccountType={account.get('accountType')}"
            f"  totalEquity={account.get('totalEquity')}"
            f"  totalWalletBalance={account.get('totalWalletBalance')}"
            f"  totalMarginBalance={account.get('totalMarginBalance')}"
            f"  totalAvailableBalance={account.get('totalAvailableBalance')}"
            f"  totalPerpUPL={account.get('totalPerpUPL')}"
        )
        print(f"  {'coin':<8} {'equity':>18} {'walletBalance':>18} {'available':>18} {'unrealisedPnl':>16}")
        for entry in account.get("coin", []):
            if hide_zero and not _nonzero(entry.get("equity")) and not _nonzero(entry.get("walletBalance")):
                continue
            available = (
                entry.get("availableToWithdraw")
                or entry.get("availableToBorrow")
                or entry.get("free")
                or "-"
            )
            print(
                f"  {entry.get('coin', ''):<8} {entry.get('equity', '-'):>18}"
                f" {entry.get('walletBalance', '-'):>18} {available:>18}"
                f" {entry.get('unrealisedPnl', '-'):>16}"
            )


def _nonzero(value):
    try:
        return float(value) != 0
    except (TypeError, ValueError):
        return False


def main():
    load_dotenv()
    # IMPORTANT: create a file named .env and store secrets in the format shown
    '''
    API_KEY=apikey
    API_SECRET=secretkey
    '''

    API_KEY = "3c4869129e646d5f4da44103f64d2fd748c43b8608c1a4b4e4f207b741ff3cd4"
    API_SECRET = "0a93070cae21558ff4001ebd3390c63d6aaf84c19e5e2be12253e4bea022ffc3"
    if not API_KEY or not API_SECRET:
        raise SystemExit("API_KEY / API_SECRET missing -- put them in a .env file next to this script")

    trader = DMABybit(API_KEY, API_SECRET, symbol="AMZNUSDT")

    # 1. Wallet balance -- whole unified account (all non-zero assets)
    # print("\n1. Getting Wallet Balance (UNIFIED):")
    # balance = trader.get_wallet_balance(account_type="UNIFIED")
    # print(json.dumps(balance, indent=2))
    # print_wallet_balance(balance)

    # # 2. Wallet balance -- specific coins only
    # print("\n2. Getting Wallet Balance (USDT, USDC):")
    # balance_usdt = trader.get_wallet_balance(account_type="UNIFIED", coin="USDT,USDC")
    # print(json.dumps(balance_usdt, indent=2))

    # # 3. Just one coin's numbers
    # print("\n3. USDT only:")
    # print(json.dumps(trader.get_coin_balance("USDT"), indent=2))

    # Generate Signature needed for socket connections
    # socket_signature_result = trader.generate_socket_signature()
    # print(json.dumps(socket_signature_result, indent=2))

    # # 0. Update switch_isolated
    # print("\n1. Switch Isolated")
    # leverage_result = trader.switch_isolated(trade_mode=1, leverage=5)  # 0: cross margin. 1: isolated margin
    # print(json.dumps(leverage_result, indent=2))
    #
    # # 0. Update switch_position_mode
    # print("\n1. Switch switch_position_mode")
    # leverage_result = trader.switch_position_mode(mode=3)  # Position mode. 0: Merged Single. 3: Both Sides
    # print(json.dumps(leverage_result, indent=2))

    # 1. Set leverage to 5x
    # print("\n2. Setting Leverage:")
    # leverage_result = trader.set_leverage(leverage=100)
    # print(json.dumps(leverage_result, indent=2))

    # 2. Transfer funds
    # print("\n2. Transfer Funds:")
    # funds_transfer_result = trader.transfer_funds("OUT", 4000)
    # print(json.dumps(funds_transfer_result, indent=2))

    # 3. Place order
    print("\n3. Placing Order:")
    body = {
    "side": "SELL",
    "orderType": "Market",
    "qty": "1.46",
    "symbol": "AMZNUSDT",
    "reduceOnly": True
}
    order_result = trader.place_order(body)
    print(json.dumps(order_result, indent=2))

    # # Extract order link ID
    # order_link_id = order_result['result']['orderLinkId']

    # # 3.1 Get order details
    # print("\n3.1 Getting Order Details:")
    # order_details = trader.get_order_details("batch-6-abc")
    # print(json.dumps(order_details, indent=2))

    # 4. Get Open Positions
    # print("\n4. Getting Open Positions:")
    # positions = trader.get_open_positions()
    # print(json.dumps(positions, indent=2))

    # # # 5. Get Account Info
    # print("\n5. Getting Account Info:")
    # account_info = trader.get_account_info()
    # print(json.dumps(account_info, indent=2))

    # # 6. Set Margin Mode
    # Options: "REGULAR_MARGIN" | "PORTFOLIO_MARGIN" | "ISOLATED_MARGIN"
    # print("\n6. Setting Margin Mode:")
    # margin_mode_result = trader.set_margin_mode("REGULAR_MARGIN")
    # print(json.dumps(margin_mode_result, indent=2))

    # # # 5. Add $1 margin
    # print("\n5. Adding $1 Margin:")
    # add_margin_result = trader.add_margin(1)
    # print(json.dumps(add_margin_result, indent=2))

    # # 6. Get updated position info
    # print("\n6. Getting Updated Position Info:")
    # updated_positions = trader.get_open_positions()
    # print(json.dumps(updated_positions, indent=2))

    # # 7. Remove $1 margin
    # print("\n7. Removing $1 Margin:")
    # remove_margin_result = trader.remove_margin(1)
    # print(json.dumps(remove_margin_result, indent=2))

    # # 8. Get position info again
    # print("\n8. Getting Final Position Info:")
    # final_positions = trader.get_open_positions()
    # print(json.dumps(final_positions, indent=2))

    # # 9. Get trades for the order
    # print("\n9. Getting Order Trades:")
    # trades = trader.get_trades(order_link_id)
    # print(json.dumps(trades, indent=2))

    # # 10. Switch position mode
    # print("\n10. Switching position mode:")
    # switch_mode_result = trader.switch_position_mode(mode=0)
    # print(json.dumps(switch_mode_result, indent=2))


if __name__ == "__main__":
    main()
