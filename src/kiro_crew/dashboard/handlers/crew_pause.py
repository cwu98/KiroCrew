"""Ultra Mode "Pause all" / "Resume all" for one root and its whole crew.

``POST /api/chat/slots/{slot}/crew-workers/pause`` stops the root's turn and every
running crew member's turn at every depth with the Stop button's own mechanism
(``stop_slot_turn``), leaves a member that waits on the person (a tool approval or
an open question card) as it is, and records the pause (``kiro_crew.crew_pause``):
while it stands the root and the stopped members start no turn (their queued
messages stay queued, the root's patrol wakes are refused as busy) and no one in
the crew may open a new session. The root's chat gets one notice row.

``POST /api/chat/slots/{slot}/crew-workers/resume`` drops the record and hands
each member the pause stopped back with the Continue button's own mechanism
(``continue_slot_turn``), or drains what queued for it meanwhile. Only what the
pause stopped is continued.

Both take the gate every other crew-workers route takes: app tokens are answered
404 and the caller must be the dashboard owner. Neither reaches a session outside
the named root's own tree.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from aiohttp import web

from kiro_crew import crew_pause
from kiro_crew.dashboard.handlers.crew_workers import (
    _bad,
    _gate,
    _live_root,
    _not_root,
    _stamped_rows,
    _tree_workers,
)

logger = logging.getLogger(__name__)

#: The ``meta["kind"]`` of the root's "You paused the crew" row. A wire value the
#: dashboard keys its localized card on (``CrewPausedNoticeCard.tsx``).
CREW_PAUSED_NOTICE_KIND = "crew_paused_notice"

#: Fallback text for readers that cannot localize (channel mirrors, SSE).
_NOTICE_TEXT = "You paused the crew. {n} workers stopped. Resume from the Board."


def _waits_on_person(row: Mapping[str, Any]) -> bool:
    return bool(row.get("pending_approval") or row.get("needs_input"))


def _is_running(row: Mapping[str, Any]) -> bool:
    return bool(row.get("running") or row.get("linked_turn_running"))


#: ``stop_slot_turn`` answers that mean the turn did NOT stop: nothing ran, the
#: provider held no turn to cancel, or the session's automatic compaction declined
#: the cooperative cancel.
_NOT_STOPPED = frozenset({"not running", "no active turn", "compacting"})


async def _stop(state: Any, key: str) -> bool:
    """Stop *key*'s turn as a first Stop press would. True only when a turn stopped."""
    from kiro_crew.dashboard import chat_handlers

    slot = state.get_slot(key)
    if slot is None:
        return False
    try:
        # Never a hard kill: a pause pressed twice is not a second decision, and a
        # hard kill would discard the queue the pause promises to keep.
        out = await chat_handlers.stop_slot_turn(
            state, slot, source="dashboard:crew_pause", escalate=False
        )
    except Exception:
        logger.warning("crew pause: stop failed for %s", key, exc_info=True)
        return False
    return out.get("info") not in _NOT_STOPPED


#: One Pause all or Resume all at a time per root, so a Resume still waiting on a
#: stopped turn's teardown never interleaves with a newer Pause all.
_ROOT_LOCKS: dict[str, asyncio.Lock] = {}


def _root_lock(root: str) -> asyncio.Lock:
    return _ROOT_LOCKS.setdefault(root, asyncio.Lock())


async def api_crew_pause(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/crew-workers/pause -- pause the whole crew."""
    refused = await _gate(request, "crew_mode.pause")
    if refused is not None:
        return refused
    async with _root_lock(request.match_info.get("slot", "")):
        return await _pause(request)


async def _pause(request: web.Request) -> web.Response:
    state = request.app["state"]
    root = request.match_info.get("slot", "")
    rows = _stamped_rows(state)
    kind, root_row = _live_root(rows, root)
    if kind != "crew" or root_row is None:
        return _not_root()
    members = _tree_workers(rows, root)
    tree = [str(row.get("key")) for row in members]
    targets = [
        str(row.get("key")) for row in members if _is_running(row) and not _waits_on_person(row)
    ]
    # The root is held for the whole pause; it is continued on resume only when
    # the pause stopped a turn of its own.
    # A root waiting on the person (an approval, a question card) is left as it
    # is, like a member in that state: stopping it would cancel the wait.
    own = [root] if _is_running(root_row) and not _waits_on_person(root_row) else []
    # Recorded BEFORE the stops: a stopped turn's own end must already find its
    # queue held, or it would start the next queued message on its way out.
    await asyncio.to_thread(crew_pause.pause, root, tree, own + targets)
    asked = [*own, *targets]
    results = await asyncio.gather(*(_stop(state, key) for key in asked))
    really = [key for key, ok in zip(asked, results, strict=True) if ok]
    # Only a turn that stopped is continued on resume. One that kept running (a
    # compaction declined the cancel) stays held when it ends, like the rest.
    if len(really) != len(asked):
        await asyncio.to_thread(crew_pause.settle_stopped, root, really)
    workers_stopped = [key for key in really if key != root]
    slot = state.get_slot(root)
    if slot is not None:
        from kiro_crew.dashboard.state import append_and_surface

        await asyncio.to_thread(
            append_and_surface,
            state,
            slot,
            "notice",
            _NOTICE_TEXT.format(n=len(workers_stopped)),
            "msg msg-info",
            meta={"kind": CREW_PAUSED_NOTICE_KIND, "stopped": len(workers_stopped)},
        )
    state.push_slots_update()
    not_stopped = [key for key in targets if key not in really]
    _audit(root, "pause", f"stopped={len(workers_stopped)} not_stopped={len(not_stopped)}")
    return web.json_response(
        {"ok": True, "paused": True, "stopped": workers_stopped, "not_stopped": not_stopped}
    )


def _queue_is_restored(slot: Any) -> bool:
    """Whether every queued entry came from disk (a restart during the pause)."""
    from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY

    queue = list(getattr(slot, "_queue", None) or [])
    return bool(queue) and all(entry.get(RESTORED_QUEUE_KEY) for entry in queue)


async def drain_held_queue(state: Any, slot: Any) -> str:
    """Start *slot*'s next queued turn under its lock, as an idle drain does.

    Re-checked under the lock: a send or Continue that started a turn while the
    caller awaited must not get a second runner.
    """
    from kiro_crew.dashboard import chat_handlers

    async with slot._lock:
        if (
            state.get_slot(slot.key) is not slot
            or slot.running
            or slot.linked_turn_running
            or slot._turn_admission_reserved
            or slot.is_closing
            or slot._stop_state != "idle"
        ):
            return "running"
        slot._turn_admission_reserved = True
        try:
            started = await chat_handlers._start_next_queued_turn(state, slot)
        finally:
            slot._turn_admission_reserved = False
    return "drained" if started else "held"


#: How long a stopped turn is given to finish tearing down before resume.
_SETTLE_TRIES = 25
_SETTLE_STEP_SECS = 0.2


async def _hand_back(state: Any, key: str, *, stopped: bool = True) -> str:
    """Continue *key* as its own Continue button would; drain its queue if one waits.

    A key the pause did not stop (*stopped* False: the root when it was idle, a
    member that was idle or waiting on the person) only has its held queue
    drained: it gets no Continue it never had a turn for. A queue restored from
    disk is never drained on its own, so a stopped turn is continued over it, as
    the Continue button does.
    """
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.state import load_window_off_loop

    slot = state.get_slot(key)
    if slot is None:
        return "closed"
    # A stopped turn may still be tearing down: give it a short while to settle,
    # so its continuation is not lost to a "still running" reading.
    for _ in range(_SETTLE_TRIES if stopped else 0):
        if not (slot.running or slot.linked_turn_running):
            break
        await asyncio.sleep(_SETTLE_STEP_SECS)
    if slot.running or slot.linked_turn_running:
        return "running"
    # A restored tab loads its window off the loop before anything reads it.
    try:
        await load_window_off_loop(slot)
    except Exception:
        logger.warning("crew resume: window of %s could not be loaded", key, exc_info=True)
        return "history_unavailable"
    if state.get_slot(key) is not slot:
        return "closed"
    try:
        if slot.queue_depth and not _queue_is_restored(slot):
            # What queued while paused runs first; it carries the thread on.
            return await drain_held_queue(state, slot)
        if not stopped:
            return "idle"
        await chat_handlers.continue_slot_turn(
            state, slot, directive_user_origin=True, allow_restored_queue=True
        )
        return "continued"
    except chat_handlers.SlotContinueRefusal as exc:
        return exc.code
    except Exception:
        logger.warning("crew resume: continue failed for %s", key, exc_info=True)
        return "error"


async def api_crew_resume(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/crew-workers/resume -- continue what the pause stopped.

    Answers for a paused root whose tab is closed too, so a pause can always be
    lifted: the record is dropped and only what is still open is handed back.
    """
    refused = await _gate(request, "crew_mode.resume")
    if refused is not None:
        return refused
    async with _root_lock(request.match_info.get("slot", "")):
        return await _resume(request)


async def _resume(request: web.Request) -> web.Response:
    state = request.app["state"]
    root = request.match_info.get("slot", "")
    kind, _root_row = _live_root(_stamped_rows(state), root)
    if kind != "crew" and crew_pause.record(root) is None:
        return _not_root()
    record = await asyncio.to_thread(crew_pause.resume, root)
    if record is None:
        return _bad("not_paused", "this crew is not paused", status=409)
    # Every session the record held or stopped that still has a tab, read from
    # the record itself: after a restart with the root's tab closed, or once a
    # member left the tree, the live stamps no longer name them, and their held
    # queues must still drain.
    stopped = set(record.get("stopped") or [])
    keys = sorted(
        key
        for key in set(record.get("tree") or []) | stopped
        if key != root and state.get_slot(key) is not None
    )
    outcomes = await asyncio.gather(
        _hand_back(state, root, stopped=root in stopped),
        *(_hand_back(state, key, stopped=key in stopped) for key in keys),
    )
    # A stopped turn still tearing down after the wait is held again on its own,
    # so the next Resume all continues it instead of losing it.
    root_pending = outcomes[0] == "running" and root in stopped
    pending = [
        k for k, o in zip(keys, outcomes[1:], strict=True) if o == "running" and k in stopped
    ]
    if pending or root_pending:
        await asyncio.to_thread(crew_pause.keep_pending, root, pending, root_pending=root_pending)
    state.push_slots_update()
    _audit(root, "resume", f"handed_back={len(keys)} pending={len(pending)}")
    return web.json_response(
        {
            "ok": True,
            "paused": bool(pending or root_pending),
            "root": outcomes[0],
            "resumed": dict(zip(keys, outcomes[1:], strict=True)),
        }
    )


def pause_view(root: str) -> dict[str, Any] | None:
    """What the worker list says about *root*'s pause: ``{stopped, paused_at}`` or ``None``."""
    rec = crew_pause.record(root)
    if rec is None:
        return None
    return {"stopped": rec.get("stopped") or [], "paused_at": rec.get("paused_at") or ""}


def _audit(root: str, action: str, detail: str) -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=f"dashboard:{root}",
            operation=f"crew_mode.{action}",
            outcome="ok",
            source="dashboard",
            resources=detail,
        )
    except Exception:
        logger.debug("crew %s: audit failed", action, exc_info=True)
