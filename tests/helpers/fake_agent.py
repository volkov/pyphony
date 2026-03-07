#!/usr/bin/env python3
"""Fake agent subprocess for testing. Reads JSON-RPC from stdin, writes responses to stdout."""

import json
import sys


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")

        if method == "initialize":
            print(json.dumps({
                "id": msg["id"],
                "result": {"serverInfo": {"name": "fake"}},
            }))
        elif method == "initialized":
            # notification, no response
            pass
        elif method == "thread/start":
            print(json.dumps({
                "id": msg["id"],
                "result": {"thread": {"id": "test-thread-1"}},
            }))
        elif method == "turn/start":
            print(json.dumps({
                "id": msg["id"],
                "result": {"turn": {"id": "test-turn-1"}},
            }))
            # Immediately send turn/completed
            print(json.dumps({"method": "turn/completed", "params": {}}))

        sys.stdout.flush()


if __name__ == "__main__":
    main()
