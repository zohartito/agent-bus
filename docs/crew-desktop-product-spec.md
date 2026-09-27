# Crew Desktop product specification

Status: architecture decision draft, 2026-08-15

## Decision

`tmux` is no longer the primary human interface. It remains a diagnostic fallback and a useful proof that the harnesses can coexist, but Crew Desktop will own its own window, roster, process lifecycle, PTYs, Crew-task kickoff, and state. The harness owns its terminal interface and input.

The recommended desktop stack is:

- Electron main process for application and child-process lifecycle
- `node-pty` for macOS/Linux PTYs and Windows ConPTY
- xterm.js for terminal rendering
- a packaged Python stdio sidecar that imports the existing `bus.py` core
- a local React/TypeScript renderer with no Node.js access

This is a local desktop application. It does not open a network port and it does not add a hosted service.

Electron is heavier than Tauri, but that cost is small beside 3–16 live coding-agent processes. The important advantage is implementation certainty: Microsoft's `node-pty` supports macOS, Linux, and Windows ConPTY, and its repository includes an Electron+xterm.js example. Tauri remains a future optimization only after a PTY spike proves equal behavior on both machines.

Primary references:

- [Electron process model](https://www.electronjs.org/docs/latest/tutorial/process-model)
- [Electron security guidance](https://www.electronjs.org/docs/latest/tutorial/security)
- [Microsoft node-pty](https://github.com/microsoft/node-pty)
- [Microsoft's Electron + node-pty + xterm.js example](https://github.com/microsoft/node-pty/blob/main/examples/electron/README.md)
- [xterm.js](https://xtermjs.org/)
- [Tauri v2 overview](https://v2.tauri.app/start/)

## Product promise

A user can configure, load, inspect, brief, and stop a group of coding agents without typing a terminal command and without wondering what a button will cause.

Four rules are visible in the UI and enforced in code:

1. **Configure is not load.** Adding an agent changes the roster only.
2. **Load is not send.** Loading opens an idle harness with no prompt, task, or mailbox message.
3. **Send is not collaborate.** A direct message goes to one selected agent. Multi-agent communication begins only in Crew task mode after a preview and explicit confirmation.
4. **A panel is the harness.** Each agent panel is a real interactive PTY running that harness's unmodified CLI/TUI. Crew adds a frame and coordination state around it; it does not replace Cursor, Claude, Codex, OpenCode, Kimi Code, Hermes, or another harness with a generic chat UI.

## Human modes

### Workbench

Workbench is the default.

- Any number of configured agents may be loaded or stopped.
- The user selects one agent at a time.
- The user clicks inside that agent's live terminal and types into the harness's own input, exactly as in Terminal.app.
- Agents do not contact each other automatically.
- The mailbox may be available, but no orchestration instruction is installed.

This covers “open seven agents and let me decide what to do later.”

### Crew task

Crew task is an explicit mode switch.

- The user chooses the orchestrator.
- The user chooses participating agents; loaded does not imply participating.
- The app previews the exact recipients, first briefing, working directory, permissions, and communication channel.
- **Start collaboration** creates a fresh run and sends the briefing.
- The activity timeline shows mailbox messages and DONE state separately from terminal output.

The app never silently converts a Workbench into a Crew task.

## Team setup: 1 to 16+ agents

Team size is a roster property, not a pane-layout decision.

- `−` and `+` change the desired count.
- **Add agents…** supports a quantity, harness mix, default model/effort, and naming pattern.
- Every row has harness, model/effort, working directory, permissions, and Remove.
- Saved team templates can represent “3 reviewers,” “7-agent build crew,” or any other reusable setup.
- Adding agents never launches them.
- Removing a loaded agent asks whether to stop the process; removing an unloaded agent is immediate.

The default workspace is **Crew Wall**: every configured agent has a stable panel and every loaded agent's live terminal remains visible on the same screen. Team size selects an adaptive grid rather than changing the product model:

- 1 agent: 1×1
- 2 agents: 2×1
- 3–4 agents: 2×2
- 5–6 agents: 3×2
- 7–8 agents: 4×2
- 9 agents: 3×3
- 10–12 agents: 4×3
- 13–16 agents: 4×4

Empty cells are not drawn. Headers become compact before terminal type becomes illegible. The supported desktop target is a resizable laptop or external display; at very high counts the app may recommend a larger window, but it never silently removes agents from the wall.

Clicking a panel enters **Focus** without losing the wall: the chosen terminal grows and the other live terminals remain visible in a narrow surrounding strip. Escape or **Show all** returns to Crew Wall. Focus changes layout only; it does not pause, restart, or detach any provider.

## Window layout

### App bar

- Workspace name
- Workbench / Crew task mode switch
- truthful summary: `7 configured · 4 loaded · no task sent`
- Load all / Stop all

### Crew Wall

- all agent panels remain visible at once
- mouse drag reorders panels; the layout is stable across launches
- add/remove and saved-team controls live in the app bar, not in a space-consuming permanent sidebar
- **Show all** and **Focus** are view controls, not process controls
- no Terminal.app windows and no tmux chrome

### Agent panel

Each panel has a thin Crew-owned header and a harness-owned terminal body.

Crew owns only:

- agent name and orchestrator marker
- harness/model label
- process/task status
- Load, Stop, Focus, and panel menu
- a subtle activity edge when the agent produces output or sends/receives a mailbox message

The harness owns every pixel below that header, including its own input surface. Crew launches the literal configured executable—such as `cursor-agent`, `claude`, `codex`, `opencode`, or another registered command—inside the panel's PTY. Cursor must look and behave like Cursor CLI; Codex like Codex; Claude like Claude; the same rule applies to OpenCode, Kimi Code, Hermes, and custom harnesses. ANSI color, alternate-screen behavior, cursor shape, scrollback, links, progress animation, prompts, permission questions, keyboard input, and harness shortcuts are rendered from the real PTY byte stream. Crew does not scrape, restyle, recreate, or redraw harness output.

There is no Crew-authored message box inside or beneath an individual agent panel. Clicking the terminal gives it keyboard focus; typing and Enter go directly to that PTY, so the user interacts with the same native `claude`, `codex`, or other input they would see after launching the command in Terminal.app.

The focused panel alone receives keyboard input. Mouse clicks inside a terminal are forwarded only when that harness has enabled terminal mouse reporting; clicks on the header remain Crew controls.

### Native input and Crew kickoff

Workbench has no application-level composer. The selected harness's native terminal input is the composer.

**Start crew task…** opens a separate Crew-owned confirmation sheet. It shows the orchestrator, participants, working directory, permissions, channel, and exact initial briefing, then ends with **Start collaboration**. This sheet is coordination control, not a replacement terminal or persistent chat box, and it closes after kickoff.

### Activity

The activity view is structured data, not scraped terminal text. It is a collapsible rail beneath the wall, so it can explain agent-to-agent traffic without replacing or covering native provider output:

- user task sent
- agent loaded/stopped/crashed
- bus message sent/received
- waiting for user
- DONE verdict

## State model

Each agent has two independent state axes.

Process state:

`configured → loading → idle → stopped`

with `error` reachable from loading or running states.

Task state:

`no task → queued → working → waiting → done`

Loading changes only process state. Sending a task changes task state. This separation prevents the current “Start all means several hidden things” failure.

Each Crew run also has a lifecycle:

`draft → previewed → active → finishing → complete | cancelled | error`

## Desktop architecture

```text
Renderer (React + one xterm.js instance per agent, sandboxed)
        │ typed IPC allowlist
        ▼
Electron main process
  ├── roster/run state machine
  ├── provider adapters
  ├── node-pty process supervisor
  ├── local persistence
  └── Python stdio sidecar
          └── existing bus.py core and deterministic mailbox state
```

The renderer cannot spawn commands, read arbitrary files, or access Node.js. It receives terminal bytes and structured state through a narrow IPC contract. The main process validates every harness executable, argument, working directory, and environment key. Terminal keystrokes are sent through a dedicated PTY-input IPC message to the focused PTY only; they do not pass through the mailbox or a Crew text composer.

Each loaded agent owns exactly one PTY and one long-lived xterm.js terminal. Moving between Crew Wall and Focus reparents or resizes the same terminal; it never substitutes a transcript view. The renderer sends measured columns and rows to the main process, which calls the PTY resize operation so full-screen provider TUIs receive the normal terminal resize signal and reflow themselves.

Baseline terminal compatibility includes UTF-8, 256 colors, truecolor, alternate screen, bracketed paste, hyperlinks, cursor styles, bell suppression settings, and harness mouse-reporting modes. Compatibility is verified against every supported installed harness rather than assumed from static sample output.

The Python sidecar uses newline-delimited JSON over stdin/stdout. It exposes only channel creation, send, recv, status, done, and channels. It imports `bus.py`; it does not duplicate the mailbox protocol and it does not listen on a port.

## Harness registry

The application core never contains a fixed provider dropdown. It queries a local harness registry.

Crew ships tested adapters for Cursor Agent, Claude Code, Codex CLI, OpenCode, Kimi Code, and Hermes. At launch and from **Rescan harnesses**, it checks `PATH` plus platform-appropriate application locations and shows only what is actually installed. The chooser also offers **Custom CLI…**.

A custom harness is registered through a local manifest and a guided test:

1. Pick an executable; Crew resolves and displays its absolute path.
2. Supply a bare interactive argv as an argument array, never a shell command string.
3. Launch a disposable PTY and confirm that the native screen appears and resizes correctly.
4. Optionally configure model discovery, explicit task start, and Crew mailbox instructions.
5. Save only after the user reviews the executable, arguments, working directory behavior, and capabilities.

Harnesses advertise capability levels instead of Crew pretending every CLI supports the same features:

- **Terminal ready:** can be loaded and used manually in Workbench.
- **Task ready:** can receive an explicitly confirmed initial task.
- **Crew ready:** can receive the mailbox protocol and participate automatically in collaboration.

An arbitrary interactive CLI can therefore occupy a panel immediately as **Terminal ready**. It becomes **Crew ready** only after its adapter proves the collaboration contract. Unsupported model, effort, resume, image, or permission controls are hidden rather than emulated.

Registry precedence is built-in tested adapter, user override, then discovered terminal-only harness. User manifests live in the Crew application-data directory and are portable as explicit exports; they do not modify the harness installation.

## Harness adapter contract

Each harness adapter defines:

- harness id, display name, and optional aliases
- executable discovery and version probe
- account/auth readiness check
- current model enumeration where supported
- bare interactive launch argv
- initial-prompt argv for an explicitly confirmed fresh task
- readiness detector
- resize and signal behavior
- graceful stop and forced-stop fallback
- collaboration briefing/wait strategy where supported
- capability flags: terminal, initial task, Crew mailbox, model, effort, permissions, resume, image input

The process supervisor never invents model syntax. A selected model id comes from the harness's current enumeration or a tested default.

## Loading and sending

### Load

1. Validate the registered harness, executable, cwd, model, and auth.
2. Create a PTY.
3. Launch the bare interactive harness.
4. Wait for the adapter's ready signal.
5. Mark the agent Idle.

No prompt is placed on the command line. No terminal input is written. No mailbox message is created.

### Native single-agent interaction

1. Require Workbench mode and one selected loaded agent.
2. Click inside the live terminal.
3. Type into the harness's native input and submit with that harness's normal key binding.
4. Crew forwards the keystrokes to that PTY only; it does not construct or inject a task.

### Start collaboration

1. Require Crew task mode, one orchestrator, at least one participant, and a non-empty task.
2. Create a unique bus run directory and channel.
3. Preview the exact initial briefing and permissions.
4. On click, brief the orchestrator and install worker wait instructions only for selected participants.
5. Observe the mailbox and DONE state.

## Persistence

Persist:

- team templates
- workspace roster and ordering
- provider/model/effort selections
- wall ordering, density, and focused panel
- run metadata and structured activity
- PTY scrollback snapshots needed for recovery

Do not persist:

- secrets entered into terminals
- arbitrary environment variables
- auth tokens
- hidden prompts the user cannot preview

Use OS credential storage only if Crew itself later owns credentials. Initially it should rely on each installed harness's existing authentication.

## Failure behavior

- Missing executable: row says **Not installed** and offers a harness-specific explanation; other agents still load.
- Authentication required: row says **Sign-in needed**; loading stops before task send.
- Invalid model: model selector refreshes from the harness; no model-list dump fills the terminal.
- Process crash: terminal remains inspectable, row says Error, and **Reload** is available.
- App restart: processes are either deliberately reattached through the supervisor or marked Stopped; never guessed from stale pane ids.
- Bus failure: Workbench remains usable. Crew task pauses and names the failed operation.
- 16-agent load: progress is per row and cancellable; one failure does not cancel unrelated agents.

## Security boundaries

- Package only local renderer assets; no remote code.
- `nodeIntegration` off, context isolation on, renderer sandbox on.
- Typed IPC allowlist with sender validation.
- Resolved harness executable allowlist; no arbitrary shell strings.
- Custom harness manifests require explicit local review and cannot add preload scripts, remote renderer code, or hidden environment variables.
- Arguments are arrays, never shell-concatenated commands.
- Prompts and secrets never appear in process argv.
- Working directories must be explicit existing directories.
- Bus runs use unique local directories and preserve old runs without replaying them.
- Stop and delete are different: Stop is reversible; Delete template/run data requires confirmation.

## Acceptance tests

Human acceptance:

- From Finder/Explorer, open Crew and configure 3, 7, and 16 agents without a terminal command.
- Load all and verify every agent is Idle, mailbox count is zero, and no task text appears.
- Click into a loaded `claude`, `codex`, and `cursor-agent` panel and verify the real installed TUI accepts typing through its own native input; no Crew-authored input field is present.
- Submit one task through one harness's native input and verify no other agent receives anything.
- Start one Crew task only after the preview correctly names orchestrator and participants.
- With 3, 7, and 16 loaded agents, every terminal is live and visible in Crew Wall on one application screen.
- Cursor, Claude, Codex, OpenCode, Kimi Code, and Hermes each preserve their installed full-screen TUI, input surface, colors, shortcuts, prompt behavior, permission prompts, and resize behavior inside their panel.
- For each built-in adapter, compare a captured standalone Terminal.app session with the embedded PTY at matching rows, columns, font, and theme; differences caused by Crew redraw or replacement are release blockers.
- Add a previously unknown executable through **Custom CLI…**, prove it Terminal ready, and use it without changing application code.
- A Terminal-ready-only harness cannot be silently included in Crew collaboration; the preview explains which capability is missing.
- Switch between Crew Wall and Focus without losing PTY state, scrollback, or the provider's alternate-screen contents.
- While agents collaborate, see each provider continue working in its own panel and see mailbox sender/recipient activity in the optional rail.
- Stop one agent without disturbing the other 15.

Automated acceptance:

- state-machine unit tests prove Configure, Load, Send, and Collaborate cannot collapse into one transition
- harness registry tests cover detection, aliases, precedence, capability gating, and malicious manifests
- adapter argv tests use current fixture model lists
- fake PTY harnesses cover ready, ANSI output, alternate screen, mouse mode, resize, crash, and stop
- bus sidecar contract tests run without live models
- integration tests load 16 fake agents with zero messages
- screenshot tests cover laptop and large-desktop Crew Walls at 3, 7, and 16 agents plus the focused layout
- Mac and Windows packaging smoke tests open, load one fake agent, and quit cleanly

## Build sequence

1. Freeze `crew.py desk` as a fallback; fix only safety or data-loss bugs.
2. Build the Electron shell and state machine with fake agents.
3. Prove one real PTY on Mac and Windows.
4. Implement Workbench with 1–16 silent loads.
5. Add structured activity and direct single-agent send.
6. Add the Python bus sidecar and Crew task preview.
7. Add templates, Crew Wall density/focus layouts, recovery, and packaging.

Do not start phase 6 until the silent-load acceptance test is solid. The app must first be trustworthy when it promises to do nothing.
