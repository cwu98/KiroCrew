"""Shared Crew over AWS Systems Manager: detection, invites, and the owner invariant.

The invariant under test: an SSM port forward arrives as a loopback connection,
which is what the owner's own browser looks like, so a connection on the SSM
port is never the owner whatever it carries. The gate tests use real sockets:
one aiohttp app, the real token middleware, and two loopback sites.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from kiro_crew.config.sections_shared_crew import SharedCrewConfig, shared_crew_config_from
from kiro_crew.dashboard import ssm_gate, tailnet_live_allowlist
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware
from kiro_crew.shared_crew import access, access_set, routes_ssm, ssm, ssm_invites
from kiro_crew.shared_crew.contracts import CapabilityState, Provenance
from kiro_crew.shared_crew.identity import actor_of, ssm_principal


@pytest.fixture(autouse=True)
def _isolated(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[None]:
    import kiro_crew.dashboard.revocation_gen as rg
    import kiro_crew.dashboard.token_auth as ta

    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    _floor_monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    _floor_monkeypatch.setattr(ssm_invites, "root", lambda: tmp_path / "ssm")
    _floor_monkeypatch.setattr(rg, "_gen", 0)
    _floor_monkeypatch.setattr(ta, "_revoked_store_singleton", None)
    ssm_invites._reset_for_tests()
    ssm._reset_for_tests()
    ta._state.clear_all()
    tailnet_live_allowlist.reset_for_tests()
    access_set.reset_for_tests()
    access_set.install((), owner_login="owner@example.com", owner_user_id="11")
    yield
    ta._state.clear_all()
    ssm_invites._reset_for_tests()
    ssm._reset_for_tests()
    tailnet_live_allowlist.reset_for_tests()
    access_set.reset_for_tests()


# ── config ──


def test_ssm_is_off_by_default_and_reads_off_for_anything_but_true() -> None:
    assert SharedCrewConfig().ssm is False
    assert SharedCrewConfig().ssm_port == 5477
    assert shared_crew_config_from({"ssm": True}).ssm is True
    for raw in ("true", 1, None, [True]):
        assert shared_crew_config_from({"ssm": raw}).ssm is False


def test_the_port_is_clamped_to_unprivileged_ports() -> None:
    assert shared_crew_config_from({"ssm_port": 80}).ssm_port == 1024
    assert shared_crew_config_from({"ssm_port": 70000}).ssm_port == 65535
    assert shared_crew_config_from({"ssm_port": "x"}).ssm_port == 5477


# ── detection and the command ──

INSTANCE = "i-0123456789abcdef0"


def test_detect_target_prefers_the_operator_override_and_validates_it() -> None:
    env = {"KIROCREW_SSM_TARGET": "mi-0123456789abcdef0", "AWS_REGION": "eu-west-1"}
    found = ssm.detect_target(env, imds=lambda: pytest.fail("override must skip metadata"))
    assert found == ssm.Target("mi-0123456789abcdef0", "eu-west-1")
    for bad in ("i-xyz", "i-0123456789abcdef0; rm -rf /", "$(id)", "i-0123456789abcdef0 extra"):
        assert ssm.detect_target({"KIROCREW_SSM_TARGET": bad}, imds=lambda: None) is None


def test_detect_target_reads_instance_metadata_and_fills_a_missing_region() -> None:
    assert ssm.detect_target({}, imds=lambda: ssm.Target(INSTANCE, "us-east-1")) == ssm.Target(
        INSTANCE, "us-east-1"
    )
    assert ssm.detect_target({"AWS_REGION": "us-west-2"}, imds=lambda: ssm.Target(INSTANCE)) == (
        ssm.Target(INSTANCE, "us-west-2")
    )
    assert ssm.detect_target({}, imds=lambda: None) is None


def test_a_bad_region_is_dropped_not_passed_to_the_command() -> None:
    found = ssm.detect_target({"KIROCREW_SSM_TARGET": INSTANCE, "AWS_REGION": "x; id"})
    assert found == ssm.Target(INSTANCE, "")


def test_the_start_session_command_is_an_argument_list_with_the_documented_document() -> None:
    args = ssm.start_session_args(ssm.Target(INSTANCE, "us-east-1"), 5477)
    assert args == [
        "aws",
        "ssm",
        "start-session",
        "--target",
        INSTANCE,
        "--document-name",
        "AWS-StartPortForwardingSession",
        "--parameters",
        "portNumber=5477,localPortNumber=5477",
        "--region",
        "us-east-1",
    ]
    assert " --region" not in ssm.start_session_command(ssm.Target(INSTANCE), 5477)
    assert ssm.invite_url(5477, "abc.def") == "http://localhost:5477/?ssm_invite=abc.def"


def test_tools_are_found_on_path_without_running_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ssm.shutil, "which", lambda name: "/x/aws" if name == "aws" else None)
    assert ssm.tools() == {"aws_cli": True, "session_manager_plugin": False}


# ── invites ──


def test_an_invite_verifies_until_it_is_revoked_and_stores_only_a_hash() -> None:
    invite_id, credential = ssm_invites.create("  Dana\n")
    secret = credential.split(".", 1)[1]
    assert ssm_invites.verify(credential) == ssm_invites.SsmGuest(invite_id, "Dana")
    stored = (ssm_invites.root() / ssm_invites.STATE_FILE).read_text()
    assert secret not in stored and credential not in stored
    assert json.loads(stored)["invites"][invite_id]["secret_sha256"]
    assert ssm_invites.verify(f"{invite_id}.wrong") is None
    assert ssm_invites.verify(f"nope.{secret}") is None
    for odd in ("", ".", invite_id, f"{invite_id}.", None, 7):
        assert ssm_invites.verify(odd) is None
    assert ssm_invites.revoke(invite_id) is True
    assert ssm_invites.verify(credential) is None
    assert ssm_invites.revoke(invite_id) is False


def test_invites_survive_a_restart_and_the_list_carries_no_secret() -> None:
    invite_id, credential = ssm_invites.create("Lee")
    ssm_invites._reset_for_tests()
    assert ssm_invites.verify(credential) is not None
    rows = ssm_invites.public_rows()
    assert [r["id"] for r in rows] == [invite_id]
    assert set(rows[0]) == {"id", "alias", "created_at", "joined", "principal_id"}


def test_warm_reads_the_store_so_a_request_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    _id, credential = ssm_invites.create("Dana")
    ssm_invites._reset_for_tests()
    ssm_invites.warm()
    monkeypatch.setattr(ssm_invites, "_read", lambda: pytest.fail("a request read the disk"))
    assert ssm_invites.verify(credential) is not None
    assert ssm_invites.has_invites() is True


def test_the_store_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssm_invites, "MAX_INVITES", 2)
    ssm_invites.create("a")
    ssm_invites.create("b")
    with pytest.raises(ssm_invites.InviteLimitError):
        ssm_invites.create("c")


def test_the_ssm_principal_is_keyed_by_the_invite_not_the_alias() -> None:
    one = ssm_principal("abc123", "Dana")
    assert one.id == ssm_principal("abc123", "Renamed").id
    assert one.id != ssm_principal("abc124", "Dana").id
    assert one.provenance is Provenance.SSM_INVITE
    assert one.display.name == "Dana"


# ── setup path ──


def test_the_setup_path_follows_the_listener() -> None:
    assert access.setup_paths_dict()["ssm"]["reason"] == "not_configured"
    ssm.note_listener(ssm.Listener(ssm.STATE_UNAVAILABLE, ssm.REASON_NO_TARGET, 5477))
    assert access.setup_paths_dict()["ssm"] == {"state": "unavailable", "reason": "no_target"}
    ssm.note_listener(ssm.Listener(ssm.STATE_ACTIVE, "", 5477, ssm.Target(INSTANCE)))
    assert access.setup_paths_dict()["ssm"]["state"] == CapabilityState.AVAILABLE.value


# ── resolve ──


def test_resolve_never_binds_without_a_target_a_free_port_and_the_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = SharedCrewConfig(ssm=True, ssm_port=5477)
    monkeypatch.setattr(ssm, "detect_target", lambda: None)
    assert ssm_gate.resolve(SharedCrewConfig(), 5476)[1] is None
    answer, gate = ssm_gate.resolve(on, 5476)
    assert (answer.reason, gate) == (ssm.REASON_NO_TARGET, None)
    answer, gate = ssm_gate.resolve(on, 5477)
    assert (answer.reason, gate) == (ssm.REASON_PORT_IN_USE, None)
    monkeypatch.setattr(ssm, "detect_target", lambda: ssm.Target(INSTANCE, "us-east-1"))
    answer, gate = ssm_gate.resolve(on, 5476)
    assert answer.active and gate is not None and gate.port == 5477
    assert ssm_invites._cache is not None, "resolve warms the invite store off the event loop"


# ── the gate: real sockets, the real token middleware ──


OWNER = "local-app"


async def _whoami(request: web.Request) -> web.Response:
    actor = actor_of(request)
    return web.json_response(
        {
            "user": request.get("user"),
            "provenance": actor.principal.provenance.value,
            "kind": actor.principal.kind.value,
            "owner": is_owner_dashboard_request(request),
            "internal_auth": request.get("internal_auth") is True,
            "app": request.get("app"),
        }
    )


async def _slow(request: web.Request) -> web.Response:
    request.app["slow_started"].set()
    await asyncio.sleep(10)
    return web.json_response({})


class Servers:
    def __init__(self, main: int, ssm_port: int, runner: web.AppRunner) -> None:
        self.main = main
        self.ssm = ssm_port
        self.runner = runner


@pytest_asyncio.fixture
async def servers() -> AsyncIterator[Servers]:
    ref = ssm_gate.GateRef()
    app = web.Application(
        middlewares=[
            token_auth_middleware(
                internal_secret="s3cret",
                internal_paths=frozenset({"/api/internal-only"}),
                ssm_gate=ref,
            )
        ]
    )
    app["state"] = SimpleNamespace(owner_id="")
    app["slow_started"] = asyncio.Event()
    app.router.add_get("/api/whoami", _whoami)
    app.router.add_get("/api/slow", _slow)
    app.router.add_get("/api/internal-only", _whoami)
    routes_ssm.register(app)
    runner = web.AppRunner(app)
    await runner.setup()
    ports: list[int] = []
    for _ in range(2):
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        ports.append(site._server.sockets[0].getsockname()[1])  # type: ignore[union-attr]
    ref.gate = ssm_gate.SsmGate(port=ports[1])
    ssm.note_listener(ssm.Listener(ssm.STATE_ACTIVE, "", ports[1], ssm.Target(INSTANCE)))
    try:
        yield Servers(ports[0], ports[1], runner)
    finally:
        await runner.cleanup()


def _jar() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())


async def _get(
    port: int,
    path: str,
    *,
    params: dict[str, str] | None = None,
    headers: Any = None,
    cookies: dict[str, str] | None = None,
) -> tuple[int, Any, aiohttp.ClientResponse]:
    async with _jar() as client:
        async with client.get(
            f"http://127.0.0.1:{port}{path}",
            params=params,
            headers=headers,
            cookies=cookies,
            allow_redirects=False,
        ) as resp:
            text = await resp.text()
            try:
                body = json.loads(text)
            except ValueError:
                body = text
            return resp.status, body, resp


async def _join(port: int, credential: str) -> dict[str, str]:
    status, _body, resp = await _get(port, "/", params={"ssm_invite": credential})
    assert status == 302
    cookie = resp.cookies[ssm_gate.COOKIE_NAME]
    assert cookie["httponly"] and cookie["samesite"] == "Strict"
    assert "ssm_invite" not in resp.headers["Location"]
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    return {ssm_gate.COOKIE_NAME: cookie.value}


def _owner_token() -> str:
    return generate_token(OWNER, ttl_seconds=300)


@pytest.mark.asyncio
async def test_the_owner_still_is_the_owner_on_the_dashboard_port(servers: Servers) -> None:
    status, body, _ = await _get(servers.main, "/api/whoami", params={"token": _owner_token()})
    assert status == 200 and body["user"] == OWNER and body["owner"] is True


@pytest.mark.asyncio
async def test_a_connection_on_the_ssm_port_with_no_invite_is_refused(servers: Servers) -> None:
    status, body, _ = await _get(servers.ssm, "/api/whoami")
    assert status == 403 and body["code"] == "ssm_refused"
    status, body, _ = await _get(servers.ssm, "/api/whoami", params={"ssm_invite": "a.b"})
    assert status == 403 and body["code"] == "ssm_refused"


@pytest.mark.asyncio
async def test_the_owners_dashboard_token_does_not_make_an_ssm_connection_the_owner(
    servers: Servers,
) -> None:
    token = _owner_token()
    cookie = {f"mc_token_{servers.ssm}": token, f"mc_token_{servers.main}": token}
    for kwargs in ({"params": {"token": token}}, {"cookies": cookie}):
        status, _body, _ = await _get(servers.ssm, "/api/whoami", **kwargs)
        assert status == 403


@pytest.mark.asyncio
async def test_the_internal_secret_is_worth_nothing_on_the_ssm_port(servers: Servers) -> None:
    for path in ("/api/whoami", "/api/internal-only"):
        status, _body, _ = await _get(servers.ssm, path, headers={"X-Internal-Secret": "s3cret"})
        assert status == 403
    _id, credential = ssm_invites.create("Dana")
    cookie = await _join(servers.ssm, credential)
    status, _body, _ = await _get(servers.ssm, "/api/internal-only", cookies=cookie)
    assert status == 403


@pytest.mark.asyncio
async def test_a_forwarded_connection_is_never_the_owner_even_holding_every_credential(
    servers: Servers,
) -> None:
    invite_id, credential = ssm_invites.create("Dana")
    cookie = await _join(servers.ssm, credential)
    token = _owner_token()
    status, body, _ = await _get(
        servers.ssm,
        "/api/whoami",
        params={"token": token},
        headers={"X-Internal-Secret": "s3cret"},
        cookies={**cookie, f"mc_token_{servers.ssm}": token},
    )
    assert status == 403, "the internal secret header alone refuses the request"
    status, body, _ = await _get(
        servers.ssm,
        "/api/whoami",
        params={"token": token},
        cookies={**cookie, f"mc_token_{servers.ssm}": token},
    )
    assert status == 200
    assert body["user"] == f"ssm:{invite_id}"
    assert body["owner"] is False
    assert body["provenance"] == Provenance.SSM_INVITE.value
    assert body["internal_auth"] is False and body["app"] == ""
    # The owner-only routes refuse the guest.
    for path in ("/api/shared-crew/ssm",):
        status, refused, _ = await _get(servers.ssm, path, cookies=cookie)
        assert status == 403 and refused["code"] == "owner_only"
    async with _jar() as client:
        async with client.post(
            f"http://127.0.0.1:{servers.ssm}/api/shared-crew/ssm/invites",
            json={"alias": "x"},
            cookies=cookie,
        ) as resp:
            assert resp.status == 403
    assert len(ssm_invites.public_rows()) == 1


@pytest.mark.asyncio
async def test_the_invite_cookie_opens_nothing_on_the_dashboard_port(servers: Servers) -> None:
    _id, credential = ssm_invites.create("Dana")
    cookie = await _join(servers.ssm, credential)
    status, _body, _ = await _get(servers.main, "/api/whoami", cookies=cookie)
    assert status == 403
    status, _body, _ = await _get(servers.main, "/api/whoami", params={"ssm_invite": credential})
    assert status == 403


@pytest.mark.asyncio
async def test_a_revoked_invite_stops_working_on_the_next_request(servers: Servers) -> None:
    invite_id, credential = ssm_invites.create("Dana")
    cookie = await _join(servers.ssm, credential)
    assert (await _get(servers.ssm, "/api/whoami", cookies=cookie))[0] == 200
    ssm_invites.revoke(invite_id)
    assert (await _get(servers.ssm, "/api/whoami", cookies=cookie))[0] == 403


@pytest.mark.asyncio
async def test_a_revoked_invite_closes_a_request_that_is_still_open(
    servers: Servers, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ssm_gate, "REVERIFY_SECS", 0.05)
    invite_id, credential = ssm_invites.create("Dana")
    cookie = await _join(servers.ssm, credential)

    async def revoke_once_open() -> None:
        await asyncio.wait_for(servers.runner.app["slow_started"].wait(), timeout=5)
        ssm_invites.revoke(invite_id)

    task = asyncio.create_task(revoke_once_open())
    # The server closes the connection; aiohttp retries an idempotent GET once,
    # and that retry meets the revoked invite. Either way the 10 s handler ends
    # within a re-check, and nothing is served.
    try:
        status, _body, _ = await asyncio.wait_for(
            _get(servers.ssm, "/api/slow", cookies=cookie), timeout=5
        )
        assert status == 403
    except aiohttp.ClientError:
        pass
    await task


def test_the_gate_owns_only_the_loopback_ssm_port() -> None:
    def request(sockname: Any) -> Any:
        transport = SimpleNamespace(get_extra_info=lambda name: sockname)
        return SimpleNamespace(transport=transport)

    gate = ssm_gate.SsmGate(port=5477)
    assert gate.owns(request(("127.0.0.1", 5477)))
    assert gate.owns(request(("::1", 5477, 0, 0)))
    assert not gate.owns(request(("127.0.0.1", 5476)))
    assert not gate.owns(request(("100.64.0.1", 5477)))
    assert not gate.owns(request(None))
    assert not gate.owns(SimpleNamespace(transport=None))


# ── owner routes ──


@pytest.mark.asyncio
async def test_the_owner_makes_and_revokes_invites(servers: Servers) -> None:
    token = _owner_token()
    async with _jar() as client:
        base = f"http://127.0.0.1:{servers.main}"
        async with client.get(f"{base}/api/shared-crew/ssm", params={"token": token}) as resp:
            status = await resp.json()
        assert status["state"] == "active" and status["invites"] == []
        assert status["command"].startswith("aws ssm start-session --target " + INSTANCE)
        assert status["local_url"] == f"http://localhost:{servers.ssm}/"
        access_set.acknowledge_first_share("owner")
        async with client.post(
            f"{base}/api/shared-crew/ssm/invites", params={"token": token}, json={"alias": "Dana"}
        ) as resp:
            made = await resp.json()
        assert made["invite"]["alias"] == "Dana"
        credential = made["url"].split("ssm_invite=", 1)[1]
        assert ssm_invites.verify(credential) is not None
        async with client.get(f"{base}/api/shared-crew/ssm", params={"token": token}) as resp:
            listed = await resp.json()
        assert credential not in json.dumps(listed)
        invite_id = made["invite"]["id"]
        async with client.delete(
            f"{base}/api/shared-crew/ssm/invites/{invite_id}", params={"token": token}
        ) as resp:
            assert (await resp.json())["revoked"] is True
        async with client.delete(
            f"{base}/api/shared-crew/ssm/invites/{invite_id}", params={"token": token}
        ) as resp:
            assert resp.status == 404
    assert ssm_invites.verify(credential) is None


@pytest.mark.asyncio
async def test_the_first_invite_waits_for_the_first_share_acknowledgement(
    servers: Servers,
) -> None:
    token = _owner_token()
    url = f"http://127.0.0.1:{servers.main}/api/shared-crew/ssm/invites"
    async with _jar() as client:
        async with client.post(url, params={"token": token}, json={"alias": "Dana"}) as resp:
            assert resp.status == 409
            assert (await resp.json())["code"] == "first_share_ack_required"
        assert ssm_invites.public_rows() == []
        access_set.acknowledge_first_share("owner")
        async with client.post(url, params={"token": token}, json={"alias": "Dana"}) as resp:
            assert resp.status == 200
    # An SSM invite is access: with one out, someone else can open the Crew.
    assert access_set.nobody_else_has_access() is False


def test_an_ssm_invite_counts_as_someone_else_having_access() -> None:
    assert access_set.nobody_else_has_access() is True
    ssm_invites.create("Dana")
    assert access_set.nobody_else_has_access() is False


@pytest.mark.asyncio
async def test_an_invite_reads_joined_once_someone_opens_it_and_names_their_person(
    servers: Servers,
) -> None:
    invite_id, credential = ssm_invites.create("Dana")
    [row] = ssm_invites.public_rows()
    assert row["joined"] is False
    assert row["principal_id"] == ssm_principal(invite_id).id
    await _join(servers.ssm, credential)
    [row] = ssm_invites.public_rows()
    assert row["joined"] is True
    ssm_invites._reset_for_tests()
    assert ssm_invites.public_rows()[0]["joined"] is True
    # A refused credential joins nobody.
    other, _ = ssm_invites.create("Lee")
    await _get(servers.ssm, "/", params={"ssm_invite": f"{other}.wrong"})
    assert [r["joined"] for r in ssm_invites.public_rows() if r["id"] == other] == [False]


@pytest.mark.asyncio
async def test_no_invite_is_made_while_the_listener_is_not_active(servers: Servers) -> None:
    ssm.note_listener(ssm.Listener(ssm.STATE_UNAVAILABLE, ssm.REASON_NO_TARGET, servers.ssm))
    async with _jar() as client:
        async with client.post(
            f"http://127.0.0.1:{servers.main}/api/shared-crew/ssm/invites",
            params={"token": _owner_token()},
            json={},
        ) as resp:
            assert resp.status == 409
            assert (await resp.json())["code"] == "ssm_not_active"
    assert ssm_invites.public_rows() == []


@pytest.mark.asyncio
async def test_the_routes_answer_404_without_the_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard.routes import shared_crew as slice_

    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "0")
    app = web.Application()
    slice_.register(app)
    handler = next(
        r.handler
        for r in app.router.routes()
        if r.resource is not None and r.resource.canonical == "/api/shared-crew/ssm"
    )
    with pytest.raises(web.HTTPNotFound):
        await handler(SimpleNamespace())
