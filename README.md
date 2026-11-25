# agent-bus

Harness-neutral agent-to-agent mailbox. Two live coding-agent sessions, each in its own harness, send and receive on one channel until both say done.

This is Zohar's (`github.com/zohartito/agent-bus`). It is not BridgeMind.

v0 is a local file mailbox: one directory per channel under `~/.agent-bus`, one JSON file per message, monotonic gapless ids claimed with an atomic hard link. Stdlib Python only — no dependencies, no daemon, no hosted service. The product surface is a CLI (`bus.py`) and a stdio MCP server (`mcp_server.py`) over the same core. The original build spec is [`GOAL.md`](GOAL.md).

Install: clone this repository on a POSIX system (macOS or Linux; secure mailbox access requires no-follow directory operations). It needs only Python 3.11 or newer (standard library), so there is nothing else to install. Run `python3 bus.py` from the checkout as shown below; replace `/path/to/agent-bus` in the examples with your checkout's path.

## Quick start: you direct, `spawn` starts the peer

One `.agent-bus.toml` in your working directory (or any parent) is your identity; no env prefixes:

```toml
agent = "claude"
harness = "claude-code"
channel = "case-r14"
# root = "~/.agent-bus"   (optional; a relative root is relative to this file)
```

```bash
# Run this setup from the repository checkout.
export AGENT_BUS_CHECKOUT="$PWD"
BUS() { python3 "$AGENT_BUS_CHECKOUT/bus.py" "$@"; }
BUS send --kind plan --body "Plan: you model the lid, I review. Your move."
BUS spawn --agent codex -- codex exec -m gpt-6-astra -c 'model_reasoning_effort="high"' \
    --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check "$(cat peer-prompt.md)"
BUS watch --body-only    # blocks until codex posts; prints just the new bodies
BUS status               # who is watching, who went silent, is the peer process alive
BUS done --verdict PASS --summary "lid modelled and reviewed"
```

`peer-prompt.md` is the peer's standing instruction — see [`TRY.md`](TRY.md) for a complete one. `spawn` starts the peer in its own session (it survives your shell), puts `AGENT_BUS_AGENT/HARNESS/CHANNEL/DIR` in its env so its bus calls need no flags, reads its session id, and registers the matching resume as its wake — see [Spawn a peer](#spawn-a-peer).

Without a config file, `--as NAME --harness H` or the env vars work the same; precedence is flag > env > `.agent-bus.toml` > default. Two agents working in one directory must not share one config file: the env `spawn` sets beats the file, and a peer you start by hand there needs `--as`.

`send` prints `seq id` (`--json` for the whole record). `recv` returns only messages you have not seen and never your own; it advances your cursor (that is the ack). `--peek` reads without acking; `--after 0` replays the whole channel; `--body-only` prints just the bodies. When **every** participant has posted `done`, `VERDICT.json` appears at the channel root and `status` reports `complete: true` — that file is the shared DONE artifact both sides (and Zohar) can read.

## Protocol

- **Message**: `{seq, id, ts, channel, agent, harness, kind, body}` — identity on every message — plus `for` and `refs` when used.
- **Kinds**: `send --kind` takes `message` (default), `plan`, `question`, `answer`, `checkpoint`, `review`, `blocker`; `done` and `phase` come only from `done`. `recv --kind K` (repeatable) is a filtered *view*: like `--after` it never moves your cursor, so a filter can never swallow a message you did not look at. Plain `recv`/`watch` acks.
- **Refs**: `send --ref path[@sha256]` (repeatable) stores `{path, sha256, size}` per file; a given hash (full, or a prefix of 8+ hex) must match the file on disk now. `verify-refs <seq>` reports each ref as `unchanged`, `changed`, or `missing` and exits 1 on any drift, so a review names the exact artifact it judged.
- **Ids**: a sender claims `max+1` by hard-linking a fully written temp file to `messages/<seq>.json`; `EEXIST` means retry. Ids are monotonic and gapless, and a reader that sees seq N can already see everything below N.
- **Cursor**: `cursors/<agent>.json` per reader. Fetch-and-ack is one step; `peek`/`after` are the escape hatches.
- **DONE**: `done --verdict <PASS|FAIL> --summary "..."` writes `done/<agent>.json` *and* posts a `done`-kind message so the peer's next `recv` sees it. All participants done ⇒ `VERDICT.json` (overall `FAIL` if anyone failed). A participant is anyone who has posted; every one of them must post a verdict before the channel is complete — a read-only observer never blocks, an agent that spoke and went silent does. That is deliberate: both sides must agree the job is done. A person (`--harness human`) is consulted, not a signatory: a human who answered a question never blocks `VERDICT.json`, but a human `done --verdict FAIL` still makes it `FAIL`.
- **Phases**: `done --phase A --verdict PASS --summary "..."` posts a `phase`-kind message (with `phase` and `verdict` fields), records `phases/A/<agent>.json`, and leaves the channel open. `status.phases.A` lists the verdicts and turns `complete` when every non-human participant has one.
- **Liveness**: `status` gives each participant — and each of `readers` (polled or spawned, never posted) — `heartbeat_age_s`, `watching`/`watch_pid` (a live `watch` holds the lock right now), `silent` (no poll, send, or live watch for `--silent-after` minutes, default 10), and for a spawned peer `spawn_pid`/`spawn_alive`. Silent and alive is busy; silent and dead is crashed.
- **One reader per agent**: every read that acks holds an OS lock (`flock`) on `watchers/<agent>.lock`, with the holder's pid, host, and start time in `watchers/<agent>.json`. While a `watch` (or `recv --wait`, or MCP `bus_recv` with `wait`) holds it, any other acking `recv`/`watch` for that agent waits up to 1 s and is then refused, because a stray watcher would silently ack the next message; `--peek`, `--after`, and `--kind` still work. `--force` (with or without `--wait`) asks the holder to let go via `watchers/<agent>.stop`: the holder stops at its next poll without acking, and the forcing read proceeds (or errors after 5 s if the holder never polls). The kernel drops the lock when the holder exits, even on SIGKILL, so there are no stale locks; SIGTERM and SIGHUP also clear the info file. POSIX only: without `fcntl` (Windows) there is no lock.
- **Turn-passing** is a convention, not a lock: end each message by handing the turn (a question or an assignment), then `watch`. Don't post again until the peer replies or times out.
  - Before starting: `python3 bus.py config -c demo --max-turns 230 --moderator reviewer`.
  - Each non-human `message`, `plan`, `question`, `answer`, `review`, `checkpoint` or `blocker` costs one turn; `done` and `phase` cost none.
  - The default is `AGENT_BUS_MAX_TURNS` or 230 (measured baseline: twice 112 turns, rounded up to 10). `scripts/channel_stats.py` reads counts and suggests a cap; it never changes budgets.
  - The first send persists the default, including on old channels. `AGENT_BUS_ROOT` is an alias for `AGENT_BUS_DIR` (the latter takes precedence).
  - At the cap, all sends (including human, done and phase) stop with CLI exit 4; wakes stop, `CAPPED.json` is written once, and peers cannot finish by exchanging DONE.
  - `status` / MCP `bus_status` expose `turns_used`, `cap`, `capped`; MCP `bus_send` returns an error on refusal.
  - A human (`--as operator --harness human`) or the named moderator can `raise-cap -c demo --by 10` or `close -c demo --verdict FAIL --summary "turn budget exhausted"`.
  - Once configured or in use, changing config also requires that role. Identity is operator-supplied, not an authentication boundary.
  - Close writes a permanent `VERDICT.json` with `closed_by` and `reason` (`turn_cap` or `moderator`); raising retains the original cap receipt.
- **Tripwire**: `send` refuses bodies that look like credentials (AWS/GitHub/Slack/`sk-` keys, private key blocks, JWTs). `--allow-secret-like` overrides a false positive. Secrets never go on the bus; the bus carries messages and file paths, not publishes.
- **Wake**: a message is only seen on the next `recv`, so a participant whose session has ended never hears it. `join --wake "<command>"` registers, per agent per channel, the shell command that resumes *that agent's own* session (`wake/<agent>.json`, with harness and registration time). The `watch-wake` daemon runs it when a message from someone else lands past that agent's cursor and the agent is not polling — every `recv`/`watch` touches `heartbeats/<agent>`, and a heartbeat younger than `--idle` (60 s) means "leave me alone". One wake per message, at most one per `--cooldown` (120 s) per agent, so a chatty peer cannot spawn sessions in a loop. `send --wake` forces it regardless of the heartbeat (the cooldown still holds). `done` clears your registration unless `--keep-wake`. Every wake is logged to `wake/log.jsonl` with the triggering seq and the command's exit code; the resumed harness's output lands in `wake/<agent>.out`. The wake command runs with `AGENT_BUS_AGENT`/`HARNESS`/`CHANNEL`/`DIR` set to the *woken* agent, whoever triggered it.

Mailbox storage rejects symlinks at the configured root and below it. Operations use directory descriptors so replacing a checked path with a symlink cannot redirect file access. Ancestors outside the configured root are trusted. Directories are created as 0700 and files as 0600; access validates ownership and tightens existing owned entries to those modes. Entries owned by another user are refused. Permission checks also apply during reads.

Env vars: `AGENT_BUS_DIR` (root, default `~/.agent-bus`), `AGENT_BUS_AGENT`, `AGENT_BUS_HARNESS`, `AGENT_BUS_CHANNEL` (default `main`); each also has a flag (`--root`, `--as`, `--harness`, `-c`) and an `.agent-bus.toml` key (`root`, `agent`, `harness`, `channel`). Point `AGENT_BUS_DIR` at a synced folder and the same protocol crosses machines; that stays out of scope for v0.

## Wake a stopped peer

```bash
# beta, once per channel, registers how to resume itself (the command is beta's, from beta's operator):
python3 bus.py join -c demo --wake 'codex exec resume 019a-... "read agent-bus channel demo and continue"'
python3 bus.py join -c demo --wake 'claude --resume 7f3c-... -p "read agent-bus channel demo and continue"'

# one daemon per machine (a POSIX lifetime lock under the root refuses a second one):
python3 bus.py watch-wake                  # --cooldown 120 --idle 60 --poll 1
python3 bus.py watch-wake --once           # one sweep, wait for the wakes, exit (cron-friendly)

python3 bus.py send -c demo --body "your move" --wake   # force it now
python3 bus.py wake -c demo beta                        # same, without a message
python3 bus.py join -c demo --clear                     # unregister without done
```

A **Claude Code** session registers its own wake from its Bash tool, which sees `CLAUDE_CODE_SESSION_ID`. The double quotes expand the id and `$PWD` at registration time. Add the permission flags the session already runs with, or the headless resume cannot run `bus.py`:

```bash
python3 /path/to/agent-bus/bus.py join -c case-r14 --wake \
  "cd $PWD && claude --resume $CLAUDE_CODE_SESSION_ID --dangerously-skip-permissions -p 'read agent-bus channel case-r14 and continue'"
```

`send --json` reports `wake_registered` (which peers can be woken) so the sender knows whether silence means "gone for now" or "never coming back". MCP: `bus_register_wake` selects an operator-configured hook ID, and `bus_wake` runs a registered hook, plus `wake` on `bus_send` and `keep_wake` on `bus_done`.

For MCP, configure `AGENT_BUS_WAKE_HOOKS` in the server environment before startup, for example `{"resume":["codex","exec","resume","<session-id>","read agent-bus channel demo and continue"]}`. `bus_register_wake` takes `hook_id: "resume"` (or `clear: true`), never a command or replacement arguments. The server freezes the allowlist at startup and executes the exact argv without a shell. With no configured hooks, MCP cannot register a wake.

Safety: CLI `join --wake` and `spawn` remain operator shell interfaces. Their verbatim-message and secret checks are accidental-paste tripwires, not a security boundary: whitespace or equivalent shell syntax can evade the message check. Never copy an untrusted message into these commands. A heartbeat only proves polling: an agent that is alive but mid-task for longer than `--idle` without a `recv` will be woken, and what a resume command does to a session that is still open interactively is the harness's problem, not the bus's.

## Spawn a peer

```bash
python3 bus.py spawn -c case-r14 --agent codex -- codex exec -m gpt-6-astra --dangerously-bypass-approvals-and-sandbox "..."
python3 bus.py spawn -c case-r14 --agent reviewer -- claude --model opus --dangerously-skip-permissions -p "..."
```

`spawn` starts the command in its own session (`setsid`; stdin from `/dev/null`; stdout and stderr to `spawn/<agent>.log`), so closing your terminal or ending your session does not take the peer down. Its identity goes in its env. For `codex` it then waits up to `--id-timeout` (30 s) for `session id: <uuid>` in the output; for `claude` it passes `--session-id <uuid>` itself (or keeps yours). It registers the resume as the peer's wake: `cd <cwd> && codex exec resume [flags] <id> "<prompt>"` or `cd <cwd> && claude --resume <id> [flags] -p "<prompt>"`, repeating the flags that decide whether a headless run can act (codex `-m -c --enable --disable --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check`; claude `--model --effort --permission-mode --dangerously-skip-permissions`). The prompt defaults to `read agent-bus channel <channel> and continue` (`--wake-prompt`). `--wake COMMAND` registers yours instead, `--no-wake` none; any other harness needs `--wake`.

The result reports `session_id`, `wake_registered` (or `wake_reason`), `running`, and `exit_code` if the peer already died. `spawn/<agent>.json` keeps pid, argv, and cwd, and `status` shows `spawn_alive`. Spawning an agent that is still running is refused, and the secret tripwire applies to the command line. A detached peer has no terminal, so it runs its harness's headless mode (`codex exec`, `claude -p`); the work stays visible on the bus and in the log. Put the loop in its prompt: watch, act, reply ending with its move, repeat, `done` when both agree.

The honest limit: a wake is a full harness start — a resumed session costs the same as opening one by hand, takes seconds to come up, and re-reads its context. It only works while the machine running `watch-wake` is on and the resume command is still valid (session ids expire, harnesses update). A resumed session must itself `recv` and reply; the bus does not paste the message in. Nothing here crosses machines.

## Ask a person

A person is a participant with `--harness human` (e.g. `--as zohar --harness human`). Register a notify hook once per channel, as that person:

```bash
python3 bus.py join -c case-r14 --as zohar --harness human \
  --notify "python3 /path/to/agent-bus/examples/notify-telegram.py"
```

An agent asks with `send --kind question --for zohar --body "Fillet 2 or 3 mm?"`. Any message `--for zohar` runs the hook detached, with `AGENT_BUS_MSG_SEQ/ID/FROM/KIND/BODY` and `AGENT_BUS_AGENT=zohar` in its env; launches go to `notify/log.jsonl`, output to `notify/zohar.out`. [`examples/notify-telegram.py`](examples/notify-telegram.py) forwards it to Telegram (credentials from `~/.config/printer-telegram/telegram.env`, never on the bus; `--dry-run` prints instead) and ends with the exact command that posts the answer back as a normal message: `bus.py send --as zohar --harness human --kind answer --body "3 mm"`. CLI notify hooks use the same accidental-paste tripwires as CLI wake commands; the operator must supply a trusted command. `join --clear` drops both hooks.

## Join a channel

**MCP (Claude Code, Codex, cursor-agent, Gemini CLI, any MCP harness).** Identity lives in the server's env config, so the model cannot mislabel its own messages (the MCP server does not read `.agent-bus.toml`). Tools: `bus_send` (`kind`, `for`, `refs`), `bus_recv` (`kind`, `force`), `bus_done` (`phase`), `bus_status` (`silent_after_min`), `bus_channels`, `bus_register_wake`, `bus_wake`, `bus_verify_refs`.

Claude Code:

```bash
claude mcp add agent-bus --scope user \
  -e AGENT_BUS_AGENT=claude -e AGENT_BUS_HARNESS=claude-code \
  -- python3 /path/to/agent-bus/mcp_server.py
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.agent-bus]
command = "python3"
args = ["/path/to/agent-bus/mcp_server.py"]
env = { AGENT_BUS_AGENT = "codex", AGENT_BUS_HARNESS = "codex-cli" }
```

cursor-agent (`~/.cursor/mcp.json`) and Gemini CLI (`~/.gemini/settings.json`), same shape:

```json
{"mcpServers": {"agent-bus": {
  "command": "python3",
  "args": ["/path/to/agent-bus/mcp_server.py"],
  "env": {"AGENT_BUS_AGENT": "cursor", "AGENT_BUS_HARNESS": "cursor-agent"}}}}
```

**CLI (grok CLI, Kimi Code, anything that can run a shell).** No install: an `.agent-bus.toml`, `--as`/`--harness`, or env vars, as in the quick start. To brief a peer agent in one paste, fill in [`examples/peer-instruction.md`](examples/peer-instruction.md) — that template is the "one standing instruction" from the v0 acceptance test. A third harness joins the same channel the same way; nothing about the protocol changes.

## Crew (the window you click)

Prerequisites: Python 3.11+, tmux 3.2+ on PATH, and the harness CLIs you intend to load. On macOS, run `python3 crew.py shortcut` once from the checkout to install **Crew.app** and **Crew.command** on the Desktop. The shipped configs resolve their working directory relative to the checkout. Shortcut installation refuses unrelated or unmarked `Crew.app` / `Crew.command` collisions (move older unmarked installs aside first). Replacing a managed install stages both launchers first and retains `Crew.*.backup-*` copies; an installation failure restores the previous pair.

This is for you, not for an agent. **After that setup, normal use requires no typing:** double-click **Crew** on the Desktop, then use the mouse. A window opens with four empty seats and a compact control dock: Orch, Scout, Claude, Codex.

Each row has **Load** and provider controls, plus **Remove** for workers. Claude and cursor-agent rows also have an effort control; Codex rows do not. **+ Custom** walks through name → provider → effort. **Load all** opens every agent and deliberately sends no prompt, task, or mailbox message. Loading and briefing are separate human decisions. The dock says whether anyone has actually spoken. **Shut down** closes Crew. Opening Crew again moves the one live workspace to the new window instead of making competing copies.

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

Covers send/recv/de-dupe/DONE, concurrent id claiming, cursor independence, the secret tripwire, wake-on-message (once per message, cooldown, heartbeat suppression, forced wake, done clears, command tripwires), liveness and the single-reader lock (refusal, takeover, stale holder, against a real `watch` subprocess), refs and `verify-refs`, kinds and phases, human participants and notify hooks, `spawn` (codex id capture, claude id injection, flag carry-over, own session, no-id and secret refusals), `.agent-bus.toml` precedence, quiet output, the CLI, and the MCP stdio handshake. Fake harnesses only; no live model needed.

## Live proof

v0 acceptance (two different harnesses, one standing instruction each, no human paste after kickoff, ≥2 round trips, shared DONE/PASS file): evidence in [`docs/proof-v0.md`](docs/proof-v0.md) — Claude Code (Fable 5) ↔ Codex CLI over channel `v0-proof`.

## References, not destinations

- `~/projects/claude-bridge` — GitHub-issue bus, Claude Code ↔ Claude Code; remote fallback transport
- `~/projects/handoff` — session packet + MCP; continuity, not conversation
- `~/projects/panel` — one question, many independent seats; not a channel
