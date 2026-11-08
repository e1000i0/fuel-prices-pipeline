-- =============================================================================
-- Fuel price analysis. Run with:
--   docker exec -i fuel-postgres psql -U fuel -d fuel < sql/analysis.sql
-- "Today" means the most recent day loaded, so results are reproducible.
-- =============================================================================


-- 1. The 10 cheapest stations for Gasóleo A in Huelva on the latest day.
--    DENSE_RANK keeps ties together (several stations often share a price).
WITH latest AS (
    SELECT max(date_key) AS date_key FROM fact_fuel_price
),
huelva_diesel AS (
    SELECT s.brand, s.municipality, s.address, p.price,
           DENSE_RANK() OVER (ORDER BY p.price) AS price_rank
    FROM fact_fuel_price p
    JOIN latest l       USING (date_key)
    JOIN dim_fuel f     USING (fuel_id)
    JOIN dim_station s  USING (station_id)
    WHERE f.fuel_code = 'gasoleo_a'
      AND s.province_id = '21'          -- Huelva (INE code)
)
SELECT price_rank, price, brand, municipality, address
FROM huelva_diesel
ORDER BY price, brand
LIMIT 10;


-- 2. Average price per Andalusian province (CCAA 01) and its ranking,
--    for diesel and 95 petrol, on the latest day. RANK restarts per fuel.
WITH latest AS (
    SELECT max(date_key) AS date_key FROM fact_fuel_price
),
province_avg AS (
    SELECT f.fuel_name, s.province,
           round(avg(p.price), 3) AS avg_price,
           count(*)               AS stations
    FROM fact_fuel_price p
    JOIN latest l      USING (date_key)
    JOIN dim_fuel f    USING (fuel_id)
    JOIN dim_station s USING (station_id)
    WHERE s.ccaa_id = '01'
      AND f.fuel_code IN ('gasoleo_a', 'gasolina_95_e5')
    GROUP BY f.fuel_name, s.province
)
SELECT fuel_name,
       RANK() OVER (PARTITION BY fuel_name ORDER BY avg_price) AS rank_cheapest,
       province, avg_price, stations,
       round(avg_price - avg(avg_price) OVER (PARTITION BY fuel_name), 3) AS diff_vs_region
FROM province_avg
ORDER BY fuel_name, rank_cheapest;


-- 3. Day-over-day change per station (LAG) and 7-day moving average.
--    RANGE over the date (not ROWS) means "the last 7 calendar days": if a day
--    is missing for a station, the window does not silently stretch further back.
--    Shown for Gasóleo A at the 5 Huelva stations with most history.
WITH diesel AS (
    SELECT p.station_id, d.full_date, p.price
    FROM fact_fuel_price p
    JOIN dim_fuel f USING (fuel_id)
    JOIN dim_date d USING (date_key)
    WHERE f.fuel_code = 'gasoleo_a'
),
sample_stations AS (
    SELECT station_id
    FROM diesel JOIN dim_station s USING (station_id)
    WHERE s.province_id = '21'
    GROUP BY station_id
    ORDER BY count(*) DESC, station_id
    LIMIT 5
),
series AS (
    SELECT di.station_id, di.full_date, di.price,
           di.price - LAG(di.price) OVER w_station AS change_vs_prev_day,
           round(avg(di.price) OVER (
               PARTITION BY di.station_id ORDER BY di.full_date
               RANGE BETWEEN INTERVAL '6 days' PRECEDING AND CURRENT ROW), 3) AS moving_avg_7d
    FROM diesel di
    JOIN sample_stations USING (station_id)
    WINDOW w_station AS (PARTITION BY di.station_id ORDER BY di.full_date)
)
SELECT s.brand, s.municipality, se.full_date, se.price, se.change_vs_prev_day, se.moving_avg_7d
FROM series se JOIN dim_station s USING (station_id)
ORDER BY s.brand, se.station_id, se.full_date;


-- 4. National daily average and its 7-day moving average (feeds chart 1).
WITH daily AS (
    SELECT d.full_date, f.fuel_name, avg(p.price) AS avg_price
    FROM fact_fuel_price p
    JOIN dim_fuel f USING (fuel_id)
    JOIN dim_date d USING (date_key)
    WHERE f.fuel_code IN ('gasoleo_a', 'gasolina_95_e5')
    GROUP BY d.full_date, f.fuel_name
)
SELECT full_date, fuel_name, round(avg_price, 3) AS avg_price,
       round(avg(avg_price) OVER (
           PARTITION BY fuel_name ORDER BY full_date
           RANGE BETWEEN INTERVAL '6 days' PRECEDING AND CURRENT ROW), 3) AS moving_avg_7d
FROM daily
ORDER BY fuel_name, full_date;


-- 5. Is fuel cheaper on some weekdays? Uses dim_date.day_of_week.
--    Each day is compared with the average of its own ISO week to remove the trend.
WITH daily AS (
    SELECT d.full_date, date_trunc('week', d.full_date) AS week_start,  -- unique across years
           d.day_of_week, avg(p.price) AS avg_price
    FROM fact_fuel_price p
    JOIN dim_fuel f USING (fuel_id)
    JOIN dim_date d USING (date_key)
    WHERE f.fuel_code = 'gasoleo_a'
    GROUP BY d.full_date, d.day_of_week
),
vs_week AS (
    SELECT day_of_week,
           avg_price - avg(avg_price) OVER (PARTITION BY week_start) AS diff_vs_week_avg
    FROM daily
)
SELECT day_of_week,
       to_char(DATE '2024-01-01' + (day_of_week - 1), 'Dy') AS weekday,  -- 2024-01-01 = Monday
       round(avg(diff_vs_week_avg) * 1000, 2) AS avg_diff_millieuros,
       count(*) AS days
FROM vs_week
GROUP BY day_of_week
ORDER BY day_of_week;
