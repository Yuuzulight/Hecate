{{ config(materialized='table') }}

-- What is being discussed that the dataset does not contain.
--
-- Every source is seeded by cumulative popularity - GitHub's most-starred,
-- npm's most-installed - and then asked what people are talking about. A
-- project trending today has not accumulated the stars to be seeded yet, so
-- the selection quietly excludes the answer.
--
-- This is that exclusion, made visible. One row per project URL nobody here
-- tracks, ranked by the attention it is getting.

with unresolved as (

    select * from {{ ref('stg_social_mentions') }}
    where repository_id is null
      and target_url is not null

)

select
    md5(target_url) as target_key,
    target_url,

    count(*) as posts,
    count(distinct platform) as platforms,
    sum(score) as total_score,
    sum(comments) as total_comments,
    max(posted_at) as last_posted_at,
    min(age_days) as days_since_newest_post,

    -- - Same fortnight half-life as the resolved fact, so the two rank on
    --   comparable terms and a candidate can be weighed against something
    --   already tracked.
    round(sum(score * exp(-greatest(age_days, 0) / 14.0))::numeric, 2) as decayed_score

from unresolved
group by target_url
