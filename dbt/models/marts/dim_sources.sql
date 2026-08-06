{{ config(materialized='table') }}

-- One row per source, summarising what each contributes.
--
-- Doubles as a coverage report: the null counts say which fields a source
-- actually reports, which is the first thing worth knowing before comparing
-- anything across them.

select
    md5(source) as source_key,
    source,

    count(*) as repository_count,

    -- - Counts skip nulls, so these are how many rows carry each field at all.
    count(stars) filter (where stars > 0) as with_stars,
    count(downloads) as with_downloads,
    count(language_normalized) as with_language,
    count(created_at) as with_created_at,

    max(stars) as max_stars,
    sum(downloads) as total_downloads,
    round(avg(days_since_update), 1) as avg_days_since_update,

    min(created_at) as earliest_created_at,
    max(extracted_at) as last_extracted_at

from {{ ref('stg_repositories') }}
group by source
