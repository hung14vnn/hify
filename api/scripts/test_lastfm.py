from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import HTTPException
from starlette.requests import Request

import lastfm
import main


class LastfmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = SimpleNamespace(
            lastfm_api_key="test-key",
            lastfm_api_secret="test-secret",
            lastfm_callback_url="https://music.test/hify-api/api/v1/lastfm/callback?source=profile",
            lastfm_return_url="https://music.test/profile",
        )
        self.patches = [patch.object(module, "settings", self.config) for module in (lastfm, main)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        self.addAsyncCleanup(self.http.aclose)
        self.calls = []
        self.reply = {"session": {"key": "test-session", "name": "listener"}}
        self.service = lastfm.LastfmService(self.http)
        self.save = AsyncMock()
        save_patch = patch.object(lastfm.db, "lastfm_save", self.save)
        save_patch.start()
        self.addCleanup(save_patch.stop)

    def respond(self, request):
        self.calls.append(request)
        return httpx.Response(200, json=self.reply)

    def request(self, state=""):
        app = SimpleNamespace(state=SimpleNamespace(services={"lastfm": self.service}))
        headers = [(b"cookie", f"lastfm_state={state}".encode())] if state else []
        return Request({"type": "http", "query_string": b"", "headers": headers, "app": app})

    async def start(self, user="user-a"):
        url, browser_state = await self.service.connect_url(user)
        auth = parse_qs(urlsplit(url).query)
        callback = urlsplit(auth["cb"][0])
        self.assertEqual(parse_qs(callback.query)["state"], [browser_state])
        return auth, callback, browser_state

    async def test_web_auth_does_not_request_or_send_desktop_token(self):
        auth, callback, state = await self.start()
        self.assertEqual(auth["api_key"], ["test-key"])
        self.assertNotIn("token", auth)
        self.assertEqual(callback.path, "/hify-api/api/v1/lastfm/callback")
        self.assertEqual(parse_qs(callback.query)["source"], ["profile"])
        self.assertGreaterEqual(len(state), 32)
        self.assertEqual(self.calls, [])

    async def test_callback_exchanges_returned_token_and_links_initiator_once(self):
        _, _, state = await self.start()
        response = await main.lastfm_callback(self.request(state), token="returned-token", state=state)
        self.assertEqual(response.status_code, 200)
        self.save.assert_awaited_once_with("user-a", "test-session", "listener")
        params = dict(self.calls[0].url.params)
        self.assertEqual(params["method"], "auth.getSession")
        self.assertEqual(params["token"], "returned-token")
        signature = params.pop("api_sig")
        self.assertEqual(signature, lastfm._sign(params, "test-secret"))
        self.assertIsNone(await self.service.finish("returned-token", state))
        self.assertEqual(len(self.calls), 1)

    async def test_missing_unknown_or_expired_state_cannot_link(self):
        _, _, state = await self.start()
        for token, value in [("token", ""), ("", state), ("token", "unknown")]:
            result = await main.lastfm_callback(self.request(state), token=token, state=value)
            self.assertEqual(result.status_code, 400)
            self.assertNotIn(b"<script>", result.body)
        with patch.object(lastfm.time, "time", return_value=self.service._pending[state][1] + 1):
            self.assertIsNone(await self.service.finish("token", state))
        self.save.assert_not_awaited()
        self.assertEqual(self.calls, [])

    async def test_new_attempt_replaces_old_and_preserves_other_users(self):
        _, _, first = await self.start()
        _, _, other = await self.start("user-b")
        _, _, latest = await self.start()
        self.assertNotEqual(first, latest)
        self.assertIsNone(await self.service.finish("token", first))
        self.assertEqual(await self.service.finish("token-b", other), "user-b")
        self.assertEqual(await self.service.finish("token-a", latest), "user-a")

    async def test_expired_attempts_are_pruned(self):
        self.service._pending["expired"] = ("old-user", 0)
        await self.start()
        self.assertNotIn("expired", self.service._pending)

    async def test_lastfm_rejection_does_not_save_or_redirect(self):
        self.reply = {"error": 14, "message": "Unauthorized token"}
        _, _, state = await self.start()
        response = await main.lastfm_callback(self.request(state), token="token", state=state)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(b"<script>", response.body)
        self.save.assert_not_awaited()

    async def test_connect_configuration_and_no_cache(self):
        response = await main.lastfm_connect(self.request(), user={"user_id": "user-a"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("cb=", json.loads(response.body)["url"])
        self.config.lastfm_api_secret = ""
        with self.assertRaises(HTTPException) as failure:
            await main.lastfm_connect(self.request(), user={"user_id": "user-a"})
        self.assertEqual(failure.exception.status_code, 503)

    async def test_callback_escapes_return_url(self):
        self.config.lastfm_return_url = 'https://music.test/profile?q="</script><script>alert(1)</script>'
        _, _, state = await self.start()
        response = await main.lastfm_callback(self.request(state), token="token", state=state)
        self.assertNotIn(b"<script>alert", response.body)
        self.assertIn(b"&quot;&lt;/script&gt;", response.body)
        self.assertIn(b"\\u003c/script>", response.body)
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    async def test_forwarded_authorization_url_cannot_link_another_browser(self):
        _, _, state = await self.start()
        for cookie in ("", "another-browser"):
            response = await main.lastfm_callback(self.request(cookie), token="token", state=state)
            self.assertEqual(response.status_code, 400)
        self.assertIn(state, self.service._pending)
        self.save.assert_not_awaited()
        self.assertEqual(self.calls, [])

    async def test_new_attempt_waits_for_inflight_completion(self):
        _, _, old = await self.start()
        saving = asyncio.Event()
        release = asyncio.Event()

        async def save(*args):
            saving.set()
            await release.wait()

        self.save.side_effect = save
        completion = asyncio.create_task(self.service.finish("old-token", old))
        await saving.wait()
        restart = asyncio.create_task(self.start())
        await asyncio.sleep(0)
        self.assertFalse(restart.done())
        release.set()
        await completion
        _, _, latest = await restart
        self.assertEqual(await self.service.finish("new-token", latest), "user-a")
        self.assertEqual(self.save.await_count, 2)

    async def test_http_cookie_round_trip_and_credentialed_cors(self):
        self.config.lastfm_callback_url = "https://music.test/api/v1/lastfm/callback"
        with patch.object(main.app.state, "services", {"lastfm": self.service}, create=True):
            with patch.dict(main.app.dependency_overrides, {
                main.auth.current_user_http: lambda: {"user_id": "user-a"},
            }):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=main.app), base_url="https://music.test",
                ) as client:
                    origin = "http://127.0.0.1:5180"
                    preflight = await client.options("/api/v1/lastfm/connect", headers={
                        "Origin": origin, "Access-Control-Request-Method": "GET",
                        "Access-Control-Request-Headers": "authorization",
                    })
                    self.assertEqual(preflight.headers["access-control-allow-origin"], origin)
                    self.assertEqual(preflight.headers["access-control-allow-credentials"], "true")
                    start = await client.get("/api/v1/lastfm/connect", headers={"Origin": origin})
                    self.assertEqual(start.headers["access-control-allow-origin"], origin)
                    cookie = start.headers["set-cookie"]
                    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=600"):
                        self.assertIn(attribute, cookie)
                    state = client.cookies.get("lastfm_state")
                    callback = await client.get("/api/v1/lastfm/callback", params={"state": state, "token": "token"})
                    self.assertEqual(callback.status_code, 200)
                    self.assertIsNone(client.cookies.get("lastfm_state"))
                    self.save.assert_awaited_once_with("user-a", "test-session", "listener")

    async def test_network_error_does_not_expose_signed_request(self):
        request = httpx.Request("GET", "https://music.test/?token=private-token")
        with patch.object(self.http, "get", side_effect=httpx.ConnectError("private-token", request=request)):
            with self.assertLogs("lastfm", level="WARNING") as logs:
                self.assertIsNone(await self.service._call("auth.getSession", token="private-token"))
        self.assertNotIn("private-token", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
