import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from pipeline import transform as t

FIXTURE = Path(__file__).parent / "fixtures" / "sample.json"


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --- decimal cleaning -------------------------------------------------------


def test_parse_decimal_converts_comma_and_blanks():
    result = t.parse_decimal(pd.Series(["1,799", "", "  2,1 ", None, "abc", "-1,539167"]))
    assert result.dtype == "float64"
    assert result[0] == pytest.approx(1.799)
    assert result[2] == pytest.approx(2.1)
    assert result[5] == pytest.approx(-1.539167)
    assert result[[1, 3, 4]].isna().all()


def test_clean_text_trims_and_nulls_empty():
    result = t.clean_text(pd.Series(["ALEGRIA-DULANTZI         ", "  a   b ", ""]))
    assert result.tolist()[:2] == ["ALEGRIA-DULANTZI", "a b"]
    assert pd.isna(result[2])


@pytest.mark.parametrize(
    ("column", "code"),
    [
        ("Precio Gasoleo A", "gasoleo_a"),
        ("Precio Gasolina 95 E5", "gasolina_95_e5"),
        ("Precio Diésel Renovable", "diesel_renovable"),
        ("Precio Gases licuados del petróleo", "gases_licuados_del_petroleo"),
    ],
)
def test_fuel_code_is_ascii_snake_case(column, code):
    assert t.fuel_code(column) == code


def test_snapshot_date_uses_api_fecha(payload):
    assert t.snapshot_date(payload) == date(2026, 11, 7)
    assert t.snapshot_date({"Fecha": "08/11/2026 0:51:41"}) == date(2026, 11, 8)
    with pytest.raises(t.TransformError):
        t.snapshot_date({"Fecha": "garbage"})


# --- stations ---------------------------------------------------------------


def test_stations_types_and_leading_zeros(payload):
    stations = t.build_stations(t.to_frame(payload))
    assert len(stations) == 5
    assert stations["station_id"].dtype == "int64"
    assert stations["municipality_id"].dtype == "int64"
    assert stations["latitude"].dtype == "float64"
    assert stations["latitude"].between(27, 44).all()  # Canarias .. Cantábrico
    assert stations["postal_code"].str.len().eq(5).all()
    assert stations["province_id"].str.len().eq(2).all()  # '01', not 1


def test_missing_required_column_raises(payload):
    for station in payload["ListaEESSPrecio"]:
        del station["IDEESS"]
    with pytest.raises(t.TransformError, match="IDEESS"):
        t.to_frame(payload)


def test_duplicated_station_keeps_last(payload):
    first = dict(payload["ListaEESSPrecio"][0])
    dup = dict(first, **{"Precio Gasoleo A": "1,000"})
    payload["ListaEESSPrecio"] = [first, dup]
    prices = t.build_prices(t.to_frame(payload), date(2026, 11, 7))
    gasoleo = prices[prices["fuel_code"] == "gasoleo_a"]
    assert gasoleo["price"].tolist() == [1.0]


# --- prices -----------------------------------------------------------------


def test_prices_long_format_drops_nulls(payload):
    day = date(2026, 11, 7)
    prices = t.build_prices(t.to_frame(payload), day)
    assert set(prices.columns) == {"price_date", "station_id", "fuel_code", "price"}
    assert prices["price"].notna().all()
    assert (prices["price_date"] == day).all()
    assert not prices.duplicated(["station_id", "fuel_code"]).any()  # matches the fact PK
    # station 15952 has no Gasoleo A in the fixture -> no row for it
    no_diesel = prices[(prices["station_id"] == 15952) & (prices["fuel_code"] == "gasoleo_a")]
    assert no_diesel.empty
    row = prices[(prices["station_id"] == 7013) & (prices["fuel_code"] == "gasoleo_a")]
    assert row["price"].item() == pytest.approx(1.799)


def test_out_of_range_prices_are_dropped(payload):
    station = payload["ListaEESSPrecio"][0]
    station["Precio Gasoleo A"] = "0,000"
    station["Precio Gasolina 95 E5"] = "999,9"
    prices = t.build_prices(t.to_frame(payload), date(2026, 11, 7))
    mine = prices[prices["station_id"] == int(station["IDEESS"])]
    assert not mine["fuel_code"].isin(["gasoleo_a", "gasolina_95_e5"]).any()


def test_transform_end_to_end(payload):
    day, stations, fuels, prices = t.transform(payload)
    assert day == date(2026, 11, 7)
    assert len(fuels) == 23
    assert set(prices["fuel_code"]) <= set(fuels["fuel_code"])
    assert set(prices["station_id"]) <= set(stations["station_id"])
