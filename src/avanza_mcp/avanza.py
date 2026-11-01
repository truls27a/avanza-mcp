"""Read-only client for Avanza's undocumented web API.

Account data needs a BankID web session (see bankid.py), which Avanza ends after at most 24 hours.
Without a session, accounts and holdings fall back to the last snapshot, with holdings re-priced from
public quotes. Market data is public and never needs a login.

The endpoints are the ones avanza.se's own web app uses. They are not a public API and can change.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any

import httpx

from .store import Store

ORIGIN = "https://www.avanza.se"
SESSION_INFO = "/_api/authentication/session/info/session"
LOGOUT = "/_api/authentication/sessions/webtoken"
ACCOUNTS = "/_api/account-overview/overview/categorizedAccounts"
POSITIONS = "/_api/position-data/positions"
TRANSACTIONS = "/_api/transactions/list"
SEARCH = "/_api/search/filtered-search"
QUOTE = "/_api/market-guide/stock/{id}/quote"
STOCK = "/_api/market-guide/stock/{id}"
FUND = "/_api/fund-guide/guide/{id}"
CHART = "/_api/price-chart/stock/{id}"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; avanza-mcp)",
}
QUOTE_CACHE_SECONDS = 60
PERIODS = {
    "one_week": "day", "one_month": "day", "three_months": "day", "this_year": "week",
    "one_year": "week", "three_years": "month", "five_years": "month",
}
FUND_FIELDS = (
    "name", "isin", "nav", "navDate", "currency", "fundTypeName", "categories", "rating", "risk",
    "productFee", "managementFee", "developmentOneDay", "developmentOneMonth", "developmentThreeMonths",
    "developmentThisYear", "developmentOneYear", "developmentThreeYears", "developmentFiveYears",
    "sharpeRatio", "standardDeviation", "capital", "indexFund", "sustainabilityRating", "primaryBenchmark",
)


class AvanzaError(Exception):
    """An error whose message is safe to show to the user."""


class LoginRequired(AvanzaError):
    def __init__(self) -> None:
        super().__init__("Avanza login required: call connect_avanza and log in with BankID")


def http_client(transport: httpx.AsyncBaseTransport | None = None, **kw: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=ORIGIN, headers={**HEADERS, **kw.pop("headers", {})}, timeout=20,
                             follow_redirects=False, transport=transport, **kw)


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def _value(v: Any) -> Any:
    """Money and decimal fields come either as plain numbers or as {'value': x, 'unit': 'SEK'}."""
    return v.get("value") if isinstance(v, dict) else v


def _account(a: dict) -> dict:
    name = a.get("name") or {}
    return {
        "account_id": str(a.get("id")),
        "name": name.get("userDefinedName") or name.get("defaultName"),
        "type": a.get("type"),
        "total_value": _value(a.get("totalValue")),
        "cash": _value(a.get("balance")),
        "hidden": bool((a.get("settings") or {}).get("IS_HIDDEN")),
    }


def _position(p: dict) -> dict:
    instrument = p.get("instrument") or {}
    orderbook = instrument.get("orderbook") or {}
    return {
        "account_id": str((p.get("account") or {}).get("id")),
        "name": instrument.get("name"),
        "type": instrument.get("type"),
        "isin": instrument.get("isin"),
        "order_book_id": str(orderbook["id"]) if orderbook.get("id") is not None else None,
        "currency": instrument.get("currency"),
        "volume": _value(p.get("volume")),
        "value": _value(p.get("value")),
        "acquired_value": _value(p.get("acquiredValue")),
        "average_price": _value(p.get("averageAcquiredPrice")),
        "last_price": _value((orderbook.get("quote") or {}).get("latest")),
    }


def _cash(c: dict) -> dict:
    return {"account_id": str((c.get("account") or {}).get("id")), "amount": _value(c.get("totalBalance"))}


def _transaction(t: dict) -> dict:
    return {
        "date": t.get("date"),
        "account_id": str((t.get("account") or {}).get("id")),
        "type": t.get("type"),
        "description": t.get("description"),
        "instrument": t.get("instrumentName"),
        "volume": _value(t.get("volume")),
        "price": _value(t.get("priceInTradedCurrency") or t.get("price")),
        "amount": _value(t.get("amount")),
        "commission": _value(t.get("commission")),
    }


class Avanza:
    def __init__(self, store: Store, transport: httpx.AsyncBaseTransport | None = None):
        self.store = store
        self.transport = transport
        self.public = http_client(transport)
        self._quotes: dict[str, tuple[float, Any]] = {}

    async def aclose(self) -> None:
        await self.public.aclose()

    # Session

    def save_session(self, cookies: list[dict], security_token: str) -> None:
        self.store.put("session", {"cookies": cookies, "token": security_token, "created": time.time()})

    def session_info(self) -> dict:
        s = self.store.get("session")
        snap = self.store.get("snapshot")
        return {
            "connected": s is not None,
            "session_started": iso(s and s["created"]),
            "snapshot_as_of": iso(snap and snap["fetched"]),
        }

    def _session_client(self, s: dict) -> httpx.AsyncClient:
        jar = httpx.Cookies()
        for c in s["cookies"]:
            jar.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
        return http_client(self.transport, headers={"X-SecurityToken": s["token"]}, cookies=jar)

    async def _private(self, path: str, params: dict | None = None) -> Any:
        s = self.store.get("session")
        if not s:
            raise LoginRequired()
        async with self._session_client(s) as c:
            r = await c.get(path, params=params)
        if r.status_code in (401, 403) or r.is_redirect:
            self.store.put("session", None)
            raise LoginRequired()
        if r.status_code != 200:
            raise AvanzaError(f"Avanza request failed (HTTP {r.status_code})")
        return r.json()

    async def logout(self) -> None:
        s = self.store.get("session")
        self.store.put("session", None)
        if s:
            try:
                async with self._session_client(s) as c:
                    await c.delete(LOGOUT)
            except httpx.HTTPError:
                pass

    # Account data

    async def refresh_snapshot(self) -> dict:
        accounts = await self._private(ACCOUNTS)
        positions = await self._private(POSITIONS)
        snap = {
            "fetched": time.time(),
            "accounts": [_account(a) for a in accounts.get("accounts") or []],
            "positions": [_position(p) for k in ("withOrderbook", "withoutOrderbook") for p in positions.get(k) or []],
            "cash": [_cash(c) for c in positions.get("cashPositions") or []],
        }
        self.store.put("snapshot", snap)
        return snap

    async def _snapshot_or_live(self) -> tuple[dict, bool]:
        try:
            return await self.refresh_snapshot(), True
        except LoginRequired:
            snap = self.store.get("snapshot")
            if not snap:
                raise
            return snap, False

    async def accounts(self) -> dict:
        snap, live = await self._snapshot_or_live()
        out = {"accounts": snap["accounts"], "source": "live" if live else "snapshot", "as_of": iso(snap["fetched"])}
        if not live:
            out["note"] = "Not logged in: values are from the last snapshot. connect_avanza gives live values."
        return out

    async def holdings(self, account_id: str | None = None) -> dict:
        snap, live = await self._snapshot_or_live()
        rows = [dict(p) for p in snap["positions"] if not account_id or p["account_id"] == account_id]
        cash = [c for c in snap["cash"] if not account_id or c["account_id"] == account_id]
        if live:
            return {"positions": rows, "cash": cash, "source": "live", "as_of": iso(snap["fetched"])}
        priced = 0
        for p in rows:
            price = await self.last_price(p["order_book_id"]) if p["order_book_id"] else None
            if price is not None and p["volume"] is not None:
                p["last_price"], p["value"], p["priced_live"] = price, round(price * float(p["volume"]), 2), True
                priced += 1
            else:
                p["priced_live"] = False
        return {
            "positions": rows, "cash": cash, "source": "snapshot",
            "positions_as_of": iso(snap["fetched"]), "priced_live": f"{priced}/{len(rows)}",
            "note": "Not logged in: quantities and cash are from the last snapshot, prices are live public quotes. "
                    "Trades since the snapshot are missing; connect_avanza gives live data.",
        }

    async def transactions(self, date_from: date, date_to: date, account_id: str | None = None,
                           limit: int = 100) -> dict:
        params = {"from": date_from.isoformat(), "to": date_to.isoformat(), "maxElements": 1000}
        body = await self._private(TRANSACTIONS, params)
        rows = [_transaction(t) for t in body.get("transactions") or []]
        if account_id:
            rows = [t for t in rows if t["account_id"] == account_id]
        return {"date_from": params["from"], "date_to": params["to"], "count": len(rows),
                "truncated": len(rows) > limit, "transactions": rows[:limit]}

    # Market data (public)

    async def _public(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.public.request(method, path, **kw)
        if r.status_code == 404:
            raise AvanzaError("Not found on Avanza")
        if r.status_code != 200:
            raise AvanzaError(f"Avanza request failed (HTTP {r.status_code})")
        return r.json()

    async def last_price(self, order_book_id: str) -> Any:
        hit = self._quotes.get(order_book_id)
        if hit and time.time() - hit[0] < QUOTE_CACHE_SECONDS:
            return hit[1]
        try:
            last = (await self._public("GET", QUOTE.format(id=order_book_id))).get("last")
        except (AvanzaError, httpx.HTTPError):
            return None
        self._quotes[order_book_id] = (time.time(), last)
        return last

    async def search(self, query: str, instrument_type: str | None = None, limit: int = 10) -> dict:
        body = await self._public("POST", SEARCH, json={
            "query": query,
            "searchFilter": {"types": [instrument_type.upper()] if instrument_type else []},
            "pagination": {"size": limit, "from": 0},
        })
        return {"total": body.get("totalNumberOfHits"), "hits": [
            {"order_book_id": h.get("orderBookId"), "name": h.get("title"), "type": h.get("type"),
             "market": h.get("marketPlaceName"), "last": (h.get("price") or {}).get("last"),
             "currency": (h.get("price") or {}).get("currency")}
            for h in body.get("hits") or []]}

    async def instrument(self, order_book_id: str) -> dict:
        s = await self._public("GET", STOCK.format(id=order_book_id))
        if s.get("type") != "FUND":
            listing, k = s.get("listing") or {}, s.get("keyIndicators") or {}
            return {
                "order_book_id": order_book_id, "name": s.get("name"), "type": s.get("type"),
                "isin": s.get("isin"), "ticker": listing.get("tickerSymbol"), "currency": listing.get("currency"),
                "market": listing.get("marketListName") or listing.get("marketPlaceName"),
                "quote": s.get("quote"), "closing_prices": s.get("historicalClosingPrices"),
                "key_indicators": {key: _value(v) for key, v in k.items() if not isinstance(v, dict) or "value" in v},
                "dividend": k.get("dividend"), "next_report": k.get("nextReport"),
                "sectors": [x.get("sectorName") for x in s.get("sectors") or []],
            }
        f = await self._public("GET", FUND.format(id=order_book_id))
        out = {k: f.get(k) for k in FUND_FIELDS}
        out["top_holdings"] = [{"name": h.get("name"), "percent": h.get("y")}
                               for h in (f.get("holdingChartData") or [])[:10]]
        return {"order_book_id": order_book_id, "type": "FUND", **out}

    async def price_history(self, order_book_id: str, period: str) -> dict:
        if period not in PERIODS:
            raise AvanzaError("period must be one of: " + ", ".join(PERIODS))
        body = await self._public("GET", CHART.format(id=order_book_id),
                                  params={"timePeriod": period, "resolution": PERIODS[period]})
        points = [{"date": datetime.fromtimestamp(p["timestamp"] / 1000, timezone.utc).date().isoformat(),
                   "close": p.get("close")} for p in body.get("ohlc") or []]
        return {"order_book_id": order_book_id, "period": period, "resolution": PERIODS[period],
                "previous_close": body.get("previousClosingPrice"), "points": points}
