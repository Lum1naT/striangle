"""Explicit provider adapters; normalized feeds never masquerade as complete markets."""
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser

from defusedxml import ElementTree

from .configuration import dec


class ProviderError(Exception):
    pass


def http_json(url, *, payload=None, headers=None, method=None, timeout=15):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "Striangle/1.0", "Accept": "application/json", **({"Content-Type": "application/json"} if data else {}), **(headers or {})}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ProviderError("Provider response exceeds size limit")
            return json.loads(raw)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        # Do not echo request headers, URLs containing signatures, or response bodies.
        raise ProviderError(f"Provider request failed ({type(exc).__name__})") from None


def observation(source, source_id, symbol, kind, body, event_at=None, received_at=None):
    now = time.time() if received_at is None else received_at
    event_at = now if event_at is None else event_at
    if event_at > now + 5:
        raise ValueError("Provider clock is more than five seconds ahead")
    return {"source": source, "source_id": str(source_id), "symbol": symbol, "kind": kind,
            "event_at": event_at, "received_at": now, "payload": body}


def normalize_book(bids, asks):
    result = {}
    for side, levels, reverse in (("bids", bids, True), ("asks", asks, False)):
        parsed = [(dec(p), dec(q)) for p, q in levels]
        if any(p <= 0 or q < 0 for p, q in parsed):
            raise ValueError("Invalid book level")
        parsed = sorted(((p, q) for p, q in parsed if q), reverse=reverse)
        if not parsed or len({p for p, _ in parsed}) != len(parsed):
            raise ValueError("Empty or duplicate book levels")
        result[side] = [[str(p), str(q)] for p, q in parsed[:20]]
    if dec(result["bids"][0][0]) >= dec(result["asks"][0][0]):
        raise ValueError("Crossed order book")
    result["coverage"] = "top_20_snapshot"
    return result


def normalize_binance(message, received_at=None):
    stream, data = message["stream"], message["data"]
    symbol = stream.split("@")[0].upper()
    now = time.time() if received_at is None else received_at
    if "@depth20" in stream:
        body = normalize_book(data["bids"], data["asks"])
        body.update(update_id=data["lastUpdateId"], timestamp_basis="local_receipt_no_exchange_timestamp")
        return observation("binance_spot", f"{data['lastUpdateId']}:{int(now*1000)}", symbol, "book", body, received_at=now)
    if data.get("e") == "aggTrade":
        price, quantity = dec(data["p"]), dec(data["q"])
        if price <= 0 or quantity <= 0 or not isinstance(data["m"], bool):
            raise ValueError("Invalid trade")
        return observation("binance_spot", data["a"], symbol, "trade", {"price": str(price), "quantity": str(quantity), "taker_buy": not data["m"]}, data["T"]/1000, now)
    if data.get("e") == "kline" and data["k"]["x"]:
        k = data["k"]
        body = candle_payload([k["t"], k["o"], k["h"], k["l"], k["c"], k["v"], k["T"]])
        return observation("binance_spot", k["t"], symbol, "candle", body, k["T"]/1000, now)
    return None


def candle_payload(row):
    o, h, l, c, v = [dec(x) for x in row[1:6]]
    if min(o, h, l, c) <= 0 or v < 0 or not l <= min(o, c) <= max(o, c) <= h:
        raise ValueError("Invalid OHLCV bounds")
    opened, closed = float(row[0])/1000, (float(row[6])+1)/1000
    if closed-opened != 60:
        raise ValueError("Bot research currently requires 1-minute candles")
    return {"opened_at": opened, "closed_at": closed, "open": str(o), "high": str(h), "low": str(l), "close": str(c), "volume": str(v)}


def normalize_bybit(message, cache, received_at=None):
    now = time.time() if received_at is None else received_at
    topic = message.get("topic", "")
    if topic.startswith("allLiquidation."):
        rows = []
        for n, item in enumerate(message["data"]):
            price, qty = dec(item["p"]), dec(item["v"])
            if price <= 0 or qty <= 0 or item["S"] not in ("Buy", "Sell"):
                raise ValueError("Invalid liquidation")
            body = {"price": str(price), "quantity": str(qty), "position_side": "long" if item["S"] == "Buy" else "short", "price_basis": "bankruptcy_price", "venue": "Bybit derivatives"}
            digest = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:20]
            rows.append(observation("bybit_linear", f"{message['ts']}:{n}:{digest}", item["s"], "liquidation", body, item["T"]/1000, now))
        return rows
    if topic.startswith("tickers."):
        symbol, data = topic.split(".")[1], message["data"]
        if message.get("type") == "snapshot":
            cache[symbol] = {}
        cache.setdefault(symbol, {}).update(data)
        full = cache[symbol]
        if "fundingRate" not in full or "openInterest" not in full:
            return []
        body = {"funding_rate": float(dec(full["fundingRate"])), "open_interest": float(dec(full["openInterest"])), "venue": "Bybit derivatives"}
        return [observation("bybit_linear", message.get("cs", message["ts"]), symbol, "derivatives", body, message["ts"]/1000, now)]
    return []


def fetch_candles(symbol, count=1000):
    if symbol not in ("BTCUSDT", "ETHUSDT") or not 50 <= count <= 5000:
        raise ValueError("Choose BTCUSDT or ETHUSDT and 50–5,000 candles")
    host = "https://data-api.binance.vision/api/v3/"
    server_time = int(http_json(host+"time")["serverTime"])
    end, result = server_time, {}
    while len(result) < count:
        url = host+"klines?"+urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": min(1000, count-len(result)+1), "endTime": end})
        rows = http_json(url)
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            if int(row[6]) < server_time:
                body = candle_payload(row)
                result[body["opened_at"]] = body
        next_end = int(rows[0][0])-1
        if next_end >= end:
            raise ProviderError("Candle pagination did not advance")
        end = next_end
    return sorted(result.values(), key=lambda x: x["opened_at"])[-count:]


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def clean_text(value, limit=600):
    parser = PlainText()
    parser.feed(value or "")
    return unescape(" ".join(" ".join(parser.parts).split()))[:limit]


def parse_rss(raw, now=None):
    now = time.time() if now is None else now
    root = ElementTree.fromstring(raw)
    result = []
    for item in root.findall(".//item")[:40]:
        title = clean_text(item.findtext("title"), 240)
        link = item.findtext("link", "").strip()
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", "")).timestamp()
        except (TypeError, ValueError, OverflowError):
            continue
        if not title or urllib.parse.urlparse(link).scheme != "https" or not now-86400 <= published <= now+5:
            continue
        result.append({"title": title, "summary": clean_text(item.findtext("description")), "url": link, "published_at": published})
    return result


def fetch_news(url):
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("News feeds must use HTTPS")
    req = urllib.request.Request(url, headers={"User-Agent": "Striangle/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ProviderError("News feed exceeds size limit")
        return parse_rss(raw)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError(f"News request failed ({type(exc).__name__})") from None


def fetch_heatmap(symbol, key):
    url = "https://open-api-v4.coinglass.com/api/futures/liquidation/heatmap/model1?" + urllib.parse.urlencode({"exchange": "Binance", "symbol": symbol, "range": "12h"})
    response = http_json(url, headers={"CG-API-KEY": key})
    if response.get("code") != "0":
        raise ProviderError("Heatmap unavailable; check API entitlement")
    data = response["data"]
    rows, axis = data["liquidation_leverage_data"], data["y_axis"]
    latest = max((int(row[0]) for row in rows), default=-1)
    levels = []
    for x, y, weight in rows:
        if int(x) == latest and 0 <= int(y) < len(axis) and dec(weight) > 0:
            levels.append({"price": str(dec(axis[int(y)])), "weight": float(dec(weight))})
    return {"levels": sorted(levels, key=lambda x: x["weight"], reverse=True)[:50], "estimated": True, "model": "CoinGlass Model1", "venue": "Binance derivatives"}
