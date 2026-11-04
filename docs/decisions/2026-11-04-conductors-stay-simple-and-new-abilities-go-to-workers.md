# Conductors stay simple: new MCP servers and skills go to workers, never to conductors

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-11-04

## Decision
A conductor, the Ultra Mode conductor or a mate's, mounts only Crew's own MCP servers and loads only the skills it names itself; new MCP servers and skills go to workers, and a change that adds capability to a conductor or binds Ultra Mode or mates back to custom agents needs an explicit owner decision.

## Why
- Ultra Mode and mates replace custom agents and move to self-evolution.
- A mate takes MCP servers, skills and tools from a custom agent only as its starting abilities. After that it grows through its own memory and its own abilities: chatting with the mate, or the mate's Abilities settings. A custom agent's instructions are not copied on purpose; the mate's private memory carries them.
- Ultra Mode wants general ability, so its worker gets every MCP server and skill from the start.
- A conductor only analyzes the problem, dispatches tasks and organizes their execution. Keeping it simple means it gets no user servers or user skills.

## Evidence
- The pull request that adds this entry, which also scopes the mate conductor to its named skills: https://github.com/kirodotdev/KiroCrew/pull/29926
- The maintainer's restatement on that pull request; that comment is the on-record artifact this entry rests on.
- The design it builds on: `docs/request-for-change/rfc-mate-owned-agent-config.md` §3 (the conductor's prompt, tools, MCP servers and skills are source-defined).
