"""BankID login against Avanza's web login endpoints.

Two modes:

- ``qr``:  for scanning from another device. The QR payload is animated (it embeds the seconds since
  the order started) and is refreshed from Avanza on every poll.
- ``app``: for BankID on the same device. Returns an ``https://app.bankid.com/?autostarttoken=...`` link.

BankID cancels an order the app has not picked up within about 30 seconds (Avanza answers the next
collect with HTTP 400, ``errors: ["START_FAILED"]``). In ``qr`` mode a fresh order is started
transparently until the attempt deadline; in ``app`` mode the attempt ends as ``expired``, which is
why the login panel starts an app login only when the user taps the button.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal
from urllib.parse import urlencode

import httpx

from .avanza import SESSION_INFO, Avanza, AvanzaError, http_client

log = logging.getLogger(__name__)

BANKID = "/_api/authentication/v2/sessions/bankid"
PENDING = {"OUTSTANDING_TRANSACTION", "PENDING", "STARTED", "USER_SIGN"}
ATTEMPT_SECONDS = 180
POLL_SECONDS = 1.0

Mode = Literal["qr", "app"]
Status = Literal["idle", "starting", "waiting", "done", "expired", "cancelled", "timed_out", "failed"]


def app_link(autostart_token: str) -> str:
    return "https://app.bankid.com/?" + urlencode({"autostarttoken": autostart_token, "redirect": "null"})


class LoginAttempt:
    """One BankID login with its own cookie jar, polled in the background."""

    def __init__(self, avanza: Avanza, mode: Mode):
        self.avanza, self.mode = avanza, mode
        self.client = http_client(avanza.transport)
        self.status: Status = "starting"
        self.qr: str | None = None
        self.autostart_token: str | None = None
        self.task: asyncio.Task | None = None
        self.deadline = time.monotonic() + ATTEMPT_SECONDS

    async def _order(self) -> None:
        if self.mode == "qr":
            body = {"method": "QR_START"}
        else:
            body = {"method": "AUTOSTART", "returnScheme": "NULL"}
        r = await self.client.post(BANKID, json=body)
        if r.status_code >= 300:
            raise AvanzaError(f"Avanza did not start BankID (HTTP {r.status_code})")
        data = r.json()
        self.qr, self.autostart_token = data.get("qrToken"), data.get("autostartToken")
        if self.mode == "qr" and not self.qr:
            raise AvanzaError("Avanza did not return a BankID QR code")
        if self.mode == "app" and not self.autostart_token:
            raise AvanzaError("Avanza did not return a BankID launch token")

    async def start(self) -> None:
        try:
            await self._order()
        except Exception:
            self.status = "failed"
            await self.client.aclose()
            raise
        self.status = "waiting"
        self.task = asyncio.create_task(self._run())

    async def cancel(self) -> None:
        """Stop polling and cancel the BankID order at Avanza if it is still open."""
        if self.task and not self.task.done():
            try:
                await self.client.delete(BANKID)
            except httpx.HTTPError:
                pass
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if not self.client.is_closed:
            await self.client.aclose()

    async def _run(self) -> None:
        last = None
        try:
            while time.monotonic() < self.deadline:
                await asyncio.sleep(POLL_SECONDS)
                if self.mode == "qr":
                    r = await self.client.post(BANKID + "/restart", json={})
                    if r.status_code == 200 and r.json().get("qrToken"):
                        self.qr = r.json()["qrToken"]
                r = await self.client.post(BANKID + "/collect", json={})
                body = r.json() if r.content else {}
                state, errors = body.get("state"), body.get("errors") or []
                seen = (r.status_code, state, body.get("hint"), tuple(errors))
                if seen != last:
                    log.info("bankid %s: http=%s state=%s hint=%s errors=%s", self.mode, *seen)
                    last = seen
                if state in PENDING:
                    continue
                if "START_FAILED" in errors:
                    if self.mode == "app":
                        self.status = "expired"
                        return
                    await self._order()
                    continue
                if state != "COMPLETE":
                    self.status = "cancelled" if "CANCEL" in str(state) else "failed"
                    return
                await self._finish(body)
                self.status = "done"
                return
            self.status = "timed_out"
        except Exception as e:
            log.warning("bankid %s login failed: %s: %s", self.mode, type(e).__name__, e)
            self.status = "failed"
        finally:
            self.qr = None
            await self.client.aclose()

    async def _verified(self) -> tuple[bool, str | None]:
        body = (await self.client.get(SESSION_INFO)).json()
        user = body.get("user") or {}
        ok = user.get("loggedIn") is True and body.get("isContextVerifiedWithBackend") is not False
        return ok, user.get("securityToken")

    async def _finish(self, collect: dict) -> None:
        ok, token = await self._verified()
        if not ok:
            logins = collect.get("logins") or []
            if len(logins) != 1:
                raise AvanzaError("This BankID has several Avanza customers; choosing one is not supported")
            r = await self.client.get(f"{BANKID}/collect/{logins[0]['customerId']}")
            r.raise_for_status()
            ok, token = await self._verified()
        if not ok or not token:
            raise AvanzaError("Avanza session could not be verified")
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                   for c in self.client.cookies.jar]
        self.avanza.save_session(cookies, token)
        try:
            await self.avanza.refresh_snapshot()
        except AvanzaError:
            pass


class BankIDLogin:
    """Holds the current login attempt; starting a new one cancels the previous one."""

    def __init__(self, avanza: Avanza):
        self.avanza = avanza
        self.attempt: LoginAttempt | None = None

    async def start(self, mode: Mode) -> dict:
        if mode not in ("qr", "app"):
            raise AvanzaError("mode must be 'qr' or 'app'")
        await self.close()
        log.info("bankid %s login started", mode)
        self.attempt = LoginAttempt(self.avanza, mode)
        await self.attempt.start()
        return self.status()

    def status(self) -> dict:
        a = self.attempt
        if not a:
            return {"status": "idle", "mode": None, "qr": None, "app_url": None}
        waiting = a.status == "waiting"
        return {
            "status": a.status,
            "mode": a.mode,
            "qr": a.qr if waiting and a.mode == "qr" else None,
            "app_url": app_link(a.autostart_token) if waiting and a.mode == "app" and a.autostart_token else None,
        }

    async def close(self) -> None:
        if self.attempt:
            await self.attempt.cancel()
