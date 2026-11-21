import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
from app.key_manager import KeyManager


def test_rotation_serialized_persistent_and_no_foreign_revoke(tmp_path, monkeypatch):
    async def run():
        settings = SimpleNamespace(
            coc_auto_keys=True,
            coc_email="test",
            coc_password="secret",
            coc_api_base="https://api.test",
            coc_clan_tag="#CLAN",
        )
        game = SimpleNamespace(
            headers={}, get=AsyncMock(return_value=SimpleNamespace(status_code=200))
        )
        manager = KeyManager(settings, game, tmp_path / "state")
        calls = []
        claims = (
            base64.urlsafe_b64encode(
                json.dumps({"limits": [{"cidrs": ["8.8.8.8/32"]}]}).encode()
            )
            .decode()
            .rstrip("=")
        )

        async def handler(request):
            route = request.url.path.rsplit("/", 1)[-1]
            calls.append(route)
            data = {
                "login": {"temporaryAPIToken": "x." + claims + ".x"},
                "list": {"keys": [{"id": "manual", "name": "manual"}]},
                "create": {"key": {"id": "ours", "key": "new-secret"}},
            }[route]
            return httpx.Response(200, json=data)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr("app.key_manager.httpx.AsyncClient", lambda **kw: client)
        results = await asyncio.gather(*(manager.recover() for _ in range(10)))
        assert results.count(True) == 1
        assert calls.count("create") == 1 and "revoke" not in calls
        assert game.headers["Authorization"] == "Bearer new-secret"
        assert "new-secret" not in (tmp_path / "state").read_text()
        assert (tmp_path / "state").stat().st_mode & 0o777 == 0o600
        assert KeyManager(settings, game, tmp_path / "state").state["ids"] == ["ours"]

    asyncio.run(run())


def test_portal_failure_keeps_manual_credentials(tmp_path, monkeypatch):
    async def run():
        settings = SimpleNamespace(
            coc_auto_keys=True, coc_email="test", coc_password="secret"
        )
        game = SimpleNamespace(headers={"Authorization": "Bearer manual"})

        async def handler(request):
            return httpx.Response(403)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr("app.key_manager.httpx.AsyncClient", lambda **kw: client)
        manager = KeyManager(settings, game, tmp_path / "state")
        assert not await manager.recover()
        assert game.headers["Authorization"] == "Bearer manual"
        assert manager.status == "PORTAL_RECOVERY_FAILED"

    asyncio.run(run())


def test_changed_ip_switches_then_revokes_only_owned_key(tmp_path, monkeypatch):
    async def run():
        settings = SimpleNamespace(
            coc_auto_keys=True,
            coc_email="test",
            coc_password="secret",
            coc_api_base="https://api.test",
            coc_clan_tag="#CLAN",
        )
        game = SimpleNamespace(
            headers={"Authorization": "Bearer old"},
            get=AsyncMock(return_value=SimpleNamespace(status_code=200)),
        )
        manager = KeyManager(settings, game, tmp_path / "state")
        manager.state.update(ip="1.1.1.1", ids=["old-id"])
        manager.save()
        claims = (
            base64.urlsafe_b64encode(
                json.dumps({"limits": [{"cidrs": ["8.8.8.8/32"]}]}).encode()
            )
            .decode()
            .rstrip("=")
        )
        revoked = []

        async def handler(request):
            route = request.url.path.rsplit("/", 1)[-1]
            if route == "revoke":
                assert game.headers["Authorization"] == "Bearer new"
                revoked.append(json.loads(request.content)["id"])
                return httpx.Response(200, json={})
            data = {
                "login": {"temporaryAPIToken": "x." + claims + ".x"},
                "list": {
                    "keys": [
                        {
                            "id": "old-id",
                            "name": "ha-coc-" + manager.state["owner"],
                            "cidrRanges": ["1.1.1.1"],
                        },
                        {"id": "manual", "name": "manual"},
                    ]
                },
                "create": {"key": {"id": "new-id", "key": "new"}},
            }[route]
            return httpx.Response(200, json=data)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr("app.key_manager.httpx.AsyncClient", lambda **kw: client)
        assert await manager.recover()
        assert revoked == ["old-id"]
        assert manager.state["ip"] == "8.8.8.8"
        assert manager.state["ids"] == ["new-id"]

    asyncio.run(run())
