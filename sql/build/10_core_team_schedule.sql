-- One row per NBA team per game: who they play, home or away, the fantasy week,
-- and rest between games. Every game appears twice, once from each team's side.
-- Worked out in sql/analysis/matchups.sql. Checks: python -m fbb.validate

DROP TABLE IF EXISTS core.team_schedule CASCADE;
CREATE TABLE core.team_schedule AS
WITH schedule AS (
    -- ESPN's pro schedule JSON, unnested in three steps:
    --   payload.settings.proTeams       array  -> one row per NBA team
    --   team.proGamesByScoringPeriod    object -> one row per (team, scoring period)
    --   ...[scoring period]             array  -> one row per game in that period
    SELECT
        s.season,
        (t ->> 'id')::int                AS pro_team_id,   -- whose schedule list this row came from
        (g ->> 'id')::bigint             AS espn_game_id,
        (g ->> 'scoringPeriodId')::int   AS scoring_period,
        (g ->> 'homeProTeamId')::int     AS home_team_id,
        (g ->> 'awayProTeamId')::int     AS away_team_id,
        -- epoch ms in UTC; convert to Eastern first, or late West-coast tips land on the next day
        (to_timestamp((g ->> 'date')::bigint / 1000.0) AT TIME ZONE 'America/New_York')::date AS game_date
    FROM raw.espn_pro_schedule s
    CROSS JOIN LATERAL jsonb_array_elements(s.payload #> '{settings,proTeams}') t
    CROSS JOIN LATERAL jsonb_each(t -> 'proGamesByScoringPeriod') sp
    CROSS JOIN LATERAL jsonb_array_elements(sp.value) g
    WHERE (t ->> 'id')::int <> 0       -- id 0 is "FA" (free agents), not a team
      AND s.season >= 2022             -- NBA game logs start in 2021-22; 2021 also has a
                                       -- ghost postponed game (CHA, 2021-02-07, listed twice)
),
team_schedules AS (
    SELECT
        s.season,
        s.espn_game_id,
        s.pro_team_id,
        tm.abbrev                                        AS team,
        CASE WHEN s.pro_team_id = s.home_team_id
             THEN s.away_team_id ELSE s.home_team_id END AS opponent_pro_team_id,
        opp.abbrev                                       AS opponent,
        s.pro_team_id = s.home_team_id                   AS is_home,
        s.game_date,
        to_char(s.game_date, 'Dy')                       AS day_of_week,
        s.scoring_period,                                -- ESPN fantasy day; joins to core.player_days
        m.matchup_period,                                -- fantasy week; null outside the fantasy season
        -- previous game in the same season (null on opening night)
        lag(s.game_date) OVER (PARTITION BY s.season, s.pro_team_id ORDER BY s.game_date) AS prev_game_date,
        -- previous game in any season (opening night gets last season's final game)
        lag(s.game_date) OVER (PARTITION BY s.pro_team_id ORDER BY s.game_date)           AS prev_game_date_any_season
    FROM schedule s
    LEFT JOIN ref.pro_teams tm  ON tm.pro_team_id = s.pro_team_id
    LEFT JOIN ref.pro_teams opp ON opp.pro_team_id =
        CASE WHEN s.pro_team_id = s.home_team_id THEN s.away_team_id ELSE s.home_team_id END
    LEFT JOIN raw.espn_matchup_days m ON m.season = s.season AND m.scoring_period = s.scoring_period
)
SELECT
    *,
    game_date - prev_game_date                       AS days_since_last_game,  -- 1 = back-to-back; null on opening night
    coalesce(game_date - prev_game_date = 1, false)  AS is_back_to_back
FROM team_schedules;

ALTER TABLE core.team_schedule ADD PRIMARY KEY (espn_game_id, pro_team_id);
ALTER TABLE core.team_schedule ADD UNIQUE (pro_team_id, game_date);
CREATE INDEX ON core.team_schedule (season, scoring_period);
