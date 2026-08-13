# agent-bus — for any harness

Harness-neutral mailbox two live coding-agent sessions can poll. Not a TUI injector. Not a one-shot handoff packet. Not a panel of independent seats.

## What this is

A small local channel: send, recv (own messages hidden, de-duped), optional watch, explicit DONE. Any two harnesses that can call MCP or the CLI should work. The pair is not Codex+Cursor specifically.

## How to verify a change

- Tests: whatever `README.md` names after v0 exists (until then: `python3 -m unittest discover -s tests` if tests exist)
- Live proof: two different harnesses, no human paste after kickoff, ≥2 round trips, shared DONE file

## Layout

Keep it tiny. Protocol and tests before a desktop app. MCP stdio + a CLI are the product surface. Do not add a hosted service in v0.

## Conventions

- Follow this file. Match existing style once files exist.
- Surgical, single-purpose changes.
- Secrets never go on the bus.

## Agent rules (all harnesses)

- Determinism boundary: identity, ids, de-dupe, and DONE live in code; the model only orchestrates.
- No PTY injection into another harness.
- Do not name this BridgeMind / BridgeSpace / BridgeSwarm / BridgeMCP.
- Implement in this repo. `~/projects/claude-bridge` and `~/projects/handoff` are references, not destinations.
- Keep this file current and under ~150 lines when the run/test story changes.

## Related

- `~/projects/claude-bridge` — GitHub-issue bus, Claude Code ↔ Claude Code
- `~/projects/handoff` — session packet + MCP
- `~/projects/panel` — one question, many independent seats
