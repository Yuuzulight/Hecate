{{ config(materialized='table') }}

-- What is accelerating, which is a different question from what is popular and
-- from what is being discussed.
--
-- The project has three attention signals and ranked on none of them together.
-- A project gaining stars quickly, being installed more each week, and turning
-- up in discussion is a different thing from one strong on any single axis.
--
-- Two rules make this honest rather than merely impressive.
--
-- A source that does not report a signal is not penalised for it. npm has no
-- stars and GitHub no downloads, so a missing component contributes nothing to
-- the total and nothing to the divisor - otherwise every npm package would rank
-- below every repository for a reason that has nothing to do with momentum.
--
-- Every component stays visible next to the total. A ranking nobody can take
-- apart is a ranking nobody should trust, and these weights are guesses.

with growth as (

    select * from {{ ref('fct_repository_growth') }}

),

mentions as (

    select
        repository_id,
        sum(decayed_score) as decayed_score
    from {{ ref('fct_repository_mentions') }}
    -- - Link-resolved only. Name matching is off by default and, when on, is
    --   wrong in ways that look plausible; a momentum ranking is exactly where
    --   that would do damage.
    where lowest_confidence >= 1.0
    group by repository_id

),

scored as (

    select
        g.repository_id,
        g.stars,
        g.downloads,
        g.stars_gained_7d,
        g.stars_growth_pct_7d,
        g.downloads_gained_7d,
        m.decayed_score,

        -- - Each component normalised to roughly 0-100 so the weights mean
        --   something. The constants are order-of-magnitude guesses, deliberately
        --   round, and worth revisiting once there is more than a week of data.
        case when g.stars_growth_pct_7d is not null
             then least(g.stars_growth_pct_7d * 10, 100) end as growth_component,

        case when g.downloads_gained_7d is not null and g.downloads > 0
             then least(100.0 * g.downloads_gained_7d / nullif(g.downloads, 0) * 10, 100) end
             as usage_component,

        case when m.decayed_score is not null
             then least(m.decayed_score, 100) end as attention_component

    from growth g
    left join mentions m on m.repository_id = g.repository_id

)

select
    repository_id,
    stars,
    downloads,
    stars_gained_7d,
    stars_growth_pct_7d,
    downloads_gained_7d,
    decayed_score,

    growth_component,
    usage_component,
    attention_component,

    -- - Mean of whatever is measurable. coalesce to 0 in the numerator and a
    --   matching 0/1 in the divisor means an absent signal neither adds nor
    --   dilutes, which is the whole point.
    round(
        (
            coalesce(growth_component, 0)
          + coalesce(usage_component, 0)
          + coalesce(attention_component, 0)
        )
        / nullif(
            (case when growth_component is not null then 1 else 0 end)
          + (case when usage_component is not null then 1 else 0 end)
          + (case when attention_component is not null then 1 else 0 end)
        , 0)::numeric
    , 2) as momentum,

    (case when growth_component is not null then 1 else 0 end)
  + (case when usage_component is not null then 1 else 0 end)
  + (case when attention_component is not null then 1 else 0 end)
    as signals_measured

from scored
