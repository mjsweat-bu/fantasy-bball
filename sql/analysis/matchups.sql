-- NBA schedule from ESPN, unnested to one row per team per game.
-- The JSON is nested three levels deep, so it takes three LATERAL steps:
--   payload.settings.proTeams          array   -> one row per NBA team
--   team.proGamesByScoringPeriod       object  -> one row per (team, scoring period)
--   ...[scoring period]                array   -> one row per game in that period
-- Each game is listed under BOTH teams, so every game appears twice: once per side.
WITH SCHEDULE AS (
SELECT
    s.season,
    (t ->> 'id')::int                       AS pro_team_id,        -- whose schedule list this row came from
    (g ->> 'id')::bigint                    AS espn_game_id,
    (g ->> 'scoringPeriodId')::int          AS scoring_period,
    (g ->> 'homeProTeamId')::int            AS home_team_id,
    (g ->> 'awayProTeamId')::int            AS away_team_id,
    -- date is epoch milliseconds in UTC; convert to Eastern before taking the date,
    -- or late West-coast tips land on the next day
    (to_timestamp((g ->> 'date')::bigint / 1000.0) AT TIME ZONE 'America/New_York')::date AS game_date
FROM raw.espn_pro_schedule s
CROSS JOIN LATERAL jsonb_array_elements(s.payload #> '{settings,proTeams}') t     -- step 1: teams
CROSS JOIN LATERAL jsonb_each(t -> 'proGamesByScoringPeriod') sp                  -- step 2: {period: [games]}
CROSS JOIN LATERAL jsonb_array_elements(sp.value) g                               -- step 3: games
WHERE (t ->> 'id')::int <> 0                -- id 0 is "FA" (free agents), not a team
  AND s.season >= 2022                      -- NBA game logs start in 2021-22; 2021 also has a
                                            -- ghost postponed game (CHA, 2021-02-07, listed twice)
),
-- Your turn, building on SCHEDULE:
--   team / opponent      join ref.pro_teams twice (pro_team_id, and the other side's id)
--   is_home              home_team_id = pro_team_id
--   day_of_week          from game_date
--   matchup_period       join raw.espn_matchup_days on (season, scoring_period)
--   is_back_to_back      previous game for the same team was the day before (window function)
TEAM_SCHEDULES as (
    SELECT
        s.season                                                            as season,
        s.espn_game_id                                                      as espn_game_id,
        s.pro_team_id                                                       as pro_team_id,
        tm.abbrev                                                           as team,
        opp.abbrev                                                          as opponent,
        CASE WHEN s.pro_team_id = s.home_team_id THEN TRUE ELSE FALSE END   as is_home,
        s.game_date                                                         as game_date,
        TO_CHAR(s.game_date, 'Dy')                                          as day_of_week,
        m.matchup_period                                                    as matchup_period,
        lag(s.game_date) OVER (PARTITION BY s.pro_team_id ORDER BY s.game_date) AS prev_game_date
    FROM SCHEDULE s
    LEFT JOIN ref.pro_teams tm  ON tm.pro_team_id  = s.pro_team_id
    LEFT JOIN ref.pro_teams opp ON opp.pro_team_id =
        CASE WHEN s.pro_team_id = s.home_team_id THEN s.away_team_id ELSE s.home_team_id END
    LEFT JOIN raw.espn_matchup_days m ON s.season = m.season AND s.scoring_period = m.scoring_period
)
SELECT
    *,
    CASE WHEN COALESCE(game_date - prev_game_date,0) <> 1 THEN FALSE ELSE TRUE END as is_back_to_back,
    game_date - prev_game_date as days_rest
FROM TEAM_SCHEDULES
ORDER BY season, pro_team_id, game_date

-- SELECT
--     pro_team_id, game_date,
--     count(*)    OVER (PARTITION BY pro_team_id) as number_count,
--     row_number() OVER (PARTITION BY pro_team_id ORDER BY game_date) as game_num,
--     lag(game_date) OVER (PARTITION BY pro_team_id ORDER BY game_date) as prev_game_date
-- FROM SCHEDULE s
-- WHERE pro_team_id = 1


