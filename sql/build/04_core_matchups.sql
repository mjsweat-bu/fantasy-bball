-- Head-to-head matchups from the league schedule (raw.espn_league, kind = 'league').

DROP TABLE IF EXISTS core.matchups CASCADE;
CREATE TABLE core.matchups AS
SELECT
    l.season,
    (m ->> 'id')::int                              AS matchup_id,
    (m ->> 'matchupPeriodId')::int                 AS matchup_period,
    m ->> 'playoffTierType'                        AS playoff_tier,   -- NONE = regular season
    (m ->> 'playoffTierType') <> 'NONE'            AS is_playoff,
    (m #>> '{home,teamId}')::int                   AS home_team_id,
    (m #>> '{away,teamId}')::int                   AS away_team_id,   -- null on a bye
    (m #>> '{home,totalPoints}')::numeric          AS home_points,
    (m #>> '{away,totalPoints}')::numeric          AS away_points,
    (m #>> '{home,cumulativeScore,wins}')::int     AS home_cat_wins,
    (m #>> '{away,cumulativeScore,wins}')::int     AS away_cat_wins,
    (m #>> '{home,cumulativeScore,ties}')::int     AS cat_ties,
    m ->> 'winner'                                 AS winner          -- HOME, AWAY, TIE, UNDECIDED
FROM raw.espn_league l
CROSS JOIN LATERAL jsonb_array_elements(l.payload -> 'schedule') m
WHERE l.kind = 'league';
ALTER TABLE core.matchups ADD PRIMARY KEY (season, matchup_id);

-- One row per team per matchup: the easiest table for records, streaks, points against.
DROP TABLE IF EXISTS core.team_matchups CASCADE;
CREATE TABLE core.team_matchups AS
SELECT season, matchup_id, matchup_period, playoff_tier, is_playoff,
       home_team_id AS team_id, away_team_id AS opponent_id, true AS is_home,
       home_points AS points, away_points AS opponent_points,
       home_cat_wins AS cat_wins, away_cat_wins AS cat_losses, cat_ties,
       CASE winner WHEN 'HOME' THEN 'W' WHEN 'AWAY' THEN 'L' WHEN 'TIE' THEN 'T' END AS result
FROM core.matchups
UNION ALL
SELECT season, matchup_id, matchup_period, playoff_tier, is_playoff,
       away_team_id, home_team_id, false,
       away_points, home_points,
       away_cat_wins, home_cat_wins, cat_ties,
       CASE winner WHEN 'AWAY' THEN 'W' WHEN 'HOME' THEN 'L' WHEN 'TIE' THEN 'T' END
FROM core.matchups
WHERE away_team_id IS NOT NULL;

-- Category leagues: each team's value and result in each category of each matchup.
DROP TABLE IF EXISTS core.matchup_categories CASCADE;
CREATE TABLE core.matchup_categories AS
SELECT
    l.season,
    (m ->> 'id')::int                     AS matchup_id,
    (m ->> 'matchupPeriodId')::int        AS matchup_period,
    (m -> side ->> 'teamId')::int         AS team_id,
    st.key                                AS stat_id,
    s.stat_name,
    (st.value ->> 'score')::numeric       AS value,
    st.value ->> 'result'                 AS result     -- WIN, LOSS, TIE
FROM raw.espn_league l
CROSS JOIN LATERAL jsonb_array_elements(l.payload -> 'schedule') m
CROSS JOIN LATERAL unnest(ARRAY['home', 'away']) side
CROSS JOIN LATERAL jsonb_each(m -> side #> '{cumulativeScore,scoreByStat}') st
LEFT JOIN ref.stats s ON s.stat_id = st.key
WHERE l.kind = 'league' AND m ? side
  AND jsonb_typeof(m -> side #> '{cumulativeScore,scoreByStat}') = 'object';

-- Points per team per day (points leagues; category leagues show 0).
DROP TABLE IF EXISTS core.team_days CASCADE;
CREATE TABLE core.team_days AS
SELECT
    l.season,
    (m ->> 'matchupPeriodId')::int    AS matchup_period,
    (m -> side ->> 'teamId')::int     AS team_id,
    p.key::int                        AS scoring_period,
    p.value::numeric                  AS points
FROM raw.espn_league l
CROSS JOIN LATERAL jsonb_array_elements(l.payload -> 'schedule') m
CROSS JOIN LATERAL unnest(ARRAY['home', 'away']) side
CROSS JOIN LATERAL jsonb_each_text(m -> side -> 'pointsByScoringPeriod') p
WHERE l.kind = 'league' AND m ? side
  AND jsonb_typeof(m -> side -> 'pointsByScoringPeriod') = 'object';
