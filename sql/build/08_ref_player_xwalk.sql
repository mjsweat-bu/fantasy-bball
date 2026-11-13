-- Crosswalk between stats.nba.com player ids (raw.nba_game_logs) and ESPN player ids
-- (core.player_seasons). Both ids are stable across seasons, so it's one row per NBA player.
--
-- Matching, first rule that applies wins:
--   manual     ref.player_xwalk_manual (your fixes; never dropped by a rebuild)
--   name       normalized name matches exactly one ESPN player
--   name_team  name matches several ESPN players; exactly one was on the same NBA team
--              in the same season
-- Anyone left over shows up in core.xwalk_unmatched.

-- Hand-entered fixes. Created once and kept: this script never drops it.
CREATE TABLE IF NOT EXISTS ref.player_xwalk_manual (
    nba_player_id   integer PRIMARY KEY,
    espn_player_id  integer,        -- null = "no ESPN match, stop listing as unmatched"
    note            text
);

-- Lowercase, strip accents, punctuation and suffixes:
-- "P.J. Washington Jr." -> "pj washington", "Nikola Jokić" -> "nikola jokic"
CREATE OR REPLACE FUNCTION core.norm_name(name text) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT trim(regexp_replace(
             regexp_replace(
               regexp_replace(lower(unaccent(coalesce(name, ''))), '[.''`]', '', 'g'),  -- P.J. -> pj
               '[^a-z ]', ' ', 'g'),                                                   -- hyphens etc. -> space
             '\s+(jr|sr|ii|iii|iv|v)\s*$|\s+', ' ', 'g'))
$$;

-- ESPN and NBA disagree on two team abbreviations.
CREATE OR REPLACE FUNCTION core.nba_team_abbrev(espn_abbrev text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE espn_abbrev WHEN 'PHL' THEN 'PHI' WHEN 'PHO' THEN 'PHX' ELSE espn_abbrev END
$$;

DROP TABLE IF EXISTS ref.player_xwalk CASCADE;
CREATE TABLE ref.player_xwalk AS
WITH nba AS (                                   -- each NBA player's (season, team) stints
    SELECT DISTINCT nba_player_id, season,
           payload ->> 'TEAM_ABBREVIATION'                     AS team,
           core.norm_name(payload ->> 'PLAYER_NAME')           AS norm
    FROM raw.nba_game_logs
),
espn AS (
    SELECT DISTINCT player_id AS espn_player_id, season,
           core.nba_team_abbrev(pro_team)                      AS team,
           core.norm_name(player_name)                         AS norm
    FROM core.player_seasons
),
candidates AS (                                 -- every ESPN player sharing a normalized name
    SELECT DISTINCT n.nba_player_id, e.espn_player_id
    FROM (SELECT DISTINCT nba_player_id, norm FROM nba) n
    JOIN (SELECT DISTINCT espn_player_id, norm FROM espn) e USING (norm)
),
by_name AS (
    SELECT nba_player_id, min(espn_player_id) AS espn_player_id
    FROM candidates GROUP BY nba_player_id HAVING count(*) = 1
),
by_team AS (                                    -- ambiguous names: same team in the same season
    SELECT c.nba_player_id, min(c.espn_player_id) AS espn_player_id
    FROM candidates c
    WHERE c.nba_player_id NOT IN (SELECT nba_player_id FROM by_name)
      AND EXISTS (SELECT 1 FROM nba n JOIN espn e USING (season, team)
                  WHERE n.nba_player_id = c.nba_player_id AND e.espn_player_id = c.espn_player_id)
    GROUP BY c.nba_player_id
    HAVING count(DISTINCT c.espn_player_id) = 1
)
SELECT nba_player_id, espn_player_id, 'manual' AS match_method FROM ref.player_xwalk_manual
UNION ALL
SELECT nba_player_id, espn_player_id, 'name' FROM by_name
WHERE nba_player_id NOT IN (SELECT nba_player_id FROM ref.player_xwalk_manual)
UNION ALL
SELECT nba_player_id, espn_player_id, 'name_team' FROM by_team
WHERE nba_player_id NOT IN (SELECT nba_player_id FROM ref.player_xwalk_manual);
ALTER TABLE ref.player_xwalk ADD PRIMARY KEY (nba_player_id);

-- NBA players with no ESPN match, most minutes first: the list to work through by hand
-- (add a row to ref.player_xwalk_manual, then rebuild). Players ESPN never listed in
-- your league's player pool (deep bench, two-way contracts) will show up here too.
CREATE OR REPLACE VIEW core.xwalk_unmatched AS
SELECT
    g.nba_player_id,
    max(g.payload ->> 'PLAYER_NAME')                       AS nba_name,
    string_agg(DISTINCT g.payload ->> 'TEAM_ABBREVIATION', ',') AS teams,
    min(g.season)                                          AS first_season,
    max(g.season)                                          AS last_season,
    count(*)                                               AS games,
    sum((g.payload ->> 'MIN')::numeric)                    AS minutes
FROM raw.nba_game_logs g
LEFT JOIN ref.player_xwalk x USING (nba_player_id)
WHERE x.nba_player_id IS NULL
GROUP BY g.nba_player_id
ORDER BY minutes DESC;
