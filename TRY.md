# Try the bus

Two ways: **spawn** (one terminal: you direct, a Codex peer runs detached) or **two pastes** (two harness sessions you drive by hand). Both use a throwaway root so nothing collides with `~/.agent-bus`.

## 1. Spawn — you plus a detached Codex peer

```bash
# Start in the repository checkout.
export AGENT_BUS_CHECKOUT="$PWD"
BUS() { python3 "$AGENT_BUS_CHECKOUT/bus.py" "$@"; }
mkdir -p /tmp/agent-bus-try/director && cd /tmp/agent-bus-try/director
printf 'agent = "claude"\nharness = "claude-code"\nchannel = "try-2"\nroot = "/tmp/agent-bus-try"\n' > .agent-bus.toml
```

Save the peer's standing instruction in that folder as `peer-prompt.md`:

```text
You are agent codex on agent-bus channel try-2. Your identity is already in your env, so every
bus call is just: python3 "$AGENT_BUS_CHECKOUT/bus.py" <verb>. Talk to the peer only through the bus.
Loop: 1) run `... watch --timeout 60 --body-only`; if it prints nothing, run it again (up to 10 times).
2) Do what the message asks with your own tools. 3) Reply with `... send --kind answer --body "..."`,
ending by handing the turn back. Go to 1. When the peer proposes finishing and you agree, run
`... done --verdict PASS --summary "one line"` and stop. No secrets on the bus; under 150 words a message.
```

Then:

```bash
BUS send --kind plan --body "Round 1: reply HASH=<first 8 hex of sha256 of the UTF-8 string 'agent-bus v0', no trailing newline>, then ask me one short verification question. Your move, codex."
BUS spawn --agent codex -- codex exec -m gpt-6-astra -c 'model_reasoning_effort="high"' \
    --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check "$(cat peer-prompt.md)"
BUS watch --body-only     # expect HASH=bb4aa524 and a question
BUS status                # codex: watching true (between turns), spawn_alive true
BUS send --body "Correct. <answer its question>. Let's finish: post done PASS."
BUS watch --body-only     # expect DONE PASS
BUS done --verdict PASS --summary "spawned peer, two round trips, hash matched"
BUS status                # complete: true, verdict: PASS
```

`spawn` printed the session id it captured and the resume command it registered as codex's wake. If codex's `exec` turn ends before you are done, `BUS wake codex` resumes it (or leave `BUS watch-wake` running). What it did is in `/tmp/agent-bus-try/try-2/spawn/codex.log`. A `status` showing codex `silent` with `spawn_alive: false` means it died, not that it is busy.

## 2. Two pastes — two sessions you drive

```bash
mkdir -p /tmp/agent-bus-try
# Start in the repository checkout.
export AGENT_BUS_CHECKOUT="$PWD"
```

Start both harnesses from this shell so they inherit `AGENT_BUS_CHECKOUT` (or set it to this checkout in each session).

Start **Agent B first** (it waits). Then start **Agent A** (it sends first).

Do not paste both into the same session.

---

### Agent B — waiter (paste this whole block)

You are agent `cursor` on harness `cursor-agent`. You share a local mailbox with peer `claude`. Communicate ONLY by running the bus CLI. Never edit files for the peer. Never type into another window.

Prefix for every call (copy exactly):

```
AGENT_BUS_DIR=/tmp/agent-bus-try AGENT_BUS_AGENT=cursor AGENT_BUS_HARNESS=cursor-agent python3 "$AGENT_BUS_CHECKOUT/bus.py"
```

Loop:

1. Fetch: `… recv -c try-1 --wait --timeout 20`. If it prints `[]`, run it again (up to 30 times).
2. Do what the peer asked with your own tools. Reply with `… send -c try-1 --body "…"`. End by handing the turn back. Return to step 1.
3. When the peer proposes finishing and you agree: `… done -c try-1 --verdict PASS --summary "one line"` then stop.
4. If you disagree, `send` why and keep going.

Rules: no secrets. Under 150 words. If you lose the thread: `… recv -c try-1 --after 0 --include-own`. If 30 empty waits: `… done -c try-1 --verdict FAIL --summary "peer went silent"` and stop.

You start by waiting. Do not send first.

---

### Agent A — starter (paste this whole block, after B is waiting)

You are agent `claude` on harness `claude-code`. You share a local mailbox with peer `cursor`. Communicate ONLY by running the bus CLI. Never edit files for the peer. Never type into another window.

Prefix for every call (copy exactly):

```
AGENT_BUS_DIR=/tmp/agent-bus-try AGENT_BUS_AGENT=claude AGENT_BUS_HARNESS=claude-code python3 "$AGENT_BUS_CHECKOUT/bus.py"
```

Your job:

1. Send this as the first message (`… send -c try-1 --body "…"`):

Round 1. (a) Compute the first 8 hex characters of sha256 of the exact UTF-8 string `agent-bus v0` with no trailing newline. Reply HASH=<hex8>. (b) Ask me one short verification question. Then wait. Over to you, cursor.

2. Fetch with `… recv -c try-1 --wait --timeout 20` (retry empty `[]` up to 30 times).
3. Independently compute the same hash. If theirs matches `bb4aa524` and they asked a real question, answer it, then propose finishing.
4. When they post done PASS, post `… done -c try-1 --verdict PASS --summary "two round trips, hash matched"` and stop.
5. If the hash is wrong or they go silent for 30 waits: `… done -c try-1 --verdict FAIL --summary "…"`.

Rules: no secrets. Under 150 words. Replay if lost: `… recv -c try-1 --after 0 --include-own`.

When both are done, run `… status -c try-1` and show me VERDICT.json.

---

### Watch from a third shell (optional)

```bash
AGENT_BUS_DIR=/tmp/agent-bus-try python3 "$AGENT_BUS_CHECKOUT/bus.py" recv -c try-1 --after 0 --include-own -a observer
AGENT_BUS_DIR=/tmp/agent-bus-try python3 "$AGENT_BUS_CHECKOUT/bus.py" status -c try-1
```

It worked if `status` shows `"complete": true` and `"verdict": "PASS"`.
