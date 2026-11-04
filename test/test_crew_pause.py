"""Ultra Mode "Pause all" / "Resume all": the pause record and the rules it drives.

Pause stops every running crew member at every depth and leaves one that waits on
the person alone; while it stands the root and the stopped members start no turn,
their patrol wakes are refused, and nobody in the crew opens a session; Resume all
continues exactly the stopped set; the record survives a reload.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_mode, crew_pause


@pytest.fixture(autouse=True)
def _pause_store(tmp_path, monkeypatch):
    path = tmp_path / "work-ledger" / "crew-pause.json"
    monkeypatch.setattr(crew_pause, "_path", lambda: path)
    monkeypatch.setattr(crew_pause, "_loaded_from", None)
    monkeypatch.setattr(crew_pause, "_records", {})
    monkeypatch.setattr(crew_pause, "_priors", {})
    monkeypatch.setattr(crew_mode, "_known_roots", set())
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    return path


def _forget_memory() -> None:
    """What a gateway restart does to the in-memory copy (the page reload reads the file)."""
    crew_pause._records.clear()
    crew_pause._priors.clear()
    crew_pause._loaded_from = None


# ── the record ─────────────────────────────────────────────────────────────────


def test_a_pause_holds_the_root_and_what_it_stopped_and_survives_a_reload(_pause_store):
    crew_pause.pause("root", ["w1", "w2", "w3"], ["w1", "w3"])
    _forget_memory()
    assert crew_pause.record("root")["stopped"] == ["w1", "w3"]
    # The whole crew is held, idle members included (their patrol wakes too).
    assert crew_pause.holds_turn("root") and crew_pause.holds_turn("w3")
    assert crew_pause.holds_turn("w2") and not crew_pause.holds_turn("other")
    assert crew_pause.blocks_create("root") and crew_pause.blocks_create("w2")
    assert not crew_pause.blocks_create("other")
    assert oct(_pause_store.stat().st_mode & 0o777) == "0o600"


def test_a_members_own_continue_releases_only_that_member():
    crew_pause.pause("root", ["w1", "w2"], ["w1", "w2"])
    assert crew_pause.release("w1") is True
    assert not crew_pause.holds_turn("w1") and crew_pause.holds_turn("w2")
    # Still in the crew, so it still opens no session while paused.
    assert crew_pause.blocks_create("w1")
    _forget_memory()
    assert crew_pause.record("root")["stopped"] == ["w2"]


def test_resume_drops_the_record_and_everything_it_held():
    crew_pause.pause("root", ["w1"], ["w1"])
    assert crew_pause.resume("root")["stopped"] == ["w1"]
    assert crew_pause.record("root") is None
    assert not crew_pause.holds_turn("root") and not crew_pause.blocks_create("w1")
    assert crew_pause.resume("root") is None


def test_a_write_that_fails_changes_no_live_state(monkeypatch):
    crew_pause.pause("root", ["w1"], ["w1"])

    def _fail(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(crew_pause, "atomic_write", _fail)
    with pytest.raises(OSError):
        crew_pause.resume("root")
    assert crew_pause.record("root") is not None and crew_pause.holds_turn("w1")
    with pytest.raises(OSError):
        crew_pause.release("w1")
    assert crew_pause.holds_turn("w1")


def test_a_damaged_file_pauses_nothing(_pause_store):
    _pause_store.parent.mkdir(parents=True)
    _pause_store.write_text("{not json", encoding="utf-8")
    assert crew_pause.record("root") is None and not crew_pause.holds_turn("root")


# ── the routes ─────────────────────────────────────────────────────────────────


def _row(key: str, **over: Any) -> dict[str, Any]:
    return {"key": key, "agent": "kirocrew", "agent_kind": "template", "parent": None, **over}


# root runs; w1 runs; its sub-team member w3 runs too; w2 waits on an approval;
# w4 is idle; "other" is outside the crew.
_TREE = [
    _row("root", agent="kirocrew-conductor", running=True),
    _row("w1", created_by="root", running=True),
    _row("w2", created_by="root", running=True, pending_approval=True),
    _row("w3", created_by="w1", running=True),
    _row("w4", created_by="root"),
    _row("other", running=True),
]


def _fake_slot(key: str, *, queued: int = 0) -> Any:
    return SimpleNamespace(
        key=key,
        running=False,
        linked_turn_running=False,
        queue_depth=queued,
        _queue=[{"id": "q"}] * queued,
        _lock=asyncio.Lock(),
        _turn_admission_reserved=False,
        is_closing=False,
        _stop_state="idle",
        window_pending=False,
    )


def _state(rows: list[dict[str, Any]], slots: dict[str, Any] | None = None) -> Any:
    def serialize_slots(**_k: Any) -> list[dict[str, Any]]:
        out = [dict(r) for r in rows]
        crew_mode.stamp_rows(out)
        return out

    slots = slots if slots is not None else {r["key"]: _fake_slot(r["key"]) for r in rows}
    return SimpleNamespace(
        serialize_slots=serialize_slots, get_slot=slots.get, push_slots_update=lambda: None
    )


def _req(state: Any, slot: str = "root") -> Any:
    app = web.Application()
    app["state"] = state
    return make_mocked_request("POST", "/x", app=app, match_info={"slot": slot})


@pytest.fixture
def routes(monkeypatch):
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard import state as dash_state
    from kiro_crew.dashboard.handlers import crew_pause as handler

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(handler, "_gate", _allow)
    calls: dict[str, list[Any]] = {"stop": [], "continue": [], "drain": [], "notice": []}

    async def _stop(state: Any, slot: Any, **kw: Any) -> dict[str, Any]:
        calls["stop"].append((slot.key, kw.get("escalate")))
        return {"ok": True}

    async def _continue(state: Any, slot: Any, **kw: Any) -> str:
        calls["continue"].append(slot.key)
        return "resumed"

    async def _drain(state: Any, slot: Any, **kw: Any) -> bool:
        calls["drain"].append(slot.key)
        return True

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", _stop)
    monkeypatch.setattr(chat_handlers, "continue_slot_turn", _continue)
    monkeypatch.setattr(chat_handlers, "_start_next_queued_turn", _drain)
    monkeypatch.setattr(
        dash_state, "append_and_surface", lambda *a, **kw: calls["notice"].append(kw["meta"])
    )
    return SimpleNamespace(handler=handler, calls=calls)


@pytest.mark.asyncio
async def test_pause_stops_every_running_member_at_every_depth_and_leaves_needs_you(routes):
    resp = await routes.handler.api_crew_pause(_req(_state(_TREE)))
    assert resp.status == 200, resp.text
    assert json.loads(resp.text)["stopped"] == ["w1", "w3"]
    stopped = sorted(key for key, _ in routes.calls["stop"])
    assert stopped == ["root", "w1", "w3"]
    # A first Stop press each: never escalated to a hard kill that drops the queue.
    assert {esc for _, esc in routes.calls["stop"]} == {False}
    rec = crew_pause.record("root")
    assert rec["stopped"] == ["root", "w1", "w3"]
    assert rec["tree"] == ["w1", "w2", "w3", "w4"]
    assert routes.calls["notice"] == [{"kind": "crew_paused_notice", "stopped": 2}]
    # Nothing new starts: the root, the needs-you member and the idle one open no session.
    assert all(crew_pause.blocks_create(k) for k in ("root", "w2", "w4"))
    assert crew_pause.holds_turn("w2") and not crew_pause.holds_turn("other")


@pytest.mark.asyncio
async def test_a_stop_the_compaction_declined_is_not_counted_as_stopped(routes, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    async def _stop(state: Any, slot: Any, **kw: Any) -> dict[str, Any]:
        return {"ok": True, "info": "compacting"} if slot.key == "w3" else {"ok": True}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", _stop)
    resp = await routes.handler.api_crew_pause(_req(_state(_TREE)))
    body = json.loads(resp.text)
    assert body["stopped"] == ["w1"] and body["not_stopped"] == ["w3"]
    assert crew_pause.record("root")["stopped"] == ["root", "w1"]
    # Still held, so its turn's end starts nothing.
    assert crew_pause.holds_turn("w3")


@pytest.mark.asyncio
async def test_resume_continues_exactly_what_the_pause_stopped(routes):
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    slots["w3"] = _fake_slot("w3", queued=1)
    resp = await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))
    assert resp.status == 200, resp.text
    # Stopped members are continued; the idle and needs-you ones only drain a queue.
    assert json.loads(resp.text)["resumed"] == {
        "w1": "continued",
        "w2": "idle",
        "w3": "drained",
        "w4": "idle",
    }
    assert sorted(routes.calls["continue"]) == ["root", "w1"]
    assert routes.calls["drain"] == ["w3"]
    assert crew_pause.record("root") is None
    again = await routes.handler.api_crew_resume(_req(_state(_TREE)))
    assert again.status == 409


@pytest.mark.asyncio
async def test_resume_gives_an_idle_root_no_continue_it_never_had_a_turn_for(routes):
    rows = [{**r, "running": False} if r["key"] == "root" else r for r in _TREE]
    await routes.handler.api_crew_pause(_req(_state(rows)))
    await routes.handler.api_crew_resume(_req(_state(rows)))
    assert "root" not in routes.calls["continue"]


@pytest.mark.asyncio
async def test_pause_refuses_a_session_that_is_not_a_crew_root(routes):
    resp = await routes.handler.api_crew_pause(_req(_state(_TREE), "other"))
    assert resp.status == 404
    assert crew_pause.record("other") is None


@pytest.mark.asyncio
async def test_the_pause_routes_take_the_owner_gate(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_pause as handler
    from kiro_crew.dashboard.handlers import crew_workers

    async def _deny(*_a: Any, **_k: Any) -> web.Response:
        return web.json_response({"code": "owner_only"}, status=403)

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(crew_workers, "_deny_app_caller", _allow)
    monkeypatch.setattr(crew_workers, "require_owner_dashboard_request", _deny)
    paused = await handler.api_crew_pause(_req(_state(_TREE)))
    resumed = await handler.api_crew_resume(_req(_state(_TREE)))
    assert paused.status == resumed.status == 403
    assert crew_pause.record("root") is None


@pytest.mark.asyncio
async def test_the_worker_list_carries_the_pause(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(crew_workers, "_gate", _allow)
    monkeypatch.setattr(crew_workers, "_closed_workers", lambda root, live: [])
    monkeypatch.setattr(crew_workers, "_ledger_items", lambda root: {})
    state = SimpleNamespace(**{**vars(_state(_TREE)), "get_slot": lambda _k: None})
    before = json.loads((await crew_workers.api_crew_workers(_req(state))).text)
    assert "paused" not in before
    crew_pause.pause("root", ["w1"], ["w1"])
    after = json.loads((await crew_workers.api_crew_workers(_req(state))).text)
    assert after["paused"]["stopped"] == ["w1"]


# ── what the pause holds ───────────────────────────────────────────────────────


def test_a_held_slot_keeps_its_queue_and_starts_no_turn(tmp_path):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_turn.queue_drain import _start_next_queued_turn

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-held")
    slot._queue.append({"id": "q1", "content": "queued while paused"})
    crew_pause.pause("chat-root", ["chat-held"], ["chat-held"])
    assert asyncio.run(_start_next_queued_turn(state, slot)) is False
    assert [e["id"] for e in slot._queue] == ["q1"]


def test_a_paused_crew_opens_no_new_session(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state
    from session_control_helpers import (  # noqa: F401 -- autouse fixtures register by import
        _agent_resolves,
        _enabled_fixture,
        _fresh_create_budget,
        _key,
        _slot,
    )

    from kiro_crew.dashboard import session_control as sc

    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    caller = _slot(state, "chat-root")
    monkeypatch.setattr(crew_mode, "slot_is_crew_mode_root", lambda slot: True)
    crew_pause.pause("chat-root", [], [])
    with pytest.raises(sc.SessionControlError) as refused:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    assert refused.value.code == "crew_paused"


@pytest.mark.asyncio
async def test_a_patrol_wake_on_a_paused_root_is_refused_as_busy(monkeypatch):
    from test_autonudge_dashboard_fire import _fake_spawn, _loop, _orchestrator
    from test_autonudge_dashboard_fire import _slot as nudge_slot

    from kiro_crew.slack import gateway as gw

    orch = _orchestrator()
    orch.dashboard_state.get_slot = MagicMock(return_value=nudge_slot("chat-1-1785"))
    monkeypatch.setattr(crew_mode, "slot_is_crew_mode_root", lambda slot: True)
    crew_pause.pause("chat-1-1785", [], [])
    spawn = _fake_spawn()
    with (
        patch.object(gw, "spawn_guarded_turn", spawn),
        patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
    ):
        assert await orch._fire_dashboard_nudge(_loop()) is False
    assert spawn.calls == []
    orch.autonudge_svc.remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_continue_on_a_paused_root_points_at_the_board(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_api.continue_turn import api_chat_slot_continue

    state = _make_state(tmp_path)
    state.get_or_create_slot("chat-root")
    monkeypatch.setattr(crew_mode, "slot_is_crew_mode_root", lambda slot: True)
    crew_pause.pause("chat-root", [], [])
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", "/x", app=app, match_info={"slot": "chat-root"})
    resp = await api_chat_slot_continue(req)
    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "crew_paused"


# ── Ultra Mode runs on the team lead ───────────────────────────────────────────


def test_ultra_mode_defaults_to_the_team_lead_and_a_set_key_keeps_its_value():
    from kiro_crew.config.loader_builders import _build_agent_config
    from kiro_crew.config.sections_agent import _CREW_MODE_AGENT_DEFAULT

    assert crew_mode.DEFAULT_CREW_MODE_AGENT == "kirocrew-team-lead"
    assert _CREW_MODE_AGENT_DEFAULT == crew_mode.DEFAULT_CREW_MODE_AGENT
    assert _build_agent_config({}).crew_mode_agent == "kirocrew-team-lead"
    assert _build_agent_config({"crew_mode_agent": "kirocrew-conductor"}).crew_mode_agent == (
        "kirocrew-conductor"
    )
    # The deprecated ledger alias still names the shipped conductor, not the default.
    assert crew_mode.CONDUCTOR_ALIASES["kirocrew-ledger-conductor"] == "kirocrew-conductor"


# ── switching a mid-chat Ultra Mode chat back ──────────────────────────────────


def _lock(slot: Any, agent: str, *, owner: bool = True, app: str = "", own_turn: bool = False):
    from kiro_crew.dashboard import chat_handlers

    caller = chat_handlers.SlotAgentSwitchCaller(
        app=app, remote="t", owner=owner, own_turn=own_turn
    )
    return chat_handlers._crew_mode_lock_refusal(slot, agent, "template", caller)


def _root_slot(key: str = "r") -> Any:
    return SimpleNamespace(
        key=key,
        agent="kirocrew-team-lead",
        agent_kind="template",
        crew_mode_agent="kirocrew-team-lead",
        mode="",
        memory_mode="persistent",
        _created_by="",
        _app="",
        instance_id="",
        messages=[{"role": "user", "content": "go"}],
        _disk_older_count=0,
    )


def test_a_mid_chat_ultra_chat_may_switch_back_to_its_prior_agent_only(monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    monkeypatch.setattr(chat_handlers, "sel", lambda: MagicMock())
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    root = _root_slot()
    # Born in Ultra Mode (no prior agent on record): locked as before.
    assert _lock(root, "kirocrew").status == 409
    crew_pause.remember_prior("r", "kirocrew", "template")
    assert _lock(root, "kirocrew") is None
    # Only onto that agent, and only by the person in the dashboard.
    assert _lock(root, "other-agent").status == 409
    assert _lock(root, "kirocrew", owner=False).status == 409
    assert _lock(root, "kirocrew", app="some-app").status == 409
    assert _lock(root, "kirocrew", own_turn=True).status == 409
    # A chat that was on the default agent (named none) goes back to a plain
    # template, never onto another conductor.
    crew_pause.remember_prior("r", "", "template")
    assert _lock(root, "kirocrew") is None
    assert _lock(root, "kirocrew-security-conductor").status == 409


def test_switching_back_frees_the_chat_and_keeps_its_crew_held():
    crew_pause.remember_prior("r", "kirocrew", "template")
    crew_pause.pause("r", ["w1"], ["r", "w1"])
    _forget_memory()
    assert crew_pause.prior_agent("r") == {"agent": "kirocrew", "agent_kind": "template"}
    crew_pause.switched_back("r")
    _forget_memory()
    assert crew_pause.prior_agent("r") is None
    # The chat is a normal chat again; its crew members wait for their own Continue.
    assert not crew_pause.holds_turn("r") and not crew_pause.blocks_create("r")
    assert not crew_pause.root_paused("r")
    assert crew_pause.holds_turn("w1")
    crew_pause.release("w1")
    assert not crew_pause.holds_turn("w1")


def test_the_slot_row_names_the_agent_it_switches_back_to():
    from kiro_crew.dashboard.slot_projection import _crew_switch_back

    assert _crew_switch_back("r") == {"crew_mode_switch_back": False, "crew_mode_prior_agent": ""}
    crew_pause.remember_prior("r", "", "template")
    assert _crew_switch_back("r") == {"crew_mode_switch_back": True, "crew_mode_prior_agent": ""}
    crew_pause.remember_prior("r", "kirocrew", "template")
    assert _crew_switch_back("r")["crew_mode_prior_agent"] == "kirocrew"


@pytest.mark.asyncio
async def test_switch_on_mid_chat_then_back_restores_the_previous_agent(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    from kiro_crew.config.loader import KiroCrewConfig, ResolvedBindings
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    def bindings(agent: str) -> ResolvedBindings:
        return ResolvedBindings(
            workspace_dir=tmp_path / "ws",
            effective_memory_config={},
            memory_store_name="default",
            requested_resolved=True,
            resolved_alias=agent,
            kiro_agent=agent,
            selection_kind="template",
            execution_context=ExecutionContext(
                None, MemoryStoreRef("default"), "template", agent, selection_name=agent
            ),
        )

    mod = "kiro_crew.dashboard.chat_handlers"
    monkeypatch.setattr(f"{mod}.KiroCrewConfig.load", lambda: KiroCrewConfig())
    monkeypatch.setattr(f"{mod}._workspace_name_for_dir", lambda cfg, ws_dir: "default")
    monkeypatch.setattr(f"{mod}.warm_project_agent_names", AsyncMock())
    monkeypatch.setattr(f"{mod}.cached_project_agent_names", lambda project_dir: frozenset())
    monkeypatch.setattr(f"{mod}.default_project_dir", lambda ws: str(tmp_path / "ws"))
    monkeypatch.setattr(f"{mod}.resolve_agent_bindings", lambda cfg, name, *a, **k: bindings(name))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    monkeypatch.setattr(crew_mode, "_configured", crew_mode.DEFAULT_CREW_MODE_AGENT)
    state = _make_state(tmp_path)
    state.sessions.reset = AsyncMock()
    chat = state.get_or_create_slot("r")
    chat.agent, chat.agent_kind = "", "template"
    chat.messages.append({"role": "user", "content": "hello"})
    caller = chat_handlers.SlotAgentSwitchCaller(app="", remote="", owner=True)

    on = await chat_handlers.switch_slot_agent(
        state, chat, "r", "kirocrew-team-lead", "template", caller
    )
    assert on.status == 200, on.text
    assert chat.crew_mode_agent == "kirocrew-team-lead"
    # The chat was on the default agent, which names none.
    assert crew_pause.prior_agent("r") == {"agent": "", "agent_kind": "template"}

    back = await chat_handlers.switch_slot_agent(state, chat, "r", "kirocrew", "template", caller)
    assert back.status == 200, back.text
    assert str(chat.agent) == "kirocrew" and chat.crew_mode_agent == ""
    assert crew_pause.prior_agent("r") is None


# ── the goal stays current ─────────────────────────────────────────────────────

_GOAL_FIRST = "call `work_ledger_record` `action=goal` FIRST, before you dispatch or reply"


def test_the_lead_prompt_and_both_skills_carry_the_goal_first_rule():
    from pathlib import Path

    from kiro_crew.agent_parts.spec_prompts import _TEAM_LEAD_SYSTEM_PROMPT

    skills = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "builtin_skills"
    texts = {
        "prompt": _TEAM_LEAD_SYSTEM_PROMPT,
        "team-lead": (skills / "team-lead" / "SKILL.md").read_text(encoding="utf-8"),
        "goal-conductor": (skills / "goal-conductor" / "SKILL.md").read_text(encoding="utf-8"),
    }
    for name, text in texts.items():
        flat = " ".join(text.split())
        assert _GOAL_FIRST in flat, name
        assert "`new_goal=true` for a new goal" in flat, name
        assert '"Done when ..." sentence' in flat, name


def test_an_ultra_mode_board_turn_tells_the_lead_to_write_the_goal(monkeypatch):
    from kiro_crew import dashboard_agentic

    monkeypatch.setattr(dashboard_agentic, "turn_block", lambda key: "[DASHBOARD] goal-board")
    monkeypatch.setattr(dashboard_agentic, "_shows_board", lambda key: True)
    from kiro_crew.dashboard_templates import instance as instance_store

    monkeypatch.setattr(instance_store, "read", lambda key: SimpleNamespace(state="adopted"))
    block = dashboard_agentic.session_turn_block("chat-root")
    assert dashboard_agentic.BOARD_IN_PANEL_LINE in block
    assert "If your ledger has no goal yet, record it now." in block
    assert _GOAL_FIRST in block


@pytest.mark.asyncio
async def test_a_send_to_a_held_idle_session_is_queued_not_started(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app, _make_state

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    started: list[Any] = []
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat._run_chat",
        AsyncMock(side_effect=lambda *a, **k: started.append(1)),
    )
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("held-slot")
    assert not slot.running
    crew_pause.pause("chat-root", ["held-slot"], ["held-slot"])
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat?ws=1", json={"message": "later", "slot": "held-slot"})
        assert resp.status == 200
        data = await resp.json()
    assert data["queued"] is True
    assert [e.get("content") for e in slot._queue] == ["later"]
    assert started == []


def test_a_peer_send_to_a_held_session_is_queued_not_started(tmp_path):
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("held-peer")
    crew_pause.pause("chat-root", ["held-peer"], [])
    run = AsyncMock()
    assert slot.enqueue_or_run_prompt("from a sibling", run, state) is False
    assert [e.get("content") for e in slot._queue] == ["from a sibling"]
    run.assert_not_called()


@pytest.mark.asyncio
async def test_resume_reaches_members_the_live_tree_no_longer_names(routes):
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    # The root's tab closed and the stamps no longer walk to it: the record still
    # names the crew, so every member with a tab is handed back.
    rows = [r for r in _TREE if r["key"] != "root"]
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    resp = await routes.handler.api_crew_resume(_req(_state(rows, slots)))
    assert resp.status == 200, resp.text
    assert set(json.loads(resp.text)["resumed"]) == {"w1", "w2", "w3", "w4"}
    assert crew_pause.record("root") is None


@pytest.mark.asyncio
async def test_a_stopped_turn_still_tearing_down_stays_held_for_the_next_resume(
    routes, monkeypatch
):
    monkeypatch.setattr(routes.handler, "_SETTLE_TRIES", 1)
    monkeypatch.setattr(routes.handler, "_SETTLE_STEP_SECS", 0)
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    slots["w1"].running = True
    resp = await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))
    body = json.loads(resp.text)
    assert body["paused"] is True and body["resumed"]["w1"] == "running"
    rec = crew_pause.record("root")
    assert rec["stopped"] == ["w1"] and crew_pause.holds_turn("w1")
    # The rest of the crew is free, and so is the root's own turn.
    assert not crew_pause.holds_turn("w3") and not crew_pause.holds_turn("root")
    slots["w1"].running = False
    again = await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))
    assert json.loads(again.text)["resumed"] == {"w1": "continued"}
    assert crew_pause.record("root") is None


def test_after_switch_back_only_held_members_are_blocked_and_the_record_ends():
    crew_pause.pause("r", ["w1", "w2"], ["w1"])
    crew_pause.switched_back("r")
    crew_pause.release("w1")
    assert not crew_pause.blocks_create("w1") and crew_pause.blocks_create("w2")
    crew_pause.release("w2")
    assert crew_pause.record("r") is None


def test_a_chat_that_is_no_longer_a_root_is_not_held_as_one():
    """A stale record (its switched-back update lost to a failed write) never holds
    a chat that is a normal chat now; its held crew members stay held."""
    crew_pause.pause("r", ["w1"], ["w1"])
    assert crew_pause.holds_turn("r", lambda: True)
    assert not crew_pause.holds_turn("r", lambda: False)
    assert not crew_pause.blocks_create("r", lambda: False)
    assert crew_pause.holds_turn("w1", lambda: False)


@pytest.mark.asyncio
async def test_a_slow_resume_never_overwrites_a_newer_pause(routes, monkeypatch):
    crew_pause.pause("root", ["w1", "w2"], ["w1", "w2"])
    crew_pause.keep_pending("root", ["w9"], root_pending=False)
    rec = crew_pause.record("root")
    assert set(rec["held"]) >= {"w1", "w2", "w9"} and set(rec["stopped"]) >= {"w1", "w2", "w9"}
