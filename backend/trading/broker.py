"""Binance spot execution adapter. Instantiating it never submits an order."""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_DOWN, ROUND_UP

from .configuration import dec


class BrokerError(Exception):
    def __init__(self, code=None):
        self.code = code
        super().__init__(f"Exchange request failed ({code or 'network/unknown status'})")


def round_step(value, step, up=False):
    return (dec(value)/dec(step)).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)*dec(step)


class BinanceSpot:
    def __init__(self, api_key, secret, testnet=True, transport=None):
        self.key, self.secret, self.testnet = api_key, secret, testnet
        self.host = "https://testnet.binance.vision" if testnet else "https://api.binance.com"
        self.transport = transport or self._http
        self.offset, self.clock_at = 0, 0

    def _http(self, method, path, params, signed):
        values = dict(params)
        if signed:
            values.update(timestamp=int(time.time()*1000)+self.offset, recvWindow=5000)
        query = urllib.parse.urlencode(values)
        if signed:
            query += "&signature="+hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        request = urllib.request.Request(self.host+path+"?"+query, method=method, headers={"X-MBX-APIKEY": self.key, "User-Agent": "Striangle/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read(2_000_000))
        except urllib.error.HTTPError as exc:
            try:
                code = json.loads(exc.read(10000)).get("code")
            except (ValueError, AttributeError):
                code = exc.code
            raise BrokerError(code) from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise BrokerError() from None

    def call(self, method, path, params=None, signed=True):
        if signed and time.time()-self.clock_at > 60:
            remote = self.transport("GET", "/api/v3/time", {}, False)
            self.offset = int(remote["serverTime"])-int(time.time()*1000)
            self.clock_at = time.time()
        return self.transport(method, path, params or {}, signed)

    def account(self):
        return self.call("GET", "/api/v3/account")

    def permissions(self):
        if self.testnet:
            return {"enableWithdrawals": False, "enableSpotAndMarginTrading": True, "testnet": True}
        return self.call("GET", "/sapi/v1/account/apiRestrictions")

    def symbol_info(self, symbol):
        rows = self.call("GET", "/api/v3/exchangeInfo", {"symbol": symbol}, False)["symbols"]
        if len(rows) != 1 or rows[0]["status"] != "TRADING" or not rows[0].get("ocoAllowed"):
            raise ValueError("Symbol must support spot trading and native OCO protection")
        return rows[0]

    def book(self, symbol):
        return self.call("GET", "/api/v3/depth", {"symbol": symbol, "limit": 20}, False)

    def submit(self, kind, payload):
        return self.call("POST", "/api/v3/orderList/oco" if kind == "protection" else "/api/v3/order", payload)

    def query(self, kind, symbol, client_id):
        if kind == "protection":
            return self.call("GET", "/api/v3/orderList", {"origClientOrderId": client_id})
        return self.call("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id})

    def cancel_protection(self, symbol, client_id):
        return self.call("DELETE", "/api/v3/orderList", {"symbol": symbol, "listClientOrderId": client_id})

    def trades(self, symbol, order_id):
        return self.call("GET", "/api/v3/myTrades", {"symbol": symbol, "orderId": order_id, "limit": 1000})

    def open_orders(self, symbol):
        return self.call("GET", "/api/v3/openOrders", {"symbol": symbol})


def order_parameters(info, book, side, quantity, slippage_bps):
    filters = {x["filterType"]: x for x in info["filters"]}
    lot, tick = filters["LOT_SIZE"], filters["PRICE_FILTER"]["tickSize"]
    slip = dec(slippage_bps)/10000
    best = dec(book["asks" if side == "BUY" else "bids"][0][0])
    price = round_step(best*(1+slip if side == "BUY" else 1-slip), tick, up=side == "SELL")
    quantity = round_step(quantity, lot["stepSize"])
    minimum = dec(filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {})).get("minNotional", 0))
    if quantity < dec(lot["minQty"]) or quantity > dec(lot["maxQty"]) or quantity*price < minimum:
        raise ValueError("Order is outside exchange quantity/notional limits; no order submitted")
    return {"symbol": info["symbol"], "side": side, "type": "LIMIT", "timeInForce": "IOC", "quantity": format(quantity, "f"), "price": format(price, "f"), "newOrderRespType": "FULL"}


def protection_parameters(info, quantity, entry, config):
    filters = {x["filterType"]: x for x in info["filters"]}
    quantity = round_step(quantity, filters["LOT_SIZE"]["stepSize"])
    tick = filters["PRICE_FILTER"]["tickSize"]
    stop = round_step(dec(entry)*(1-dec(config["stop_pct"])/100), tick)
    target = round_step(dec(entry)*(1+dec(config["take_pct"])/100), tick, up=True)
    if quantity <= 0 or stop <= 0:
        raise ValueError("Position is too small for exchange protection")
    return {"symbol": info["symbol"], "side": "SELL", "quantity": format(quantity, "f"),
            "aboveType": "LIMIT_MAKER", "abovePrice": format(target, "f"),
            "belowType": "STOP_LOSS", "belowStopPrice": format(stop, "f")}
