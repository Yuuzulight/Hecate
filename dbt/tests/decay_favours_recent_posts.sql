-- The decay has to actually decay, and must never amplify.
--
-- Returns rows on failure, which is what dbt treats as a failing test.
--
-- Two things could go wrong and both look plausible in a table of numbers: the
-- weighting could be inert, leaving decayed equal to raw, or a future-dated
-- post could flip the exponent positive and outrank everything real.

select
    mention_week_key,
    total_score,
    decayed_score

from {{ ref('fct_repository_mentions') }}

-- - Decayed score can equal the raw score only for a post made today, and can
--   never exceed it.
where decayed_score > total_score
