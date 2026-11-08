"""Command line entry point.

    python -m pipeline run                 # today's snapshot (current endpoint)
    python -m pipeline backfill --days 30  # the previous N days (historical endpoint)
    python -m pipeline init-db             # create tables only

Exit code is 0 on success and 1 if any day failed, so a scheduler can alert on it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

from pipeline import extract, load, transform
from pipeline.config import Settings, load_settings
from pipeline.log import setup_logging

log = logging.getLogger("pipeline")


def process_day(settings: Settings, conn, day: date, *, historical: bool, force: bool) -> int:
    started = datetime.now().astimezone()
    payload = extract.extract_day(settings, day, historical=historical, force=force)
    snap_day, stations, fuels, prices = transform.transform(payload)
    if historical and snap_day != day:
        raise transform.TransformError(f"Asked for {day}, API returned {snap_day}")
    return load.load_snapshot(
        conn,
        snap_day,
        stations,
        fuels,
        prices,
        source="historical" if historical else "current",
        started_at=started,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create tables")
    sub.add_parser("run", help="load today's snapshot")
    bf = sub.add_parser("backfill", help="load the previous N days from the historical endpoint")
    bf.add_argument("--days", type=int, default=30)
    bf.add_argument("--force", action="store_true", help="re-download cached raw files")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging(settings.log_level)

    try:
        conn = load.connect(settings.pg_conninfo)
    except Exception as exc:  # noqa: BLE001 - report any connection problem clearly
        log.error(
            "Cannot connect to PostgreSQL at %s:%s (%s). Is `docker compose up -d` running?",
            settings.pg_host,
            settings.pg_port,
            exc,
        )
        return 1

    with conn:
        load.init_schema(conn)
        if args.command == "init-db":
            log.info("Schema ready")
            return 0

        if args.command == "run":
            jobs = [(date.today(), False)]
        else:
            jobs = [(date.today() - timedelta(days=n), True) for n in range(args.days, 0, -1)]

        failed: list[date] = []
        for day, historical in jobs:
            try:
                process_day(
                    settings, conn, day, historical=historical, force=getattr(args, "force", False)
                )
            except Exception as exc:  # noqa: BLE001 - one bad day must not stop the backfill
                log.exception("Day %s failed: %s", day, exc)
                conn.rollback()
                failed.append(day)

    if failed:
        log.error("%d/%d day(s) failed: %s", len(failed), len(jobs), ", ".join(map(str, failed)))
        return 1
    log.info("Done: %d day(s) loaded", len(jobs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
