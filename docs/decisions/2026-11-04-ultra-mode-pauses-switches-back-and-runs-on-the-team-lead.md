# Ultra Mode can pause its crew, a mid-chat switch can go back, and it runs on the team lead

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-04
Supersedes: 2026-11-02-an-ultra-mode-row-shows-only-what-needs-attention.md (in part: "It is one way, and the chip is then gone"); 2026-10-27-crew-mode-is-a-composer-switch-to-the-conductor-template.md (in part: the lock after the first message, for a chat switched on mid-chat only, and the conductor as the default agent)

## Decision

An Ultra Mode chat's Board offers **Pause all** under the Workers search bar. After
a confirm it stops the main chat's turn and every running crew member's at every
depth, holds the whole crew (no turn starts, patrol wakes are refused, sends and
queued messages wait, no new session opens) and leaves members that wait on the
person as they are. **Resume all** continues exactly what the pause stopped. Each
stopped member keeps its own **Continue**. The pause survives a reload.

A plain chat switched to Ultra Mode after it already had messages keeps the Ultra
Mode button right of the permission button, pressed. Pressing it switches the chat
back to the agent it had before, pausing running crew members first after a
confirm. A chat born in Ultra Mode keeps the lock after its first message.

Ultra Mode runs on `kirocrew-team-lead` when `agent.crew_mode_agent` is unset. A set
value is kept.

The lead records the work-ledger goal first whenever the person gives a new goal or
changes the scope, so the Board's Overview stays current. No server summary and no
model call does it.

Everything else in the superseded entries applies unchanged.

## Why

- A crew needs a brake: stopping each tab by hand while the patrol starts new work
  does not stop it.
- Trying Ultra Mode in an existing chat should not be a one-way door.
- The team lead can do a small item itself and dispatch the rest, which is what an
  Ultra Mode chat is for.
- A two-week-old goal on the Overview told the person the crew worked on something
  it no longer did.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/29891 -- the scope the maintainer
  approved from the reviewed mock, filed for the pull request that adds this entry.
