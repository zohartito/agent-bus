# Goal

Build a small local agent-to-agent bus that two live coding-agent sessions can use as a shared channel.

Read `AGENTS.md` first. Related code you may read, not copy blindly:

- `~/projects/claude-bridge` — closest owned bus (GitHub issue send/recv, Claude Code only)
- `~/projects/handoff` — session packet + MCP; one-shot dump, not a conversation
- `~/projects/panel` — jury of independent seats; not a channel

Do not wait on any other chat. Decide the v0 substrate, then implement it in this repo.

## What to build

Two agents, each already running in its own harness, send and receive on one channel until both agree the job is done. The pair is not fixed. Any two of: Claude Code, Codex CLI, cursor-agent, Gemini CLI, grok CLI, Kimi Code, or any later harness that can call MCP or a tiny CLI. Same protocol for every pair.

This is a mailbox both sides poll. Not a DM into the other TUI. Not a one-shot handoff packet. Not a panel of independent seats. Not a GitHub-issue chat that only Claude Code can speak unless that is a fallback transport.

## Hard rules

1. No PTY/TUI injection. Neither harness types into the other window. The bus is MCP tools and/or a CLI.
2. Identity on every message (who sent, which harness, monotonic id). Recv hides your own and de-dupes.
3. Explicit DONE. Infinite ping-pong is a bug. Shared verdict/DONE artifact both can read.
4. No secrets, tokens, account names, or money on the bus.
5. Zohar presses irreversible buttons. The bus carries messages and file paths, not publishes.
6. v0 is local-first. No new hosted service if files, git, or stdio MCP will do.
7. This repo is `github.com/zohartito/agent-bus`. Do not name anything BridgeMind, BridgeSpace, BridgeSwarm, or BridgeMCP. Those are someone else's products. The only transferable idea is "a channel both sides call."

## Decide, then implement, in this order

1. One substrate: extend the idea of claude-bridge, extend the idea of handoff, or a new local inbox in this repo. Implement the code **here**. Do not dump v0 into those other repos.
2. Protocol: send, recv/ack, optional watch, turn-passing, DONE.
3. Join path: MCP config for Claude Code, Codex, and cursor-agent; CLI for anything that cannot MCP.
4. Tests that do not need a live model: send/recv/dedupe/DONE.
5. One live proof with two different real harnesses.

## Done when

Two different harnesses, one standing instruction each, no human paste after kickoff, at least two round trips, then a shared DONE/PASS file. A third harness can join the same channel later without a protocol change.

## Non-goals for v0

ACP, Google A2A, Herdr, Adobe control, X posting, a BridgeMind clone, human-as-bus as the happy path.
