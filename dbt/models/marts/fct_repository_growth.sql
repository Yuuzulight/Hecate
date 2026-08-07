{{ config(materialized='table') }}

-- Change over time, which is what the snapshot table was built to make possible
-- and what nothing had yet used.
--
-- Two rules run through this model.
--
-- Everything is keyed on repository_id, never on name. The same name exists
-- across sources - vite is an npm package with no stars and a GitHub repository
-- with eighty thousand - and grouping by name subtracts one from the other and
-- calls the difference growth. That mistake has already been made once here, in
-- a documented query, and produced a confident 82,239.
--
-- A window with insufficient history is null, not zero. A project first seen
-- yesterday has no thirty-day figure, and reporting zero would rank it
-- alongside projects that genuinely have not moved.

with bounds as (

    select
        repository_id,
        max(captured_on) as latest_on,
        min(captured_on) as first_seen_on,
        count(*) as days_observed
    from {{ source('raw', 'repository_snapshots') }}
    group by repository_id

),

latest as (

    select s.*, b.first_seen_on, b.days_observed
    from {{ source('raw', 'repository_snapshots') }} s
    join bounds b
      on b.repository_id = s.repository_id
     and b.latest_on = s.captured_on

)

select
    l.repository_id,
    l.captured_on as as_of,
    l.first_seen_on,
    l.days_observed,

    l.stars,
    l.downloads,
    l.mention_count,

    -- - Absolute change. Null where no snapshot that old exists, which the
    --   lateral joins give for free by returning no row.
    l.stars - d1.stars as stars_gained_1d,
    l.stars - d7.stars as stars_gained_7d,
    l.stars - d30.stars as stars_gained_30d,

    l.downloads - d7.downloads as downloads_gained_7d,
    l.mention_count - d7.mention_count as mentions_gained_7d,

    -- - Rate as well as absolute, because they answer different questions. A
    --   project going 200 to 400 doubled; one going 200,000 to 200,200 barely
    --   moved, and on absolute change the second one wins.
    round(
        100.0 * (l.stars - d7.stars) / nullif(d7.stars, 0), 2
    ) as stars_growth_pct_7d,
    round(
        100.0 * (l.stars - d30.stars) / nullif(d30.stars, 0), 2
    ) as stars_growth_pct_30d

from latest l

left join lateral (
    select p.stars, p.downloads, p.mention_count
    from {{ source('raw', 'repository_snapshots') }} p
    where p.repository_id = l.repository_id
      and p.captured_on <= l.captured_on - 1
    order by p.captured_on desc
    limit 1
) d1 on true

left join lateral (
    select p.stars, p.downloads, p.mention_count
    from {{ source('raw', 'repository_snapshots') }} p
    where p.repository_id = l.repository_id
      and p.captured_on <= l.captured_on - 7
    order by p.captured_on desc
    limit 1
) d7 on true

left join lateral (
    select p.stars, p.downloads, p.mention_count
    from {{ source('raw', 'repository_snapshots') }} p
    where p.repository_id = l.repository_id
      and p.captured_on <= l.captured_on - 30
    order by p.captured_on desc
    limit 1
) d30 on true
