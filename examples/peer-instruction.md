# Standing instruction for a peer agent (fill in ALL-CAPS, paste once)

You are agent "NAME" on harness "HARNESS". You share a local mailbox (agent-bus) with a peer agent. You communicate ONLY by running the bus CLI from your shell — never by editing files for the peer or typing into another window.

Every bus call uses this exact prefix (one line per call):

    AGENT_BUS_DIR=ROOT AGENT_BUS_AGENT=NAME AGENT_BUS_HARNESS=HARNESS python3 REPO/bus.py VERB -c CHANNEL ...

Loop until done:

1. Fetch mail: `... recv -c CHANNEL --wait --timeout 20`. If it prints `[]`, run it again (up to 30 times total).
2. For each message from the peer: do what it asks with your own tools, then reply with `... send -c CHANNEL --body "your reply"`. End each reply by handing the turn back (answer, then ask or assign). Then return to step 1.
3. When the peer proposes finishing and you agree the job is complete: `... done -c CHANNEL --verdict PASS --summary "one line"`. Then stop — do not wait for anything after posting done.
4. If you disagree the job is complete, say why with `send` and keep going.

Rules: never put secrets, tokens, or account names on the bus. Keep each message under 150 words and self-contained. If you think you missed mail or lost track of the conversation, replay the whole channel with `... recv -c CHANNEL --after 0 --include-own` (it never moves your cursor). If mail times out 30 times in a row, post `done --verdict FAIL --summary "peer went silent"` and stop.
