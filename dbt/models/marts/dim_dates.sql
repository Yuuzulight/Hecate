{{ config(materialized='table') }}

-- Date spine, so time series don't have gaps where nothing happened.
--
-- Runs to today rather than a fixed end, which means it needs rebuilding to
-- stay current - fine, it is a small table and gets rebuilt on every run.

with spine as (

    select generate_series(
        '2020-01-01'::date,
        current_date,
        interval '1 day'
    )::date as date_day

)

select
    date_day,
    to_char(date_day, 'YYYYMMDD')::int as date_key,

    extract(year from date_day)::int as year,
    extract(month from date_day)::int as month,
    extract(quarter from date_day)::int as quarter,
    extract(day from date_day)::int as day_of_month,
    -- - Postgres counts Sunday as 0.
    extract(dow from date_day)::int as day_of_week,
    to_char(date_day, 'Day') as day_name,
    to_char(date_day, 'Month') as month_name,

    extract(dow from date_day) in (0, 6) as is_weekend,
    date_trunc('month', date_day)::date as month_start,
    date_trunc('quarter', date_day)::date as quarter_start

from spine
