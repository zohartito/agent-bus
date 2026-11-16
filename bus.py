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
`spawn` starts a peer detached and registers its wake from the session id
the harness reports.

Stdlib only. Same file is the CLI (`python3 bus.py send ...`) and the library
that mcp_server.py and the tests import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
if sys.version_info < (3, 11):
    raise SystemExit("agent-bus requires Python 3.11 or newer; run with python3.11+.")

import tomllib
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # no flock (Windows): locks become no-ops
    fcntl = None

DEFAULT_ROOT = "~/.agent-bus"
DEFAULT_CHANNEL = "main"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SEQ_WIDTH = 8
POLL_SECONDS = 0.5
VERDICTS = ("PASS", "FAIL")
# Anyone may send these; "done" and "phase" are posted only by post_done.
SEND_KINDS = ("message", "plan", "question", "answer", "checkpoint", "review", "blocker")
KINDS = SEND_KINDS + ("done", "phase")
HUMAN_HARNESS = "human"   # a person: consulted, never blocks VERDICT.json; a FAIL still counts
DEFAULT_MAX_TURNS = 230   # measured baseline: 2 * 112 counted turns, rounded up to 10
DEFAULT_COOLDOWN = 120.0  # seconds between two wakes of the same agent on one channel
DEFAULT_IDLE = 60.0       # no recv/watch heartbeat for this long means "not polling"
DEFAULT_SILENT = 600.0    # no poll, send, or live watch for this long: status says "silent"
WAKE_POLL_SECONDS = 1.0
WAKE_COMMAND_ENV = "AGENT_BUS_WAKE_COMMAND"
WAKE_ARGV_ENV = "AGENT_BUS_WAKE_ARGV"
CONFIG_NAME = ".agent-bus.toml"
CONFIG_KEYS = ("agent", "harness", "channel", "root")
HOST = socket.gethostname()
_DETACHED: list = []  # supervisors nobody waits for; polled on the next launch

# `session id: <uuid>` (codex exec), `"thread_id":"<uuid>"` (codex exec --json).
SESSION_ID_RE = re.compile(
    r"(?:session[ _-]?id|thread_id)\"?\s*[:=]\s*\"?([0-9A-Za-z][0-9A-Za-z_-]{7,})", re.I)
DEFAULT_HARNESS = {"codex": "codex-cli", "claude": "claude-code"}
# Flags a headless resume must repeat to act like the spawned session
# (model, and whether it may run tools unattended): name -> values it takes.
RESUME_FLAGS = {
    "codex": {"-m": 1, "--model": 1, "-c": 1, "--config": 1, "--enable": 1, "--disable": 1,
              "--dangerously-bypass-approvals-and-sandbox": 0, "--skip-git-repo-check": 0},
    "claude": {"--model": 1, "--effort": 1, "--permission-mode": 1,
               "--dangerously-skip-permissions": 0},
}

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


class TurnCapError(BusError):
    """The channel needs a moderator before further messages can be sent."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _age_of(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return time.time() - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def resolve_root(root: str | None = None) -> Path:
    raw = root or os.environ.get("AGENT_BUS_DIR") or os.environ.get("AGENT_BUS_ROOT") or DEFAULT_ROOT
    # Absolute, so a wake or spawn that runs `cd <cwd> && ...` still finds it.
    return Path(raw).expanduser().absolute()


def load_config(start: Path | None = None) -> dict:
    """The nearest .agent-bus.toml at or above `start` (default: cwd), or {}."""
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        path = folder / CONFIG_NAME
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as err:
            raise BusError(f"{path}: {err}") from err
        unknown = sorted(set(data) - set(CONFIG_KEYS))
        if unknown:
            raise BusError(f"{path}: unknown keys {unknown}; allowed: {list(CONFIG_KEYS)}")
        if "root" in data:  # a relative root is relative to the file, not to the caller
            data["root"] = str(path.parent / Path(str(data["root"])).expanduser())
        return data
    return {}


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


def _detach(proc: subprocess.Popen) -> None:
    _DETACHED[:] = [p for p in _DETACHED if p.poll() is None]
    _DETACHED.append(proc)


@contextmanager
def _flock(path: Path, blocking: bool = True):
    """Exclusive advisory lock for the block. Yields False instead of waiting
    when non-blocking and held. The kernel drops it if the process dies."""
    if fcntl is None:
        yield True
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def secret_like(text: str) -> str | None:
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


# --- channel limits and moderation ------------------------------------------


@contextmanager
def _channel_lock(ch: Path):
    """Serialize budget checks, message claims, closure and config changes."""
    path = ch / ".verdict.lock"
    if fcntl is not None:
        with _flock(path):
            yield
    else:  # Windows needs a real lock here, unlike the optional reader locks.
        import msvcrt
        with path.open("a+b") as fh:
            if not fh.seek(0, os.SEEK_END):
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as err:
                raise BusError("channel busy; retry the operation") from err
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


def _positive_int(value, label: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)) or int(value) < 1:
        raise BusError(f"{label} must be a positive integer")
    return int(value)


def _moderator_name(name: str) -> str:
    if secret_like(name):
        raise BusError("moderator looks like it contains a secret")
    return _valid_name("moderator", name)


def channel_config(ch: Path) -> dict:
    """Read only; a stored budget beats the environment, including for old channels."""
    path = ch / "config.json"
    if path.exists():
        cfg = _read_json(path)
        if not isinstance(cfg, dict) or set(cfg) != {"max_turns", "moderator"}:
            raise BusError("invalid channel config.json")
        cap = _positive_int(cfg["max_turns"], "max_turns")
        moderator = cfg["moderator"]
        if moderator is not None:
            if not isinstance(moderator, str):
                raise BusError("invalid moderator in channel config.json")
            _moderator_name(moderator)
        return {"max_turns": cap, "moderator": moderator}
    return {"max_turns": _positive_int(os.environ.get("AGENT_BUS_MAX_TURNS", DEFAULT_MAX_TURNS),
                                       "AGENT_BUS_MAX_TURNS"), "moderator": None}


def _turn_state(ch: Path, msgs: list[dict] | None = None, cfg: dict | None = None) -> dict:
    cfg = channel_config(ch) if cfg is None else cfg
    msgs = _read_after(ch, 0) if msgs is None else msgs
    used = sum(m["kind"] in SEND_KINDS and m["harness"] != HUMAN_HARNESS for m in msgs)
    return {"turns_used": used, "cap": cfg["max_turns"], "capped": used >= cfg["max_turns"]}


def _record_cap(ch: Path, state: dict) -> None:
    # Called with the channel lock held. Retained after a raise as a one-time receipt.
    if state["capped"] and not (ch / "CAPPED.json").exists():
        _write_json(ch, ch / "CAPPED.json", dict(state, channel=ch.name, ts=_now_iso()))


def _closed(ch: Path) -> dict | None:
    record = _read_json(ch / "VERDICT.json")
    return record if record and record.get("closed_by") else None


def _require_moderator(cfg: dict, agent: str | None, harness: str | None) -> None:
    if not agent or not harness or not (harness == HUMAN_HARNESS or agent == cfg["moderator"]):
        raise BusError("only a human or the configured moderator may change or close this channel")
    _valid_name("agent", agent)
    _valid_name("harness", harness)


def configure_channel(root: Path, channel: str, max_turns: int | None = None,
                      moderator: str | None = None, agent: str | None = None,
                      harness: str | None = None) -> dict:
    if max_turns is not None:
        max_turns = _positive_int(max_turns, "max_turns")
    if moderator is not None:
        moderator = _moderator_name(moderator)
    ch = _channel_dir(root, channel, create=True)
    with _channel_lock(ch):
        cfg = channel_config(ch)
        if (ch / "config.json").exists() or _existing_seqs(ch / "messages"):
            if max_turns is None and moderator is None:
                return cfg
            _require_moderator(cfg, agent, harness)
        if _closed(ch):
            raise BusError(f"channel {channel} is closed")
        if max_turns is not None:
            cfg["max_turns"] = max_turns
        if moderator is not None:
            cfg["moderator"] = moderator
        _write_json(ch, ch / "config.json", cfg)
        state = _turn_state(ch, cfg=cfg)
        _record_cap(ch, state)
        # Lowering the cap onto a channel the peers already signed off reopens the
        # question: a capped channel needs a moderator decision, so the earlier
        # completion receipt no longer holds (send() applies the same rule).
        if state["capped"]:
            (ch / "VERDICT.json").unlink(missing_ok=True)
        return cfg


def raise_cap(root: Path, channel: str, agent: str, harness: str, by: int) -> dict:
    by = _positive_int(by, "by")
    ch = _channel_dir(root, channel)
    with _channel_lock(ch):
        cfg = channel_config(ch)
        _require_moderator(cfg, agent, harness)
        if _closed(ch):
            raise BusError(f"channel {channel} is closed")
        cfg["max_turns"] += by
        _write_json(ch, ch / "config.json", cfg)
        return dict(cfg, **_turn_state(ch, cfg=cfg))


def close_channel(root: Path, channel: str, agent: str, harness: str,
                  verdict: str, summary: str) -> dict:
    verdict = (verdict or "").upper()
    if verdict not in VERDICTS:
        raise BusError(f"verdict must be one of {VERDICTS}")
    if not summary or not summary.strip():
        raise BusError("summary is required")
    if secret_like(summary):
        raise BusError("summary looks like it contains a secret")
    ch = _channel_dir(root, channel)
    with _channel_lock(ch):
        cfg = channel_config(ch)
        _require_moderator(cfg, agent, harness)
        if _closed(ch):
            raise BusError(f"channel {channel} is closed")
        state = _turn_state(ch, cfg=cfg)
        _record_cap(ch, state)
        record = dict(state, channel=channel, complete=True, verdict=verdict,
                      summary=summary, closed_by=agent, harness=harness,
                      reason="turn_cap" if state["capped"] else "moderator", ts=_now_iso())
        _write_json(ch, ch / "VERDICT.json", record)
        return record


# --- pinned references -------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def pin_ref(spec: str) -> dict:
    """`path` or `path@<sha256 or a prefix of 8+ hex>` -> {path, sha256, size}.
    A pinned hash must match the file on disk now."""
    path_part, want = spec, None
    head, sep, tail = spec.rpartition("@")
    if sep and not Path(spec).expanduser().is_file() and re.fullmatch(r"[0-9a-fA-F]{8,64}", tail):
        path_part, want = head, tail.lower()
    path = Path(path_part).expanduser().resolve()
    if not path.is_file():
        raise BusError(f"ref {spec!r}: {path} is not a file")
    digest = _sha256(path)
    if want and not digest.startswith(want):
        raise BusError(f"ref {spec!r}: the file on disk is sha256 {digest[:16]}..., not {want}")
    return {"path": str(path), "sha256": digest, "size": path.stat().st_size}


def verify_refs(root: Path, channel: str, seq: int) -> list[dict]:
    """Each ref of message `seq` with status unchanged, changed, or missing."""
    ch = _channel_dir(root, channel)
    msg = _read_json(_message_path(ch / "messages", seq))
    if msg is None:
        raise BusError(f"no message {seq} on channel {channel!r}")
    out = []
    for ref in msg.get("refs", []):
        path = Path(ref["path"])
        current = _sha256(path) if path.is_file() else None
        state = ("missing" if current is None
                 else "unchanged" if current == ref["sha256"] else "changed")
        out.append(dict(ref, status=state, current_sha256=current))
    return out


def send(root: Path, channel: str, agent: str, harness: str, body: str,
         kind: str = "message", allow_secret_like: bool = False,
         wake: bool = False, cooldown: float = DEFAULT_COOLDOWN,
         for_agent: str | None = None, refs: list[str] | None = None,
         _extra: dict | None = None) -> dict:
    agent = _valid_name("agent", agent)
    harness = _valid_name("harness", harness)
    if kind not in KINDS:
        raise BusError(f"kind must be one of {KINDS}")
    if for_agent is not None:
        _valid_name("for", for_agent)
    if not body or not body.strip():
        raise BusError("body is empty")
    if not allow_secret_like:
        hit = secret_like(body)
        if hit:
            raise BusError(
                f"body looks like it contains a secret ({hit}); secrets never go on the bus. "
                "If this is a false positive, pass --allow-secret-like."
            )
    pinned = [pin_ref(spec) for spec in refs or ()]
    ch = _channel_dir(root, channel, create=True)
    msgs_dir, tmp_dir = ch / "messages", ch / "tmp"
    record = {
        "seq": 0, "id": "", "ts": "",
        "channel": channel, "agent": agent, "harness": harness,
        "kind": kind, "body": body,
    }
    if for_agent:
        record["for"] = for_agent
    if pinned:
        record["refs"] = pinned
    record.update(_extra or {})
    with _channel_lock(ch):
        cfg = channel_config(ch)
        # Persist the initial default once so changing env cannot lift a live cap.
        if not (ch / "config.json").exists():
            _write_json(ch, ch / "config.json", cfg)
        if _closed(ch):
            raise BusError(f"channel {channel} is closed")
        state = _turn_state(ch, cfg=cfg)
        _record_cap(ch, state)
        if state["capped"]:
            raise TurnCapError(f"turn cap {state['cap']} reached on {channel}; "
                               "a moderator must close or raise it")
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
            break
        else:
            raise BusError("could not claim a message id after 10000 attempts")
        _record_cap(ch, _turn_state(ch, cfg=cfg))
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
    if for_agent:
        result["notified"] = _notify(root, ch, record)
    return result


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


# --- single active reader ------------------------------------------------------
#
# <channel>/watchers/<agent>.lock   flock held by the one reader that acks for <agent>;
#                                   the kernel drops it when that process dies
# <channel>/watchers/<agent>.json   {pid, host, ts} of the holder, for status and errors
# <channel>/watchers/<agent>.stop   a --force reader asking the holder to let go

READER_GRACE = 1.0   # seconds an acking read waits out a brief holder before refusing
TAKEOVER_WAIT = 5.0  # seconds --force waits for the holder to see .stop and let go


def _watch_path(ch: Path, agent: str, suffix: str) -> Path:
    return ch / "watchers" / f"{agent}.{suffix}"


def watch_holder(ch: Path, agent: str) -> dict | None:
    """{pid, host, ts} of the reader holding agent's lock right now, or None."""
    lock = _watch_path(ch, agent, "lock")
    if fcntl is None or not lock.exists():
        return None
    fd = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return None
    except BlockingIOError:
        return _read_json(_watch_path(ch, agent, "json")) or {"pid": None, "host": None, "ts": None}
    finally:
        os.close(fd)


def _take_reader(ch: Path, channel: str, agent: str, force: bool) -> int | None:
    """Become agent's one acking reader. Returns the fd whose close lets go."""
    if fcntl is None:
        return None
    lock, stop = _watch_path(ch, agent, "lock"), _watch_path(ch, agent, "stop")
    lock.parent.mkdir(exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + (TAKEOVER_WAIT if force else READER_GRACE)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if force and not stop.exists():
                _write_json(ch, stop, {"pid": os.getpid(), "host": HOST, "ts": _now_iso()})
            if time.monotonic() < deadline:
                time.sleep(0.02)
                continue
            os.close(fd)
            holder = _read_json(_watch_path(ch, agent, "json")) or {}
            if force:
                stop.unlink(missing_ok=True)
                raise BusError(f"the reader of {channel!r} as {agent!r} (pid {holder.get('pid')}) "
                               f"did not let go within {TAKEOVER_WAIT:g}s; stop that process")
            raise BusError(
                f"another reader is watching {channel!r} as {agent!r} (pid {holder.get('pid')} on "
                f"{holder.get('host')}, since {holder.get('ts')}); a second reader would silently "
                "ack its messages. Stop that watch, read with --peek, or pass --force to take over.")
    stop.unlink(missing_ok=True)  # ours now; an old request must not stop us
    _write_json(ch, _watch_path(ch, agent, "json"),
                {"agent": agent, "pid": os.getpid(), "host": HOST, "ts": _now_iso()})
    return fd


def _drop_reader(ch: Path, agent: str, fd: int | None) -> None:
    if fd is not None:
        _watch_path(ch, agent, "json").unlink(missing_ok=True)  # still ours: we hold the lock
        os.close(fd)


def recv(root: Path, channel: str, agent: str, wait: bool = False,
         timeout: float = 60.0, peek: bool = False, include_own: bool = False,
         after: int | None = None, poll: float = POLL_SECONDS,
         kinds: list[str] | None = None, force: bool = False) -> list[dict]:
    agent = _valid_name("agent", agent)
    ch = _channel_dir(root, channel, create=True)
    wanted = set(kinds or ())
    if wanted - set(KINDS):
        raise BusError(f"kind must be one of {KINDS}, got {sorted(wanted - set(KINDS))}")
    if after is not None or wanted:
        peek = True  # explicit ranges and kind views never move the cursor
    reader = None if peek else _take_reader(ch, channel, agent, force)
    deadline = time.monotonic() + max(timeout, 0.0)
    try:
        while True:
            _touch_heartbeat(ch, agent)  # "I am polling": watch-wake leaves me alone
            if reader is not None and _watch_path(ch, agent, "stop").exists():
                raise BusError(f"the watch on {channel!r} as {agent!r} was taken over by another "
                               "reader (--force); this one stopped without acking")
            start = _read_cursor(ch, agent) if after is None else after
            batch = _read_after(ch, start)
            visible = [m for m in batch
                       if (include_own or m["agent"] != agent)
                       and (not wanted or m["kind"] in wanted)]
            if batch and not peek:
                _write_cursor(ch, agent, batch[-1]["seq"])
            if visible or not wait or time.monotonic() >= deadline:
                return visible
            time.sleep(poll)
    finally:
        _drop_reader(ch, agent, reader)


def post_done(root: Path, channel: str, agent: str, harness: str,
              verdict: str, summary: str, keep_wake: bool = False,
              phase: str | None = None) -> dict:
    verdict = (verdict or "").upper()
    if verdict not in VERDICTS:
        raise BusError(f"verdict must be one of {VERDICTS}")
    if not summary or not summary.strip():
        raise BusError("summary is required: one line on what was (or was not) accomplished")
    if phase is not None:
        return _post_phase(root, channel, agent, harness, _valid_name("phase", phase),
                           verdict, summary)
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
    # Snapshot and publish under one lock, so the last writer always saw every
    # done record and every participant that posted before it.
    with _channel_lock(ch):
        st = status(root, channel)
        if st["complete"] and not _closed(ch):
            tmp = ch / "tmp" / f"verdict-{uuid.uuid4().hex}"
            tmp.write_text(json.dumps({
                "channel": channel, "complete": True, "verdict": st["verdict"],
                "participants": st["participants"], "ts": _now_iso(),
            }, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, ch / "VERDICT.json")
    return {"done": record, "complete": st["complete"], "verdict": st["verdict"],
            "wake_cleared": wake_cleared}


def _post_phase(root: Path, channel: str, agent: str, harness: str, phase: str,
                verdict: str, summary: str) -> dict:
    """A verdict on one phase: posted and recorded, the channel stays open."""
    send(root, channel, agent, harness, f"PHASE {phase} {verdict}: {summary}", kind="phase",
         _extra={"phase": phase, "verdict": verdict})
    ch = _channel_dir(root, channel)
    (ch / "phases" / phase).mkdir(parents=True, exist_ok=True)
    record = {"agent": agent, "harness": harness, "phase": phase, "verdict": verdict,
              "summary": summary, "ts": _now_iso()}
    _write_json(ch, ch / "phases" / phase / f"{agent}.json", record)
    st = status(root, channel)
    return {"phase": record, "phase_complete": st["phases"][phase]["complete"],
            "phase_verdict": st["phases"][phase]["verdict"], "complete": st["complete"]}


def _liveness(ch: Path, agent: str, last_ts: str | None, silent_after: float) -> dict:
    heartbeat = _heartbeat_age(ch, agent)
    holder = watch_holder(ch, agent)
    ages = [age for age in (heartbeat, _age_of(last_ts)) if age is not None]
    out = {"heartbeat_age_s": None if heartbeat is None else round(heartbeat, 1),
           "watching": holder is not None,
           "watch_pid": holder.get("pid") if holder else None,
           "silent": holder is None and (not ages or min(ages) > silent_after)}
    spawned = _read_json(ch / "spawn" / f"{agent}.json")
    if spawned:
        out["spawn_pid"] = spawned.get("pid")
        out["spawn_alive"] = _spawn_alive(spawned)
    return out


def _phases(ch: Path, signers: list[dict]) -> dict:
    base = ch / "phases"
    out = {}
    for phase_dir in sorted(base.iterdir()) if base.is_dir() else ():
        verdicts = {}
        for path in sorted(phase_dir.glob("*.json")):
            rec = _read_json(path)
            if rec:
                verdicts[rec["agent"]] = {k: rec[k] for k in ("verdict", "summary", "ts")}
        complete = bool(signers) and all(p["agent"] in verdicts for p in signers)
        verdict = None
        if complete:
            verdict = "PASS" if all(v["verdict"] == "PASS" for v in verdicts.values()) else "FAIL"
        out[phase_dir.name] = {"verdicts": verdicts, "complete": complete, "verdict": verdict}
    return out


def status(root: Path, channel: str, silent_after: float = DEFAULT_SILENT) -> dict:
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
    for p in participants:
        p["human"] = p["harness"] == HUMAN_HARNESS
        p.update(_liveness(ch, p["agent"], p["last_ts"], silent_after))
    signers = [p for p in participants if not p["human"]]
    complete = bool(signers) and all(p["done"] for p in signers)
    verdict = None
    if complete:
        verdict = ("PASS" if all(p["done"]["verdict"] == "PASS" for p in participants if p["done"])
                   else "FAIL")
    limits = _turn_state(ch, msgs)
    closed = _closed(ch)
    if closed:
        complete, verdict = True, closed["verdict"]
    elif limits["capped"]:
        complete, verdict = False, None
    # Polled, watched, or spawned, but never posted: shown, never a signatory.
    seen = set()
    if (ch / "heartbeats").is_dir():
        seen |= {p.name for p in (ch / "heartbeats").iterdir()}
    for sub in ("watchers", "spawn"):
        if (ch / sub).is_dir():
            seen |= {p.stem for p in (ch / sub).glob("*.json")}
    readers = [dict(agent=name, **_liveness(ch, name, None, silent_after))
               for name in sorted(seen - set(agents))]
    notify_dir = ch / "notify"
    return {
        "channel": channel,
        "root": str(root),
        "message_count": len(msgs),
        **limits,
        "last_seq": msgs[-1]["seq"] if msgs else 0,
        "participants": participants,
        "readers": readers,
        "complete": complete,
        "verdict": verdict,
        "phases": _phases(ch, signers),
        "wakes": [w["agent"] for w in list_wakes(root, channel)],
        "notify": sorted(p.stem for p in notify_dir.glob("*.json")) if notify_dir.is_dir() else [],
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


def _check_hook(ch: Path, what: str, command: str, allow_secret_like: bool) -> None:
    if not command or not command.strip():
        raise BusError(f"{what} command is empty")
    if not allow_secret_like:
        hit = secret_like(command)
        if hit:
            raise BusError(
                f"{what} command looks like it contains a secret ({hit}); put credentials in the "
                "harness's own config, not in the command. False positive: --allow-secret-like.")
    # The command is the operator's, never a peer's: refuse anything that
    # already appears in a message body on this channel.
    needle = command.strip()
    for m in _read_after(ch, 0):
        if needle in m["body"]:
            raise BusError(
                f"{what} command appears verbatim in message {m['id']}; {what} commands are "
                "registered by the operator and never taken from a message body")


def register_wake(root: Path, channel: str, agent: str, harness: str, command: str | list[str],
                  allow_secret_like: bool = False) -> dict:
    agent = _valid_name("agent", agent)
    harness = _valid_name("harness", harness)
    ch = _channel_dir(root, channel, create=True)
    if isinstance(command, list):
        if not command or not all(isinstance(a, str) for a in command) or not command[0]:
            raise BusError("wake argv must include an executable and string arguments")
        _check_hook(ch, "wake", shlex.join(command), allow_secret_like)
    else:
        _check_hook(ch, "wake", command, allow_secret_like)
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
    with _channel_lock(ch):
        if _turn_state(ch)["capped"] or _closed(ch):
            return {"channel": channel, "agent": agent, "seq": seq, "forced": force,
                    "woke": False, "reason": "channel capped or closed"}, None
        return _launch_wake_locked(root, channel, agent, seq, force, cooldown, idle, source)


def _launch_wake_locked(root: Path, channel: str, agent: str, seq: int, force: bool,
                        cooldown: float, idle: float, source: str
                        ) -> tuple[dict, subprocess.Popen | None]:
    ch = _channel_dir(root, channel)
    result = {"channel": channel, "agent": agent, "seq": seq, "forced": force,
              "woke": False, "reason": ""}
    # Check and reserve under one lock: a send --wake racing watch-wake launches once.
    with _flock(_wake_dir(ch) / f"{agent}.lock"):
        reg = get_wake(root, channel, agent)
        if reg is None:
            result["reason"] = "no wake registration"
            return result, None
        now = time.time()
        # A registration that never woke has no seq to repeat, even seq 0 on an empty channel.
        if float(reg.get("last_wake_epoch", 0.0)) and seq <= int(reg.get("last_wake_seq", 0)):
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
    env.pop(WAKE_COMMAND_ENV, None)
    env.pop(WAKE_ARGV_ENV, None)
    if isinstance(reg["command"], list):
        env[WAKE_ARGV_ENV] = json.dumps(reg["command"])
    else:
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
    command = (json.loads(os.environ[WAKE_ARGV_ENV]) if WAKE_ARGV_ENV in os.environ
               else os.environ.get(WAKE_COMMAND_ENV))
    if not command:
        _wake_log(ch, {"event": "exit", "agent": agent, "seq": seq, "forced": forced,
                       "source": source, "pid": os.getpid(), "exit_code": None,
                       "error": f"{WAKE_COMMAND_ENV} not set"})
        return 1
    # The resumed harness speaks as the woken agent, whoever launched the wake.
    env = dict(os.environ, AGENT_BUS_DIR=str(root), AGENT_BUS_CHANNEL=channel,
               AGENT_BUS_AGENT=agent, AGENT_BUS_HARNESS=harness)
    env.pop(WAKE_COMMAND_ENV, None)
    env.pop(WAKE_ARGV_ENV, None)
    proc = subprocess.run(command, shell=isinstance(command, str), env=env, stdin=subprocess.DEVNULL)
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
        if _turn_state(ch)["capped"] or _closed(ch):
            continue
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
    if pid <= 0:
        return False  # os.kill(0, 0) would probe our own process group
    for proc in _DETACHED:
        proc.poll()  # reap our own exited children: a zombie still answers kill(pid, 0)
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


# --- notify hooks (a person, or anyone, addressed with --for) -----------------
#
# <channel>/notify/<agent>.json   registration: harness, command
# <channel>/notify/log.jsonl      one "launch" line per forwarded message
# <channel>/notify/<agent>.out    stdout+stderr of the hook


def _notify_dir(ch: Path) -> Path:
    d = ch / "notify"
    d.mkdir(exist_ok=True)
    return d


def register_notify(root: Path, channel: str, agent: str, harness: str, command: str,
                    allow_secret_like: bool = False) -> dict:
    agent = _valid_name("agent", agent)
    harness = _valid_name("harness", harness)
    ch = _channel_dir(root, channel, create=True)
    _check_hook(ch, "notify", command, allow_secret_like)
    record = {"agent": agent, "harness": harness, "command": command,
              "registered_ts": _now_iso()}
    _write_json(ch, _notify_dir(ch) / f"{agent}.json", record)
    return record


def clear_notify(root: Path, channel: str, agent: str) -> bool:
    ch = _channel_dir(root, channel)
    try:
        (_notify_dir(ch) / f"{_valid_name('agent', agent)}.json").unlink()
        return True
    except FileNotFoundError:
        return False


def _notify(root: Path, ch: Path, record: dict) -> dict:
    """Run the addressee's notify hook, detached, with the message in its env."""
    target = record["for"]
    reg = _read_json(_notify_dir(ch) / f"{target}.json")
    if not reg or not reg.get("command"):
        return {"agent": target, "notified": False, "reason": "no notify hook registered"}
    env = dict(os.environ, AGENT_BUS_DIR=str(root), AGENT_BUS_CHANNEL=record["channel"],
               AGENT_BUS_AGENT=target, AGENT_BUS_HARNESS=reg["harness"],
               AGENT_BUS_MSG_SEQ=str(record["seq"]), AGENT_BUS_MSG_ID=record["id"],
               AGENT_BUS_MSG_FROM=record["agent"], AGENT_BUS_MSG_KIND=record["kind"],
               AGENT_BUS_MSG_BODY=record["body"])
    try:  # the message is already on the bus: a failed hook must not look like a failed send
        with (_notify_dir(ch) / f"{target}.out").open("ab") as out:
            proc = subprocess.Popen(reg["command"], shell=True, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    except (OSError, ValueError) as err:
        return {"agent": target, "notified": False, "reason": f"notify hook did not start: {err}"}
    _detach(proc)
    with (_notify_dir(ch) / "log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _now_iso(), "event": "launch", "agent": target,
                             "seq": record["seq"], "from": record["agent"],
                             "pid": proc.pid}) + "\n")
    return {"agent": target, "notified": True, "pid": proc.pid}


# --- spawn ---------------------------------------------------------------------
#
# <channel>/spawn/<agent>.json   pid, argv, cwd, session id, derived wake command
# <channel>/spawn/<agent>.log    stdout+stderr of the spawned harness


def _proc_started(pid: int) -> str | None:
    """The process's start time as ps reports it: with the pid, it names one process."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _spawn_alive(record: dict) -> bool:
    """The spawned process is still running, and its pid was not reused since."""
    pid = int(record.get("pid") or 0)
    if not _pid_alive(pid):
        return False
    return not record.get("started") or _proc_started(pid) == record["started"]


def _harness_kind(name: str) -> str | None:
    base = Path(name).name.lower()
    return next((kind for kind in RESUME_FLAGS if base.startswith(kind)), None)


def _carry_flags(table: dict, args: list[str]) -> list[str]:
    out, i = [], 0
    while i < len(args) and args[i] != "--":  # after `--` everything is the prompt
        takes = table.get(args[i].split("=", 1)[0])
        if takes is None:
            i += 1
            continue
        span = 1 if "=" in args[i] else 1 + takes
        out.extend(args[i:i + span])
        i += span
    return out


def resume_command(kind: str, session_id: str, channel: str, cwd: str,
                   argv: list[str] | None = None, prompt: str | None = None) -> str:
    """Shell command that resumes a harness session headless and points it at the channel."""
    argv = argv or [kind]
    prompt = prompt or f"read agent-bus channel {channel} and continue"
    flags = _carry_flags(RESUME_FLAGS[kind], argv[1:])
    if kind == "codex":
        cmd = [argv[0], "exec", "resume", *flags, session_id, prompt]
    else:
        cmd = [argv[0], "--resume", session_id, *flags, "-p", prompt]
    return f"cd {shlex.quote(cwd)} && {shlex.join(cmd)}"


def _await_session_id(log: Path, offset: int, proc: subprocess.Popen,
                      timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while True:
        exited = proc.poll() is not None  # checked first so an exit still gets one last read
        with log.open("rb") as fh:
            fh.seek(offset)
            match = SESSION_ID_RE.search(fh.read().decode("utf-8", "replace"))
        if match:
            return match.group(1)
        if exited or time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def spawn(root: Path, channel: str, agent: str, harness: str | None, argv: list[str],
          cwd: str | None = None, id_timeout: float = 30.0, wake_command: str | None = None,
          wake_prompt: str | None = None, register: bool = True,
          allow_secret_like: bool = False) -> dict:
    """Start a peer harness in its own session (it outlives this shell), learn its
    session id, and register the matching resume command as its wake."""
    agent = _valid_name("agent", agent)
    if not argv:
        raise BusError("spawn needs the harness command after --")
    kind = _harness_kind(argv[0]) or _harness_kind(harness or "")
    harness = _valid_name("harness", harness or DEFAULT_HARNESS.get(kind) or Path(argv[0]).name)
    if not allow_secret_like:
        hit = secret_like(" ".join(argv))
        if hit:
            raise BusError(f"spawn command looks like it contains a secret ({hit}); put "
                           "credentials in the harness's own config. False positive: "
                           "--allow-secret-like.")
    ch = _channel_dir(root, channel, create=True)
    spawn_dir = ch / "spawn"
    spawn_dir.mkdir(exist_ok=True)
    # Held through launch and id capture, so two spawns of one agent cannot both start.
    with _flock(spawn_dir / f"{agent}.lock", blocking=False) as mine:
        if not mine:
            raise BusError(f"another spawn of {agent!r} on {channel!r} is in progress")
        return _spawn_locked(root, ch, channel, agent, harness, kind, argv, cwd, id_timeout,
                             wake_command, wake_prompt, register, allow_secret_like)


def _spawn_locked(root: Path, ch: Path, channel: str, agent: str, harness: str,
                  kind: str | None, argv: list[str], cwd: str | None, id_timeout: float,
                  wake_command: str | None, wake_prompt: str | None, register: bool,
                  allow_secret_like: bool) -> dict:
    spawn_dir = ch / "spawn"
    previous = _read_json(spawn_dir / f"{agent}.json")
    if previous and _spawn_alive(previous):
        raise BusError(f"{agent!r} is already running on {channel!r} (pid {previous['pid']}); "
                       "stop it before spawning another")
    workdir = Path(cwd or os.getcwd()).expanduser().resolve()
    if not workdir.is_dir():
        raise BusError(f"cwd {workdir} is not a directory")
    argv = list(argv)
    session_id = None
    if kind == "claude":  # claude takes its session id instead of printing one
        if "--session-id" in argv[:-1]:
            session_id = argv[argv.index("--session-id") + 1]
        else:
            session_id = str(uuid.uuid4())
            argv[1:1] = ["--session-id", session_id]
    log = spawn_dir / f"{agent}.log"
    offset = log.stat().st_size if log.exists() else 0
    env = dict(os.environ, AGENT_BUS_DIR=str(Path(root).resolve()), AGENT_BUS_CHANNEL=channel,
               AGENT_BUS_AGENT=agent, AGENT_BUS_HARNESS=harness)
    try:
        with log.open("ab") as out:
            proc = subprocess.Popen(argv, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
    except OSError as err:
        raise BusError(f"could not start {argv[0]!r}: {err}") from err
    _detach(proc)
    started = _proc_started(proc.pid)  # now, while the pid is surely still ours
    if kind == "codex":
        session_id = _await_session_id(log, offset, proc, id_timeout)
    record = {"agent": agent, "harness": harness, "pid": proc.pid, "argv": argv,
              "cwd": str(workdir), "log": str(log), "session_id": session_id,
              "started_ts": _now_iso(), "started": started, "wake_command": None}
    extra = {}
    if not register:
        extra["wake_reason"] = "--no-wake"
    elif wake_command is None and not (kind and session_id):
        extra["wake_reason"] = (f"no session id in its output within {id_timeout:g}s"
                                if kind else "unknown harness: pass --wake COMMAND")
    else:
        command = wake_command or resume_command(kind, session_id, channel, str(workdir),
                                                 argv, wake_prompt)
        try:
            register_wake(root, channel, agent, harness, command, allow_secret_like)
            record["wake_command"] = command
        except BusError as err:
            extra["wake_reason"] = str(err)
    if record["wake_command"] is None and previous:
        # The last spawn's wake resumes a session this process is not; drop it
        # (an operator's own `join --wake` is a different command and stays).
        current = get_wake(root, channel, agent)
        if current and current.get("command") == previous.get("wake_command"):
            extra["wake_cleared"] = clear_wake(root, channel, agent)
    _write_json(ch, spawn_dir / f"{agent}.json", record)
    exit_code = proc.poll()
    return dict(record, **extra, wake_registered=record["wake_command"] is not None,
                running=exit_code is None, exit_code=exit_code)


def _identity(args: argparse.Namespace, cfg: dict,
              need_harness: bool = True) -> tuple[str, str | None]:
    agent = args.agent or os.environ.get("AGENT_BUS_AGENT") or cfg.get("agent")
    harness = args.harness or os.environ.get("AGENT_BUS_HARNESS") or cfg.get("harness")
    if not agent:
        raise BusError(f"identity required: pass --as, set AGENT_BUS_AGENT, "
                       f"or put agent = \"...\" in {CONFIG_NAME}")
    if need_harness and not harness:
        raise BusError(f"identity required: pass --harness, set AGENT_BUS_HARNESS, "
                       f"or put harness = \"...\" in {CONFIG_NAME}")
    return agent, harness


def _exit_on_signal(signum, _frame):
    sys.exit(128 + signum)  # unwinds recv's finally, which drops the watch lock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bus", description="agent-bus: a local mailbox two live coding-agent sessions poll")
    sub = parser.add_subparsers(dest="command", required=True)

    # Identity, channel and root: flag > env > .agent-bus.toml (cwd or a parent) > default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help=f"bus root (env AGENT_BUS_DIR, default {DEFAULT_ROOT})")
    common.add_argument("-c", "--channel",
                        help=f"channel name (env AGENT_BUS_CHANNEL, default {DEFAULT_CHANNEL})")
    common.add_argument("-a", "--agent", "--as", dest="agent",
                        help="who you are (env AGENT_BUS_AGENT)")
    common.add_argument("--harness", help="which harness runs you (env AGENT_BUS_HARNESS; "
                                          f"'{HUMAN_HARNESS}' for a person)")

    p = sub.add_parser("send", parents=[common], help="post one message to the channel")
    p.add_argument("--body", required=True, help="message text; '-' reads stdin")
    p.add_argument("--kind", default="message", choices=SEND_KINDS,
                   help="what the message is (default %(default)s)")
    p.add_argument("--for", dest="for_agent", metavar="AGENT",
                   help="address it to one participant; their notify hook (join --notify) fires")
    p.add_argument("--ref", action="append", default=[], metavar="PATH[@SHA256]",
                   help="pin a file by content hash (repeatable); verify-refs <seq> reports drift")
    p.add_argument("--json", action="store_true", help="print the whole record, not 'seq id'")
    p.add_argument("--allow-secret-like", action="store_true",
                   help="override the secret tripwire for a false positive")
    p.add_argument("--wake", action="store_true",
                   help="run every registered peer's wake command now, heartbeat or not")

    read_opts = argparse.ArgumentParser(add_help=False)
    read_opts.add_argument("--kind", action="append", choices=KINDS,
                           help="only this kind (repeatable); a filtered read never moves the cursor")
    read_opts.add_argument("--body-only", action="store_true",
                           help="print just the bodies of the new messages")
    read_opts.add_argument("--force", action="store_true",
                           help="read even while another watch holds this agent's lock (takes it over)")

    p = sub.add_parser("recv", parents=[common, read_opts],
                       help="new messages from others; advances your cursor")
    p.add_argument("--wait", action="store_true", help="block until a message arrives or --timeout")
    p.add_argument("--timeout", type=float, default=60.0, help="seconds to wait (default %(default)s)")
    p.add_argument("--peek", action="store_true", help="read without advancing the cursor")
    p.add_argument("--include-own", action="store_true", help="also show your own messages")
    p.add_argument("--after", type=int,
                   help="read from this seq instead of your cursor (implies --peek; 0 = everything)")

    p = sub.add_parser("watch", parents=[common, read_opts],
                       help="recv --wait with a long default timeout")
    p.add_argument("--timeout", type=float, default=300.0, help="seconds to wait (default %(default)s)")

    p = sub.add_parser("done", parents=[common],
                       help="post your verdict; when every participant has, VERDICT.json appears")
    p.add_argument("--verdict", required=True,
                   choices=list(VERDICTS) + [v.lower() for v in VERDICTS])
    p.add_argument("--summary", required=True,
                   help="one line: what was (or was not) accomplished")
    p.add_argument("--phase", help="a verdict on this phase only; the channel stays open")
    p.add_argument("--keep-wake", action="store_true",
                   help="leave your wake registration in place after done")

    p = sub.add_parser("config", parents=[common], help="set or show the channel turn budget")
    p.add_argument("--max-turns", type=int)
    p.add_argument("--moderator", help="named agent allowed to close or change the budget")

    p = sub.add_parser("close", parents=[common], help="moderator: close the channel permanently")
    p.add_argument("--verdict", required=True, choices=VERDICTS)
    p.add_argument("--summary", required=True)

    p = sub.add_parser("raise-cap", parents=[common], help="moderator: add to the turn budget")
    p.add_argument("--by", type=int, required=True)

    p = sub.add_parser("status", parents=[common],
                       help="participants, liveness, verdicts, complete flag for the channel")
    p.add_argument("--silent-after", type=float, default=DEFAULT_SILENT / 60,
                   help="minutes with no poll, send, or watch before a peer counts as silent "
                        "(default %(default)g)")
    sub.add_parser("channels", parents=[common], help="list channels under the root")

    p = sub.add_parser("verify-refs", parents=[common],
                       help="which files a message pinned with --ref changed since; exit 1 if any")
    p.add_argument("seq", type=int)

    p = sub.add_parser("join", parents=[common],
                       help="register the shell command that resumes YOUR session on this channel")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--wake", metavar="COMMAND",
                   help='e.g. \'cd ~/work && claude --resume <id> -p "read agent-bus channel X and continue"\'')
    g.add_argument("--notify", metavar="COMMAND",
                   help="run this when a message --for you lands (e.g. a Telegram forwarder)")
    g.add_argument("--clear", action="store_true", help="remove your wake and notify registrations")
    p.add_argument("--allow-secret-like", action="store_true",
                   help="override the secret tripwire for a false positive")

    p = sub.add_parser("wake", parents=[common],
                       help="run a registered peer's wake command now (cooldown still applies)")
    p.add_argument("peer", help="agent to wake")

    p = sub.add_parser("spawn", help="start a peer detached; register its resume as its wake")
    p.add_argument("--root", help=f"bus root (env AGENT_BUS_DIR, default {DEFAULT_ROOT})")
    p.add_argument("-c", "--channel", help="channel name (env AGENT_BUS_CHANNEL)")
    p.add_argument("--agent", required=True, help="the peer's name on the bus")
    p.add_argument("--harness", help="the peer's harness (default from the binary: "
                                     "codex -> codex-cli, claude -> claude-code)")
    p.add_argument("--cwd", help="the peer's working directory (default: here)")
    p.add_argument("--id-timeout", type=float, default=30.0,
                   help="seconds to wait for codex to print its session id (default %(default)s)")
    p.add_argument("--wake", metavar="COMMAND", help="register this instead of the derived resume")
    p.add_argument("--wake-prompt", help="prompt of the derived resume "
                                         "(default: read agent-bus channel <channel> and continue)")
    p.add_argument("--no-wake", action="store_true", help="register no wake")
    p.add_argument("--allow-secret-like", action="store_true",
                   help="override the secret tripwire for a false positive")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="-- then the harness command")

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
    try:
        cfg = load_config()
        root = resolve_root(args.root or os.environ.get("AGENT_BUS_DIR")
                            or os.environ.get("AGENT_BUS_ROOT") or cfg.get("root"))
        args.channel = (args.channel or os.environ.get("AGENT_BUS_CHANNEL")
                        or cfg.get("channel") or DEFAULT_CHANNEL)
        if args.command == "send":
            body = sys.stdin.read() if args.body == "-" else args.body
            agent, harness = _identity(args, cfg)
            result = send(root, args.channel, agent, harness, body, kind=args.kind,
                          allow_secret_like=args.allow_secret_like, wake=args.wake,
                          for_agent=args.for_agent, refs=args.ref)
            if not args.json:
                print(f"{result['seq']} {result['id']}")
                for woken in result.get("woken", []):
                    print(f"wake {woken['agent']}: {woken['reason']}", file=sys.stderr)
                notified = result.get("notified")
                if notified and not notified["notified"]:
                    print(f"notify {notified['agent']}: {notified['reason']}", file=sys.stderr)
                return 0
        elif args.command in ("recv", "watch"):
            agent, _ = _identity(args, cfg, need_harness=False)
            wait = (args.command == "watch") or args.wait
            if wait:  # SIGHUP from a dying parent shell must still drop the watch lock
                for signum in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
                    if signum is not None:
                        signal.signal(signum, _exit_on_signal)
            result = recv(root, args.channel, agent, wait=wait, timeout=args.timeout,
                          peek=getattr(args, "peek", False),
                          include_own=getattr(args, "include_own", False),
                          after=getattr(args, "after", None),
                          kinds=args.kind, force=args.force)
            if args.body_only:
                for m in result:
                    sys.stdout.write(m["body"] if m["body"].endswith("\n") else m["body"] + "\n")
                return 0
        elif args.command == "done":
            agent, harness = _identity(args, cfg)
            result = post_done(root, args.channel, agent, harness, args.verdict, args.summary,
                               keep_wake=args.keep_wake, phase=args.phase)
        elif args.command == "config":
            agent = args.agent or os.environ.get("AGENT_BUS_AGENT") or cfg.get("agent")
            harness = args.harness or os.environ.get("AGENT_BUS_HARNESS") or cfg.get("harness")
            result = configure_channel(root, args.channel, args.max_turns, args.moderator,
                                       agent, harness)
        elif args.command == "close":
            agent, harness = _identity(args, cfg)
            result = close_channel(root, args.channel, agent, harness, args.verdict, args.summary)
        elif args.command == "raise-cap":
            agent, harness = _identity(args, cfg)
            result = raise_cap(root, args.channel, agent, harness, args.by)
        elif args.command == "status":
            result = status(root, args.channel, silent_after=args.silent_after * 60)
        elif args.command == "channels":
            result = list_channels(root)
        elif args.command == "verify-refs":
            result = verify_refs(root, args.channel, args.seq)
            print(json.dumps(result, indent=2))
            return 0 if all(r["status"] == "unchanged" for r in result) else 1
        elif args.command == "join":
            if args.clear:
                agent, _ = _identity(args, cfg, need_harness=False)
                result = {"agent": agent, "channel": args.channel,
                          "cleared": clear_wake(root, args.channel, agent),
                          "notify_cleared": clear_notify(root, args.channel, agent)}
            elif args.notify:
                agent, harness = _identity(args, cfg)
                result = register_notify(root, args.channel, agent, harness, args.notify,
                                         allow_secret_like=args.allow_secret_like)
            else:
                agent, harness = _identity(args, cfg)
                result = register_wake(root, args.channel, agent, harness, args.wake,
                                       allow_secret_like=args.allow_secret_like)
        elif args.command == "wake":
            result = wake_agent(root, args.channel, args.peer, source="wake")
        elif args.command == "spawn":
            cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
            result = spawn(root, args.channel, args.agent, args.harness, cmd, cwd=args.cwd,
                           id_timeout=args.id_timeout, wake_command=args.wake,
                           wake_prompt=args.wake_prompt, register=not args.no_wake,
                           allow_secret_like=args.allow_secret_like)
        elif args.command == "watch-wake":
            return watch_wake(root, cooldown=args.cooldown, idle=args.idle,
                              poll=args.poll, once=args.once)
        else:  # _supervise
            agent, harness = _identity(args, cfg)
            return _supervise(root, args.channel, agent, harness, args.seq,
                              args.source, args.forced)
    except BusError as err:
        print(f"error: {err}", file=sys.stderr)
        return 4 if isinstance(err, TurnCapError) else 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
