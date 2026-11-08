"""Integration test against a real PostgreSQL (skipped unless TEST_DATABASE_URL is set).

CI starts a postgres service container; locally:
    TEST_DATABASE_URL=postgresql://fuel:<pw>@localhost:5432/fuel_test pytest
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline import load, transform

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")

FIXTURE = Path(__file__).parent / "fixtures" / "sample.json"
TABLES = "fact_fuel_price, etl_run, dim_station, dim_fuel, dim_date"


@pytest.fixture
def conn():
    with load.connect(DSN) as c:
        load.init_schema(c)
        c.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        c.commit()
        yield c


def _load(conn, payload):
    day, stations, fuels, prices = transform.transform(payload)
    return load.load_snapshot(
        conn, day, stations, fuels, prices, source="test", started_at=datetime.now(UTC)
    )


def _count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_load_twice_is_idempotent(conn):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    first = _load(conn, payload)
    second = _load(conn, payload)
    assert first == second == _count(conn, "fact_fuel_price") > 0
    assert _count(conn, "dim_station") == 5
    assert _count(conn, "etl_run") == 2


def test_rerun_updates_price_and_removes_vanished_rows(conn):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    _load(conn, payload)
    station = payload["ListaEESSPrecio"][0]
    station["Precio Gasoleo A"] = "1,111"
    station["Precio Gasolina 95 E5"] = ""  # no longer sold
    _load(conn, payload)
    rows = dict(
        conn.execute(
            "SELECT f.fuel_code, p.price FROM fact_fuel_price p JOIN dim_fuel f USING (fuel_id)"
            " WHERE p.station_id = %s",
            (int(station["IDEESS"]),),
        ).fetchall()
    )
    assert float(rows["gasoleo_a"]) == pytest.approx(1.111)
    assert "gasolina_95_e5" not in rows


def test_older_snapshot_does_not_overwrite_station(conn):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    _load(conn, payload)  # 07/11/2026
    older = json.loads(FIXTURE.read_text(encoding="utf-8"))
    older["Fecha"] = "01/11/2026 0:00:00"
    older["ListaEESSPrecio"][0]["Rótulo"] = "OLD BRAND"
    _load(conn, older)
    sid = int(older["ListaEESSPrecio"][0]["IDEESS"])
    brand, first, last = conn.execute(
        "SELECT brand, first_seen, last_seen FROM dim_station WHERE station_id = %s", (sid,)
    ).fetchone()
    assert brand != "OLD BRAND"
    assert (str(first), str(last)) == ("2026-11-01", "2026-11-07")
