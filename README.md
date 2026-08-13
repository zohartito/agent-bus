# agent-bus

Harness-neutral agent-to-agent mailbox. Two live coding-agent sessions, each in its own harness, send and receive until both say done.

Not built yet. The spec to paste is [`GOAL.md`](GOAL.md).

This is Zohar's (`github.com/zohartito/agent-bus`). It is not BridgeMind.

## Open this folder

```bash
cd ~/projects/agent-bus
```

Then start **one** agent. Do not give the same `GOAL.md` to Claude, Cursor, and Codex at the same time — they will collide in the working tree.

## Who should get the first paste

**Claude Code, Fable 5, effort max.** The v0 still has to pick a substrate and a protocol. That is judgment, not typing. Cursor Grok and Composer are the wrong first call. Codex Sol is the right *second* call once files and tests exist.

| Role | Harness | Model | Effort |
|---|---|---|---|
| First build (architecture + v0) | Claude Code | `fable` | `max` |
| Same model, Cursor desktop/CLI | Cursor | `claude-fable-5-thinking-max` | baked into the slug |
| Second pass (tests, CLI, harden) | Codex CLI | `gpt-5.6-sol` | `ultra` |
| Do not start here | Cursor Grok / Composer | — | — |

## Terminal

Claude Code (recommended first). Your shell alias already injects `--effort xhigh`, so pass `max` yourself:

```bash
cd ~/projects/agent-bus
claude --model fable --effort max
```

Then paste the contents of `GOAL.md`.

Same model via Cursor's agent CLI:

```bash
cd ~/projects/agent-bus
cursor-agent --model claude-fable-5-thinking-max
```

Codex, only after Fable has a protocol and some files — or if you are deliberately skipping Claude:

```bash
cd ~/projects/agent-bus
codex -m gpt-5.6-sol -c model_reasoning_effort=ultra
```

## Desktop apps

**Cursor:** File → Open Folder → `~/projects/agent-bus`. New agent chat. Model picker: **Fable 5 Max Thinking** (`claude-fable-5-thinking-max`). Paste `GOAL.md`.

**Claude Code:** same as the terminal command, or open this folder as the project and `/model fable` then set effort to max.

**Codex app:** `codex app`, open `~/projects/agent-bus`, model GPT-5.6 Sol, reasoning ultra. Better as the second session.

## References, not destinations

- `~/projects/claude-bridge`
- `~/projects/handoff`
- `~/projects/panel`
