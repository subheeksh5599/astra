#!/usr/bin/env python3
"""Watch the invariant on a schedule, and journal every pass.

The rail finishes a transfer when someone asks it to. This is the other half: a
pass that runs on its own and writes down what it found, so a transfer that is
able to move and has not moved becomes a record instead of nobody's problem.

It does not spend anything. Execution stays on demand on purpose: a background
process that finishes transfers unattended is the thing this rail exists to make
unnecessary, so the watcher only observes, names, and journals.

    python3 scripts/astra_watch.py --once --blocks 3000
    python3 scripts/astra_watch.py --interval 300 --blocks 3000

Exits non-zero on a pass that finds the invariant broken (--once), so a scheduler
can treat "money is stuck" as a failed job.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra import pairing  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def journal_path(when: float) -> str:
    folder = os.path.join(ROOT, "artifacts", "watch")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(when)) + ".json")


def one_pass(pass_number: int, blocks: int, limit: int, quiet: bool,
             watched_only: bool = False) -> dict:
    started = time.time()
    result = pairing.collect(blocks=blocks, limit=limit, watched_only=watched_only,
                             progress=None if quiet else lambda line: print(f"  {line}", file=sys.stderr))
    broken = pairing.broken(result)
    entry = {
        "pass": pass_number,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "window_blocks": blocks,
        "domains": result["domains"],
        "summary": pairing.summarise(result),
        "broken": broken,
        "stranded": [row for row in result["rows"] if row["verdict"] == "stranded"],
        "seconds": round(time.time() - started, 1),
    }
    path = journal_path(started)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, indent=2)
    print(f"[{entry['at']}] pass {pass_number}: {entry['summary']} in {entry['seconds']}s -> {path}")
    for row in entry["stranded"]:
        print(f"    stranded: {row['transfer_id']} {row['amount_usdc']} USDC to "
              f"{row['destination_name']} — burn {row['burn_tx']}")
    for row in result.get("duplicates") or []:
        print(f"    received twice: {row['transfer_id']} ({row['delivered_count']} mints)")
    return entry


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single pass and exit")
    ap.add_argument("--interval", type=int, default=300, help="seconds between passes")
    ap.add_argument("--blocks", type=int, default=3000)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--passes", type=int, default=0, help="stop after this many passes (0 = forever)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--watched-only", action="store_true",
                    help="report only transfers this rail can act on")
    args = ap.parse_args()

    pass_number = 0
    broken_seen: list = []
    while True:
        pass_number += 1
        entry = one_pass(pass_number, args.blocks, args.limit, args.quiet, args.watched_only)
        if entry["broken"]:
            broken_seen.append(entry)
        if args.once:
            return 1 if entry["broken"] else 0
        if args.passes and pass_number >= args.passes:
            return 1 if broken_seen else 0
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    sys.exit(main())
