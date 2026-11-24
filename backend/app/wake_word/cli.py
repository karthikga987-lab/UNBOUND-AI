"""Foreground wake listener diagnostics; does not start automatically."""
from __future__ import annotations

import argparse
import time

from backend.app.wake_word.service import OpenWakeWordService, WakeWordUnavailable


def main() -> int:
    parser = argparse.ArgumentParser(description="UNBOUND local Hey UNBOUND detector diagnostics")
    parser.add_argument("command", choices=("validate", "run"))
    args = parser.parse_args()
    service = OpenWakeWordService()
    try:
        if args.command == "validate":
            print(service.validate())
            return 0
        print(service.start()["detail"])
        print("Foreground wake detection is active. Press Ctrl+C to stop.")
        last_event_id = 0
        while True:
            event = service.event_after(last_event_id)
            if event:
                last_event_id = int(event["event_id"])
                print("Hey UNBOUND detected locally; microphone has been released.")
                return 0
            current = service.status()
            if current["status"] in {"error", "unavailable"}:
                print(current["detail"])
                return 1
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("Stopping wake listener…")
        return 0
    except WakeWordUnavailable as exc:
        print(f"Wake word unavailable: {exc}")
        return 2
    finally:
        service.stop()


if __name__ == "__main__":
    raise SystemExit(main())
