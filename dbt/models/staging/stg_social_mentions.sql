-- Posts, with their age worked out.
--
-- A view, for the same reason the repository staging model is one: it is a thin
-- pass over the raw table and a copy would go stale between runs.

select
    id as mention_id,
    platform,
    -- - Null where the post is about something not tracked. Kept, not filtered:
    --   the marts decide what to do with it.
    repository_id,
    target_url,
    title,
    url,
    coalesce(score, 0) as score,
    coalesce(comments, 0) as comments,
    author,
    channel,
    posted_at,
    date_part('day', now() - posted_at)::int as age_days,
    extracted_at

from {{ source('raw', 'social_mentions') }}
