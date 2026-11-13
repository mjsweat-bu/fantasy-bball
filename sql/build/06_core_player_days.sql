-- Daily box scores: every rostered player, every day, with lineup slot and stat line.
-- From raw.espn_scoring_periods (rosterForCurrentScoringPeriod of each matchup).

DROP TABLE IF EXISTS core.player_days CASCADE;
CREATE TABLE core.player_days AS
WITH entries AS (
    SELECT
        r.season,
        r.scoring_period,
        r.matchup_period,
        (m ->> 'id')::int                 AS matchup_id,
        (m -> side ->> 'teamId')::int     AS team_id,
        e
    FROM raw.espn_scoring_periods r
    CROSS JOIN LATERAL jsonb_array_elements(r.payload -> 'schedule') m
    CROSS JOIN LATERAL unnest(ARRAY['home', 'away']) side
    CROSS JOIN LATERAL jsonb_array_elements(m -> side #> '{rosterForCurrentScoringPeriod,entries}') e
    WHERE m ? side
)
SELECT
    x.season,
    x.scoring_period,
    x.matchup_period,
    x.matchup_id,
    x.team_id,
    (x.e ->> 'playerId')::int                              AS player_id,
    x.e #>> '{playerPoolEntry,player,fullName}'            AS player_name,
    ls.slot_name                                           AS lineup_slot,
    ls.slot_name NOT IN ('BE', 'IR')                       AS is_starter,
    pt.abbrev                                              AS pro_team,
    coalesce((day.st ->> 'appliedTotal')::numeric,
             (x.e #>> '{playerPoolEntry,appliedStatTotal}')::numeric, 0) AS fantasy_points,
    coalesce((day.st #>> '{stats,42}')::numeric, 0) > 0
        OR coalesce((day.st #>> '{stats,40}')::numeric, 0) > 0 AS played,  -- 42 = GP, 40 = MIN
    core.stat_names(day.st -> 'stats')                     AS stats
FROM entries x
LEFT JOIN ref.lineup_slots ls ON ls.slot_id = (x.e ->> 'lineupSlotId')::int
LEFT JOIN ref.pro_teams pt    ON pt.pro_team_id = (x.e #>> '{playerPoolEntry,player,proTeamId}')::int
LEFT JOIN LATERAL (
    -- the actual (statSourceId 0) stat line for this day
    SELECT st
    FROM jsonb_array_elements(x.e #> '{playerPoolEntry,player,stats}') st
    WHERE (st ->> 'scoringPeriodId')::int = x.scoring_period
      AND (st ->> 'statSourceId')::int = 0
    LIMIT 1
) day ON true;
ALTER TABLE core.player_days ADD PRIMARY KEY (season, scoring_period, team_id, player_id);
CREATE INDEX ON core.player_days (player_id);
