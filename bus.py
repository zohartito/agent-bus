#!/usr/bin/env python3
"""agent-bus: a local mailbox two live coding-agent sessions poll.

One channel = one directory under the bus root. A message is one JSON file
named by its zero-padded sequence number, claimed with an atomic hard link so
ids stay monotonic and gapless under concurrent senders. Identity, ids,
de-dupe, and DONE live here in code; the models only orchestrate.

Wake-on-message: an agent may register (via `join --wake`) the shell command
that resumes its own harness session. `watch-wake` is a per-machine daemon
that runs that command when a message lands for an agent that is behind and
not polling. Wake commands come from the operator, never from a message.

Stdlib only. Same file is the CLI (`python3 bus.py send ...`) and the library
that mcp_server.py and the tests import.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = "~/.agent-bus"
DEFAULT_CHANNEL = "main"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SEQ_WIDTH = 8
POLL_SECONDS = 0.5
VERDICTS = ("PASS", "FAIL")
KINDS = ("message", "done")
DEFAULT_COOLDOWN = 120.0  # seconds between two wakes of the same agent on one channel
DEFAULT_IDLE = 60.0       # no recv/watch heartbeat for this long means "not polling"
WAKE_POLL_SECONDS = 1.0
WAKE_COMMAND_ENV = "AGENT_BUS_WAKE_COMMAND"
_DETACHED: list = []  # supervisors nobody waits for; polled on the next launch

# High-precision shapes only: this is a tripwire against pasting credentials,
# not a vault. Override with --allow-secret-like on a false positive.
SECRET_PATTERNS = (
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("api key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.")),
)


class BusError(Exception):
    """Refused or failed bus operation; the message is safe to show the agent."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolve_root(root: str | None = None) -> Path:
    raw = root or os.environ.get("AGENT_BUS_DIR") or DEFAULT_ROOT
    return Path(raw).expanduser()


def _valid_name(what: str, name: str | None) -> str:
    if not name or not NAME_RE.match(name):
        raise BusError(f"{what} must match {NAME_RE.pattern}, got {name!r}")
    return name


def _channel_dir(root: Path, channel: str, create: bool = False) -> Path:
    ch = root / _valid_name("channel", channel)
    if create:
        for sub in ("messages", "tmp", "cursors", "done", "heartbeats", "wake"):
            (ch / sub).mkdir(parents=True, exist_ok=True)
    elif not (ch / "messages").is_dir():
        raise BusError(f"channel {channel!r} does not exist under {root}")
    return ch


def _existing_seqs(msgs_dir: Path) -> list[int]:
    stem_len = SEQ_WIDTH  # names shorter than this are never ours
    return sorted(
        int(p.name[:-5])
        for p in msgs_dir.iterdir()
        if p.name.endswith(".json") and p.name[:-5].isdigit() and len(p.name) - 5 >= stem_len
    )


def _message_path(msgs_dir: Path, seq: int) -> Path:
    return msgs_dir / f"{seq:0{SEQ_WIDTH}d}.json"


def _read_after(ch: Path, after_seq: int) -> list[dict]:
    # Walk seq+1, seq+2, ... until a hole. Directory listing can miss a file
    # created during iterdir, and a cursor that jumped that hole would lose it.
    msgs_dir = ch / "messages"
    out = []
    seq = after_seq + 1
    while True:
        path = _message_path(msgs_dir, seq)
        if not path.is_file():
            break
        with path.open("r", encoding="utf-8") as fh:
            out.append(json.load(fh))
        seq += 1
    return out


def _write_json(ch: Path, path: Path, data: dict) -> None:
    tmp = ch / "tmp" / f"{path.stem}-{uuid.uuid4().hex}"
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def secret_like(text: str) -> str | None:
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


def send(root: Path, channel: str, agent: str, harness: str, body: str,
         kind: str = "message", allow_secret_like: bool = False,
         wake: bool = False, cooldown: float = DEFAULT_COOLDOWN) -> dict:
    agent = _valid_name("agent", agent)
    harness = _valid_name("harness", harness)
    if kind not in KINDS:
        raise BusError(f"kind must be one of {KINDS}")
    if not body or not body.strip():
        raise BusError("body is empty")
    if not allow_secret_like:
        hit = secret_like(body)
        if hit:
            raise BusError(
                f"body looks like it contains a secret ({hit}); secrets never go on the bus. "
                "If this is a false positive, pass --allow-secret-like."
            )
    ch = _channel_dir(root, channel, create=True)
    msgs_dir, tmp_dir = ch / "messages", ch / "tmp"
    record = {
        "seq": 0, "id": "", "ts": "",
        "channel": channel, "agent": agent, "harness": harness,
        "kind": kind, "body": body,
    }
    for _ in range(10000):
        seqs = _existing_seqs(msgs_dir)
        seq = (seqs[-1] if seqs else 0) + 1
        record["seq"] = seq
        record["id"] = f"{channel}:{seq:0{SEQ_WIDTH}d}"
        record["ts"] = _now_iso()
        tmp = tmp_dir / f"{uuid.uuid4().hex}.json"
        tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        try:
            os.link(tmp, _message_path(msgs_dir, seq))
        except FileExistsError:
            tmp.unlink(missing_ok=True)
            time.sleep(0.002)
            continue
        tmp.unlink(missing_ok=True)
        verdict_path = ch / "VERDICT.json"
        if verdict_path.is_file() and not status(root, channel)["complete"]:
            verdict_path.unlink(missing_ok=True)
        # Tell the sender which peers can be woken; --wake forces it now.
        peers = [w["agent"] for w in list_wakes(root, channel) if w["agent"] != agent]
        result = dict(record, wake_registered=peers)
        if wake:
            result["woken"] = [
                wake_agent(root, channel, peer, seq=seq, force=True,
                           cooldown=cooldown, source="send --wake")
                for peer in peers
            ]
        return result
    raise BusError("could not claim a message id after 10000 attempts")


def _cursor_path(ch: Path, agent: str) -> Path:
    return ch / "cursors" / f"{agent}.json"


def _read_cursor(ch: Path, agent: str) -> int:
    try:
        return int(json.loads(_cursor_path(ch, agent).read_text(encoding="utf-8"))["last_seq"])
    except (OSError, ValueError, KeyError):
        return 0


def _write_cursor(ch: Path, agent: str, last_seq: int) -> None:
    tmp = ch / "tmp" / f"cursor-{uuid.uuid4().hex}"
    tmp.write_text(json.dumps({"last_seq": last_seq, "ts": _now_iso()}), encoding="utf-8")
    os.replace(tmp, _cursor_path(ch, agent))


def _heartbeat_path(ch: Path, agent: str) -> Path:
    return ch / "heartbeats" / agent


def _touch_heartbeat(ch: Path, agent: str) -> None:
    path = _heartbeat_path(ch, agent)
    path.parent.mkdir(exist_ok=True)
    path.touch()


def _heartbeat_age(ch: Path, agent: str) -> float | None:
    try:
        return time.time() - _heartbeat_path(ch, agent).stat().st_mtime
    except OSError:
        return None


def recv(root: Path, channel: str, agent: str, wait: bool = False,
         timeout: float = 60.0, peek: bool = False, include_own: bool = False,
         after: int | None = None, poll: float = POLL_SECONDS) -> list[dict]:
    agent = _valid_name("agent", agent)
    ch = _channel_dir(root, channel, create=True)
    if after is not None:
        peek = True  # explicit ranges never move the cursor
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        _touch_heartbeat(ch, agent)  # "I am polling": watch-wake leaves me alone
        start = _read_cursor(ch, agent) if after is None else after
        batch = _read_after(ch, start)
        visible = [m for m in batch if include_own or m["agent"] != agent]
        if batch and not peek:
            _write_cursor(ch, agent, batch[-1]["seq"])
        if visible or not wait or time.monotonic() >= deadline:
            return visible
        time.sleep(poll)


def post_done(root: Path, channel: str, agent: str, harness: str,
              verdict: str, summary: str, keep_wake: bool = False) -> dict:
    verdict = (verdict or "").upper()
    if verdict not in VERDICTS:
        raise BusError(f"verdict must be one of {VERDICTS}")
    if not summary or not summary.strip():
        raise BusError("summary is required: one line on what was (or was not) accomplished")
    # The done message goes first: it runs the same validation and secret
    # tripwire as any send, so a refused done leaves no half-written verdict.
    send(root, channel, agent, harness, f"DONE {verdict}: {summary}", kind="done")
    ch = _channel_dir(root, channel)
    record = {"agent": agent, "harness": harness, "verdict": verdict,
              "summary": summary, "ts": _now_iso()}
    tmp = ch / "tmp" / f"done-{uuid.uuid4().hex}"
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, ch / "done" / f"{agent}.json")
    wake_cleared = False if keep_wake else clear_wake(root, channel, agent)
    st = status(root, channel)
    if st["complete"]:
        tmp = ch / "tmp" / f"verdict-{uuid.uuid4().hex}"
        tmp.write_text(json.dumps({
            "channel": channel, "complete": True, "verdict": st["verdict"],
            "participants": st["participants"], "ts": _now_iso(),
        }, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, ch / "VERDICT.json")
    return {"done": record, "complete": st["complete"], "verdict": st["verdict"],
            "wake_cleared": wake_cleared}


def status(root: Path, channel: str) -> dict:
    ch = _channel_dir(root, channel)
    msgs = _read_after(ch, 0)
    agents: dict[str, dict] = {}

    def entry(name: str, harness: str) -> dict:
        return agents.setdefault(name, {"agent": name, "harness": harness,
                                        "messages": 0, "last_ts": None, "done": None})

    for m in msgs:
        e = entry(m["agent"], m["harness"])
        e["messages"] += 1
        e["harness"] = m["harness"]
        e["last_ts"] = m["ts"]
    for path in sorted((ch / "done").glob("*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        entry(rec["agent"], rec["harness"])["done"] = {
            "verdict": rec["verdict"], "summary": rec["summary"], "ts": rec["ts"]}
    participants = [agents[name] for name in sorted(agents)]
    complete = bool(participants) and all(p["done"] for p in participants)
    verdict = None
    if complete:
        verdict = "PASS" if all(p["done"]["verdict"] == "PASS" for p in participants) else "FAIL"
    return {
        "channel": channel,
        "root": str(root),
        "message_count": len(msgs),
        "last_seq": msgs[-1]["seq"] if msgs else 0,
        "participants": participants,
        "complete": complete,
        "verdict": verdict,
        "wakes": [w["agent"] for w in list_wakes(root, channel)],
    }


def list_channels(root: Path) -> list[dict]:
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if (child / "messages").is_dir():
            out.append({
                "channel": child.name,
                "messages": len(_existing_seqs(child / "messages")),
                "complete": (child / "VERDICT.json").is_file(),
            })
    return out


# --- wake-on-message ---------------------------------------------------------
#
# <channel>/wake/<agent>.json   registration: harness, command, last wake
# <channel>/wake/log.jsonl      one "launch" and one "exit" line per wake
# <channel>/wake/<agent>.out    stdout+stderr of the resumed harness
# <channel>/heartbeats/<agent>  mtime = last recv/watch poll by that agent


def _wake_dir(ch: Path) -> Path:
    d = ch / "wake"
    d.mkdir(exist_ok=True)
    return d


def _wake_path(ch: Path, agent: str) -> Path:
    return _wake_dir(ch) / f"{agent}.json"


def _wake_log(ch: Path, entry: dict) -> None:
    with (_wake_dir(ch) / "log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(ts=_now_iso(), **entry)) + "\n")


def register_wake(root: Path, channel: str, agent: str, harness: str, command: str,
                  allow_secret_like: bool = False) -> dict:
    agent = _valid_name("agent", agent)
    harness = _valid_name("harness", harness)
    if not command or not command.strip():
        raise BusError("wake command is empty")
    if not allow_secret_like:
        hit = secret_like(command)
        if hit:
            raise BusError(
                f"wake command looks like it contains a secret ({hit}); put credentials in the "
                "harness's own config, not in the command. False positive: --allow-secret-like.")
    ch = _channel_dir(root, channel, create=True)
    # The command is the operator's, never a peer's: refuse anything that
    # already appears in a message body on this channel.
    needle = command.strip()
    for m in _read_after(ch, 0):
        if needle in m["body"]:
            raise BusError(
                f"wake command appears verbatim in message {m['id']}; wake commands are "
                "registered by the operator and never taken from a message body")
    record = {"agent": agent, "harness": harness, "command": command,
              "registered_ts": _now_iso(), "last_wake_seq": 0, "last_wake_epoch": 0.0,
              "last_wake_ts": None}
    _write_json(ch, _wake_path(ch, agent), record)
    return record


def get_wake(root: Path, channel: str, agent: str) -> dict | None:
    try:
        ch = _channel_dir(root, channel)
    except BusError:
        return None
    return _read_json(_wake_path(ch, _valid_name("agent", agent)))


def list_wakes(root: Path, channel: str) -> list[dict]:
    try:
        ch = _channel_dir(root, channel)
    except BusError:
        return []
    out = []
    for path in sorted(_wake_dir(ch).glob("*.json")):
        rec = _read_json(path)
        if rec and rec.get("command"):
            out.append(rec)
    return out


def clear_wake(root: Path, channel: str, agent: str) -> bool:
    ch = _channel_dir(root, channel)
    try:
        _wake_path(ch, _valid_name("agent", agent)).unlink()
        return True
    except FileNotFoundError:
        return False


def _launch_wake(root: Path, channel: str, agent: str, seq: int, force: bool,
                 cooldown: float, idle: float, source: str
                 ) -> tuple[dict, subprocess.Popen | None]:
    """Decide and launch. The command itself runs under a small supervisor
    subprocess (`bus _supervise`) so the exit code gets logged even when the
    caller cannot wait for a whole resumed harness session."""
    ch = _channel_dir(root, channel)
    result = {"channel": channel, "agent": agent, "seq": seq, "forced": force,
              "woke": False, "reason": ""}
    reg = get_wake(root, channel, agent)
    if reg is None:
        result["reason"] = "no wake registration"
        return result, None
    now = time.time()
    if seq <= int(reg.get("last_wake_seq", 0)):
        result["reason"] = f"already woke for seq {reg['last_wake_seq']}"
        return result, None
    since = now - float(reg.get("last_wake_epoch", 0.0))
    if since < cooldown:
        result["reason"] = f"cooldown: woke {since:.0f}s ago, cooldown is {cooldown:.0f}s"
        return result, None
    if not force:
        age = _heartbeat_age(ch, agent)
        if age is not None and age < idle:
            result["reason"] = f"polling: heartbeat {age:.0f}s ago, idle threshold {idle:.0f}s"
            return result, None
    reg.update(last_wake_seq=seq, last_wake_epoch=now, last_wake_ts=_now_iso())
    _write_json(ch, _wake_path(ch, agent), reg)
    argv = [sys.executable, str(Path(__file__).resolve()), "_supervise",
            "--root", str(root), "-c", channel, "-a", agent, "--harness", reg["harness"],
            "--seq", str(seq), "--source", source]
    if force:
        argv.append("--forced")
    env = dict(os.environ)
    env[WAKE_COMMAND_ENV] = reg["command"]
    with (_wake_dir(ch) / f"{agent}.out").open("ab") as out:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    _wake_log(ch, {"event": "launch", "agent": agent, "harness": reg["harness"], "seq": seq,
                   "forced": force, "source": source, "pid": proc.pid,
                   "command": reg["command"]})
    result.update(woke=True, reason="launched", pid=proc.pid, command=reg["command"])
    return result, proc


def _supervise(root: Path, channel: str, agent: str, harness: str, seq: int,
               source: str, forced: bool) -> int:
    ch = _channel_dir(root, channel)
    command = os.environ.get(WAKE_COMMAND_ENV)
    if not command:
        _wake_log(ch, {"event": "exit", "agent": agent, "seq": seq, "forced": forced,
                       "source": source, "pid": os.getpid(), "exit_code": None,
                       "error": f"{WAKE_COMMAND_ENV} not set"})
        return 1
    # The resumed harness speaks as the woken agent, whoever launched the wake.
    env = dict(os.environ, AGENT_BUS_DIR=str(root), AGENT_BUS_CHANNEL=channel,
               AGENT_BUS_AGENT=agent, AGENT_BUS_HARNESS=harness)
    env.pop(WAKE_COMMAND_ENV, None)
    proc = subprocess.run(command, shell=True, env=env, stdin=subprocess.DEVNULL)
    _wake_log(ch, {"event": "exit", "agent": agent, "seq": seq, "forced": forced,
                   "source": source, "pid": os.getpid(), "command": command,
                   "exit_code": proc.returncode})
    return proc.returncode


def wake_agent(root: Path, channel: str, agent: str, seq: int | None = None,
               force: bool = True, cooldown: float = DEFAULT_COOLDOWN,
               idle: float = DEFAULT_IDLE, wait: bool = False, source: str = "wake") -> dict:
    """Wake one registered peer. `force` skips the heartbeat check, never the
    cooldown or the once-per-seq guard."""
    ch = _channel_dir(root, channel)
    if get_wake(root, channel, agent) is None:
        raise BusError(f"agent {agent!r} has no wake registration on channel {channel!r}")
    if seq is None:
        seqs = _existing_seqs(ch / "messages")
        seq = seqs[-1] if seqs else 0
    result, proc = _launch_wake(root, channel, agent, seq, force, cooldown, idle, source)
    if proc is not None:
        if wait:
            result["exit_code"] = proc.wait()
        else:
            _DETACHED[:] = [p for p in _DETACHED if p.poll() is None]
            _DETACHED.append(proc)
    return result


def _sweep(root: Path, cooldown: float, idle: float, source: str
           ) -> tuple[list[dict], list[subprocess.Popen]]:
    results, procs = [], []
    for info in list_channels(root):
        channel = info["channel"]
        ch = root / channel
        regs = list_wakes(root, channel)
        if not regs:
            continue
        seqs = _existing_seqs(ch / "messages")
        last = seqs[-1] if seqs else 0
        for reg in regs:
            agent = reg["agent"]
            cursor = _read_cursor(ch, agent)
            if last <= cursor or last <= int(reg.get("last_wake_seq", 0)):
                continue
            if time.time() - float(reg.get("last_wake_epoch", 0.0)) < cooldown:
                continue
            age = _heartbeat_age(ch, agent)
            if age is not None and age < idle:
                continue
            pending = [m["seq"] for m in _read_after(ch, cursor) if m["agent"] != agent]
            if not pending:
                continue
            result, proc = _launch_wake(root, channel, agent, pending[-1], False,
                                        cooldown, idle, source)
            if proc is not None:
                results.append(result)
                procs.append(proc)
    return results, procs


def sweep_wakes(root: Path, cooldown: float = DEFAULT_COOLDOWN, idle: float = DEFAULT_IDLE,
                wait: bool = False, source: str = "watch-wake") -> list[dict]:
    """One pass over every channel: wake each registered agent that is behind
    and not polling. Returns the wakes that were launched."""
    results, procs = _sweep(root, cooldown, idle, source)
    if wait:
        for result, proc in zip(results, procs):
            result["exit_code"] = proc.wait()
    return results


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def watch_wake(root: Path, cooldown: float = DEFAULT_COOLDOWN, idle: float = DEFAULT_IDLE,
               poll: float = WAKE_POLL_SECONDS, once: bool = False) -> int:
    root.mkdir(parents=True, exist_ok=True)
    if once:
        print(json.dumps(sweep_wakes(root, cooldown, idle, wait=True), indent=2))
        return 0
    pid_path = root / "watch-wake.pid"
    try:
        other = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        other = None
    if other and other != os.getpid() and _pid_alive(other):
        raise BusError(f"watch-wake is already running on this machine (pid {other})")
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so `kill` still removes the pid file
    print(f"watch-wake: root={root} cooldown={cooldown:g}s idle={idle:g}s poll={poll:g}s",
          file=sys.stderr, flush=True)
    procs: list[subprocess.Popen] = []
    try:
        while True:
            launched, new_procs = _sweep(root, cooldown, idle, "watch-wake")
            for result in launched:
                print(json.dumps(result), flush=True)
            procs.extend(new_procs)
            procs = [p for p in procs if p.poll() is None]  # reap finished supervisors
            time.sleep(poll)
    except KeyboardInterrupt:
        pass
    finally:
        pid_path.unlink(missing_ok=True)
    return 0


def _identity(args: argparse.Namespace, need_harness: bool = True) -> tuple[str, str | None]:
    agent = args.agent or os.environ.get("AGENT_BUS_AGENT")
    harness = args.harness or os.environ.get("AGENT_BUS_HARNESS")
    if not agent:
        raise BusError("identity required: pass --agent or set AGENT_BUS_AGENT")
    if need_harness and not harness:
        raise BusError("identity required: pass --harness or set AGENT_BUS_HARNESS")
    return agent, harness


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bus", description="agent-bus: a local mailbox two live coding-agent sessions poll")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help=f"bus root (env AGENT_BUS_DIR, default {DEFAULT_ROOT})")
    common.add_argument("-c", "--channel",
                        default=os.environ.get("AGENT_BUS_CHANNEL", DEFAULT_CHANNEL),
                        help="channel name (env AGENT_BUS_CHANNEL, default %(default)s)")
    common.add_argument("-a", "--agent", help="who you are (env AGENT_BUS_AGENT)")
    common.add_argument("--harness", help="which harness runs you (env AGENT_BUS_HARNESS)")

    p = sub.add_parser("send", parents=[common], help="post one message to the channel")
    p.add_argument("--body", required=True, help="message text; '-' reads stdin")
    p.add_argument("--allow-secret-like", action="store_true",
                   help="override the secret tripwire for a false positive")
    p.add_argument("--wake", action="store_true",
                   help="run every registered peer's wake command now, heartbeat or not")

    p = sub.add_parser("recv", parents=[common],
                       help="new messages from others; advances your cursor")
    p.add_argument("--wait", action="store_true", help="block until a message arrives or --timeout")
    p.add_argument("--timeout", type=float, default=60.0, help="seconds to wait (default %(default)s)")
    p.add_argument("--peek", action="store_true", help="read without advancing the cursor")
    p.add_argument("--include-own", action="store_true", help="also show your own messages")
    p.add_argument("--after", type=int,
                   help="read from this seq instead of your cursor (implies --peek; 0 = everything)")

    p = sub.add_parser("watch", parents=[common], help="recv --wait with a long default timeout")
    p.add_argument("--timeout", type=float, default=300.0, help="seconds to wait (default %(default)s)")

    p = sub.add_parser("done", parents=[common],
                       help="post your verdict; when every participant has, VERDICT.json appears")
    p.add_argument("--verdict", required=True,
                   choices=list(VERDICTS) + [v.lower() for v in VERDICTS])
    p.add_argument("--summary", required=True,
                   help="one line: what was (or was not) accomplished")
    p.add_argument("--keep-wake", action="store_true",
                   help="leave your wake registration in place after done")

    sub.add_parser("status", parents=[common],
                   help="participants, verdicts, complete flag for the channel")
    sub.add_parser("channels", parents=[common], help="list channels under the root")

    p = sub.add_parser("join", parents=[common],
                       help="register the shell command that resumes YOUR session on this channel")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--wake", metavar="COMMAND",
                   help='e.g. \'claude --resume <id> -p "read agent-bus channel X and continue"\'')
    g.add_argument("--clear", action="store_true", help="remove your wake registration")
    p.add_argument("--allow-secret-like", action="store_true",
                   help="override the secret tripwire for a false positive")

    p = sub.add_parser("wake", parents=[common],
                       help="run a registered peer's wake command now (cooldown still applies)")
    p.add_argument("peer", help="agent to wake")

    p = sub.add_parser("watch-wake", parents=[common],
                       help="daemon, one per machine: wake agents that are behind and not polling")
    p.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN,
                   help="seconds between wakes of one agent (default %(default)s)")
    p.add_argument("--idle", type=float, default=DEFAULT_IDLE,
                   help="seconds without a recv/watch heartbeat before an agent counts as "
                        "not polling (default %(default)s)")
    p.add_argument("--poll", type=float, default=WAKE_POLL_SECONDS,
                   help="seconds between sweeps (default %(default)s)")
    p.add_argument("--once", action="store_true", help="one sweep, wait for the wakes, exit")

    p = sub.add_parser("_supervise", parents=[common], help=argparse.SUPPRESS)
    p.add_argument("--seq", type=int, required=True)
    p.add_argument("--source", default="wake")
    p.add_argument("--forced", action="store_true")

    args = parser.parse_args(argv)
    root = resolve_root(args.root)
    try:
        if args.command == "send":
            body = sys.stdin.read() if args.body == "-" else args.body
            agent, harness = _identity(args)
            result = send(root, args.channel, agent, harness, body,
                          allow_secret_like=args.allow_secret_like, wake=args.wake)
        elif args.command in ("recv", "watch"):
            agent, _ = _identity(args, need_harness=False)
            result = recv(root, args.channel, agent,
                          wait=(args.command == "watch") or getattr(args, "wait", False),
                          timeout=args.timeout,
                          peek=getattr(args, "peek", False),
                          include_own=getattr(args, "include_own", False),
                          after=getattr(args, "after", None))
        elif args.command == "done":
            agent, harness = _identity(args)
            result = post_done(root, args.channel, agent, harness, args.verdict, args.summary,
                               keep_wake=args.keep_wake)
        elif args.command == "status":
            result = status(root, args.channel)
        elif args.command == "channels":
            result = list_channels(root)
        elif args.command == "join":
            if args.clear:
                agent, _ = _identity(args, need_harness=False)
                result = {"agent": agent, "channel": args.channel,
                          "cleared": clear_wake(root, args.channel, agent)}
            else:
                agent, harness = _identity(args)
                result = register_wake(root, args.channel, agent, harness, args.wake,
                                       allow_secret_like=args.allow_secret_like)
        elif args.command == "wake":
            result = wake_agent(root, args.channel, args.peer, source="wake")
        elif args.command == "watch-wake":
            return watch_wake(root, cooldown=args.cooldown, idle=args.idle,
                              poll=args.poll, once=args.once)
        else:  # _supervise
            agent, harness = _identity(args)
            return _supervise(root, args.channel, agent, harness, args.seq,
                              args.source, args.forced)
    except BusError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
