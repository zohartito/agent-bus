#!/usr/bin/env python3
"""agent-bus notify hook: forward a message sent `--for` a person to Telegram.

Register once per channel, as the person (the command is the operator's, never a peer's):

    python3 ~/projects/agent-bus/bus.py join -c case-r14 --as zohar --harness human \\
        --notify "python3 ~/projects/agent-bus/examples/notify-telegram.py"

The bus runs it detached with the message in AGENT_BUS_MSG_* env vars. The text
ends with the exact command that posts the answer back as a normal message.
Credentials come from TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in
~/.config/printer-telegram/telegram.env (override with AGENT_BUS_TELEGRAM_ENV);
they never touch the bus and are never printed. --dry-run prints instead of sending.
"""

from __future__ import annotations

import os
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BUS = Path(__file__).resolve().parent.parent / "bus.py"
ENV_PATH = Path(os.environ.get("AGENT_BUS_TELEGRAM_ENV",
                               "~/.config/printer-telegram/telegram.env")).expanduser()


def load_creds() -> dict:
    creds = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            creds[key.strip()] = value.strip().strip('"').strip("'")
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not creds.get(k)]
    if missing:
        raise SystemExit(f"{ENV_PATH} is missing {missing}")
    return creds


def message_text(env) -> str:
    answer = shlex.join(["python3", str(BUS), "send", "--root", env["AGENT_BUS_DIR"],
                         "-c", env["AGENT_BUS_CHANNEL"], "--as", env["AGENT_BUS_AGENT"],
                         "--harness", env.get("AGENT_BUS_HARNESS", "human"),
                         "--kind", "answer", "--body"])
    return (f"agent-bus {env['AGENT_BUS_CHANNEL']} #{env['AGENT_BUS_MSG_SEQ']} "
            f"{env['AGENT_BUS_MSG_KIND']} from {env['AGENT_BUS_MSG_FROM']}:\n\n"
            f"{env['AGENT_BUS_MSG_BODY']}\n\nAnswer with:\n{answer} \"...\"")


def main() -> int:
    text = message_text(os.environ)
    if "--dry-run" in sys.argv[1:]:
        print(text)
        return 0
    creds = load_creds()
    data = urllib.parse.urlencode({"chat_id": creds["TELEGRAM_CHAT_ID"],
                                   "text": text[:4000]}).encode()
    url = f"https://api.telegram.org/bot{creds['TELEGRAM_BOT_TOKEN']}/sendMessage"
    try:
        urllib.request.urlopen(url, data=data, timeout=20).read()
    except urllib.error.HTTPError as err:  # report the code only: the URL holds the token
        print(f"telegram send failed: HTTP {err.code}", file=sys.stderr)
        return 1
    except urllib.error.URLError as err:
        print(f"telegram send failed: {err.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
