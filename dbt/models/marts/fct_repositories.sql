{{
    config(
        materialized='incremental',
        unique_key='repository_key',
        indexes=[
            {'columns': ['source_key']},
            {'columns': ['language_key']},
            {'columns': ['extracted_at']},
        ]
    )
}}

-- One row per repository or package, with keys out to the dimensions.
--
-- Incremental on extracted_at. Every row the pipeline touches gets a fresh
-- extracted_at, so a normal run brings all of them back through here and the
-- unique key merges them in place. A project that drops out of the top slice
-- keeps its old timestamp and stays put rather than vanishing from history.

with staged as (

    select * from {{ ref('stg_repositories') }}

    {% if is_incremental() %}
    where extracted_at > (select coalesce(max(extracted_at), '1970-01-01'::timestamptz) from {{ this }})
    {% endif %}

)

select
    -- - id already carries its source as a prefix, so this is belt and braces.
    --   It is here because a dimensional model is expected to key on something
    --   opaque rather than on a business identifier that might get reshaped.
    md5(id || '-' || source) as repository_key,

    id,
    md5(source) as source_key,
    case when language_normalized is not null
         then md5(language_normalized)
    end as language_key,

    name,
    url,
    description,

    stars,
    forks,
    downloads,
    popularity_tier,

    -- - Rough proxy for how much a project gets used versus admired. Null
    --   rather than divide by zero, and meaningless where stars are absent.
    case when forks > 0 then round(stars::numeric / forks, 2) end as stars_per_fork,

    language,
    language_normalized,
    creation_year,
    days_since_update,
    created_at,
    updated_at,

    extracted_at,
    loaded_at

from staged
