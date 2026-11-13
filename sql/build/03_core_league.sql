-- Seasons, league members (managers) and fantasy teams, from raw.espn_league (kind = 'league').

DROP TABLE IF EXISTS core.seasons CASCADE;
CREATE TABLE core.seasons AS
SELECT
    l.season,
    l.payload #>> '{settings,name}'                               AS league_name,
    (l.payload #>> '{settings,size}')::int                        AS num_teams,
    l.payload #>> '{settings,scoringSettings,scoringType}'        AS scoring_type,  -- H2H_POINTS, H2H_CATEGORY, H2H_MOST_CATEGORIES, ROTO
    (l.payload #>> '{settings,scheduleSettings,matchupPeriodCount}')::int  AS regular_season_matchups,
    (l.payload #>> '{settings,scheduleSettings,playoffTeamCount}')::int    AS playoff_teams,
    l.payload #>> '{settings,draftSettings,type}'                 AS draft_type,     -- SNAKE, AUCTION, ...
    (l.payload #>> '{settings,acquisitionSettings,acquisitionBudget}')::int AS faab_budget,
    (l.payload #>> '{status,firstScoringPeriod}')::int            AS first_scoring_period,
    (l.payload #>> '{status,finalScoringPeriod}')::int            AS final_scoring_period,
    (l.payload #>> '{status,currentMatchupPeriod}')::int          AS current_matchup_period,
    (l.payload ->> 'scoringPeriodId')::int                        AS current_scoring_period,
    (l.payload #>> '{status,isActive}')::boolean                  AS is_active,
    l.fetched_at
FROM raw.espn_league l
WHERE l.kind = 'league';
ALTER TABLE core.seasons ADD PRIMARY KEY (season);

-- Scoring categories (category leagues) or point values (points leagues) per season.
DROP TABLE IF EXISTS core.scoring_items CASCADE;
CREATE TABLE core.scoring_items AS
SELECT
    l.season,
    i ->> 'statId'                         AS stat_id,
    s.stat_name,
    (i ->> 'points')::numeric              AS points,
    (i ->> 'isReverseItem')::boolean       AS lower_is_better   -- e.g. TO in category leagues
FROM raw.espn_league l
CROSS JOIN LATERAL jsonb_array_elements(l.payload #> '{settings,scoringSettings,scoringItems}') i
LEFT JOIN ref.stats s ON s.stat_id = i ->> 'statId'
WHERE l.kind = 'league';

DROP TABLE IF EXISTS core.members CASCADE;
CREATE TABLE core.members AS
SELECT DISTINCT ON (m ->> 'id')
    m ->> 'id'                                         AS member_id,
    m ->> 'displayName'                                AS display_name,
    trim(concat(m ->> 'firstName', ' ', m ->> 'lastName')) AS full_name,
    l.season                                           AS last_season
FROM raw.espn_league l
CROSS JOIN LATERAL jsonb_array_elements(l.payload -> 'members') m
WHERE l.kind = 'league'
ORDER BY m ->> 'id', l.season DESC;
ALTER TABLE core.members ADD PRIMARY KEY (member_id);

DROP TABLE IF EXISTS core.teams CASCADE;
CREATE TABLE core.teams AS
SELECT
    l.season,
    (t ->> 'id')::int                                              AS team_id,
    t ->> 'abbrev'                                                 AS abbrev,
    coalesce(t ->> 'name', concat_ws(' ', t ->> 'location', t ->> 'nickname')) AS team_name,
    t #>> '{owners,0}'                                             AS primary_owner_id,
    (t ->> 'divisionId')::int                                      AS division_id,
    d ->> 'name'                                                   AS division_name,
    (t #>> '{record,overall,wins}')::int                           AS wins,
    (t #>> '{record,overall,losses}')::int                         AS losses,
    (t #>> '{record,overall,ties}')::int                           AS ties,
    (t #>> '{record,overall,pointsFor}')::numeric                  AS points_for,
    (t #>> '{record,overall,pointsAgainst}')::numeric              AS points_against,
    (t ->> 'playoffSeed')::int                                     AS playoff_seed,
    nullif(coalesce(t ->> 'rankCalculatedFinal', t ->> 'rankFinal'), '0')::int AS final_rank,
    (t #>> '{transactionCounter,acquisitions}')::int               AS acquisitions,
    (t #>> '{transactionCounter,drops}')::int                      AS drops,
    (t #>> '{transactionCounter,trades}')::int                     AS trades,
    (t #>> '{transactionCounter,acquisitionBudgetSpent}')::int     AS faab_spent,
    core.stat_names(t -> 'valuesByStat')                           AS season_stats,  -- team stat totals
    t ->> 'logo'                                                   AS logo_url
FROM raw.espn_league l
CROSS JOIN LATERAL jsonb_array_elements(l.payload -> 'teams') t
LEFT JOIN LATERAL (
    SELECT d FROM jsonb_array_elements(l.payload #> '{settings,scheduleSettings,divisions}') d
    WHERE (d ->> 'id')::int = (t ->> 'divisionId')::int
) dv ON true
WHERE l.kind = 'league';
ALTER TABLE core.teams ADD PRIMARY KEY (season, team_id);

DROP TABLE IF EXISTS core.team_owners CASCADE;
CREATE TABLE core.team_owners AS
SELECT l.season, (t ->> 'id')::int AS team_id, o.member_id, o.ord = 1 AS is_primary
FROM raw.espn_league l
CROSS JOIN LATERAL jsonb_array_elements(l.payload -> 'teams') t
CROSS JOIN LATERAL jsonb_array_elements_text(t -> 'owners') WITH ORDINALITY o(member_id, ord)
WHERE l.kind = 'league';

-- Rosters as of the most recent load of each season.
DROP TABLE IF EXISTS core.rosters CASCADE;
CREATE TABLE core.rosters AS
SELECT
    l.season,
    (t ->> 'id')::int                              AS team_id,
    (e ->> 'playerId')::int                        AS player_id,
    e #>> '{playerPoolEntry,player,fullName}'      AS player_name,
    ls.slot_name                                   AS lineup_slot,
    e ->> 'acquisitionType'                        AS acquisition_type,
    core.from_epoch_ms((e ->> 'acquisitionDate')::bigint) AS acquired_at,
    e #>> '{playerPoolEntry,player,injuryStatus}'  AS injury_status,
    l.fetched_at                                   AS as_of
FROM raw.espn_league l
CROSS JOIN LATERAL jsonb_array_elements(l.payload -> 'teams') t
CROSS JOIN LATERAL jsonb_array_elements(t #> '{roster,entries}') e
LEFT JOIN ref.lineup_slots ls ON ls.slot_id = (e ->> 'lineupSlotId')::int
WHERE l.kind = 'league';
