"""run_forever.py — keep running cycles on a schedule. Paper mode: never trades.

Start it once and leave it running:

    python3 run_forever.py

It runs one cycle immediately, then repeats every CYCLE_INTERVAL_HOURS (config.py).
Press Ctrl+C to stop.

CAVEAT: this keeps going only while this Terminal window stays open and the Mac
stays awake. If you close the lid or the machine sleeps, it pauses until you wake
it. For truly hands-off 24/7 (survives reboot, or runs even when your computer is
off), ask about the launchd or cloud-server setup — that's the next step up.
"""

import time
from datetime import datetime, timedelta, timezone

import config
import run_cycle


def main() -> None:
    interval = config.CYCLE_INTERVAL_HOURS * 3600
    print(f"Polytrade runner started — one cycle now, then every "
          f"{config.CYCLE_INTERVAL_HOURS}h. Press Ctrl+C to stop.\n")
    while True:
        try:
            run_cycle.main()
        except KeyboardInterrupt:
            print("\nStopped by user.")
            return
        except Exception as exc:
            print(f"cycle error (will retry next interval): {exc}")

        nxt = datetime.now(timezone.utc) + timedelta(seconds=interval)
        print(f"\nNext cycle ~ {nxt.isoformat()}  "
              f"(sleeping {config.CYCLE_INTERVAL_HOURS}h; Ctrl+C to stop)")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            return


if __name__ == "__main__":
    main()
