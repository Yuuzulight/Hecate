{{ config(materialized='table') }}

-- One row per language, with how the projects written in it are distributed.
--
-- Only GitHub and PyPI report a language today, so this covers those two. npm
-- doesn't say and GitLab only tells you from a separate endpoint per project,
-- so neither contributes rows here rather than contributing wrong ones.

select
    md5(language_normalized) as language_key,
    language_normalized as language,
    -- - Keep a readable version for display; the normalised one is for joining.
    min(language) as language_display,

    count(*) as repository_count,
    count(distinct source) as source_count,

    round(avg(stars), 1) as avg_stars,
    percentile_cont(0.5) within group (order by stars)::numeric as median_stars,
    max(stars) as max_stars,
    sum(stars) as total_stars

from {{ ref('stg_repositories') }}
where language_normalized is not null
group by language_normalized
