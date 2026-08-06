{{ config(materialized='table') }}

-- One row per repository per week of posting, with attention weighted by how
-- recent it was.
--
-- Raw totals answer the wrong question: a thread that did well two years ago
-- and one doing well this week look identical added up. The decayed score
-- halves roughly every fortnight, so momentum outranks history.
--
-- Repositories with no mentions are absent rather than present with zero. A
-- project nobody has posted about and a project we have not looked for are
-- different claims, and only the mention table can tell them apart.

with mentions as (

    select * from {{ ref('stg_social_mentions') }}

)

select
    md5(repository_id || '-' || date_trunc('week', posted_at)::text) as mention_week_key,
    repository_id,
    md5(repository_id) as repository_natural_key,
    date_trunc('week', posted_at)::date as week_starting,

    count(*) as posts,
    count(distinct platform) as platforms,
    sum(score) as total_score,
    sum(comments) as total_comments,
    max(score) as best_post_score,

    -- - Half-life of a fortnight. The shape matters more than the constant:
    --   last week's thread should outweigh last year's, and this says so
    --   legibly rather than precisely.
    round(sum(score * exp(-age_days / 14.0))::numeric, 2) as decayed_score

from mentions
group by repository_id, date_trunc('week', posted_at)
