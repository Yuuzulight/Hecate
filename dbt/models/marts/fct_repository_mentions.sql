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

    -- - Resolved only. Unresolved posts are kept in staging because they are
    --   the discovery signal, but they have no repository to score against.
    select * from {{ ref('stg_social_mentions') }}
    where repository_id is not null

)

select
    md5(repository_id || '-' || date_trunc('week', posted_at)::text) as mention_week_key,
    repository_id,
    date_trunc('week', posted_at)::date as week_starting,

    count(*) as posts,
    -- - So a sceptical query can drop prose matches without recomputing.
    count(*) filter (where match_confidence >= 1.0) as linked_posts,
    min(match_confidence) as lowest_confidence,
    sum(score) as total_score,
    sum(comments) as total_comments,

    -- - Half-life of a fortnight. The shape matters more than the constant:
    --   last week's thread should outweigh last year's, and this says so
    --   legibly rather than precisely.
    --
    --   Age is floored at zero. A post timestamped in the future would
    --   otherwise make the exponent positive and let one bad clock or one
    --   mangled date outrank everything real.
    round(sum(score * exp(-greatest(age_days, 0) / 14.0))::numeric, 2) as decayed_score

from mentions
group by repository_id, date_trunc('week', posted_at)
