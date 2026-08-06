-- Cleaned view over the raw extract.
--
-- No deduplication step here on purpose. id is the primary key on
-- raw_repositories and the loader upserts on conflict, so there is already
-- exactly one row per id - a "latest extracted_at per id" window would be
-- sorting a single row into first place, every time.

with raw as (

    select * from {{ source('raw', 'raw_repositories') }}

)

select
    id,
    source,
    name,
    url,
    stars,
    forks,
    downloads,

    -- - Only github and gitlab report stars. For a package, zero stars is an
    --   artefact of the schema rather than a fact about the package, so it
    --   gets no tier instead of landing at the bottom of every ranking.
    case
        when source not in ('github', 'gitlab') then null
        when stars >= 100000 then 'exceptional'
        when stars >= 10000  then 'high'
        when stars >= 1000   then 'moderate'
        else 'emerging'
    end as popularity_tier,

    language,
    -- - Case and spacing vary between sources, so group on this rather than on
    --   the raw value.
    nullif(lower(trim(language)), '') as language_normalized,

    open_issues_and_prs,
    is_fork,

    -- - An archived project is abandoned by declaration rather than by
    --   inference, which is a stronger statement than any staleness figure.
    coalesce(archived, false) as archived,
    -- - The staleness expression is repeated rather than referenced, because
    --   a column alias is not visible to its own select list.
    case
        when archived then 'archived'
        when date_part('day', now() - updated_at) > 365 then 'stale'
        when date_part('day', now() - updated_at) > 180 then 'quiet'
        when updated_at is null then null
        else 'active'
    end as maintenance_status,

    created_at,
    updated_at,
    extract(year from created_at)::int as creation_year,
    date_part('day', now() - updated_at)::int as days_since_update,

    description,
    extracted_at,
    loaded_at

from raw
