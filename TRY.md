# Try the bus — two terminals, two pastes

Use a throwaway root so this cannot collide with `~/.agent-bus`.

```bash
mkdir -p /tmp/agent-bus-try
cd ~/projects/agent-bus
```

Start **Agent B first** (it waits). Then start **Agent A** (it sends first).

Do not paste both into the same session.

---

## Agent B — waiter (paste this whole block)

You are agent `cursor` on harness `cursor-agent`. You share a local mailbox with peer `claude`. Communicate ONLY by running the bus CLI. Never edit files for the peer. Never type into another window.

Prefix for every call (copy exactly):

```
AGENT_BUS_DIR=/tmp/agent-bus-try AGENT_BUS_AGENT=cursor AGENT_BUS_HARNESS=cursor-agent python3 /Users/zohartito/projects/agent-bus/bus.py
```

Loop:

1. Fetch: `… recv -c try-1 --wait --timeout 20`. If it prints `[]`, run it again (up to 30 times).
2. Do what the peer asked with your own tools. Reply with `… send -c try-1 --body "…"`. End by handing the turn back. Return to step 1.
3. When the peer proposes finishing and you agree: `… done -c try-1 --verdict PASS --summary "one line"` then stop.
4. If you disagree, `send` why and keep going.

Rules: no secrets. Under 150 words. If you lose the thread: `… recv -c try-1 --after 0 --include-own`. If 30 empty waits: `… done -c try-1 --verdict FAIL --summary "peer went silent"` and stop.

You start by waiting. Do not send first.

---

## Agent A — starter (paste this whole block, after B is waiting)

You are agent `claude` on harness `claude-code`. You share a local mailbox with peer `cursor`. Communicate ONLY by running the bus CLI. Never edit files for the peer. Never type into another window.

Prefix for every call (copy exactly):

```
AGENT_BUS_DIR=/tmp/agent-bus-try AGENT_BUS_AGENT=claude AGENT_BUS_HARNESS=claude-code python3 /Users/zohartito/projects/agent-bus/bus.py
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

## Watch from a third shell (optional)

```bash
AGENT_BUS_DIR=/tmp/agent-bus-try python3 ~/projects/agent-bus/bus.py recv -c try-1 --after 0 --include-own -a observer
AGENT_BUS_DIR=/tmp/agent-bus-try python3 ~/projects/agent-bus/bus.py status -c try-1
```

It worked if `status` shows `"complete": true` and `"verdict": "PASS"`.
