#!/usr/bin/env python3
"""crew: tmux grid of visible coding-agent seats on the bus.

Spawns a fresh harness in a fresh pane. That is not injection into a
session someone is already running. Prefer a native interactive+prompt
invocation. tmux send-keys is a last resort when the harness cannot take
an initial prompt without going headless. Never use claude -p,
cursor-agent -p, codex exec, or kimi -p.

Stdlib only. Does not import-modify bus.py; it calls the same library
and shells out to tmux. The mailbox keeps working if this file is absent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
if sys.version_info < (3, 11):
    raise SystemExit("agent-bus requires Python 3.11 or newer; run with python3.11+.")

import tomllib
from dataclasses import dataclass
from pathlib import Path

import bus

REPO = Path(__file__).resolve().parent

# One row per provider. Adding a harness is adding a row, not an if-tree.
# prompt: "positional" = interactive argv takes the initial prompt.
#         "send-keys"  = start interactive, then type into the pane we created.
HARNESSES: dict[str, dict] = {
    "claude": {
        "bin": "claude",
        "model_flag": "--model",
        "prompt": "positional",
        "headless": ("-p", "--print"),
    },
    "codex-cli": {
        "bin": "codex",
        "model_flag": "--model",
        "prompt": "positional",
        "headless": ("exec", "-p"),
    },
    "cursor-agent": {
        "bin": "cursor-agent",
        "model_flag": "--model",
        "prompt": "positional",
        "headless": ("-p", "--print"),
    },
    "kimi": {
        "bin": "kimi",
        "model_flag": "--model",
        "prompt": "send-keys",
        "headless": ("-p", "--prompt"),
    },
}
HARNESSES["claude-code"] = HARNESSES["claude"]
HARNESSES["codex"] = HARNESSES["codex-cli"]

ADDABLE = ("cursor-agent", "claude", "codex-cli")
CYCLE = ("cursor-agent", "claude", "codex-cli", "kimi")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORT_LABEL = {
    "low": "low", "medium": "medium", "high": "high",
    "xhigh": "extra-high", "max": "max",
}
NAME_BANK = ("Scout", "Reviewer", "Builder", "Critic",
             "Researcher", "Writer", "Tester", "Fixer")
DEFAULTS = {
    "cursor-agent": {"model": "cursor-grok-4.6-xhigh-fast", "flags": ["--force"]},
    "claude": {"model": "sonnet", "flags": []},
    "codex-cli": {"model": "gpt-5.6-sol", "flags": []},
    "kimi": {"model": None, "flags": []},
}
THEME_ENV = {
    "TERM": "tmux-256color",
    "COLORFGBG": "15;0",
    "COLORTERM": "truecolor",
}
BORDER = {
    "orch": "#f5c542",
    "cursor-agent": "#7c3aed",
    "claude": "#e67e22",
    "codex-cli": "#2ecc71",
    "kimi": "#3b9eff",
}
BOARD_HEIGHT = 13


class CrewError(Exception):
    """Refused or failed crew operation; safe to show."""


@dataclass
class Seat:
    name: str
    harness: str
    model: str | None
    flags: list[str]
    prompt_path: Path | None
    prompt_text: str
    role: str = "worker"
    effort: str = "high"
    label: str = ""


@dataclass
class Crew:
    channel: str
    cwd: Path
    seats: list[Seat]
    config_path: Path
    root: Path
    session: str = ""

    def __post_init__(self) -> None:
        if not self.session:
            self.session = f"crew-{self.channel}"


def require_tmux(which=shutil.which) -> str:
    path = which("tmux")
    if not path:
        raise CrewError(
            "tmux is not on PATH. Install tmux (3.2+; 3.7b is what this was built against) "
            "and retry. crew will not fake a pane manager."
        )
    return path


def _spec(harness: str) -> dict:
    spec = HARNESSES.get(harness)
    if spec is None:
        raise CrewError(
            f"unknown harness {harness!r}; known: "
            + ", ".join(sorted(set(HARNESSES)))
        )
    return spec


def load_config(path: Path | str, root: Path | None = None) -> Crew:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise CrewError(f"config not found: {path}") from err
    except tomllib.TOMLDecodeError as err:
        raise CrewError(f"invalid TOML in {path}: {err}") from err

    channel = bus._valid_name("channel", raw.get("channel"))
    cwd = Path(str(raw.get("cwd") or ".")).expanduser()
    if not cwd.is_absolute():
        cwd = (path.parent / cwd).resolve()
    else:
        cwd = cwd.resolve()

    seats_raw = raw.get("seat") or raw.get("seats")
    if not seats_raw:
        raise CrewError("config needs at least one [[seat]]")

    seen: set[str] = set()
    seats: list[Seat] = []
    here = path.parent
    for item in seats_raw:
        name = bus._valid_name("agent", item.get("name"))
        if name in seen:
            raise CrewError(f"duplicate seat name {name!r}")
        seen.add(name)
        harness = item.get("harness") or ""
        spec = _spec(harness)
        flags = [str(f) for f in (item.get("flags") or [])]
        forbidden = set(spec.get("headless") or ()) | {"-p", "--print", "--prompt", "exec"}
        if any(f in forbidden for f in flags):
            raise CrewError(
                f"seat {name!r} flags {flags} include a headless switch; "
                "crew will not hide the work. Remove it."
            )
        rel = item.get("prompt")
        if not rel:
            raise CrewError(f"seat {name!r} needs prompt = \"path.md\"")
        prompt_path = Path(rel)
        if not prompt_path.is_absolute():
            prompt_path = here / prompt_path
        if not prompt_path.is_file():
            raise CrewError(f"prompt file missing for seat {name!r}: {prompt_path}")
        prompt_text = prompt_path.read_text(encoding="utf-8").strip()
        if not prompt_text:
            raise CrewError(f"prompt file is empty: {prompt_path}")
        secret = bus.secret_like(prompt_text)
        if secret:
            raise CrewError(
                f"prompt for seat {name!r} looks like it contains a secret ({secret}); "
                "secrets never go on a tmux command line or into pane history."
            )
        role = str(item.get("role") or ("orch" if name.lower() == "orch" else "worker"))
        effort = str(item.get("effort") or ("xhigh" if role == "orch" else "high"))
        if effort not in EFFORTS:
            raise CrewError(f"seat {name!r} effort must be one of {EFFORTS}")
        label = str(item.get("label") or name.title())
        seats.append(Seat(
            name=name, harness=harness, model=item.get("model"),
            flags=flags, prompt_path=prompt_path, prompt_text=prompt_text,
            role=role, effort=effort, label=label,
        ))

    return Crew(
        channel=channel, cwd=cwd, seats=seats, config_path=path,
        root=Path(root).expanduser() if root else bus.resolve_root(),
        session=str(raw["session"]) if raw.get("session") else "",
    )


def prompt_delivery(seat: Seat) -> str:
    return _spec(seat.harness)["prompt"]


def seat_argv(seat: Seat, include_prompt: bool = True) -> list[str]:
    spec = _spec(seat.harness)
    argv = [spec["bin"]]
    if seat.model:
        model = str(seat.model)
        if spec["bin"] == "cursor-agent" and seat.effort:
            model = _cursor_model_for_effort(model, seat.effort)
        argv += [spec["model_flag"], model]
    argv += list(seat.flags)
    if spec["bin"] == "claude" and seat.effort:
        argv += ["--effort", seat.effort]
    if include_prompt and spec["prompt"] == "positional":
        argv.append(seat.prompt_text)
    return argv


def _cursor_model_for_effort(model: str, effort: str) -> str:
    """Rewrite Cursor's explicit effort suffix without inventing an invalid id."""
    if "[" in model:
        return model
    match = re.fullmatch(r"(.+)-(low|medium|high|xhigh|max)(-fast)?", model)
    if not match:
        return model
    return f"{match.group(1)}-{effort}{match.group(3) or ''}"


def seat_env(cfg: Crew, seat: Seat) -> dict[str, str]:
    return {
        "AGENT_BUS_AGENT": seat.name,
        "AGENT_BUS_HARNESS": seat.harness,
        "AGENT_BUS_CHANNEL": cfg.channel,
        "AGENT_BUS_DIR": str(cfg.root),
    }


def observer_argv(cfg: Crew) -> list[str]:
    # bus.py watch has no --peek/--after. Do not modify bus.py; recv is the
    # command that actually replays the channel. The pane loops this.
    return [
        sys.executable, str(REPO / "bus.py"), "recv",
        "-c", cfg.channel, "--peek", "--after", "0", "--include-own",
        "-a", "observer", "--root", str(cfg.root),
    ]


def _env_args(env: dict[str, str]) -> list[str]:
    out = []
    for key, val in env.items():
        out += ["-e", f"{key}={val}"]
    return out


def pane_env(cfg: Crew, seat: Seat | None = None) -> dict[str, str]:
    env = dict(THEME_ENV)
    if seat is None:
        env.update({
            "AGENT_BUS_AGENT": "observer",
            "AGENT_BUS_HARNESS": "crew",
            "AGENT_BUS_CHANNEL": cfg.channel,
            "AGENT_BUS_DIR": str(cfg.root),
        })
    else:
        env.update(seat_env(cfg, seat))
    return env


def display_name(seat: Seat) -> str:
    return seat.label or seat.name


def pane_title(seat: Seat, waiting: bool = False) -> str:
    bin_name = _spec(seat.harness)["bin"]
    shown = display_name(seat)
    if seat.role == "orch" or seat.name.lower() == "orch":
        head = f"ORCH · {shown}"
    else:
        head = shown
    tail = "  waiting" if waiting else ""
    return f"{head} · {bin_name}{tail}"


def _fresh_name(cfg: Crew, harness: str) -> str:
    taken = {s.name.lower() for s in cfg.seats} | {s.label.lower() for s in cfg.seats if s.label}
    for candidate in NAME_BANK:
        if candidate.lower() not in taken:
            return candidate
    base = _spec(harness)["bin"].removesuffix("-agent").removesuffix("-cli")
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


def default_prompt_text(cfg: Crew, seat: Seat) -> str:
    others = ", ".join(display_name(s) for s in cfg.seats if s.name != seat.name) or "the others"
    bus_cmd = f"python3 {REPO / 'bus.py'}"
    shown = display_name(seat)
    if seat.role == "orch":
        return (
            f"You are {shown}, the orchestrator on {seat.harness}. "
            f"Workers: {others}. Channel {cfg.channel}. "
            f"Talk only through {bus_cmd}. Assign each worker one tiny read-only "
            f"question, collect answers, then propose done. No secrets. Do not use -p or exec."
        )
    return (
        f"You are {shown} on {seat.harness}, a worker. Channel {cfg.channel}. "
        f"Talk only through {bus_cmd}. Wait for orch, answer in one or two sentences, "
        f"post done when they propose finishing. No secrets. Do not use -p or exec."
    )


def add_seat(cfg: Crew, harness: str, name: str | None = None,
             effort: str = "high") -> Seat:
    _spec(harness)
    if effort not in EFFORTS:
        raise CrewError(f"effort must be one of {EFFORTS}")
    defaults = DEFAULTS.get(harness, {"model": None, "flags": []})
    shown = name or _fresh_name(cfg, harness)
    try:
        bus_name = bus._valid_name("agent", shown)
    except bus.BusError as err:
        raise CrewError(str(err)) from err
    seat = Seat(
        name=bus_name, harness=harness,
        model=defaults["model"], flags=list(defaults["flags"]),
        prompt_path=None, prompt_text="", role="worker",
        effort=effort, label=shown,
    )
    cfg.seats.append(seat)
    seat.prompt_text = default_prompt_text(cfg, seat)
    return seat


def _least_used_harness(cfg: Crew) -> str:
    counts = {harness: 0 for harness in ADDABLE}
    for seat in cfg.seats:
        if seat.harness in counts:
            counts[seat.harness] += 1
    return min(ADDABLE, key=lambda harness: (counts[harness], ADDABLE.index(harness)))


def _add_balanced(cfg: Crew, count: int) -> list[Seat]:
    added = []
    for _ in range(count):
        seat = add_seat(cfg, _least_used_harness(cfg))
        _live_add_pane(cfg, seat)
        added.append(seat)
    return added


def remove_seat(cfg: Crew, name: str) -> Seat:
    seat = next((s for s in cfg.seats if s.name == name), None)
    if seat is None:
        raise CrewError(f"no seat {name!r}")
    if seat.role == "orch" or seat.name == "orch":
        raise CrewError("the orchestrator seat stays; remove a worker instead")
    cfg.seats.remove(seat)
    return seat


def cycle_effort(cfg: Crew, name: str) -> Seat:
    seat = next((s for s in cfg.seats if s.name == name), None)
    if seat is None:
        raise CrewError(f"no seat {name!r}")
    cur = seat.effort if seat.effort in EFFORTS else "high"
    seat.effort = EFFORTS[(EFFORTS.index(cur) + 1) % len(EFFORTS)]
    return seat


def board_cards(cfg: Crew) -> list[dict]:
    cards = []
    for seat in cfg.seats:
        orch = seat.role == "orch" or seat.name.lower() == "orch"
        actions = ["start", "provider"]
        if _spec(seat.harness)["bin"] in ("claude", "cursor-agent"):
            actions.append("effort")
        if not orch:
            actions.append("delete")
        cards.append({
            "name": seat.name,
            "label": display_name(seat),
            "role": "orch" if orch else "worker",
            "provider": _spec(seat.harness)["bin"],
            "effort": seat.effort,
            "effort_label": EFFORT_LABEL.get(seat.effort, seat.effort),
            "actions": actions,
        })
    return cards


def cycle_harness(cfg: Crew, name: str) -> Seat:
    seat = next((s for s in cfg.seats if s.name == name), None)
    if seat is None:
        raise CrewError(f"no seat {name!r}")
    cur = seat.harness if seat.harness in CYCLE else "cursor-agent"
    nxt = CYCLE[(CYCLE.index(cur) + 1) % len(CYCLE)]
    defaults = DEFAULTS.get(nxt, {"model": None, "flags": []})
    seat.harness = nxt
    seat.model = defaults["model"]
    seat.flags = list(defaults["flags"])
    seat.prompt_text = default_prompt_text(cfg, seat)
    return seat


def _observer_loop(cfg: Crew) -> list[str]:
    inner = shlex.join(observer_argv(cfg))
    return ["sh", "-c", f"while :; do {inner}; echo; sleep 2; done"]


def build_plan(cfg: Crew) -> list[dict]:
    if not cfg.seats:
        raise CrewError("no seats")
    session = cfg.session
    cwd = str(cfg.cwd)
    steps: list[dict] = []

    def pane(op: str, seat: Seat | None, argv: list[str], env: dict[str, str]) -> None:
        cmd = ["tmux", op]
        if op == "new-session":
            cmd += ["-d", "-s", session]
        else:
            cmd += ["-t", f"={session}:"]
        cmd += ["-c", cwd]
        cmd += _env_args(env)
        cmd += argv
        steps.append({"op": op, "argv": cmd, "seat": seat.name if seat else "observer"})

    first = cfg.seats[0]
    pane("new-session", first, seat_argv(first), seat_env(cfg, first))
    for seat in cfg.seats[1:]:
        pane("split-window", seat, seat_argv(seat), seat_env(cfg, seat))
    obs_env = {
        "AGENT_BUS_AGENT": "observer",
        "AGENT_BUS_HARNESS": "crew",
        "AGENT_BUS_CHANNEL": cfg.channel,
        "AGENT_BUS_DIR": str(cfg.root),
    }
    pane("split-window", None, _observer_loop(cfg), obs_env)
    steps.append({"op": "layout", "argv": ["tmux", "select-layout", "-t", f"={session}:", "tiled"]})

    for i, seat in enumerate(cfg.seats):
        if prompt_delivery(seat) != "send-keys":
            continue
        target = f"={session}:0.{i}"
        steps.append({"op": "sleep", "argv": ["sleep", "1"], "seat": seat.name})
        steps.append({
            "op": "send-keys",
            "argv": ["tmux", "send-keys", "-t", target, "-l", seat.prompt_text],
            "seat": seat.name,
        })
        steps.append({
            "op": "send-keys",
            "argv": ["tmux", "send-keys", "-t", target, "Enter"],
            "seat": seat.name,
        })

    steps.append({"op": "attach", "argv": ["tmux", "attach-session", "-t", f"={session}"]})
    return steps


def format_plan(steps: list[dict]) -> str:
    lines = []
    for step in steps:
        lines.append(shlex.join(step["argv"]))
    return "\n".join(lines) + "\n"


def hold_argv(seat: Seat) -> list[str]:
    return [sys.executable, str(REPO / "crew.py"), "_hold",
            "--name", seat.name, "--harness", seat.harness, "--role", seat.role,
            "--label", display_name(seat)]


def board_argv(cfg: Crew) -> list[str]:
    return [sys.executable, str(REPO / "crew.py"), "_board",
            str(cfg.config_path), "--root", str(cfg.root),
            "--session", cfg.session]


def desk_actions(cfg: Crew) -> list[dict]:
    actions = []
    for seat in cfg.seats:
        bin_name = _spec(seat.harness)["bin"]
        label = f"Load ORCH ({bin_name})" if seat.role == "orch" or seat.name == "orch" \
            else f"Load {seat.name} ({bin_name})"
        actions.append({"id": f"load:{seat.name}", "label": label, "seat": seat.name})
    actions.append({"id": "load-all", "label": f"Load all {len(cfg.seats)}"})
    actions.append({"id": "add-one", "label": "Add 1 balanced agent"})
    actions.append({"id": "add-four", "label": "Add 4 balanced agents"})
    for seat in cfg.seats:
        if seat.role == "orch" or seat.name == "orch":
            continue
        actions.append({
            "id": f"cycle:{seat.name}",
            "label": f"{seat.name} → next provider",
            "seat": seat.name,
        })
    for harness in ADDABLE:
        actions.append({
            "id": f"add:{harness}",
            "label": f"Add {_spec(harness)['bin']}",
        })
    actions.append({"id": "remove-last", "label": "Remove last worker"})
    actions.append({"id": "status", "label": "Status"})
    actions.append({"id": "quit", "label": "Shut down"})
    return actions


def respawn_argv(cfg: Crew, seat: Seat, pane_id: str) -> list[str]:
    cmd = ["tmux", "respawn-pane", "-k", "-t", pane_id, "-c", str(cfg.cwd)]
    cmd += _env_args(pane_env(cfg, seat))
    # Loading a desk seat opens the harness and nothing else. A task briefing
    # is a separate human decision; never smuggle it into the launch argv.
    cmd += seat_argv(seat, include_prompt=False)
    return cmd


def panes_path(cfg: Crew) -> Path:
    return cfg.root / cfg.channel / "crew-panes.json"


def build_desk_plan(cfg: Crew) -> list[dict]:
    if not cfg.seats:
        raise CrewError("no seats")
    session = cfg.session
    cwd = str(cfg.cwd)
    steps: list[dict] = []

    def hold_pane(op: str, seat: Seat) -> None:
        cmd = ["tmux", op]
        if op == "new-session":
            cmd += ["-d", "-P", "-F", "#{pane_id}", "-s", session,
                    "-x", "180", "-y", "56"]
        else:
            cmd += ["-P", "-F", "#{pane_id}", "-t", f"={session}:"]
        cmd += ["-c", cwd]
        cmd += _env_args(pane_env(cfg, seat))
        cmd += hold_argv(seat)
        steps.append({"op": op, "argv": cmd, "seat": seat.name, "capture": True})

    hold_pane("new-session", cfg.seats[0])
    for seat in cfg.seats[1:]:
        hold_pane("split-window", seat)
    steps.append({"op": "layout", "argv": ["tmux", "select-layout", "-t", f"={session}:", "tiled"]})

    board = ["tmux", "split-window", "-f", "-v", "-l", str(BOARD_HEIGHT),
             "-P", "-F", "#{pane_id}", "-t", f"={session}:",
             "-c", cwd]
    board += _env_args(pane_env(cfg))
    board += board_argv(cfg)
    steps.append({"op": "split-window", "argv": board, "seat": "board", "capture": True})
    for key, val in (
        ("default-terminal", "tmux-256color"),
        ("terminal-overrides", ",*:RGB"),
        ("window-size", "latest"),
        ("aggressive-resize", "on"),
        ("window-style", "bg=#1a1a1a,fg=#d0d0d0"),
        ("window-active-style", "bg=#1a1a1a,fg=#e8e8e8"),
        ("status-style", "bg=#111111,fg=#e8e8e8"),
        ("pane-border-style", "fg=#555555"),
        ("pane-active-border-style", "fg=#f5c542"),
        ("mouse", "on"),
        ("pane-border-status", "top"),
        ("pane-border-format", " #{pane_title} "),
    ):
        steps.append({"op": "set", "argv": ["tmux", "set-option", "-t", f"={session}:", key, val]})
    # tmux normally forwards a pane click into full-screen applications. That
    # becomes literal escape text in Codex/Cursor. Crew seats only select on a
    # click; the board still receives clicks, and non-Crew tmux panes retain
    # tmux's normal select-and-forward behavior.
    steps.append({
        "op": "bind",
        "argv": [
            "tmux", "bind-key", "-n", "MouseDown1Pane",
            "if-shell", "-F", "-t", "=", "#{@crew-board}",
            "send-keys -M",
            "if-shell -F -t = '#{@crew-seat}' 'select-pane -t =' "
            "'select-pane -t = ; send-keys -M'",
        ],
    })
    steps.append({
        "op": "bind",
        "argv": [
            "tmux", "bind-key", "-n", "MouseDrag1Pane",
            "if-shell", "-F", "-t", "=", "#{@crew-board}",
            "send-keys -M",
            "if-shell -F -t = '#{@crew-seat}' 'select-pane -t =' "
            "if-shell -F '#{||:#{pane_in_mode},#{mouse_any_flag}}' "
            "'send-keys -M' 'copy-mode -M'",
        ],
    })
    steps.append({"op": "attach", "argv": ["tmux", "attach-session", "-d",
                                                "-t", f"={session}"]})
    return steps


def channel_status(cfg: Crew) -> dict:
    try:
        return bus.status(cfg.root, cfg.channel)
    except bus.BusError as err:
        raise CrewError(str(err)) from err


def _run(argv: list[str]) -> None:
    subprocess.run(argv, check=True)


def up(cfg: Crew, dry_run: bool = False, attach: bool = True) -> str:
    plan = build_plan(cfg)
    text = format_plan(plan)
    if dry_run:
        return text
    require_tmux()
    if not cfg.cwd.is_dir():
        raise CrewError(f"cwd does not exist: {cfg.cwd}")
    for seat in cfg.seats:
        bin_name = _spec(seat.harness)["bin"]
        if not shutil.which(bin_name):
            raise CrewError(f"harness binary {bin_name!r} is not on PATH (seat {seat.name!r})")
    existing = subprocess.run(["tmux", "has-session", "-t", f"={cfg.session}"],
                              capture_output=True)
    if existing.returncode == 0:
        raise CrewError(
            f"tmux session {cfg.session!r} already exists. "
            f"Attach with: tmux attach -t {cfg.session}  or  python3 crew.py down {cfg.config_path}"
        )
    for step in plan:
        if step["op"] == "attach":
            if attach and sys.stdout.isatty():
                os.execvp(step["argv"][0], step["argv"])
            print(f"session {cfg.session} is up. attach: tmux attach -t {cfg.session}",
                  file=sys.stderr)
            continue
        if step["op"] == "sleep":
            time.sleep(float(step["argv"][1]))
            continue
        _run(step["argv"])
    return text


def down(cfg: Crew) -> None:
    require_tmux()
    proc = subprocess.run(["tmux", "kill-session", "-t", f"={cfg.session}"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise CrewError(f"no tmux session {cfg.session!r} (already down, or never started)")


def _session_exists(session: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", f"={session}"],
                          capture_output=True).returncode == 0


def _fresh_desk_root(root: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return root / "crew-runs" / f"{stamp}-{os.getpid()}"


def _write_panes(cfg: Crew, panes: dict[str, str]) -> None:
    path = panes_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(panes, indent=2) + "\n", encoding="utf-8")


def _live_pane_records(cfg: Crew) -> list[dict[str, str]]:
    if not _session_exists(cfg.session):
        return []
    proc = subprocess.run(
        ["tmux", "list-panes", "-t", f"={cfg.session}:", "-F",
         "#{pane_id}\t#{@crew-seat}\t#{@crew-board}\t#{@crew-state}"
         "\t#{pane_dead}\t#{pane_width}\t#{pane_height}\t#{pane_title}"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return []
    records = []
    for line in proc.stdout.splitlines():
        fields = line.split("\t", 7)
        if len(fields) != 8:
            continue
        pane_id, seat, board, state, dead, width, height, title = fields
        records.append({
            "pane_id": pane_id,
            "seat": seat,
            "board": board,
            "state": state,
            "dead": dead,
            "width": width,
            "height": height,
            "title": title,
        })
    return records


def _read_panes(cfg: Crew) -> dict[str, str]:
    path = panes_path(cfg)
    for _ in range(20):
        stored: dict[str, str] = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    stored = {str(name): str(pane_id)
                              for name, pane_id in raw.items()}
            except (OSError, json.JSONDecodeError):
                stored = {}
        records = _live_pane_records(cfg)
        if records:
            live_ids = {record["pane_id"] for record in records}
            tagged = {record["seat"]: record["pane_id"]
                      for record in records if record["seat"]}
            panes = tagged or {name: pane_id for name, pane_id in stored.items()
                               if pane_id in live_ids}
            if panes != stored:
                _write_panes(cfg, panes)
            return panes
        time.sleep(0.1)
    raise CrewError("the live agent panes could not be found; reopen Crew and try again")


def desk(cfg: Crew, dry_run: bool = False, attach: bool = True) -> str:
    if dry_run:
        return format_plan(build_desk_plan(cfg))
    require_tmux()
    if not cfg.cwd.is_dir():
        raise CrewError(f"cwd does not exist: {cfg.cwd}")
    if _session_exists(cfg.session):
        if attach and sys.stdout.isatty():
            os.execvp("tmux", ["tmux", "attach-session", "-d", "-t", f"={cfg.session}"])
        print(f"session {cfg.session} is already up. attach: tmux attach -t {cfg.session}",
              file=sys.stderr)
        return ""
    # A new desk starts with an empty mailbox. Earlier runs remain available
    # under crew-runs for inspection, but never replay into a new board.
    cfg.root = _fresh_desk_root(cfg.root)
    plan = build_desk_plan(cfg)
    text = format_plan(plan)
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", '''
            tell application "Finder"
                set screenBounds to bounds of window of desktop
                set {leftEdge, topEdge, rightEdge, bottomEdge} to screenBounds
            end tell
            tell application "Terminal"
                activate
                try
                    set current settings of front window to settings set "RecordingSafe"
                on error
                    try
                        set current settings of front window to settings set "Pro"
                    end try
                end try
                try
                    set bounds of front window to {leftEdge + 20, topEdge + 40, rightEdge - 20, bottomEdge - 80}
                end try
            end tell
        '''], capture_output=True)
    panes: dict[str, str] = {}
    for step in plan:
        if step["op"] == "attach":
            _write_panes(cfg, panes)
            if attach and sys.stdout.isatty():
                os.execvp(step["argv"][0], step["argv"])
            print(f"session {cfg.session} is up. attach: tmux attach -t {cfg.session}",
                  file=sys.stderr)
            continue
        if step.get("capture"):
            pane_id = subprocess.check_output(step["argv"], text=True).strip()
            if step["seat"] == "board":
                _style_board_pane(pane_id)
                continue
            panes[step["seat"]] = pane_id
            _write_panes(cfg, panes)
            seat = next(s for s in cfg.seats if s.name == step["seat"])
            _style_pane(pane_id, seat, waiting=True)
            continue
        _run(step["argv"])
    return text


def _style_pane(pane_id: str, seat: Seat, waiting: bool = False) -> None:
    color = BORDER["orch"] if seat.role == "orch" or seat.name == "orch" \
        else BORDER.get(seat.harness, "#888888")
    commands = (
        ["tmux", "select-pane", "-t", pane_id, "-T",
         pane_title(seat, waiting=waiting)],
        ["tmux", "set-option", "-p", "-t", pane_id, "@crew-seat", seat.name],
        ["tmux", "set-option", "-p", "-t", pane_id, "@crew-state",
         "empty" if waiting else "loaded"],
        ["tmux", "set-option", "-p", "-t", pane_id, "remain-on-exit", "on"],
        ["tmux", "set-option", "-p", "-t", pane_id,
         "remain-on-exit-format", " Agent closed — click Load to reopen "],
        ["tmux", "set-option", "-p", "-t", pane_id,
         "pane-border-style", f"fg={color}"],
        ["tmux", "set-option", "-p", "-t", pane_id,
         "pane-active-border-style", f"fg={color},bold"],
    )
    for command in commands:
        subprocess.run(command, check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


def _style_board_pane(pane_id: str) -> None:
    for command in (
        ["tmux", "select-pane", "-t", pane_id, "-T", "CREW"],
        ["tmux", "set-option", "-p", "-t", pane_id, "@crew-board", "1"],
    ):
        subprocess.run(command, check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


def run_hold(name: str, harness: str, role: str = "worker",
             label: str = "") -> None:
    orch = role == "orch" or name.lower() == "orch"
    title = label or name
    head = f"ORCH  {title}" if orch else title
    sys.stdout.write("\033[40m\033[2J\033[H")
    if orch:
        sys.stdout.write("\033[1;33m")
    elif harness.startswith("cursor"):
        sys.stdout.write("\033[1;35m")
    elif harness.startswith("claude"):
        sys.stdout.write("\033[1;31m")
    else:
        sys.stdout.write("\033[1;32m")
    sys.stdout.write(
        f"\n  {head}\n\033[0;40m\033[37m  {harness}\n\n"
        f"  Not loaded. Click Load on the {title} row below.\n"
        "  Loading opens the agent only. It sends no task or message.\n\n\033[0m"
    )
    sys.stdout.flush()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        return


def do_action(cfg: Crew, action_id: str) -> str:
    if action_id == "quit":
        down(cfg)
        return "quit"
    if action_id == "status":
        return "status"
    if action_id.startswith("add:"):
        seat = add_seat(cfg, action_id.split(":", 1)[1])
        _live_add_pane(cfg, seat)
        return "ok"
    if action_id in {"add-one", "add-four"}:
        _add_balanced(cfg, 1 if action_id == "add-one" else 4)
        return "resized"
    if action_id == "remove-last":
        workers = [s for s in cfg.seats if s.role != "orch" and s.name != "orch"]
        if not workers:
            raise CrewError("no worker left to remove")
        seat = remove_seat(cfg, workers[-1].name)
        _live_kill_pane(cfg, seat.name)
        return "resized"
    if action_id.startswith("cycle:") or action_id.startswith("provider:"):
        name = action_id.split(":", 1)[1]
        seat = cycle_harness(cfg, name)
        panes = _read_panes(cfg)
        if name in panes:
            _style_pane(panes[name], seat, waiting=True)
        return "ok"
    if action_id.startswith("effort:"):
        name = action_id.split(":", 1)[1]
        seat = cycle_effort(cfg, name)
        panes = _read_panes(cfg)
        if name in panes:
            _style_pane(panes[name], seat, waiting=True)
        return "ok"
    if action_id.startswith("delete:"):
        name = action_id.split(":", 1)[1]
        remove_seat(cfg, name)
        _live_kill_pane(cfg, name)
        return "resized"
    panes = _read_panes(cfg)
    seats = {s.name: s for s in cfg.seats}
    load_all = action_id in {"load-all", "spawn-all"}
    names = list(seats) if load_all else [action_id.split(":", 1)[1]]
    for name in names:
        seat = seats[name]
        if name not in panes:
            _live_add_pane(cfg, seat)
            panes = _read_panes(cfg)
        pane_id = panes[name]
        _run(respawn_argv(cfg, seat, pane_id))
        _style_pane(pane_id, seat, waiting=False)
    return "loaded"


def _live_add_pane(cfg: Crew, seat: Seat) -> None:
    if not _session_exists(cfg.session):
        return
    panes = _read_panes(cfg)
    records = _live_pane_records(cfg)
    candidates = [record for record in records
                  if record["seat"] and record["seat"] != seat.name
                  and record["seat"] in panes]
    target_record = max(
        candidates,
        key=lambda record: int(record.get("width") or 0) * int(record.get("height") or 0),
        default=None,
    )
    target = target_record["pane_id"] if target_record else None
    cmd = ["tmux", "split-window", "-P", "-F", "#{pane_id}"]
    if target:
        width = int(target_record.get("width") or 0)
        height = int(target_record.get("height") or 1)
        cmd += ["-h" if width >= height * 2 else "-v", "-t", target]
    else:
        board = next((record["pane_id"] for record in records
                      if record["board"] or record["title"] == "CREW"), None)
        if board:
            cmd += ["-b", "-v", "-p", "70", "-t", board]
        else:
            cmd += ["-t", f"={cfg.session}:"]
    cmd += ["-c", str(cfg.cwd)]
    cmd += _env_args(pane_env(cfg, seat))
    cmd += hold_argv(seat)
    pane_id = subprocess.check_output(cmd, text=True).strip()
    panes[seat.name] = pane_id
    _write_panes(cfg, panes)
    _style_pane(pane_id, seat, waiting=True)


def _live_kill_pane(cfg: Crew, name: str) -> None:
    panes = _read_panes(cfg)
    pane_id = panes.pop(name, None)
    _write_panes(cfg, panes)
    if pane_id and _session_exists(cfg.session):
        subprocess.run(["tmux", "kill-pane", "-t", pane_id], check=False)


def _bus_lines(cfg: Crew, limit: int = 8) -> list[str]:
    try:
        msgs = bus.recv(cfg.root, cfg.channel, "observer", peek=True, after=0,
                        include_own=True)
    except bus.BusError:
        return ["(Nobody has contacted anyone.)"]
    if not msgs:
        return ["(Nobody has contacted anyone.)"]
    lines = []
    for m in msgs[-limit:]:
        body = " ".join(str(m.get("body", "")).split())
        if len(body) > 100:
            body = body[:97] + "..."
        lines.append(f"{m.get('seq')}  {m.get('agent')}  {body}")
    return lines


def _status_line(cfg: Crew, states: dict[str, str] | None = None) -> str:
    states = states or {}
    loaded = sum(state == "loaded" for state in states.values())
    load_text = f"{loaded}/{len(cfg.seats)} loaded · " if states else ""
    try:
        st = channel_status(cfg)
    except CrewError:
        return load_text + "0 messages · waiting for you"
    if st["message_count"] == 0:
        return load_text + "0 messages · waiting for you"
    bits = []
    for p in st["participants"]:
        done = p["done"]["verdict"] if p["done"] else "working"
        bits.append(f"{p['agent']}={done}")
    extra = "complete" if st["complete"] else "waiting on done"
    return load_text + f"{st['message_count']} msgs  {extra}  " + "  ".join(bits)


def _seat_states(cfg: Crew) -> dict[str, str]:
    states = {}
    for record in _live_pane_records(cfg):
        if not record["seat"]:
            continue
        if record["dead"] == "1":
            states[record["seat"]] = "closed"
        else:
            states[record["seat"]] = record.get("state") or "empty"
    return states


def _pin_repo(launcher: Path) -> None:
    """A launcher copied out of the repo cannot find it from its own location."""
    lines = launcher.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("REPO="):
            lines[i] = f"REPO={shlex.quote(str(REPO))}\n"
            launcher.write_text("".join(lines), encoding="utf-8")
            return
    raise CrewError(f"{launcher} has no REPO= line to point at {REPO}")


def install_shortcut(dest_dir: Path | str) -> Path:
    src = REPO / "Crew.app"
    if not (src / "Contents" / "MacOS" / "Crew").is_file():
        raise CrewError(f"Crew.app is missing at {src}")
    dest_dir = Path(dest_dir).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "Crew.app"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    _pin_repo(dest / "Contents" / "MacOS" / "Crew")
    cmd = dest_dir / "Crew.command"
    shutil.copy2(REPO / "Open Crew.command", cmd)
    _pin_repo(cmd)
    cmd.chmod(0o755)
    if shutil.which("xattr"):  # macOS only: clear the quarantine flag so a double-click runs
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(dest), str(cmd)],
                       capture_output=True)
    return dest


def run_board(cfg: Crew) -> None:
    try:
        import curses
        curses.wrapper(lambda stdscr: _board_curses(stdscr, cfg))
    except Exception:
        _board_plain(cfg)


def _fit_board_height() -> None:
    pane_id = os.environ.get("TMUX_PANE")
    if not pane_id:
        return
    subprocess.run(
        ["tmux", "resize-pane", "-t", pane_id, "-y", str(BOARD_HEIGHT)],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _hit_button(buttons: list[tuple[int, int, int, int, str]], y: int, x: int) -> str | None:
    for top, left, bottom, right, action_id in buttons:
        if top <= y <= bottom and left <= x <= right:
            return action_id
    return None


def _put(stdscr, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    import curses
    try:
        stdscr.addnstr(y, x, text, max(0, width - x - 1), attr)
    except curses.error:
        pass


def _btn(stdscr, buttons: list, y: int, x: int, text: str, action_id: str,
         attr: int | None = None) -> int:
    import curses
    if attr is None:
        attr = curses.A_REVERSE
    label = f" {text} "
    _height, width = stdscr.getmaxyx()
    _put(stdscr, y, x, label, width, attr)
    buttons.append((y, x, y, x + len(label) - 1, action_id))
    return x + len(label) + 2


def _refresh_titles(cfg: Crew) -> None:
    try:
        panes = _read_panes(cfg)
    except CrewError:
        return
    for seat in cfg.seats:
        pane_id = panes.get(seat.name)
        if not pane_id:
            continue
        subprocess.run(["tmux", "select-pane", "-t", pane_id, "-T",
                        pane_title(seat)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _board_curses(stdscr, cfg: Crew) -> None:
    import curses
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    _fit_board_height()
    stdscr.nodelay(True)
    stdscr.timeout(700)
    flash = ""
    mode = "home"
    page = 0
    page_size = 1
    pending: dict[str, str] = {}
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        buttons: list[tuple[int, int, int, int, str]] = []
        _refresh_titles(cfg)
        if mode == "add-name":
            _put(stdscr, 0, 1, "Name this agent   click one", w, curses.A_BOLD)
            x, row = 1, 2
            taken = {s.name.lower() for s in cfg.seats} | {display_name(s).lower() for s in cfg.seats}
            for nm in NAME_BANK:
                if nm.lower() in taken:
                    continue
                if x + len(nm) + 4 >= w:
                    row += 2
                    x = 1
                x = _btn(stdscr, buttons, row, x, nm, f"pick-name:{nm}")
            _btn(stdscr, buttons, row + 2, 1, "Cancel", "cancel")
        elif mode == "add-provider":
            _put(stdscr, 0, 1, f"Provider for {pending.get('name', '?')}   click one", w, curses.A_BOLD)
            x = 1
            for harness in ADDABLE:
                x = _btn(stdscr, buttons, 2, x, _spec(harness)["bin"], f"pick-provider:{harness}")
            _btn(stdscr, buttons, 4, 1, "Cancel", "cancel")
        elif mode == "add-effort":
            _put(stdscr, 0, 1, f"Effort for {pending.get('name', '?')}   click one", w, curses.A_BOLD)
            x = 1
            for ev in EFFORTS:
                x = _btn(stdscr, buttons, 2, x, EFFORT_LABEL[ev], f"pick-effort:{ev}")
            _btn(stdscr, buttons, 4, 1, "Cancel", "cancel")
        else:
            states = _seat_states(cfg)
            loaded = sum(state == "loaded" for state in states.values())
            _put(stdscr, 0, 1,
                 f"CREW · {len(cfg.seats)} agents · Load opens them silently", w,
                 curses.A_BOLD)
            if w > 100:
                x = max(1, w - 62)
                load_label = "Reload all" if loaded == len(cfg.seats) else "Load all"
                x = _btn(stdscr, buttons, 0, x, load_label, "load-all")
                x = _btn(stdscr, buttons, 0, x, "−1", "remove-last")
                x = _btn(stdscr, buttons, 0, x, "+1", "add-one")
                x = _btn(stdscr, buttons, 0, x, "+4", "add-four")
                x = _btn(stdscr, buttons, 0, x, "+ Custom", "add-start")
                _btn(stdscr, buttons, 0, x, "Shut down", "quit")
            else:
                _btn(stdscr, buttons, 0, 1, "Load all", "load-all")
                _btn(stdscr, buttons, 0, 14, "−1", "remove-last")
                _btn(stdscr, buttons, 0, 21, "+1", "add-one")
                _btn(stdscr, buttons, 0, 28, "+4", "add-four")
            _put(stdscr, 1, 1, _status_line(cfg, states), w)
            cards = board_cards(cfg)
            page_size = max(1, h - 5)
            page_count = max(1, (len(cards) + page_size - 1) // page_size)
            page = min(page, page_count - 1)
            if page_count > 1:
                x = max(1, w - 31)
                x = _btn(stdscr, buttons, 1, x, "◀", "page-prev")
                _put(stdscr, 1, x, f"page {page + 1}/{page_count}", w, curses.A_BOLD)
                _btn(stdscr, buttons, 1, x + 12, "▶", "page-next")
            row = 2
            first = page * page_size
            for card in cards[first:first + page_size]:
                state = states.get(card["name"], "empty")
                star = "★" if card["role"] == "orch" else " "
                dot = "●" if state == "loaded" else "×" if state == "closed" else "○"
                _put(stdscr, row, 1, f"{star}{dot} {card['label']}", w, curses.A_BOLD)
                x = 18 if w >= 72 else max(12, len(card["label"]) + 5)
                load_label = "Reload" if state == "loaded" else "Load"
                x = _btn(stdscr, buttons, row, x, load_label, f"load:{card['name']}")
                x = _btn(stdscr, buttons, row, x, card["provider"],
                         f"provider:{card['name']}")
                if "effort" in card["actions"]:
                    x = _btn(stdscr, buttons, row, x,
                             f"effort {card['effort_label']}",
                             f"effort:{card['name']}")
                if "delete" in card["actions"]:
                    _btn(stdscr, buttons, row, x, "Remove", f"delete:{card['name']}")
                row += 1
            _put(stdscr, row, 1, flash or "bus", w, curses.A_BOLD)
            for j, line in enumerate(_bus_lines(cfg, limit=max(2, h - row - 3))):
                if row + 1 + j >= h - 1:
                    break
                _put(stdscr, row + 1 + j, 1, line, w)
        stdscr.refresh()
        ch = stdscr.getch()
        action_id = None
        if ch == curses.KEY_MOUSE:
            try:
                _id, mx, my, _z, _bstate = curses.getmouse()
            except curses.error:
                continue
            if not (_bstate & curses.BUTTON1_PRESSED):
                continue
            action_id = _hit_button(buttons, my, mx)
        elif ch in (ord("q"), ord("Q")) and mode == "home":
            action_id = "quit"
        elif ch in (ord("a"), ord("A")) and mode == "home":
            action_id = "load-all"
        elif ch in (ord("+"),) and mode == "home":
            action_id = "add-start"
        elif ch == ord("1") and mode == "home":
            action_id = "add-one"
        elif ch == ord("4") and mode == "home":
            action_id = "add-four"
        elif ch == ord("-") and mode == "home":
            action_id = "remove-last"
        elif ch == curses.KEY_RESIZE:
            _fit_board_height()
            continue
        elif ch == -1:
            continue
        if not action_id:
            continue
        if action_id == "cancel":
            mode, pending, flash = "home", {}, ""
            continue
        if action_id == "page-prev":
            page = max(0, page - 1)
            continue
        if action_id == "page-next":
            page += 1
            continue
        if action_id == "add-start":
            mode, pending, flash = "add-name", {}, ""
            continue
        if action_id.startswith("pick-name:"):
            pending["name"] = action_id.split(":", 1)[1]
            mode = "add-provider"
            continue
        if action_id.startswith("pick-provider:"):
            pending["harness"] = action_id.split(":", 1)[1]
            mode = "add-effort"
            continue
        if action_id.startswith("pick-effort:"):
            try:
                seat = add_seat(cfg, pending["harness"], name=pending.get("name"),
                                effort=action_id.split(":", 1)[1])
                _live_add_pane(cfg, seat)
                flash = f"added {display_name(seat)}"
                page = max(0, (len(cfg.seats) - 1) // page_size)
            except (CrewError, KeyError, subprocess.CalledProcessError) as err:
                flash = f"couldn't  {err}"
            mode, pending = "home", {}
            continue
        try:
            result = do_action(cfg, action_id)
        except (CrewError, KeyError, subprocess.CalledProcessError) as err:
            flash = f"couldn't  {err}"
            continue
        if result == "quit":
            return
        if result == "loaded":
            flash = "Loaded. No task or message was sent."
        elif result == "resized":
            page = max(0, (len(cfg.seats) - 1) // page_size)
            flash = f"Team now has {len(cfg.seats)} agents. Nothing was loaded or sent."
        else:
            flash = "ok" if result == "ok" else _status_line(cfg)


def _board_plain(cfg: Crew) -> None:
    actions = desk_actions(cfg)
    while True:
        print()
        print(_status_line(cfg))
        for line in _bus_lines(cfg):
            print(" ", line)
        for i, act in enumerate(actions, 1):
            print(f"  {i}) {act['label']}")
        try:
            raw = input("> ").strip().lower()
        except EOFError:
            return
        if raw in {"q", "quit"}:
            do_action(cfg, "quit")
            return
        if raw in {"a", "all"}:
            do_action(cfg, "load-all")
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(actions):
            do_action(cfg, actions[int(raw) - 1]["id"])


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    default_desk = str(REPO / "examples" / "crew-four.toml")
    if not raw:
        raw = ["desk", default_desk]
    parser = argparse.ArgumentParser(
        prog="crew",
        description="Clickable tmux grid of coding-agent seats")
    parser.add_argument("--root", help="bus root (env AGENT_BUS_DIR)")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run_global",
                        help="print the exact commands without running them")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("desk", help="open the clickable crew window (the human app)")
    p.add_argument("config", nargs="?", default=default_desk)
    p.add_argument("--dry-run", action="store_true", dest="dry_run_up")
    p.add_argument("--no-attach", action="store_true")

    p = sub.add_parser("up", help="create the tmux session, launch seats, attach")
    p.add_argument("config", nargs="?", default="crew.toml")
    p.add_argument("--dry-run", action="store_true", dest="dry_run_up")
    p.add_argument("--no-attach", action="store_true",
                   help="leave the session detached (for scripts)")

    p = sub.add_parser("down", help="kill the crew tmux session")
    p.add_argument("config", nargs="?", default=default_desk)

    p = sub.add_parser("status", help="wrap bus.py status for the crew channel")
    p.add_argument("config", nargs="?", default=default_desk)

    p = sub.add_parser("shortcut", help="put Crew.app on the Desktop")
    p.add_argument("dest", nargs="?", default=str(Path.home() / "Desktop"))

    p = sub.add_parser("_hold", help=argparse.SUPPRESS)
    p.add_argument("--name", required=True)
    p.add_argument("--harness", required=True)
    p.add_argument("--role", default="worker")
    p.add_argument("--label", default="")

    p = sub.add_parser("_board", help=argparse.SUPPRESS)
    p.add_argument("config")
    p.add_argument("--root")
    p.add_argument("--session")

    args = parser.parse_args(raw)
    dry = bool(getattr(args, "dry_run_global", False) or getattr(args, "dry_run_up", False))
    root = Path(args.root) if getattr(args, "root", None) else None

    try:
        if args.command == "_hold":
            run_hold(args.name, args.harness, role=getattr(args, "role", "worker"),
                     label=getattr(args, "label", ""))
            return 0
        if args.command == "_board":
            cfg = load_config(args.config, root=root)
            if args.session:
                cfg.session = args.session
            run_board(cfg)
            return 0
        if args.command == "shortcut":
            print(install_shortcut(args.dest))
            return 0
        cfg = load_config(args.config, root=root)
        if args.command == "desk":
            text = desk(cfg, dry_run=dry, attach=not getattr(args, "no_attach", False))
            if dry:
                sys.stdout.write(text)
        elif args.command == "up":
            text = up(cfg, dry_run=dry, attach=not getattr(args, "no_attach", False))
            if dry:
                sys.stdout.write(text)
        elif args.command == "down":
            down(cfg)
            print(f"down: {cfg.session}")
        else:
            print(json.dumps(channel_status(cfg), indent=2))
    except CrewError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as err:
        print(f"error: command failed: {shlex.join(err.cmd)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
