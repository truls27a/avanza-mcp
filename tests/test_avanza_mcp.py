"""Offline tests: a fake Avanza behind httpx.MockTransport. No network, no account, no BankID."""

import asyncio
import json
import os
import stat
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastmcp import Client

from avanza_mcp import bankid, main
from avanza_mcp import avanza as av
from avanza_mcp.server import LOGIN_PANEL, create_server


class FakeAvanza:
    """Routes for the endpoints the server uses; attributes steer the scenario."""

    def __init__(self):
        self.calls = []
        self.orders = 0
        self.collects = 0
        self.start_failed = 0  # collects answered with START_FAILED before the normal flow
        self.pending_collects = 1  # collects answered OUTSTANDING_TRANSACTION before COMPLETE
        self.verified = False
        self.session_valid = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if path == bankid.BANKID and method == "POST":
            self.orders += 1
            if json.loads(request.content)["method"] == "QR_START":
                return httpx.Response(202, json={"transactionId": "t", "qrToken": f"bankid.qr.{self.orders}.0"})
            return httpx.Response(202, json={"transactionId": "t", "autostartToken": f"auto-{self.orders}"})
        if path == bankid.BANKID and method == "DELETE":
            return httpx.Response(200)
        if path == bankid.BANKID + "/restart":
            return httpx.Response(200, json={"qrToken": f"bankid.qr.{self.orders}.{self.collects + 1}"})
        if path == bankid.BANKID + "/collect":
            if self.start_failed:
                self.start_failed -= 1
                return httpx.Response(400, json={"statusCode": 400, "errors": ["START_FAILED"]})
            self.collects += 1
            if self.collects <= self.pending_collects:
                return httpx.Response(200, json={"state": "OUTSTANDING_TRANSACTION", "hint": "OUTSTANDING_TRANSACTION"})
            return httpx.Response(200, json={"state": "COMPLETE", "logins": [{"customerId": "c1"}]},
                                  headers={"set-cookie": "csid=abc; Domain=www.avanza.se; Path=/"})
        if path == bankid.BANKID + "/collect/c1":
            self.verified = True
            return httpx.Response(200, json={})
        if path == av.SESSION_INFO:
            return httpx.Response(200, json={"user": {"loggedIn": self.verified, "securityToken": "sec"}})
        if path in (av.ACCOUNTS, av.POSITIONS, av.TRANSACTIONS):
            if not self.session_valid or request.headers.get("X-SecurityToken") != "sec":
                return httpx.Response(401)
            if path == av.ACCOUNTS:
                return httpx.Response(200, json={"accounts": [{"id": 1, "type": "ISK", "name": {"defaultName": "ISK"},
                                                               "totalValue": {"value": 1000.0, "unit": "SEK"},
                                                               "balance": {"value": 100.0}}]})
            if path == av.POSITIONS:
                return httpx.Response(200, json={
                    "withOrderbook": [{"account": {"id": 1}, "volume": {"value": 10}, "value": {"value": 900.0},
                                       "instrument": {"name": "Example AB", "type": "STOCK", "orderbook": {"id": "42"}}}],
                    "withoutOrderbook": [],
                    "cashPositions": [{"account": {"id": 1}, "totalBalance": {"value": 100.0}}]})
            return httpx.Response(200, json={"transactions": [
                {"date": "2026-10-20", "account": {"id": 1}, "type": "BUY", "amount": {"value": -900.0}}]})
        if path == "/_api/market-guide/stock/42/quote":
            return httpx.Response(200, json={"last": 100.0})
        if path == av.SEARCH:
            return httpx.Response(200, json={"totalNumberOfHits": 1, "hits": [
                {"orderBookId": "42", "title": "Example AB", "type": "STOCK",
                 "price": {"last": "412,00", "currency": "SEK"}}]})
        if path == av.LOGOUT:
            return httpx.Response(200)
        return httpx.Response(404)


@pytest.fixture
def fake():
    return FakeAvanza()


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(bankid, "POLL_SECONDS", 0)


@pytest.fixture
async def server(tmp_path, fake):
    mcp = create_server(str(tmp_path), transport=httpx.MockTransport(fake))
    yield mcp
    await mcp.bankid.close()
    await mcp.avanza.aclose()


async def finish(mcp):
    await asyncio.wait_for(mcp.bankid.attempt.task, 5)


# BankID login


async def test_app_login_saves_session_and_snapshot(server, fake):
    st = await server.bankid.start("app")
    link = urlparse(st["app_url"])
    assert (link.scheme, link.netloc) == ("https", "app.bankid.com")
    assert parse_qs(link.query) == {"autostarttoken": ["auto-1"], "redirect": ["null"]}
    await finish(server)
    assert server.bankid.status()["status"] == "done"
    assert ("POST", bankid.BANKID + "/restart") not in fake.calls
    info = server.avanza.session_info()
    assert info["connected"] and info["snapshot_as_of"]


async def test_qr_login_refreshes_the_animated_code(server, fake):
    fake.pending_collects = 3
    st = await server.bankid.start("qr")
    assert st["qr"] == "bankid.qr.1.0" and st["app_url"] is None
    await finish(server)
    assert fake.calls.count(("POST", bankid.BANKID + "/restart")) == 4
    assert server.bankid.status() == {"status": "done", "mode": "qr", "qr": None, "app_url": None}


async def test_qr_login_starts_a_new_order_when_bankid_gives_up(server, fake):
    fake.start_failed = 2
    await server.bankid.start("qr")
    await finish(server)
    assert fake.orders == 3
    assert server.bankid.status()["status"] == "done"


async def test_app_login_expires_when_bankid_is_never_opened(server, fake):
    fake.start_failed = 1
    await server.bankid.start("app")
    await finish(server)
    assert fake.orders == 1
    assert server.bankid.status()["status"] == "expired"
    assert not server.avanza.session_info()["connected"]


async def test_new_login_cancels_the_previous_order(server, fake, monkeypatch):
    monkeypatch.setattr(bankid, "POLL_SECONDS", 60)
    await server.bankid.start("qr")
    previous = server.bankid.attempt
    await server.bankid.start("app")
    assert previous.task.cancelled() and previous.client.is_closed
    assert ("DELETE", bankid.BANKID) in fake.calls


async def test_unknown_mode_is_rejected(server):
    with pytest.raises(av.AvanzaError, match="mode"):
        await server.bankid.start("sms")


async def test_missing_launch_token_fails_without_background_polling(tmp_path):
    mcp = create_server(str(tmp_path), transport=httpx.MockTransport(lambda r: httpx.Response(202, json={})))
    with pytest.raises(av.AvanzaError, match="launch token"):
        await mcp.bankid.start("app")
    assert mcp.bankid.status()["status"] == "failed" and mcp.bankid.attempt.task is None
    await mcp.avanza.aclose()


# Account data


async def test_live_holdings_with_a_session(server):
    await server.bankid.start("app")
    await finish(server)
    h = await server.avanza.holdings()
    assert h["source"] == "live" and h["positions"][0]["value"] == 900.0


async def test_expired_session_falls_back_to_snapshot_with_live_prices(server, fake):
    await server.bankid.start("app")
    await finish(server)
    fake.session_valid = False
    h = await server.avanza.holdings()
    assert (h["source"], h["positions"][0]["value"], h["priced_live"]) == ("snapshot", 1000.0, "1/1")
    assert not server.avanza.session_info()["connected"]


async def test_no_session_and_no_snapshot_requires_login(server):
    with pytest.raises(av.LoginRequired):
        await server.avanza.holdings()


async def test_transactions_need_a_live_session(server):
    with pytest.raises(av.LoginRequired):
        await server.avanza.transactions(date(2026, 10, 1), date(2026, 10, 25))
    await server.bankid.start("app")
    await finish(server)
    t = await server.avanza.transactions(date(2026, 10, 1), date(2026, 10, 25))
    assert t["transactions"][0]["amount"] == -900.0


async def test_logout_forgets_the_session(server, fake):
    await server.bankid.start("app")
    await finish(server)
    await server.avanza.logout()
    assert ("DELETE", av.LOGOUT) in fake.calls
    assert not server.avanza.session_info()["connected"]


def test_state_is_private_on_disk(tmp_path):
    create_server(str(tmp_path / "state"))
    assert stat.S_IMODE(os.stat(tmp_path / "state").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "state" / "avanza-mcp.sqlite3").st_mode) == 0o600


# MCP surface


async def test_mcp_tools_and_login_panel(server):
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert tools["connect_avanza"].meta["ui"]["resourceUri"] == LOGIN_PANEL
        for name in ("bankid_login_start", "bankid_login_poll"):
            assert tools[name].meta["ui"]["visibility"] == ["app"]
        panel = (await client.read_resource(LOGIN_PANEL))[0]
        assert panel.mime_type == "text/html;profile=mcp-app"
        assert "bankid_login_start" in panel.text and "bankid_login_poll" in panel.text

        shown = await client.call_tool("connect_avanza", {})
        assert server.bankid.attempt is None  # showing the panel must not start a BankID order
        assert shown.structured_content["connected"] is False

        st = (await client.call_tool("bankid_login_start", {"mode": "qr"})).structured_content
        assert "<svg" in st["qr_svg"] and st["app_url"] is None
        await finish(server)
        polled = (await client.call_tool("bankid_login_poll", {})).structured_content
        assert (polled["status"], polled["connected"], polled["qr_svg"]) == ("done", True, None)
        status = (await client.call_tool("avanza_status", {})).structured_content
        assert status["connected"] and status["login_status"] == "done"

        accounts = (await client.call_tool("get_accounts", {})).structured_content
        assert accounts["accounts"][0]["name"] == "ISK"
        hits = (await client.call_tool("search_instruments", {"query": "example"})).structured_content
        assert hits["hits"][0]["order_book_id"] == "42"


async def test_bad_dates_are_rejected(server):
    async with Client(server) as client:
        result = await client.call_tool("get_transactions", {"date_from": "yesterday"}, raise_on_error=False)
        assert result.is_error and "YYYY-MM-DD" in result.content[0].text


# CLI


def test_http_on_public_address_needs_a_token(monkeypatch, tmp_path):
    monkeypatch.delenv("AVANZA_MCP_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        main(["--transport", "http", "--host", "0.0.0.0", "--data-dir", str(tmp_path)])
