"""Checked facts for the warm task brief (RFC section 3.9.1, "Checked facts").

A checked fact is something the gateway itself read for this brief: a pull
request it read live from GitHub, or a work item it read from the task
ledger. Each gets an id ``fN``. The model must cite ``{fN}`` on every body
line that names a pull request, task, file, deploy or ticket; this module
deletes the lines that do not, and the lines whose progress count is not the
ledger's. The card lists the facts in its "Checked" section.

Files, deploys and tickets have no checked facts yet, so a line naming one is
always deleted: the brief does not state what the gateway did not check.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from kiro_crew.dashboard import warm_task_brief_inputs as inputs

# The most facts one brief carries. The prompt's own budgets keep it well
# below (five PR reads; open items fit 9,000 characters, so at most ~150),
# and the citation pattern and the card read every id up to it.
MAX_FACTS = 200
FACT_REF_RE = re.compile(r"\{(f\d{1,3})\}")
# What a line may only state with a citation, beside the PR references
# ``inputs.pr_numbers`` reads.
_FILE_RE = re.compile(
    r"(?<![\w@.-])(?:[\w.-]+/)*[\w-]+\.(?:py|pyi|tsx?|jsx?|mjs|json|md|ya?ml|toml|sh|css|html|txt)\b"
)
_TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,7}\b")
_DEPLOY_RE = re.compile(r"\bdeploy(?:s|ed|ing|ment|ments)?\b", re.IGNORECASE)
_TASK_ID_RE = re.compile(r"\bit_[0-9a-f]{8}\b")
# ``3/5`` or ``3 of 5``: a progress count. A time, date or version is not one.
_COUNT_RE = re.compile(r"(?<![\w.:/#-])(\d{1,4})\s*(?:/|\bof\b)\s*(\d{1,4})(?![\w.:/-])")
_SOURCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})|<mcwidget\b", re.IGNORECASE)
_SOURCE_CLOSE_RE = re.compile(r"</mcwidget\s*>", re.IGNORECASE)


def _ask_texts(
    waiting: Sequence[Mapping[str, Any]], asks: Sequence[Mapping[str, Any]]
) -> list[str]:
    """The text of every open ask, where the live read looks for full PR URLs."""
    texts = [
        part
        for item in waiting
        for part in (str(item.get("text") or ""), *(str(o) for o in item.get("options") or []))
        if part
    ]
    for item in asks:
        values = (item.get("title"), item.get("summary"), item.get("decision"))
        texts.extend(str(v) for v in (*values, *(item.get("artifacts") or {}).values()) if v)
    return texts


def live_refs(
    work_items: Sequence[Mapping[str, Any]],
    waiting: Sequence[Mapping[str, Any]],
    terminal: Callable[[object], bool],
) -> list[inputs.PrRef]:
    """The pull requests to read before the brief may name them (M3): each
    open ask item's ledger PR, then full PR URLs in the asks' text, then
    every other open item's ledger PR. A bare ``#N`` is never guessed."""
    asks = [item for item in work_items if inputs.is_open_ask(item, terminal)]
    rest = [item for item in work_items if not terminal(item.get("state")) and item not in asks]
    ledger = [ref for item in asks if (ref := inputs.ledger_ref(item)) is not None]
    refs = inputs.pr_refs(_ask_texts(waiting, asks), ledger)
    for item in rest:
        ref = inputs.ledger_ref(item)
        if ref is not None and ref not in refs and len(refs) < inputs.MAX_LIVE_PRS:
            refs.append(ref)
    return refs


def live_links(live: Iterable[Mapping[str, Any]]) -> tuple[inputs.PrRef, ...]:
    """The link row (section 3.9.3): each pull request read live, once, its
    URL built from the read's own repo. Model text never adds to it."""
    refs: list[inputs.PrRef] = []
    for entry in live:
        repo, number = entry.get("repo"), str(entry.get("pr", "")).lstrip("#")
        if entry.get("checked") is True and isinstance(repo, str) and number.isdigit():
            ref = inputs.PrRef(repo, int(number))
            if ref not in refs:
                refs.append(ref)
    return tuple(refs)


# Machine values in the words the card shows; any other is lower-cased.
_WORDS = {
    "MERGEABLE": "can merge",
    "CLEAN": "can merge",
    "CONFLICTING": "has conflicts",
    "DIRTY": "has conflicts",
    "UNKNOWN": "",
    "progress": "in progress",
    "question": "has a question",
}


def _word(value: object) -> str:
    text = str(value or "")
    return _WORDS.get(text, text.lower().replace("_", " "))


def _checks_text(checks: object) -> str:
    """Every count the read kept, an unknown one included: an unresolved
    check is never shown as all clear."""
    if isinstance(checks, Mapping):
        parts = [f"{checks.get(k, 0)} {w}" for k, w in (("passed", "passed"), ("failed", "failed"))]
        parts += [
            f"{checks[k]} {w}"
            for k, w in (("pending", "running"), ("unknown", "unknown"))
            if checks.get(k)
        ]
        return "checks " + ", ".join(parts)
    return f"checks {checks}" if isinstance(checks, str) else ""


def checked_facts(
    live: Sequence[Mapping[str, Any]], open_items: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The facts the gateway read for this brief, numbered ``f1``…: each PR
    the live read covered (one it could not read states only "not checked
    since HH:MM"), then every work item the prompt shows, as the ledger holds
    them now."""
    facts: list[dict[str, Any]] = []
    for entry in live:
        if entry.get("checked") is not True:
            values = [f"not checked since {entry.get('not_checked_since') or 'never'}"]
        else:
            values = [_word(entry.get(k)) for k in ("state", "review", "mergeable")]
            values.append(_checks_text(entry.get("checks")))
        facts.append(
            {
                "kind": "pr",
                "pr": entry.get("pr"),
                "repo": entry.get("repo"),
                "label": f"{entry.get('repo')}{entry.get('pr')}",
                "facts": [str(v) for v in values if v],
                "source": "GitHub",
                "read_at": entry.get("read_at", ""),
            }
        )
    for item in open_items:
        state = item.get("state")
        values = [_word(state) if state != "open" else "", _word(item.get("status"))]
        if not (item.get("title") or item.get("item_id")):
            continue
        facts.append(
            {
                "kind": "task",
                "item_id": item.get("item_id"),
                "label": str(item.get("title") or item.get("item_id") or ""),
                "facts": [str(v) for v in values if v],
                "source": "task list",
            }
        )
    return [{"id": f"f{n}", **fact} for n, fact in enumerate(facts[:MAX_FACTS], 1)]


def card_facts(facts: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """The facts as ``meta.brief.checked`` stores them for the card."""
    keys = ("id", "kind", "label", "facts", "source", "read_at")
    return tuple({key: fact[key] for key in keys if fact.get(key)} for fact in facts)


def _named_tasks(line: str, facts: Sequence[Mapping[str, Any]]) -> tuple[bool, set[str]]:
    """Whether *line* names a task, and the fact ids of the tasks it names."""
    ids = {
        str(fact["id"])
        for fact in facts
        if fact["kind"] == "task"
        and any(
            name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", line, re.IGNORECASE)
            for name in (fact["label"], str(fact.get("item_id") or ""))
        )
    }
    return bool(ids) or bool(_TASK_ID_RE.search(line)), ids


def _line_ok(
    text: str, facts: Mapping[str, Mapping[str, Any]], progress: tuple[int, int] | None
) -> bool:
    """Whether one body line may stay (M11). *text* is the line in every
    scan view, so markup cannot hide a reference. A file, deploy or ticket is
    never stated (no fact covers one); each PR and task the line names must
    be a fact it cites; any count is the ledger's goal progress."""
    plain = inputs.without_repo_prs(text)
    if _FILE_RE.search(plain) or _TICKET_RE.search(plain) or _DEPLOY_RE.search(plain):
        return False
    for match in _COUNT_RE.finditer(text):
        if progress is None or (int(match.group(1)), int(match.group(2))) != progress:
            return False
    refs = FACT_REF_RE.findall(text)
    if any(ref not in facts for ref in refs):
        return False
    cited = [facts[ref] for ref in refs]
    bare = FACT_REF_RE.sub("", text)
    cited_prs = {
        (str(f["repo"]).casefold(), int(str(f["pr"]).lstrip("#")))
        for f in cited
        if f["kind"] == "pr"
    }
    if inputs.pr_numbers(bare) - {number for _, number in cited_prs}:
        return False
    if inputs.repo_prs(bare) - cited_prs:
        return False
    names_task, task_ids = _named_tasks(bare, list(facts.values()))
    if names_task and not task_ids or task_ids - set(refs):
        return False
    return True


def strict_body(
    body: str,
    facts: Sequence[Mapping[str, Any]],
    progress: tuple[int, int] | None,
    views: Callable[[str], Sequence[str]] = lambda text: (text,),
) -> str:
    """*body* without the lines M11 forbids. A picture's
    source (a fence or a widget) is kept whole: cutting lines from code
    breaks it, and the whole-body checks still read it."""
    by_id = {fact["id"]: fact for fact in facts}
    kept: list[str] = []
    fence = ""
    in_widget = False
    for line in body.split("\n"):
        opened = _SOURCE_OPEN_RE.match(line.lstrip()) if not (fence or in_widget) else None
        if fence:
            if line.strip().startswith(fence):
                fence = ""
        elif in_widget:
            in_widget = not _SOURCE_CLOSE_RE.search(line)
        elif opened:
            if opened.group(1):
                fence = opened.group(1)
            else:
                in_widget = not _SOURCE_CLOSE_RE.search(line)
        elif not _line_ok("\n".join(views(line)), by_id, progress):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def facts_digest(ledger_digest: str, waiting: object, live: Sequence[Mapping[str, Any]]) -> str:
    """What a brief stood on: the stored ledgers, what waited and each PR
    read, minus read times. Equal digests mean nothing changed since."""
    reads = [
        {k: v for k, v in entry.items() if k not in ("read_at", "not_checked_since", "_read_epoch")}
        for entry in live
    ]
    blob = json.dumps(
        [ledger_digest, waiting, reads], ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


FACTS_DIGEST_KEY = "facts_digest"


def previous_facts_digest(
    rows: Sequence[Mapping[str, Any]], result_kind: str, miss_key: str
) -> str:
    """The facts digest of the newest delivered brief, or ""."""
    for row in reversed(rows):
        meta = row.get("meta")
        if isinstance(meta, Mapping) and meta.get("kind") == result_kind:
            digest = meta.get(FACTS_DIGEST_KEY)
            return digest if isinstance(digest, str) and not meta.get(miss_key) else ""
    return ""
