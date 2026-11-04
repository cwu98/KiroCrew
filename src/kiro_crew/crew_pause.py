"""Ultra Mode "Pause all" and "switch back": what a paused crew stopped, and the
agent a chat ran before it was switched to Ultra Mode mid-chat.

Kept next to the work ledger (``<data home>/work-ledger/crew-pause.json``) so a page
reload or a gateway restart still reads them. Two maps:

``paused``, one record per paused root:

* ``tree``: the root's crew when it was paused, every depth. While the record
  stands none of them, nor the root, may open a new session
  (:func:`blocks_create`).
* ``held``: the crew members that start no turn (:func:`holds_turn`): the whole
  tree at first, less each member whose own Continue the person pressed
  (:func:`release`). The root is held for the whole pause. A held session's
  patrol wakes are refused, and its queued and newly sent messages wait.
* ``stopped``: the members whose running turn the pause stopped: what Resume
  all continues.

``prior``, one entry per chat that was switched to Ultra Mode after it already
had messages: the agent (and its namespace) it ran before, which is what
switching back puts it on (:func:`prior_agent`). A chat born in Ultra Mode has
none, so it stays locked as before.

The routes in ``dashboard/handlers/crew_pause.py`` and the agent switch write it;
the queue drain, the autonudge fire, ``session_create`` and the slot rows read
it. Reads are in memory. The file is read once per data home (it is small and
capped), and every write goes through :func:`atomic_write`.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

#: The largest pause file read back; a bigger one is ignored as damaged.
MAX_FILE_BYTES = 256 * 1024

_lock = threading.Lock()
_records: dict[str, dict[str, Any]] = {}
_priors: dict[str, dict[str, str]] = {}
_loaded_from: Path | None = None


def _path() -> Path:
    return data_home() / "work-ledger" / "crew-pause.json"


def _clean_paused(raw: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for root, rec in raw.items():
        if not isinstance(root, str) or not root or not isinstance(rec, dict):
            continue
        keys = {
            field: sorted({k for k in rec.get(field) or [] if isinstance(k, str) and k})
            for field in ("tree", "held", "stopped")
        }
        at = rec.get("paused_at")
        out[root] = {
            **keys,
            "paused_at": at if isinstance(at, str) else "",
            "root_released": rec.get("root_released") is True,
        }
    return out


def _clean_priors(raw: Any) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return out
    for root, rec in raw.items():
        if isinstance(root, str) and root and isinstance(rec, dict):
            agent, kind = rec.get("agent"), rec.get("agent_kind")
            if isinstance(agent, str) and isinstance(kind, str):
                out[root] = {"agent": agent, "agent_kind": kind}
    return out


def _ensure_loaded() -> None:
    """Load the file once per data home. Caller holds ``_lock``."""
    global _loaded_from
    path = _path()
    if _loaded_from == path:
        return
    _records.clear()
    _priors.clear()
    _loaded_from = path
    try:
        if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _records.update(_clean_paused(raw.get("paused")))
                _priors.update(_clean_priors(raw.get("prior")))
    except (OSError, ValueError):
        logger.warning("crew pause: could not read %s; no crew is paused", path, exc_info=True)


def _commit(
    records: dict[str, dict[str, Any]],
    priors: dict[str, dict[str, str]],
    *,
    publish_on_failure: bool = False,
) -> None:
    """Write *records* and *priors*, then make them the live maps. Caller holds ``_lock``.

    Written first and published after: a write that fails raises with the live
    state unchanged, so memory and the file never disagree.
    """
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"paused": records, "prior": priors}, sort_keys=True, indent=1) + "\n"
        atomic_write(path, body, mode=0o600, restrict_to_owner=True, newline="\n")
    except Exception:
        # A change that has already happened elsewhere (a committed agent switch)
        # is published anyway, so the live gateway agrees with it; the raise tells
        # the caller the file is behind until the next write.
        if publish_on_failure:
            _publish(records, priors)
        raise
    _publish(records, priors)


def _publish(records: dict[str, dict[str, Any]], priors: dict[str, dict[str, str]]) -> None:
    _records.clear()
    _records.update(records)
    _priors.clear()
    _priors.update(priors)


def _copy_records() -> dict[str, dict[str, Any]]:
    return {
        root: {k: list(v) if isinstance(v, list) else v for k, v in rec.items()}
        for root, rec in _records.items()
    }


def record(root: str) -> dict[str, Any] | None:
    """A copy of *root*'s pause record, ``None`` when it is not paused."""
    with _lock:
        _ensure_loaded()
        rec = _records.get(root)
        return {k: list(v) if isinstance(v, list) else v for k, v in rec.items()} if rec else None


def pause(root: str, tree: list[str], stopped: list[str]) -> dict[str, Any]:
    """Record *root* as paused, holding its whole crew. BLOCKING (one small file write).

    A second pause keeps what the first one stopped: Resume all continues both.
    """
    with _lock:
        _ensure_loaded()
        records = _copy_records()
        old = records.get(root) or {}
        records[root] = {
            "tree": sorted(set(tree) | set(old.get("tree") or [])),
            "held": sorted(set(tree) | set(old.get("held") or [])),
            "stopped": sorted(set(stopped) | set(old.get("stopped") or [])),
            "paused_at": old.get("paused_at") or datetime.now(timezone.utc).isoformat(),
            "root_released": False,
        }
        _commit(records, dict(_priors))
        return dict(records[root])


def settle_stopped(root: str, stopped: list[str]) -> None:
    """Narrow *root*'s stopped set to the turns that really stopped. BLOCKING."""
    with _lock:
        _ensure_loaded()
        if root not in _records:
            return
        records = _copy_records()
        records[root]["stopped"] = sorted(set(stopped) & set(records[root]["stopped"]))
        _commit(records, dict(_priors))


def resume(root: str) -> dict[str, Any] | None:
    """Drop *root*'s record and return it, ``None`` when it was not paused. BLOCKING."""
    with _lock:
        _ensure_loaded()
        records = _copy_records()
        rec = records.pop(root, None)
        if rec is not None:
            _commit(records, dict(_priors))
        return rec


def release(slot_key: str) -> bool:
    """Stop holding *slot_key* (its own Continue was pressed). BLOCKING when it was held.

    It stays in the crew, so it still opens no new session while the pause stands.
    """
    with _lock:
        _ensure_loaded()
        records = _copy_records()
        changed = False
        for root, rec in list(records.items()):
            for field in ("held", "stopped"):
                if slot_key in rec[field]:
                    rec[field] = [k for k in rec[field] if k != slot_key]
                    changed = True
            if rec.get("root_released") and not rec["held"] and not rec["stopped"]:
                # A switched-back chat's last held member was continued: done.
                del records[root]
        if changed:
            _commit(records, dict(_priors))
        return changed


def _is_root_of(key: str, root: str, rec: dict[str, Any]) -> bool:
    return key == root and not rec.get("root_released")


def root_paused(root: str) -> bool:
    """Whether *root* itself is a paused Ultra Mode root (not one switched back)."""
    with _lock:
        _ensure_loaded()
        rec = _records.get(root)
        return rec is not None and not rec.get("root_released")


def holds_turn(slot_key: str, still_root: Callable[[], bool] | None = None) -> bool:
    """Whether no new turn may start on *slot_key*: a paused root, or a crew member it holds.

    *still_root*, when given, is asked only for a key that is a paused root: a chat
    that is no longer an Ultra Mode root (switched back, the record's update lost
    to a failed write) is not held as one.
    """
    with _lock:
        _ensure_loaded()
        if any(slot_key in rec["held"] for rec in _records.values()):
            return True
        is_root = any(_is_root_of(slot_key, root, rec) for root, rec in _records.items())
    return is_root and (still_root is None or still_root())


def holds_slot(slot: Any) -> bool:
    """:func:`holds_turn` for a live slot, which knows whether it is still a root."""
    from kiro_crew import crew_mode

    return holds_turn(slot.key, lambda: crew_mode.slot_is_crew_mode_root(slot))


def blocks_create(caller_key: str, still_root: Callable[[], bool] | None = None) -> bool:
    """Whether *caller_key* (a paused root or a member of its crew) may not open a session.

    *still_root* is asked as in :func:`holds_turn`, only for a key that is a paused root.
    """
    with _lock:
        _ensure_loaded()
        # A switched-back chat's crew blocks only the members still held.
        if any(
            caller_key in rec["held" if rec.get("root_released") else "tree"]
            for rec in _records.values()
        ):
            return True
        is_root = any(_is_root_of(caller_key, root, rec) for root, rec in _records.items())
    return is_root and (still_root is None or still_root())


def remember_prior(root: str, agent: str, agent_kind: str) -> None:
    """Record the agent *root* ran before it was switched to Ultra Mode mid-chat. BLOCKING.

    ``agent`` is ``""`` for a chat on the default agent (it names none).
    """
    with _lock:
        _ensure_loaded()
        priors = dict(_priors)
        priors[root] = {"agent": agent, "agent_kind": agent_kind}
        _commit(_copy_records(), priors)


def prior_agent(root: str) -> dict[str, str] | None:
    """``{agent, agent_kind}`` *root* can switch back to, ``None`` when it has none."""
    with _lock:
        _ensure_loaded()
        rec = _priors.get(root)
        return dict(rec) if rec else None


def switched_back(root: str) -> None:
    """*root* left Ultra Mode: forget its prior agent and release the root. BLOCKING.

    The root is a normal chat now and starts turns again, but the crew members the
    pause held stay held: each one's own Continue releases it, so a sub-team lead's
    patrol does not start work nobody asked for.
    """
    with _lock:
        _ensure_loaded()
        records = _copy_records()
        priors = dict(_priors)
        had_prior = priors.pop(root, None) is not None
        rec = records.get(root)
        if rec is not None:
            rec["root_released"] = True
            rec["stopped"] = [k for k in rec["stopped"] if k != root]
            if not rec["held"] and not rec["stopped"]:
                del records[root]
        if had_prior or rec is not None:
            # The switch already committed: the live state follows it even when
            # the file cannot be written (the caller logs that).
            _commit(records, priors, publish_on_failure=True)


def keep_pending(root: str, keys: list[str], *, root_pending: bool) -> None:
    """Keep holding stopped *keys* a Resume could not hand back yet. BLOCKING.

    Their turn was still tearing down: another Resume all continues them. The rest
    of the crew is free.
    """
    with _lock:
        _ensure_loaded()
        records = _copy_records()
        old = records.get(root)
        if old is not None:
            # A newer Pause all landed meanwhile: keep it, and add these to it.
            old["held"] = sorted(set(old["held"]) | set(keys))
            old["tree"] = sorted(set(old["tree"]) | set(keys))
            old["stopped"] = sorted(
                set(old["stopped"]) | set(keys) | ({root} if root_pending else set())
            )
        else:
            records[root] = {
                "tree": sorted(keys),
                "held": sorted(keys),
                "stopped": sorted([*keys, *([root] if root_pending else [])]),
                "paused_at": datetime.now(timezone.utc).isoformat(),
                "root_released": not root_pending,
            }
        _commit(records, dict(_priors))
