-- One row per NBA player per game (stats.nba.com), with the matching ESPN player id
-- and ESPN scoring period so it joins to core.player_days and your league's weeks.

-- ESPN scoring period (one per day) for each NBA game date, from ESPN's pro schedule.
-- Dates are Eastern, the NBA's schedule time zone.
DROP TABLE IF EXISTS ref.espn_scoring_dates CASCADE;
CREATE TABLE ref.espn_scoring_dates AS
SELECT DISTINCT
    s.season,
    (to_timestamp((g ->> 'date')::bigint / 1000.0) AT TIME ZONE 'America/New_York')::date AS game_date,
    (g ->> 'scoringPeriodId')::int                                                          AS scoring_period
FROM raw.espn_pro_schedule s
CROSS JOIN LATERAL jsonb_array_elements(s.payload #> '{settings,proTeams}') t
CROSS JOIN LATERAL jsonb_each(t -> 'proGamesByScoringPeriod') sp
CROSS JOIN LATERAL jsonb_array_elements(sp.value) g;
ALTER TABLE ref.espn_scoring_dates ADD PRIMARY KEY (season, game_date);

DROP TABLE IF EXISTS core.player_games CASCADE;
CREATE TABLE core.player_games AS
SELECT
    g.season,
    CASE g.season_type WHEN 'PlayIn' THEN 'Play-In' ELSE g.season_type END AS season_type,  -- Regular Season, Play-In, Playoffs
    g.game_id,
    (p ->> 'GAME_DATE')::date                  AS game_date,
    sd.scoring_period,                          -- null outside ESPN's fantasy calendar (e.g. playoffs)
    g.nba_player_id,
    x.espn_player_id,                           -- null until matched (see core.xwalk_unmatched)
    p ->> 'PLAYER_NAME'                        AS player_name,
    p ->> 'TEAM_ABBREVIATION'                  AS team,
    right(p ->> 'MATCHUP', 3)                  AS opponent,
    p ->> 'MATCHUP' LIKE '%vs.%'               AS is_home,
    p ->> 'WL'                                 AS result,
    (p ->> 'MIN')::numeric                     AS minutes,
    (p ->> 'PTS')::int                         AS pts,
    (p ->> 'REB')::int                         AS reb,
    (p ->> 'OREB')::int                        AS oreb,
    (p ->> 'DREB')::int                        AS dreb,
    (p ->> 'AST')::int                         AS ast,
    (p ->> 'STL')::int                         AS stl,
    (p ->> 'BLK')::int                         AS blk,
    (p ->> 'TOV')::int                         AS tov,
    (p ->> 'FG3M')::int                        AS fg3m,
    (p ->> 'FG3A')::int                        AS fg3a,
    (p ->> 'FGM')::int                         AS fgm,
    (p ->> 'FGA')::int                         AS fga,
    (p ->> 'FTM')::int                         AS ftm,
    (p ->> 'FTA')::int                         AS fta,
    (p ->> 'PF')::int                          AS pf,
    (p ->> 'PLUS_MINUS')::int                  AS plus_minus
FROM raw.nba_game_logs g
CROSS JOIN LATERAL (SELECT g.payload AS p) j
LEFT JOIN ref.player_xwalk x       ON x.nba_player_id = g.nba_player_id
LEFT JOIN ref.espn_scoring_dates sd ON sd.season = g.season AND sd.game_date = (p ->> 'GAME_DATE')::date;
ALTER TABLE core.player_games ADD PRIMARY KEY (game_id, nba_player_id);
CREATE INDEX ON core.player_games (espn_player_id, game_date);
CREATE INDEX ON core.player_games (season, season_type);
