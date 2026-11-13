-- Raw ESPN API responses, stored as returned. Loaded by src/fbb/ingest.py;
-- everything in core.* is rebuilt from these tables by the later build scripts.
-- The loader also runs this file first, so the tables exist before the first load.
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS ref;
CREATE SCHEMA IF NOT EXISTS core;

-- One row per season per league request:
--   league -> views mTeam, mRoster, mMatchup, mSettings, mStandings
--             (teams, owners, end-of-load rosters, full schedule, settings, status)
--   draft  -> view mDraftDetail
CREATE TABLE IF NOT EXISTS raw.espn_league (
    season      integer NOT NULL,
    kind        text    NOT NULL,
    payload     jsonb   NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (season, kind)
);

-- Box scores for one scoring period (one day in basketball): views mMatchupScore +
-- mScoreboard filtered to that day's matchup period. Each matchup's
-- home/away.rosterForCurrentScoringPeriod holds every rostered player's lineup slot
-- and stat line for that day.
CREATE TABLE IF NOT EXISTS raw.espn_scoring_periods (
    season          integer NOT NULL,
    scoring_period  integer NOT NULL,
    matchup_period  integer NOT NULL,
    payload         jsonb   NOT NULL,
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (season, scoring_period)
);

-- Which matchup period (fantasy week) each scoring period (day) belongs to, for the
-- whole season including days not played yet. Week boundaries are irregular (short
-- opening week, long All-Star week, two-week playoff rounds), so this comes from
-- ESPN rather than a calendar rule. matchup_period is null for past days with no
-- fantasy games. Filled by ingest.load_matchup_days.
CREATE TABLE IF NOT EXISTS raw.espn_matchup_days (
    season          integer NOT NULL,
    scoring_period  integer NOT NULL,
    matchup_period  integer,
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (season, scoring_period)
);

-- Transactions (adds, drops, waivers, trades, lineup moves) per scoring period: view mTransactions2.
CREATE TABLE IF NOT EXISTS raw.espn_transactions (
    season          integer NOT NULL,
    scoring_period  integer NOT NULL,
    payload         jsonb   NOT NULL,
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (season, scoring_period)
);

-- Player pool for a season (view kona_player_info): one row per player with
-- ownership, ratings and stat splits (season totals, last 7/15/30, projections).
CREATE TABLE IF NOT EXISTS raw.espn_players (
    season      integer NOT NULL,
    player_id   integer NOT NULL,
    payload     jsonb   NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (season, player_id)
);

-- NBA schedule for a season (view proTeamSchedules_wl).
CREATE TABLE IF NOT EXISTS raw.espn_pro_schedule (
    season      integer PRIMARY KEY,
    payload     jsonb   NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now()
);

-- NBA player game logs from stats.nba.com (nba_api LeagueGameLog), one row per
-- player per game, loaded by src/fbb/ingest_nba.py. season is the ESPN-style end
-- year (2026-27 -> 2027); season_type is the type the rows were requested as
-- (Regular Season, PlayIn, Playoffs). payload is the row as {header: value}.
CREATE TABLE IF NOT EXISTS raw.nba_game_logs (
    game_id         text    NOT NULL,
    nba_player_id   integer NOT NULL,
    season          integer NOT NULL,
    season_type     text    NOT NULL,
    payload         jsonb   NOT NULL,
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, nba_player_id)
);
CREATE INDEX IF NOT EXISTS nba_game_logs_season ON raw.nba_game_logs (season, season_type);

-- Accent stripping for name matching in the player crosswalk (Jokić -> Jokic).
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Lookups, refilled by the loader from espn_api.basketball.constant
CREATE TABLE IF NOT EXISTS ref.stats (
    stat_id     text PRIMARY KEY,   -- ESPN stat id as it appears in JSON keys ("0", "1", ...)
    stat_name   text NOT NULL       -- PTS, BLK, FGM, ...
);
CREATE TABLE IF NOT EXISTS ref.lineup_slots (
    slot_id     integer PRIMARY KEY,
    slot_name   text NOT NULL       -- PG, SG, G, UT, BE, IR, ...
);
CREATE TABLE IF NOT EXISTS ref.pro_teams (
    pro_team_id integer PRIMARY KEY,
    abbrev      text NOT NULL
);
CREATE TABLE IF NOT EXISTS ref.stat_splits (
    split_prefix text PRIMARY KEY,  -- first two chars of a stat split id ("00", "10", ...)
    split_name   text NOT NULL      -- total, projected, last_7, ...
);
