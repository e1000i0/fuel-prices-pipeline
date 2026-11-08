"""Generate the README charts from the loaded data.

python scripts/make_charts.py   ->  docs/images/national_trend.png, andalucia_provinces.png
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from pipeline.config import load_settings  # noqa: E402
from pipeline.load import connect  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "docs" / "images"

# Validated categorical slots 1-2 (light surface) + recessive neutrals.
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = {"Gasoleo A": "#2a78d6", "Gasolina 95 E5": "#eb6834"}

TREND_SQL = """
WITH daily AS (
    SELECT d.full_date, f.fuel_name, avg(p.price) AS avg_price
    FROM fact_fuel_price p
    JOIN dim_fuel f USING (fuel_id) JOIN dim_date d USING (date_key)
    WHERE f.fuel_code IN ('gasoleo_a', 'gasolina_95_e5')
    GROUP BY d.full_date, f.fuel_name
)
SELECT full_date, fuel_name, avg_price,
       avg(avg_price) OVER (PARTITION BY fuel_name ORDER BY full_date
                            RANGE BETWEEN INTERVAL '6 days' PRECEDING AND CURRENT ROW) AS ma7
FROM daily ORDER BY full_date
"""

PROVINCE_SQL = """
SELECT s.province, avg(p.price) AS avg_price, count(*) AS stations
FROM fact_fuel_price p
JOIN dim_fuel f USING (fuel_id) JOIN dim_station s USING (station_id)
WHERE f.fuel_code = 'gasoleo_a' AND s.ccaa_id = '01'
  AND p.date_key = (SELECT max(date_key) FROM fact_fuel_price)
GROUP BY s.province ORDER BY avg_price
"""


def query(sql: str) -> pd.DataFrame:
    with connect(load_settings().pg_conninfo) as conn, conn.cursor() as cur:
        cur.execute(sql)
        cols = [c.name for c in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def national_trend() -> Path:
    df = query(TREND_SQL)
    df[["avg_price", "ma7"]] = df[["avg_price", "ma7"]].astype(float)
    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=150, facecolor=SURFACE)
    style(ax)
    ax.grid(axis="x", visible=False)
    for fuel, g in df.groupby("fuel_name"):
        color = SERIES[fuel]
        ax.plot(g["full_date"], g["avg_price"], color=color, linewidth=1, alpha=0.35)
        ax.plot(g["full_date"], g["ma7"], color=color, linewidth=2, label=f"{fuel} (7-day avg)")
        last = g.iloc[-1]
        ax.annotate(
            f"{fuel}  {last['ma7']:.3f} €",
            (last["full_date"], last["ma7"]),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            fontsize=9,
            color=INK,
        )
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.set_ylabel("€/litre", color=INK_2, fontsize=9)
    ax.set_title(
        "Spain — national average pump price (thin: daily, bold: 7-day moving average)",
        loc="left",
        fontsize=11,
        color=INK,
    )
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="lower left")
    ax.margins(x=0.02)
    fig.subplots_adjust(right=0.80)
    return save(fig, "national_trend.png")


def andalucia_provinces() -> Path:
    df = query(PROVINCE_SQL)
    df["avg_price"] = df["avg_price"].astype(float)
    df = df.iloc[::-1]  # cheapest on top
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=150, facecolor=SURFACE)
    style(ax)
    # Dot plot, not bars: differences are cents, and a bar must start at zero.
    names = df["province"].str.title()
    ax.scatter(
        df["avg_price"],
        names,
        s=70,
        color=SERIES["Gasoleo A"],
        edgecolors=SURFACE,
        linewidths=2,
        zorder=3,
    )
    span = df["avg_price"].max() - df["avg_price"].min()
    ax.set_xlim(df["avg_price"].min() - span * 0.3, df["avg_price"].max() + span * 0.9)
    for y, (price, n) in enumerate(zip(df["avg_price"], df["stations"], strict=True)):
        ax.annotate(
            f"{price:.3f} €  ({n} stations)",
            (price, y),
            xytext=(9, 0),
            textcoords="offset points",
            va="center",
            fontsize=8.5,
            color=INK_2,
        )
    ax.set_xlabel("€/litre", color=INK_2, fontsize=9)
    ax.set_title(
        "Gasóleo A — average price by Andalusian province, latest day",
        loc="left",
        fontsize=11,
        color=INK,
    )
    fig.tight_layout()
    return save(fig, "andalucia_provinces.png")


def save(fig, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    fig.savefig(path, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {path}")
    return path


if __name__ == "__main__":
    national_trend()
    andalucia_provinces()
