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

    -- - Star figures only count sources that have stars. A PyPI package sits
    --   at zero because the column has to hold something, not because nobody
    --   starred it, and letting those into the aggregate drags the average
    --   down and pins the median to zero for any language with more packages
    --   than repositories. starred_count says how many rows are behind these.
    count(*) filter (where source in ('github', 'gitlab')) as starred_count,

    round(avg(stars) filter (where source in ('github', 'gitlab')), 1) as avg_stars,
    percentile_cont(0.5) within group (
        order by case when source in ('github', 'gitlab') then stars end
    )::numeric as median_stars,
    max(stars) filter (where source in ('github', 'gitlab')) as max_stars

from {{ ref('stg_repositories') }}
where language_normalized is not null
group by language_normalized
