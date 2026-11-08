"""Transform: turn the raw API payload into clean, typed DataFrames.

The API returns every value as a string, uses a decimal comma ("1,799") and encodes
missing prices as "". Prices come as one column per fuel ("Precio Gasoleo A", ...);
we unpivot them into a long table (one row per station, fuel and day).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, datetime

import pandas as pd

log = logging.getLogger(__name__)

PRICE_PREFIX = "Precio "
MIN_PRICE, MAX_PRICE = 0.0, 50.0  # EUR per litre/kg; outside this range is a data error

STATION_COLUMNS = {
    "IDEESS": "station_id",
    "Rótulo": "brand",
    "Dirección": "address",
    "C.P.": "postal_code",
    "Localidad": "locality",
    "IDMunicipio": "municipality_id",
    "Municipio": "municipality",
    "IDProvincia": "province_id",
    "Provincia": "province",
    "IDCCAA": "ccaa_id",
    "Latitud": "latitude",
    "Longitud (WGS84)": "longitude",
    "Margen": "margin",
    "Tipo Venta": "sale_type",
    "Horario": "schedule",
}


class TransformError(ValueError):
    """The payload does not have the expected shape."""


def parse_decimal(values: pd.Series) -> pd.Series:
    """'1,799' -> 1.799; '' / whitespace / garbage -> NaN. Returns float64."""
    cleaned = values.astype("string").str.strip().str.replace(",", ".", regex=False)
    return pd.to_numeric(cleaned.replace("", pd.NA), errors="coerce").astype("float64")


def clean_text(values: pd.Series) -> pd.Series:
    """Trim and collapse internal whitespace; empty strings become NA."""
    cleaned = values.astype("string").str.strip().str.replace(r"\s+", " ", regex=True)
    return cleaned.replace("", pd.NA)


def fuel_code(column: str) -> str:
    """Column name -> ASCII snake_case code: 'Precio Diésel Renovable' -> 'diesel_renovable'."""
    name = column.removeprefix(PRICE_PREFIX)
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")


def snapshot_date(payload: dict) -> date:
    """Day the snapshot refers to, taken from the API 'Fecha' field ('08/11/2026 0:51:41')."""
    try:
        return datetime.strptime(payload["Fecha"].split()[0], "%d/%m/%Y").date()
    except (KeyError, ValueError, AttributeError) as exc:
        raise TransformError(f"Invalid Fecha: {payload.get('Fecha')!r}") from exc


def to_frame(payload: dict) -> pd.DataFrame:
    df = pd.DataFrame(payload["ListaEESSPrecio"], dtype="string")
    missing = set(STATION_COLUMNS) - set(df.columns)
    if missing:
        raise TransformError(f"Missing columns in payload: {sorted(missing)}")
    dupes = df["IDEESS"].duplicated(keep="last")
    if dupes.any():
        log.warning("Dropping %d duplicated station rows", int(dupes.sum()))
        df = df[~dupes]
    return df


def build_stations(df: pd.DataFrame) -> pd.DataFrame:
    out = df[list(STATION_COLUMNS)].rename(columns=STATION_COLUMNS)
    for col in ("brand", "address", "locality", "municipality", "province", "schedule"):
        out[col] = clean_text(out[col])
    for col in ("postal_code", "province_id", "ccaa_id", "margin", "sale_type"):
        out[col] = out[col].str.strip()  # keep as text: leading zeros matter ('02250')
    out["station_id"] = pd.to_numeric(out["station_id"], errors="raise").astype("int64")
    out["municipality_id"] = pd.to_numeric(out["municipality_id"], errors="raise").astype("int64")
    out["latitude"] = parse_decimal(out["latitude"])
    out["longitude"] = parse_decimal(out["longitude"])
    return out.reset_index(drop=True)


def build_fuels(df: pd.DataFrame) -> pd.DataFrame:
    columns = [c for c in df.columns if c.startswith(PRICE_PREFIX)]
    return pd.DataFrame(
        {
            "fuel_code": [fuel_code(c) for c in columns],
            "fuel_name": [c.removeprefix(PRICE_PREFIX) for c in columns],
        }
    )


def build_prices(df: pd.DataFrame, day: date) -> pd.DataFrame:
    """Wide -> long. Rows without a price are dropped (the station does not sell that fuel)."""
    price_cols = [c for c in df.columns if c.startswith(PRICE_PREFIX)]
    wide = df[["IDEESS", *price_cols]].copy()
    for col in price_cols:
        wide[col] = parse_decimal(wide[col])
    long = wide.melt(id_vars="IDEESS", var_name="column", value_name="price").dropna(
        subset=["price"]
    )
    out_of_range = ~long["price"].between(MIN_PRICE, MAX_PRICE, inclusive="neither")
    if out_of_range.any():
        log.warning(
            "Dropping %d prices outside (%s, %s)", int(out_of_range.sum()), MIN_PRICE, MAX_PRICE
        )
        long = long[~out_of_range]
    return pd.DataFrame(
        {
            "price_date": day,
            "station_id": long["IDEESS"].astype("int64"),
            "fuel_code": long["column"].map(fuel_code),
            "price": long["price"].round(3),
        }
    ).reset_index(drop=True)


def transform(payload: dict) -> tuple[date, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return (day, stations, fuels, prices) ready to load."""
    day = snapshot_date(payload)
    df = to_frame(payload)
    stations, fuels, prices = build_stations(df), build_fuels(df), build_prices(df, day)
    log.info(
        "Transformed %s: %d stations, %d fuels, %d prices",
        day,
        len(stations),
        len(fuels),
        len(prices),
    )
    return day, stations, fuels, prices
