"""Load: write one transformed snapshot into PostgreSQL in a single transaction.

Strategy: COPY each DataFrame into a temporary staging table (fast bulk insert), then
merge into the star schema with INSERT ... ON CONFLICT (upsert). Re-running the same
day therefore updates rows instead of duplicating them.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import psycopg

log = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "sql" / "schema.sql"


def connect(conninfo: str) -> psycopg.Connection:
    return psycopg.connect(conninfo, connect_timeout=10)


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.commit()


def _copy(cur: psycopg.Cursor, table: str, df: pd.DataFrame) -> None:
    cols = ", ".join(df.columns)
    with cur.copy(f"COPY {table} ({cols}) FROM STDIN") as copy:
        for row in df.astype(object).where(df.notna(), None).itertuples(index=False):
            copy.write_row(row)


def load_snapshot(
    conn: psycopg.Connection,
    day: date,
    stations: pd.DataFrame,
    fuels: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    source: str,
    started_at: datetime,
) -> int:
    """Upsert one day. Returns the number of fact rows for that day after the load."""
    date_key = int(f"{day:%Y%m%d}")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dim_date VALUES (
                %(k)s, %(d)s, EXTRACT(YEAR FROM %(d)s), EXTRACT(QUARTER FROM %(d)s),
                EXTRACT(MONTH FROM %(d)s), EXTRACT(DAY FROM %(d)s), EXTRACT(WEEK FROM %(d)s),
                EXTRACT(ISODOW FROM %(d)s), EXTRACT(ISODOW FROM %(d)s) IN (6, 7))
            ON CONFLICT (date_key) DO NOTHING
            """,
            {"k": date_key, "d": day},
        )

        cur.execute("CREATE TEMP TABLE stg_fuel (fuel_code text, fuel_name text) ON COMMIT DROP")
        _copy(cur, "stg_fuel", fuels)
        cur.execute(
            """
            INSERT INTO dim_fuel (fuel_code, fuel_name)
            SELECT fuel_code, fuel_name FROM stg_fuel
            ON CONFLICT (fuel_code) DO UPDATE SET fuel_name = EXCLUDED.fuel_name
            """
        )

        cur.execute(
            "CREATE TEMP TABLE stg_station (LIKE dim_station INCLUDING DEFAULTS) ON COMMIT DROP"
        )
        stg = stations.assign(first_seen=day, last_seen=day)
        _copy(cur, "stg_station", stg)
        cols = list(stg.columns)
        attrs = [c for c in cols if c not in ("station_id", "first_seen")]
        # Backfills may load older days after newer ones: attributes are only overwritten
        # when the incoming snapshot is at least as recent as the stored one (WHERE below).
        cur.execute(
            f"""
            INSERT INTO dim_station AS d ({", ".join(cols)})
            SELECT {", ".join(cols)} FROM stg_station
            ON CONFLICT (station_id) DO UPDATE SET
                {", ".join(f"{c} = EXCLUDED.{c}" for c in attrs)}, updated_at = now()
            WHERE EXCLUDED.last_seen >= d.last_seen
            """
        )
        cur.execute(
            """
            UPDATE dim_station d SET first_seen = s.first_seen
            FROM stg_station s
            WHERE d.station_id = s.station_id AND s.first_seen < d.first_seen
            """
        )

        cur.execute(
            "CREATE TEMP TABLE stg_price (station_id integer, fuel_code text,"
            " price numeric(6,3)) ON COMMIT DROP"
        )
        _copy(cur, "stg_price", prices[["station_id", "fuel_code", "price"]])
        cur.execute(
            """
            INSERT INTO fact_fuel_price (date_key, station_id, fuel_id, price)
            SELECT %(k)s, s.station_id, f.fuel_id, s.price
            FROM stg_price s JOIN dim_fuel f USING (fuel_code)
            ON CONFLICT (date_key, station_id, fuel_id)
            DO UPDATE SET price = EXCLUDED.price, loaded_at = now()
            """,
            {"k": date_key},
        )
        # The day must reflect the latest snapshot exactly: remove prices that vanished
        # between two runs of the same day (e.g. a station stopped selling a fuel).
        cur.execute(
            """
            DELETE FROM fact_fuel_price p
            WHERE p.date_key = %(k)s AND NOT EXISTS (
                SELECT 1 FROM stg_price s JOIN dim_fuel f USING (fuel_code)
                WHERE s.station_id = p.station_id AND f.fuel_id = p.fuel_id)
            """,
            {"k": date_key},
        )
        removed = cur.rowcount

        cur.execute("SELECT count(*) FROM fact_fuel_price WHERE date_key = %s", (date_key,))
        total = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO etl_run (snapshot_date, source, stations, prices, started_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (day, source, len(stations), total, started_at),
        )
    log.info("Loaded %s: %d fact rows (%d stale removed)", day, total, removed)
    return total
