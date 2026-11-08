-- Star schema for daily fuel prices. Idempotent: safe to run on every pipeline start.

CREATE TABLE IF NOT EXISTS dim_date (
    date_key     integer  PRIMARY KEY,          -- YYYYMMDD, readable and sortable
    full_date    date     NOT NULL UNIQUE,
    year         smallint NOT NULL,
    quarter      smallint NOT NULL,
    month        smallint NOT NULL,
    day          smallint NOT NULL,
    iso_week     smallint NOT NULL,
    day_of_week  smallint NOT NULL,             -- ISO: 1 = Monday ... 7 = Sunday
    is_weekend   boolean  NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_fuel (
    fuel_id    smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fuel_code  text NOT NULL UNIQUE,            -- natural key, e.g. 'gasoleo_a'
    fuel_name  text NOT NULL                    -- as published, e.g. 'Gasoleo A'
);

-- SCD type 1: one row per station, attributes overwritten with the newest snapshot.
-- Province and municipality are denormalised here (star schema, no snowflake joins).
CREATE TABLE IF NOT EXISTS dim_station (
    station_id       integer PRIMARY KEY,       -- IDEESS from the API
    brand            text,
    address          text,
    postal_code      text,
    locality         text,
    municipality_id  integer NOT NULL,
    municipality     text,
    province_id      text NOT NULL,
    province         text,
    ccaa_id          text NOT NULL,
    latitude         numeric(9, 6),
    longitude        numeric(9, 6),
    margin           text,
    sale_type        text,
    schedule         text,
    first_seen       date NOT NULL,
    last_seen        date NOT NULL,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_station_province ON dim_station (province_id);

-- Grain: one price per station, fuel and day.
CREATE TABLE IF NOT EXISTS fact_fuel_price (
    date_key    integer      NOT NULL REFERENCES dim_date (date_key),
    station_id  integer      NOT NULL REFERENCES dim_station (station_id),
    fuel_id     smallint     NOT NULL REFERENCES dim_fuel (fuel_id),
    price       numeric(6, 3) NOT NULL CHECK (price > 0),
    loaded_at   timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (date_key, station_id, fuel_id)
);

CREATE INDEX IF NOT EXISTS ix_fact_fuel_date ON fact_fuel_price (fuel_id, date_key);

-- Operational audit: one row per loaded snapshot.
CREATE TABLE IF NOT EXISTS etl_run (
    run_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_date  date        NOT NULL,
    source         text        NOT NULL,        -- 'current' | 'historical'
    stations       integer     NOT NULL,
    prices         integer     NOT NULL,
    started_at     timestamptz NOT NULL,
    finished_at    timestamptz NOT NULL DEFAULT now()
);
