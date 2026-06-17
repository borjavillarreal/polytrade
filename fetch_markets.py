"""fetch_markets.py — pull qualifying active markets into the markets table.

Filters (all configurable in config.py):
  * active and not closed/archived
  * total USD volume >= MIN_VOLUME_USD  (liquidity proxy)
  * resolves between MIN_DAYS_TO_RESOLUTION and MAX_DAYS_TO_RESOLUTION days out

Stores per market: market_id, question, current target ("Yes") price, volume,
resolution date, and the fetch timestamp.

Read-only. No trades, no wallet, no credentials.
"""

from datetime import datetime, timedelta, timezone

import config
import polymarket
import record


def _parse_iso(dt: str):
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except ValueError:
        return None


def main() -> None:
    now = datetime.now(timezone.utc)
    fetch_timestamp = now.isoformat()
    earliest = now + timedelta(days=config.MIN_DAYS_TO_RESOLUTION)
    latest = now + timedelta(days=config.MAX_DAYS_TO_RESOLUTION)

    conn = record.connect()
    record.init_db(conn)

    scanned = 0
    qualifying = 0
    skipped_volume = skipped_window = skipped_unusable = 0

    try:
        for raw in polymarket.iter_active_markets(limit=config.GAMMA_PAGE_LIMIT):
            scanned += 1

            m = polymarket.normalize_market(raw)
            if m is None or not m["enable_order_book"]:
                skipped_unusable += 1
                continue

            if m["volume"] < config.MIN_VOLUME_USD:
                # markets are volume-sorted desc; once we're below the floor on a
                # full page we can stop, but a single low row may be noise — count
                # and continue rather than break to stay robust to ordering.
                skipped_volume += 1
                continue

            res_dt = _parse_iso(m["resolution_date"])
            if res_dt is None or not (earliest <= res_dt <= latest):
                skipped_window += 1
                continue

            record.upsert_market(conn, m, fetch_timestamp)
            qualifying += 1
            if qualifying >= config.MAX_MARKETS_PER_FETCH:
                break

        conn.commit()
        open_preds = record.open_prediction_count(conn)
        unpredicted = len(record.markets_without_predictions(conn))
    finally:
        conn.close()

    print("=" * 60)
    print("fetch_markets.py summary")
    print("=" * 60)
    print(f"  fetch timestamp        : {fetch_timestamp}")
    print(f"  resolution window      : {config.MIN_DAYS_TO_RESOLUTION}-"
          f"{config.MAX_DAYS_TO_RESOLUTION} days  "
          f"({earliest.date()} .. {latest.date()})")
    print(f"  min volume (USD)       : {config.MIN_VOLUME_USD:,.0f}")
    print(f"  markets scanned        : {scanned}")
    print(f"  qualifying & stored    : {qualifying}")
    print(f"    skipped (volume)     : {skipped_volume}")
    print(f"    skipped (window)     : {skipped_window}")
    print(f"    skipped (unusable)   : {skipped_unusable}")
    print(f"  awaiting analysis      : {unpredicted}")
    print(f"  open predictions       : {open_preds}")


if __name__ == "__main__":
    main()
