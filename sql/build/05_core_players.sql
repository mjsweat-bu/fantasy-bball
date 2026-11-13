-- NBA players and their season stat splits, from the player pool (raw.espn_players).

-- One row per player: most recent season's info.
DROP TABLE IF EXISTS core.players CASCADE;
CREATE TABLE core.players AS
SELECT DISTINCT ON (p.player_id)
    p.player_id,
    p.payload #>> '{player,fullName}'                       AS player_name,
    pos.slot_name                                           AS position,
    pt.abbrev                                               AS pro_team,
    p.payload #>> '{player,injuryStatus}'                   AS injury_status,
    (SELECT array_agg(ls.slot_name ORDER BY ls.slot_id)
       FROM jsonb_array_elements_text(p.payload #> '{player,eligibleSlots}') es
       JOIN ref.lineup_slots ls ON ls.slot_id = es::int
      WHERE ls.slot_name NOT IN ('BE', 'IR', 'UT', ''))    AS eligible_positions,
    p.season                                                AS last_season
FROM raw.espn_players p
LEFT JOIN ref.lineup_slots pos ON pos.slot_id = (p.payload #>> '{player,defaultPositionId}')::int - 1
LEFT JOIN ref.pro_teams pt     ON pt.pro_team_id = (p.payload #>> '{player,proTeamId}')::int
ORDER BY p.player_id, p.season DESC;
ALTER TABLE core.players ADD PRIMARY KEY (player_id);

-- Player in each season's pool: who owned them at load time and how widely owned.
DROP TABLE IF EXISTS core.player_seasons CASCADE;
CREATE TABLE core.player_seasons AS
SELECT
    p.season,
    p.player_id,
    p.payload #>> '{player,fullName}'                       AS player_name,
    pt.abbrev                                               AS pro_team,
    nullif((p.payload ->> 'onTeamId')::int, 0)              AS fantasy_team_id,  -- null = free agent
    p.payload ->> 'status'                                  AS roster_status,    -- ONTEAM, FREEAGENT, WAIVERS
    (p.payload #>> '{player,ownership,percentOwned}')::numeric   AS pct_owned,
    (p.payload #>> '{player,ownership,percentStarted}')::numeric AS pct_started,
    (p.payload #>> '{player,ownership,averageDraftPosition}')::numeric AS adp,
    (p.payload #>> '{ratings,0,positionalRanking}')::int    AS position_rank,
    (p.payload #>> '{ratings,0,totalRanking}')::int         AS overall_rank,
    p.fetched_at                                            AS as_of
FROM raw.espn_players p
LEFT JOIN ref.pro_teams pt ON pt.pro_team_id = (p.payload #>> '{player,proTeamId}')::int;
ALTER TABLE core.player_seasons ADD PRIMARY KEY (season, player_id);

-- Stat splits: season totals, last 7/15/30, projections. Long format, one row per split.
DROP TABLE IF EXISTS core.player_stat_splits CASCADE;
CREATE TABLE core.player_stat_splits AS
SELECT
    p.season,
    p.player_id,
    sp.split_name                                 AS split,          -- total, last_7, last_15, last_30, projected
    (st ->> 'seasonId')::int                      AS stat_season,
    (st ->> 'appliedTotal')::numeric              AS fantasy_points,
    (st ->> 'appliedAverage')::numeric            AS fantasy_points_avg,
    (core.stat_names(st -> 'stats') ->> 'GP')::numeric AS games_played,
    core.stat_names(st -> 'stats')                AS totals,
    core.stat_names(st -> 'averageStats')         AS averages
FROM raw.espn_players p
CROSS JOIN LATERAL jsonb_array_elements(p.payload #> '{player,stats}') st
JOIN ref.stat_splits sp ON sp.split_prefix = left(st ->> 'id', 2)
WHERE (st ->> 'seasonId')::int = p.season
  AND (st ->> 'scoringPeriodId')::int = 0;
