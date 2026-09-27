# Harness launch matrix

Surveyed 2026-08-14 from live `--help` on this machine. The rule: start
interactive, and pass the initial prompt in the same invocation if the
harness allows it. Never use the headless modes. `tmux send-keys` is only
for a pane we just created, and only when the harness cannot take a prompt
without going non-interactive.

Identity for every seat is the pane environment: `AGENT_BUS_AGENT`,
`AGENT_BUS_HARNESS`, `AGENT_BUS_CHANNEL`, `AGENT_BUS_DIR`. That is what
`bus.py` reads. MCP identity is whatever the harness already has in its
own config; crew does not rewrite those files.

| Harness | Binary | Interactive + initial prompt in one argv? | Model flag | Headless (never use) | Prompt delivery |
|---|---|---|---|---|---|
| claude | `claude` | Yes. `claude [options] [prompt]` starts an interactive session. | `--model` | `-p` / `--print` | positional |
| claude-code | alias of claude | same | `--model` | `-p` / `--print` | positional |
| cursor-agent | `cursor-agent` | Yes. `cursor-agent [options] [prompt...]` | `--model` | `-p` / `--print` | positional |
| codex-cli | `codex` | Yes. `codex [OPTIONS] [PROMPT]` with no subcommand is the interactive CLI. | `--model` (`-m`) | `exec` (and alias `e`) | positional |
| codex | alias of codex-cli | same | `--model` | `exec` | positional |
| kimi | `kimi` | **No.** There is no positional prompt. `-p` / `--prompt` is non-interactive print-and-exit. | `--model` (`-m`) | `-p` / `--prompt` | **send-keys** (compromise) |

## Per harness

**claude.** `Usage: claude [options] [command] [prompt]`. Help text: starts
an interactive session by default; `-p/--print` is non-interactive. Crew
passes the prompt file contents as the last argv element.

**cursor-agent.** `Usage: agent [options] [command] [prompt...]`. Argument
help: "Initial prompt for the agent". `-p/--print` is "for scripts or
non-interactive use". Same delivery as claude.

**codex.** `Usage: codex [OPTIONS] [PROMPT]`. Help text: if no subcommand,
options go to the interactive CLI; `[PROMPT]` is "Optional user prompt to
start the session". `codex exec` is the non-interactive path. Crew never
emits `exec`.

**kimi.** `Usage: kimi [options] [command]`. No positional prompt.
`-p, --prompt` is "Run one prompt non-interactively and print the
response." The only way to start interactive *and* deliver a prompt is to
launch `kimi --model …` in the new pane, wait a second, then
`tmux send-keys -l` the prompt and `Enter`. That is typing into a TUI we
just created, not into a session someone else is already running. It is
still the compromise. Keep kimi prompts to one paragraph: a newline inside
`send-keys -l` can submit early.

## Observer pane

The brief asked for `bus.py watch -c <channel> --peek --after 0`.
`watch` does not accept `--peek` or `--after` (those flags live on
`recv`), and this repo does not modify `bus.py` for launcher features.
The observer pane loops:

```
python3 bus.py recv -c <channel> --peek --after 0 --include-own -a observer
```

every two seconds. Observer never `send`s, so it never becomes a
participant and cannot block `VERDICT.json`.

## This machine's shell aliases (not used)

Interactive zsh here wraps `claude` (`--effort xhigh`), `cursor-agent`
(`-f`), and `kimi` (`-y`). Crew invokes the binaries through tmux without
a login shell, so those aliases do not apply. Put extra flags on the seat
in `crew.toml` (`flags = ["--force"]`, etc.).

## Adding a provider

Add one row to `HARNESSES` in `crew.py`: `bin`, `model_flag`, `prompt`
(`positional` or `send-keys`), `headless` (flags crew will refuse). Then
a line in this table. No new `if harness ==`.
