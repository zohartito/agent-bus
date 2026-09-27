# agent-bus

Harness-neutral agent-to-agent mailbox. Two live coding-agent sessions, each in its own harness, send and receive on one channel until both say done.

This is Zohar's (`github.com/zohartito/agent-bus`). It is not BridgeMind.

v0 is a local file mailbox: one directory per channel under `~/.agent-bus`, one JSON file per message, monotonic gapless ids claimed with an atomic hard link. Stdlib Python only — no dependencies, no daemon, no hosted service. The product surface is a CLI (`bus.py`) and a stdio MCP server (`mcp_server.py`) over the same core. The original build spec is [`GOAL.md`](GOAL.md).

## Quick start (two terminals, any two harnesses)

Each agent sets its identity once, then talks:

```bash
export AGENT_BUS_AGENT=claude AGENT_BUS_HARNESS=claude-code   # terminal A
export AGENT_BUS_AGENT=codex  AGENT_BUS_HARNESS=codex-cli     # terminal B

python3 ~/projects/agent-bus/bus.py send -c demo --body "plan: I refactor, you review. Your move."
python3 ~/projects/agent-bus/bus.py watch -c demo --timeout 300   # blocks until the peer posts
python3 ~/projects/agent-bus/bus.py done -c demo --verdict PASS --summary "reviewed and merged"
python3 ~/projects/agent-bus/bus.py status -c demo
```

`recv` returns only messages you have not seen and never your own; it advances your cursor (that is the ack). `--peek` reads without acking; `--after 0` replays the whole channel. When **every** participant has posted `done`, `VERDICT.json` appears at the channel root and `status` reports `complete: true` — that file is the shared DONE artifact both sides (and Zohar) can read.

## Protocol

- **Message**: `{seq, id, ts, channel, agent, harness, kind, body}` — identity on every message. `kind` is `message` or `done`.
- **Ids**: a sender claims `max+1` by hard-linking a fully written temp file to `messages/<seq>.json`; `EEXIST` means retry. Ids are monotonic and gapless, and a reader that sees seq N can already see everything below N.
- **Cursor**: `cursors/<agent>.json` per reader. Fetch-and-ack is one step; `peek`/`after` are the escape hatches.
- **DONE**: `done <PASS|FAIL> --summary "..."` writes `done/<agent>.json` *and* posts a `done`-kind message so the peer's next `recv` sees it. All participants done ⇒ `VERDICT.json` (overall `FAIL` if anyone failed). A participant is anyone who has posted; every one of them must post a verdict before the channel is complete — a read-only observer never blocks, an agent that spoke and went silent does. That is deliberate: both sides must agree the job is done.
- **Turn-passing** is a convention, not a lock: end each message by handing the turn (a question or an assignment), then `watch`. Don't post again until the peer replies or times out.
- **Tripwire**: `send` refuses bodies that look like credentials (AWS/GitHub/Slack/`sk-` keys, private key blocks, JWTs). `--allow-secret-like` overrides a false positive. Secrets never go on the bus; the bus carries messages and file paths, not publishes.
- **Wake**: a message is only seen on the next `recv`, so a participant whose session has ended never hears it. `join --wake "<command>"` registers, per agent per channel, the shell command that resumes *that agent's own* session (`wake/<agent>.json`, with harness and registration time). The `watch-wake` daemon runs it when a message from someone else lands past that agent's cursor and the agent is not polling — every `recv`/`watch` touches `heartbeats/<agent>`, and a heartbeat younger than `--idle` (60 s) means "leave me alone". One wake per message, at most one per `--cooldown` (120 s) per agent, so a chatty peer cannot spawn sessions in a loop. `send --wake` forces it regardless of the heartbeat (the cooldown still holds). `done` clears your registration unless `--keep-wake`. Every wake is logged to `wake/log.jsonl` with the triggering seq and the command's exit code; the resumed harness's output lands in `wake/<agent>.out`. The wake command runs with `AGENT_BUS_AGENT`/`HARNESS`/`CHANNEL`/`DIR` set to the *woken* agent, whoever triggered it.

Env vars: `AGENT_BUS_DIR` (root, default `~/.agent-bus`), `AGENT_BUS_AGENT`, `AGENT_BUS_HARNESS`, `AGENT_BUS_CHANNEL` (default `main`). Point `AGENT_BUS_DIR` at a synced folder and the same protocol crosses machines; that stays out of scope for v0.

## Wake a stopped peer

```bash
# beta, once per channel, registers how to resume itself (the command is beta's, from beta's operator):
python3 bus.py join -c demo --wake 'codex exec resume 019a-... "read agent-bus channel demo and continue"'
python3 bus.py join -c demo --wake 'claude --resume 7f3c-... -p "read agent-bus channel demo and continue"'

# one daemon per machine (a pid file under the root refuses a second one):
python3 bus.py watch-wake                  # --cooldown 120 --idle 60 --poll 1
python3 bus.py watch-wake --once           # one sweep, wait for the wakes, exit (cron-friendly)

python3 bus.py send -c demo --body "your move" --wake   # force it now
python3 bus.py wake -c demo beta                        # same, without a message
python3 bus.py join -c demo --clear                     # unregister without done
```

`send` reports `wake_registered` (which peers can be woken) so the sender knows whether silence means "gone for now" or "never coming back". MCP: `bus_register_wake` and `bus_wake`, plus `wake` on `bus_send` and `keep_wake` on `bus_done`.

Safety: a wake command is a shell string the operator registered; it is never taken from a message body — `join` refuses a command that already appears verbatim in any message on the channel, and the same secret tripwire as `send` applies. A heartbeat only proves polling: an agent that is alive but mid-task for longer than `--idle` without a `recv` will be woken, and what a resume command does to a session that is still open interactively is the harness's problem, not the bus's.

The honest limit: a wake is a full harness start — a resumed session costs the same as opening one by hand, takes seconds to come up, and re-reads its context. It only works while the machine running `watch-wake` is on and the resume command is still valid (session ids expire, harnesses update). A resumed session must itself `recv` and reply; the bus does not paste the message in. Nothing here crosses machines.

## Join a channel

**MCP (Claude Code, Codex, cursor-agent, Gemini CLI, any MCP harness).** Identity lives in the server's env config, so the model cannot mislabel its own messages. Tools: `bus_send`, `bus_recv`, `bus_done`, `bus_status`, `bus_channels`.

Claude Code:

```bash
claude mcp add agent-bus --scope user \
  -e AGENT_BUS_AGENT=claude -e AGENT_BUS_HARNESS=claude-code \
  -- python3 /Users/zohartito/projects/agent-bus/mcp_server.py
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.agent-bus]
command = "python3"
args = ["/Users/zohartito/projects/agent-bus/mcp_server.py"]
env = { AGENT_BUS_AGENT = "codex", AGENT_BUS_HARNESS = "codex-cli" }
```

cursor-agent (`~/.cursor/mcp.json`) and Gemini CLI (`~/.gemini/settings.json`), same shape:

```json
{"mcpServers": {"agent-bus": {
  "command": "python3",
  "args": ["/Users/zohartito/projects/agent-bus/mcp_server.py"],
  "env": {"AGENT_BUS_AGENT": "cursor", "AGENT_BUS_HARNESS": "cursor-agent"}}}}
```

**CLI (grok CLI, Kimi Code, anything that can run a shell).** No install: env-prefix every call as in the quick start. To brief a peer agent in one paste, fill in [`examples/peer-instruction.md`](examples/peer-instruction.md) — that template is the "one standing instruction" from the v0 acceptance test. A third harness joins the same channel the same way; nothing about the protocol changes.

## Crew (the window you click)

This is for you, not for an agent. **Normal use requires no typing:** double-click **Crew** on the Desktop, then use the mouse. A window opens with four empty seats and a compact control dock: Orch, Scout, Claude, Codex.

Each row has **Load**, provider, effort, and (workers only) **Remove**. **+ Custom** walks through name → provider → effort. **Load all** opens every agent and deliberately sends no prompt, task, or mailbox message. Loading and briefing are separate human decisions. The dock says whether anyone has actually spoken. **Shut down** closes Crew. Opening Crew again moves the one live workspace to the new window instead of making competing copies.

The team is not fixed at four. Use **−1**, **+1**, or **+4** to resize quickly; new seats are balanced across the available providers. Use **+ Custom** when you want to pick the name, provider, and effort yourself. Larger rosters are paged in the dock, and new panes split the largest open seat so the workspace stays balanced.

The commands below are maintenance/debug fallbacks, not the everyday workflow:

```bash
python3 crew.py desk                    # the four-seat window
python3 crew.py desk --dry-run          # print the tmux commands, start nothing
python3 crew.py down                    # if you closed the window the hard way
```

`up` still exists: it starts every seat immediately with no buttons. The desk is the one meant for humans. How each harness takes a prompt is in [`docs/harness-launch-matrix.md`](docs/harness-launch-matrix.md).

## Tests

```bash
python3 -m unittest discover -s tests
```

Covers send/recv/de-dupe/DONE, concurrent id claiming, cursor independence, the secret tripwire, wake-on-message (once per message, cooldown, heartbeat suppression, forced wake, done clears, command tripwires), the CLI, and the MCP stdio handshake. No live model needed.

## Live proof

v0 acceptance (two different harnesses, one standing instruction each, no human paste after kickoff, ≥2 round trips, shared DONE/PASS file): evidence in [`docs/proof-v0.md`](docs/proof-v0.md) — Claude Code (Fable 5) ↔ Codex CLI over channel `v0-proof`.

## References, not destinations

- `~/projects/claude-bridge` — GitHub-issue bus, Claude Code ↔ Claude Code; remote fallback transport
- `~/projects/handoff` — session packet + MCP; continuity, not conversation
- `~/projects/panel` — one question, many independent seats; not a channel
