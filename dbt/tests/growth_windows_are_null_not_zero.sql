-- A window nobody has enough history for must be null, never zero.
--
-- Returns rows on failure. Zero would mean "measured, and it did not move",
-- which ranks a project first seen yesterday alongside one that has genuinely
-- gone flat for a month. That is the same collapse of absent-into-zero the rest
-- of this schema is careful to avoid.
--
-- days_observed is calendar days elapsed (latest_on - first_seen_on), so the
-- threshold is the day count itself: a repository is genuinely 7 days old
-- once days_observed reaches 7, not 8. (Before this was a snapshot row count,
-- which only matched calendar age when history was gapless - see
-- fct_repository_growth.sql.)

select
    repository_id,
    days_observed,
    stars_gained_7d,
    stars_gained_30d

from {{ ref('fct_repository_growth') }}

where (days_observed < 7 and stars_gained_7d is not null)
   or (days_observed < 30 and stars_gained_30d is not null)
