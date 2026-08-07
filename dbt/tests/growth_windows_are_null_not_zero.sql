-- A window nobody has enough history for must be null, never zero.
--
-- Returns rows on failure. Zero would mean "measured, and it did not move",
-- which ranks a project first seen yesterday alongside one that has genuinely
-- gone flat for a month. That is the same collapse of absent-into-zero the rest
-- of this schema is careful to avoid.

select
    repository_id,
    days_observed,
    stars_gained_7d,
    stars_gained_30d

from {{ ref('fct_repository_growth') }}

where (days_observed < 8 and stars_gained_7d is not null)
   or (days_observed < 31 and stars_gained_30d is not null)
