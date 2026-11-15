#!/usr/bin/env python3
"""Read-only channel counts. Message bodies and references are never printed."""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bus


def scan(root: Path) -> list[dict]:
    rows = []
    for folder in sorted(root.iterdir()) if root.is_dir() else ():
        if not (folder / "messages").is_dir():
            continue
        kinds, agents = Counter(), Counter()
        turns, skipped = 0, 0
        for path in sorted((folder / "messages").glob("*.json")):
            try:
                msg = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(msg, dict) or not all(
                    isinstance(msg.get(key), str) for key in ("kind", "agent", "harness")
                ):
                    raise ValueError("invalid message")
            except (OSError, ValueError):
                skipped += 1
                continue
            kinds[msg["kind"]] += 1
            agents[msg["agent"]] += 1
            turns += msg["kind"] in bus.SEND_KINDS and msg["harness"] != bus.HUMAN_HARNESS
        # Explicitly labelled fixtures remain visible but do not set a real-work budget.
        fixture = bool(re.search(r"(^|[-_.])(test|demo|smoke|proof|race)([-_.]|$)",
                                 folder.name, re.I))
        rows.append({"channel": folder.name, "messages": sum(kinds.values()),
                     "turns": turns, "kinds": dict(sorted(kinds.items())),
                     "agents": dict(sorted(agents.items())), "skipped": skipped,
                     "state": "done" if (folder / "VERDICT.json").is_file() else "open",
                     "fixture": fixture})
    return rows


def suggested_cap(rows: list[dict]) -> int:
    longest = max((r["turns"] for r in rows if not r["fixture"]), default=0)
    return max(10, ((2 * longest + 9) // 10) * 10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="bus root (default: bus env or ~/.agent-bus)")
    args = parser.parse_args()
    rows = scan(bus.resolve_root(args.root))
    print("CHANNEL | STATE | MESSAGES | TURNS | KINDS | AGENTS | FIXTURE | SKIPPED")
    for r in rows:
        counts = lambda values: ",".join(f"{k}:{v}" for k, v in values.items()) or "-"
        print(f"{r['channel']} | {r['state']} | {r['messages']} | {r['turns']} | "
              f"{counts(r['kinds'])} | {counts(r['agents'])} | "
              f"{'yes' if r['fixture'] else 'no'} | {r['skipped']}")
    real = [r for r in rows if not r["fixture"]]
    longest = max(real, key=lambda r: r["turns"], default=None)
    print("Longest real conversation: " + (f"{longest['channel']} ({longest['turns']} turns)"
                                            if longest else "none"))
    print(f"Suggested default cap: {suggested_cap(rows)}")
    print("Fixture heuristic: names containing a test/demo/smoke/proof/race component.")


if __name__ == "__main__":
    main()
