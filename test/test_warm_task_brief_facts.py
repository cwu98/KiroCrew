"""Checked facts and the strict line rule of the warm welcome
(RFC ``rfc-delayed-task-brief.md`` section 3.9.1, "Checked facts").

The gateway numbers what it read (``{fN}``), deletes body lines that state a
PR, task, file, deploy or ticket without citing one, keeps progress counts to
the ledger's, reads every ledger PR before the brief may name it, and does
not send a brief again when nothing it stood on changed.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_warm_task_brief import (
    _arm_model,
    _claimed,
    _foreground_slot,
    _iso,
    _posted,
    _snapshot,
    _State,
)

from kiro_crew.dashboard import warm_task_brief
from kiro_crew.dashboard import warm_task_brief_facts as facts
from kiro_crew.dashboard import warm_task_brief_inputs as inputs
from kiro_crew.session_summary import TranscriptTurn

_LIVE = (
    {
        "pr": "#12",
        "repo": "kirodotdev/KiroCrew",
        "checked": True,
        "state": "merged",
        "review": "APPROVED",
        "checks": None,
        "read_at": "09:30",
    },
    {"pr": "#13", "repo": "kirodotdev/KiroCrew", "checked": False, "not_checked_since": "never"},
)
_ITEMS = ({"item_id": "it_0000000a", "title": "Ship the importer", "state": "open"},)


def _facts():
    return facts.checked_facts(_LIVE, _ITEMS)


def test_each_read_gets_an_id_and_an_unread_pr_says_only_not_checked():
    found = _facts()
    assert [(f["id"], f["kind"], f["label"]) for f in found] == [
        ("f1", "pr", "kirodotdev/KiroCrew#12"),
        ("f2", "pr", "kirodotdev/KiroCrew#13"),
        ("f3", "task", "Ship the importer"),
    ]
    assert found[0]["facts"] == ["merged", "approved"] and found[0]["source"] == "GitHub"
    assert found[1]["facts"] == ["not checked since never"]
    assert facts.card_facts(found)[2] == {
        "id": "f3",
        "kind": "task",
        "label": "Ship the importer",
        "source": "task list",
    }


@pytest.mark.parametrize(
    ("line", "kept"),
    [
        ("- #12 merged {f1}", True),
        ("- #12 merged", False),
        # The cited fact is another PR's: #13 was never read.
        ("- #13 merged {f1}", False),
        ("- #12 merged {f9}", False),
        # Markup cannot hide a reference: every scan view is read.
        ("- #**12** merged", False),
        # The same number in another repo is another pull request.
        ("- acme/other#12 merged {f1}", False),
        ("- kirodotdev/KiroCrew#12 merged {f1}", True),
        # GitHub names are case-insensitive.
        ("- Kirodotdev/kirocrew#12 merged {f1}", True),
        # A citation of another fact does not back the task.
        ("- Ship the importer is open {f1}", False),
        ("- Ship the importer is open {f3}", True),
        # A PR the read could not reach is said to be not checked, citing its fact.
        ("- #13 not checked since never {f2}", True),
        ("- #13 not checked since never", False),
        ("- Ship the importer is open", False),
        ("- it_0000000b is stuck", False),
        # No fact covers files, deploys or tickets yet.
        ("- edited src/app/main.py", False),
        ("- deployed to beta", False),
        # A citation does not back a deploy, file or ticket: no fact covers one.
        ("- deployed to production {f1}", False),
        ("- see ABC-123", False),
        ("- 2 of 5 done", False),
        ("- 1/3 accepted", True),
        ("- Waiting for the review at 09:30", True),
        ("- On track.", True),
    ],
)
def test_a_line_stays_only_when_it_cites_what_it_states(line, kept):
    body = facts.strict_body(line, _facts(), (1, 3), warm_task_brief._scan_views)
    assert body == (line if kept else "")


def test_a_three_digit_fact_id_is_cited_and_the_count_is_bounded():
    items = [{"item_id": f"it_{n:08x}", "title": f"B{n:03d}", "state": "open"} for n in range(250)]
    found = facts.checked_facts((), items)
    assert len(found) == facts.MAX_FACTS and found[-1]["id"] == "f200"
    assert facts.strict_body("- B099 is open {f100}", found, None) == "- B099 is open {f100}"


def test_a_repository_name_that_looks_like_a_file_is_not_a_file():
    live = ({"pr": "#12", "repo": "vercel/next.js", "checked": True, "state": "merged"},)
    found = facts.checked_facts(live, ())
    for line in (
        "- vercel/next.js#12 merged {f1}",
        "- https://github.com/vercel/next.js/pull/12 {f1}",
    ):
        assert facts.strict_body(line, found, None) == line
    assert facts.strict_body("- edited next.js/app.py {f1}", found, None) == ""


def test_check_counts_keep_an_unknown_result_and_read_as_words():
    entry = {**_LIVE[0], "state": "open", "mergeable": "MERGEABLE", "review": ""}
    entry["checks"] = {"passed": 1, "failed": 0, "pending": 2, "unknown": 1}
    (fact,) = facts.checked_facts((entry,), ())
    assert fact["facts"] == ["open", "can merge", "checks 1 passed, 0 failed, 2 running, 1 unknown"]
    task = {"item_id": "it_0000000b", "title": "Ship", "state": "open", "status": "progress"}
    assert facts.checked_facts((), (task,))[0]["facts"] == ["in progress"]


def test_every_task_the_prompt_shows_is_matched_whatever_its_title_length():
    items = [
        {"item_id": f"it_{n:08x}", "title": f"Task number {n}", "state": "open"} for n in range(12)
    ]
    items.append({"item_id": "it_000000ff", "title": "Fix bug", "state": "open"})
    found = facts.checked_facts((), items)
    assert len(found) == 13
    for line in ("- Fix bug is complete", "- Task number 11 is done"):
        assert facts.strict_body(line, found, None) == ""
    for line in ("- Fix bug is open {f13}", "- Fix bugs later"):
        # A whole word only: "Fix bugs" names no task titled "Fix bug".
        assert facts.strict_body(line, found, None) == line


def test_a_picture_source_is_kept_whole():
    body = "- #12 merged\n```mermaid\nA[#12] --> B\n```\n<mcwidget>\n<p>#12</p>\n</mcwidget>\n- ok"
    assert facts.strict_body(body, _facts(), None) == body.split("\n", 1)[1]


def test_every_open_items_ledger_pr_is_read_before_it_may_be_named():
    def item(n, status):
        acceptance = {"kind": "pr_checks", "pr": n, "repo": "kirodotdev/KiroCrew"}
        return {"state": "open", "status": status, "acceptance": acceptance}

    def terminal(state):
        return state == "accepted"

    items = [item(1, "progress"), item(2, "question"), {**item(3, "done"), "state": "accepted"}]
    waiting = [{"text": "see https://github.com/acme/x/pull/9", "options": []}]
    refs = facts.live_refs(items, waiting, terminal)
    # The ask first, its URL, then the other open item; a closed item is not read.
    assert [(r.repo, r.number) for r in refs] == [
        ("kirodotdev/KiroCrew", 2),
        ("acme/x", 9),
        ("kirodotdev/KiroCrew", 1),
    ]
    many = [item(n, "progress") for n in range(1, 9)]
    assert len(facts.live_refs(many, [], terminal)) == inputs.MAX_LIVE_PRS


def test_the_digest_ignores_read_times_only():
    moved = ({**_LIVE[0], "read_at": "10:00", "_read_epoch": 1.0}, _LIVE[1])
    assert facts.facts_digest("d", [], _LIVE) == facts.facts_digest("d", [], moved)
    merged = ({**_LIVE[0], "state": "open"}, _LIVE[1])
    assert facts.facts_digest("d", [], _LIVE) != facts.facts_digest("d", [], merged)
    assert facts.facts_digest("d", [], _LIVE) != facts.facts_digest("e", [], _LIVE)


@pytest.mark.asyncio
async def test_a_posted_brief_drops_uncited_lines_and_lists_what_was_checked(monkeypatch):
    monkeypatch.setattr(inputs, "_live_cache", {})
    monkeypatch.setattr(inputs, "probe_github", lambda urls: {})
    state = _State()
    slot = _foreground_slot(state)
    item = {"item_id": "it_0000000a", "title": "Fix the test", "state": "open"}
    base = replace(_snapshot(), work_items=(item,))
    body = "- Fix the test is open {f1}\n- edited src/app/main.py\n- 3 of 4 done"

    async def run_model(*args, **kwargs):
        return json.dumps({"headline": "Back.", "body": body})

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    brief = slot.rows[-1]["meta"]["brief"]
    # The ledger count is 0 of 1: the model's 3 of 4 goes, as does the file line.
    assert brief["body"] == "- Fix the test is open {f1}"
    assert brief["checked"] == [
        {
            "id": "f1",
            "kind": "task",
            "label": "Fix the test",
            "source": "task list",
        }
    ]


@pytest.mark.asyncio
async def test_nothing_is_sent_again_when_nothing_changed(monkeypatch):
    monkeypatch.setattr(inputs, "_live_cache", {})
    monkeypatch.setattr(inputs, "probe_github", lambda urls: {})
    calls: list[str] = []

    async def run_model(*args, **kwargs):
        calls.append("model")
        return json.dumps({"headline": "Back."})

    # Only the owner spoke: nothing new in the thread since they left.
    quiet_turns = (TranscriptTurn(index=1, role="user", text="Fix it", ts=_iso(0.0)),)
    base = replace(_snapshot(), turns=quiet_turns)
    state = _State()
    slot = _foreground_slot(state)
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    digest = slot.rows[-1]["meta"][facts.FACTS_DIGEST_KEY]

    again = replace(base, fingerprint="fp2", previous_facts=digest)
    state2 = _State()
    slot2 = _foreground_slot(state2)
    _arm_model(monkeypatch, slot2, [again, _claimed(again), _claimed(again)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state2, slot2)
    assert calls == ["model"]
    assert _posted(slot2) == []
    # A changed ledger is sent.
    changed = replace(again, ledger_digest="other")
    slot3 = _foreground_slot(state2)
    _arm_model(monkeypatch, slot3, [changed, _claimed(changed), _claimed(changed)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state2, slot3)
    assert calls == ["model", "model"] and len(_posted(slot3)) == 1


def test_a_secret_in_a_checked_fact_is_caught_and_the_fallback_drops_the_facts():
    brief = warm_task_brief.Brief(
        status="on_track",
        headline="Back.",
        asked="",
        remaining="",
        done=(),
        needs_you=(),
        next=(),
        extra=(),
        checked=({"id": "f1", "kind": "task", "label": "key AKIA&#73;OSFODNN7EXAMPLE"},),
    )
    content = warm_task_brief._brief_text(brief, "en")
    assert warm_task_brief._grounding_miss(brief, content, set(), "", set()) == "secret"
    assert warm_task_brief._gateway_only(brief, "en").checked == ()
