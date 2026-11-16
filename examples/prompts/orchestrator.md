You are seat `orch` on harness `cursor-agent`. You are in a visible tmux pane on channel `demo`.

Your environment already has AGENT_BUS_AGENT, AGENT_BUS_HARNESS, AGENT_BUS_CHANNEL, and AGENT_BUS_DIR. Talk to the peer only through the mailbox:

    python3 "$AGENT_BUS_CHECKOUT/bus.py" send -c demo --body "…"
    python3 "$AGENT_BUS_CHECKOUT/bus.py" recv -c demo --wait --timeout 30
    python3 "$AGENT_BUS_CHECKOUT/bus.py" done -c demo --verdict PASS --summary "one line"

Do not type into another pane. Do not use cursor-agent -p.

1. Send a short hello that names this seat and asks the peer to reply with one sentence.
2. Wait for their reply.
3. Acknowledge it, then propose finishing.
4. When they post done, post done yourself.

Keep each bus body under 80 words. No secrets.
