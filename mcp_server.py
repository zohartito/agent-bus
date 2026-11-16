#!/usr/bin/env python3
"""agent-bus MCP server: newline-delimited JSON-RPC 2.0 over stdio, stdlib only.

Identity comes from this server's env config (AGENT_BUS_AGENT and
AGENT_BUS_HARNESS), never from the model: whoever configures the server
decides who it speaks as. Root and default channel come from AGENT_BUS_DIR
and AGENT_BUS_CHANNEL.
"""

from __future__ import annotations

import json
import os
import sys
from types import MappingProxyType

import bus

SERVER_INFO = {"name": "agent-bus", "version": "0.1.0"}
# Fixed at server startup by the operator, never populated from tool arguments.
def _load_wake_hooks() -> dict:
    raw = json.loads(os.environ.get("AGENT_BUS_WAKE_HOOKS", "{}"))
    if not isinstance(raw, dict):
        raise bus.BusError("AGENT_BUS_WAKE_HOOKS must map hook IDs to argv arrays")
    hooks = {}
    for name, argv in raw.items():
        bus._valid_name("hook ID", name)
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv) or not argv[0]:
            raise bus.BusError("wake hooks must be nonempty argv arrays with an executable")
        hooks[name] = tuple(argv)
    return MappingProxyType(hooks)


WAKE_HOOKS = _load_wake_hooks()
DEFAULT_TIMEOUT = 25.0  # keep MCP waits short; poll again instead

TOOLS = [
    {
        "name": "bus_send",
        "description": "Post one message to the shared agent-bus channel. "
                       "Peers see it on their next bus_recv. Refused at the channel turn cap; "
                       "a moderator must close or raise it. Never put secrets on the bus.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "body": {"type": "string", "description": "Message text."},
                "channel": {"type": "string",
                            "description": "Channel name (default from AGENT_BUS_CHANNEL or 'main')."},
                "wake": {"type": "boolean",
                         "description": "Also run every registered peer's wake command now, "
                                        "whether or not the peer is polling (cooldown still applies)."},
                "kind": {"type": "string", "enum": list(bus.SEND_KINDS),
                         "description": "What the message is (default 'message')."},
                "for": {"type": "string",
                        "description": "Address it to one participant; their notify hook "
                                       "(e.g. a person's Telegram forwarder) fires."},
                "refs": {"type": "array", "items": {"type": "string"},
                         "description": "Files to pin by content hash: 'path' or 'path@sha256'. "
                                        "Use absolute paths. bus_verify_refs reports drift later."},
            },
            "required": ["body"],
        },
    },
    {
        "name": "bus_recv",
        "description": "Fetch new messages from peers on the channel. Your own messages are "
                       "hidden and delivery is de-duped; each call advances your cursor unless peek.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "wait": {"type": "boolean",
                         "description": "Block until a message arrives or timeout_s elapses."},
                "timeout_s": {"type": "number",
                              "description": "Wait limit in seconds (default 25). Keep short; call again."},
                "peek": {"type": "boolean", "description": "Read without advancing the cursor."},
                "include_own": {"type": "boolean", "description": "Also return your own messages."},
                "after": {"type": "integer",
                          "description": "Read from this seq instead of your cursor (implies peek; 0 = everything)."},
                "kind": {"type": "array", "items": {"type": "string", "enum": list(bus.KINDS)},
                         "description": "Only these kinds. A filtered read never moves your cursor."},
                "force": {"type": "boolean",
                          "description": "Read even while another watch holds your lock (takes it over)."},
            },
        },
    },
    {
        "name": "bus_done",
        "description": "Post your final verdict for the channel. When every participant has posted "
                       "one, VERDICT.json appears at the channel root and status.complete turns true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
                "summary": {"type": "string",
                            "description": "One line: what was (or was not) accomplished."},
                "channel": {"type": "string"},
                "keep_wake": {"type": "boolean",
                              "description": "Leave your wake registration in place (default: done clears it)."},
                "phase": {"type": "string",
                          "description": "Record a verdict for this phase only; the channel stays open."},
            },
            "required": ["verdict", "summary"],
        },
    },
    {
        "name": "bus_status",
        "description": "Channel status: participants, liveness (heartbeat age, live watch, silent, "
                       "spawned process alive), per-agent and per-phase verdicts, complete flag, "
                       "turns_used, cap and capped.",
        "inputSchema": {"type": "object", "properties": {
            "channel": {"type": "string"},
            "silent_after_min": {"type": "number",
                                 "description": "Minutes without a poll, send, or watch before a "
                                                "participant counts as silent (default 10)."}}},
    },
    {
        "name": "bus_channels",
        "description": "List channels under the bus root.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "bus_register_wake",
        "description": "Register an operator-approved wake hook for YOUR OWN session. "
                       "Only immutable hook IDs configured when the server starts are accepted; "
                       "commands and arguments cannot be supplied by the model.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "hook_id": {"type": "string",
                            **({"enum": list(WAKE_HOOKS)} if WAKE_HOOKS else {}),
                            "description": "Operator-configured hook ID."},
                "channel": {"type": "string"},
                "clear": {"type": "boolean", "description": "Remove your registration instead."},
            },
        },
    },
    {
        "name": "bus_wake",
        "description": "Run a registered peer's wake command now, whether or not the peer is polling. "
                       "Refused if the peer has no registration; the cooldown still applies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "The peer to wake."},
                "channel": {"type": "string"},
            },
            "required": ["agent"],
        },
    },
    {
        "name": "bus_verify_refs",
        "description": "For a message sent with refs, report each pinned file as unchanged, "
                       "changed, or missing since it was sent.",
        "inputSchema": {
            "type": "object",
            "properties": {"seq": {"type": "integer"}, "channel": {"type": "string"}},
            "required": ["seq"],
        },
    },
]


def _identity() -> tuple[str, str]:
    agent = os.environ.get("AGENT_BUS_AGENT")
    harness = os.environ.get("AGENT_BUS_HARNESS")
    if not agent or not harness:
        raise bus.BusError(
            "identity is not configured: set AGENT_BUS_AGENT and AGENT_BUS_HARNESS "
            "in this MCP server's env config")
    return agent, harness


def _channel(arguments: dict) -> str:
    return arguments.get("channel") or os.environ.get("AGENT_BUS_CHANNEL") or bus.DEFAULT_CHANNEL


def call_tool(name: str, arguments: dict):
    root = bus.resolve_root()
    if name == "bus_send":
        agent, harness = _identity()
        kind = arguments.get("kind") or "message"
        if kind not in bus.SEND_KINDS:  # done and phase go through bus_done
            raise bus.BusError(f"kind must be one of {bus.SEND_KINDS}")
        return bus.send(root, _channel(arguments), agent, harness, arguments.get("body", ""),
                        kind=kind, wake=bool(arguments.get("wake", False)),
                        for_agent=arguments.get("for") or None, refs=arguments.get("refs") or None)
    if name == "bus_recv":
        agent, _ = _identity()
        after = arguments.get("after")
        kinds = arguments.get("kind")
        return bus.recv(root, _channel(arguments), agent,
                        wait=bool(arguments.get("wait", False)),
                        timeout=float(arguments.get("timeout_s", DEFAULT_TIMEOUT)),
                        peek=bool(arguments.get("peek", False)),
                        include_own=bool(arguments.get("include_own", False)),
                        after=None if after is None else int(after),
                        kinds=[kinds] if isinstance(kinds, str) else kinds,
                        force=bool(arguments.get("force", False)))
    if name == "bus_done":
        agent, harness = _identity()
        return bus.post_done(root, _channel(arguments), agent, harness,
                             arguments.get("verdict", ""), arguments.get("summary", ""),
                             keep_wake=bool(arguments.get("keep_wake", False)),
                             phase=arguments.get("phase"))
    if name == "bus_status":
        return bus.status(root, _channel(arguments),
                          silent_after=float(arguments.get("silent_after_min", 10)) * 60)
    if name == "bus_channels":
        return bus.list_channels(root)
    if name == "bus_register_wake":
        if set(arguments) - {"hook_id", "channel", "clear"}:
            raise bus.BusError("wake registration accepts only hook_id, channel and clear")
        agent, harness = _identity()
        channel = _channel(arguments)
        if arguments.get("clear"):
            return {"agent": agent, "channel": channel,
                    "cleared": bus.clear_wake(root, channel, agent)}
        hook_id = arguments.get("hook_id")
        if not isinstance(hook_id, str) or hook_id not in WAKE_HOOKS:
            raise bus.BusError("unknown wake hook ID; configure AGENT_BUS_WAKE_HOOKS before server startup")
        return bus.register_wake(root, channel, agent, harness, list(WAKE_HOOKS[hook_id]))
    if name == "bus_wake":
        return bus.wake_agent(root, _channel(arguments), arguments.get("agent", ""), source="bus_wake")
    if name == "bus_verify_refs":
        return bus.verify_refs(root, _channel(arguments), int(arguments.get("seq", 0)))
    raise bus.BusError(f"unknown tool {name!r}")


def _reply(msg_id, result=None, error=None) -> None:
    payload = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def handle(msg: dict) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")
    if method is None or msg_id is None:
        return  # client responses and notifications need no reply
    if method == "initialize":
        client_version = (msg.get("params") or {}).get("protocolVersion", "2024-11-05")
        _reply(msg_id, {"protocolVersion": client_version,
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO})
    elif method == "ping":
        _reply(msg_id, {})
    elif method == "tools/list":
        _reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = msg.get("params") or {}
        try:
            result = call_tool(params.get("name", ""), params.get("arguments") or {})
            _reply(msg_id, {"content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                            "isError": False})
        except bus.BusError as err:
            _reply(msg_id, {"content": [{"type": "text", "text": f"error: {err}"}],
                            "isError": True})
    else:
        _reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print("agent-bus: dropped an unparseable line", file=sys.stderr)
            continue
        try:
            handle(msg)
        except Exception as err:  # keep serving; surface the failure to the caller
            if isinstance(msg, dict) and msg.get("id") is not None:
                _reply(msg["id"], error={"code": -32603, "message": f"internal error: {err}"})
            else:
                print(f"agent-bus: internal error: {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
