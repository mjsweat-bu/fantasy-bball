-- Losses caused by the bench (category leagues): weeks a team lost, but would have won if it
-- had started benched players who played on days when a starter didn't play.
-- Needs daily data (core.player_days), so 2019 onward.
--
-- Each day: open slots = starters who didn't play. Benched players (not IR) who did play fill
-- those slots, best first (ranked by PTS+REB+AST+3PM+STL+BLK-TO). Their stats are added to
-- the team's weekly totals, and the 9 categories are scored again against the opponent's
-- actual totals. Positions are ignored, so a few of these swaps may not have been legal.
-- Starters who played are never swapped out, so this counts only empty slots, not bad choices.
WITH params AS (
    SELECT NULL::int AS season          -- e.g. 2027 for one season, NULL for all
),
days AS (
    SELECT pd.*,
           count(*) FILTER (WHERE pd.is_starter AND NOT pd.played)
               OVER (PARTITION BY pd.season, pd.scoring_period, pd.team_id) AS open_slots
    FROM core.player_days pd
    CROSS JOIN params p
    WHERE p.season IS NULL OR pd.season = p.season
),
bench AS (
    SELECT d.*,
           row_number() OVER (
               PARTITION BY d.season, d.scoring_period, d.team_id
               ORDER BY (coalesce((d.stats->>'PTS')::numeric, 0) + coalesce((d.stats->>'REB')::numeric, 0)
                       + coalesce((d.stats->>'AST')::numeric, 0) + coalesce((d.stats->>'3PM')::numeric, 0)
                       + coalesce((d.stats->>'STL')::numeric, 0) + coalesce((d.stats->>'BLK')::numeric, 0)
                       - coalesce((d.stats->>'TO')::numeric, 0)) DESC) AS rk
    FROM days d
    WHERE d.lineup_slot = 'BE' AND d.played AND d.open_slots > 0
),
added AS (       -- stats the team left on the bench, per team per matchup
    SELECT season, matchup_id, team_id,
           count(*)                                       AS games_left_on_bench,
           sum(coalesce((stats->>'PTS')::numeric, 0))     AS pts,
           sum(coalesce((stats->>'REB')::numeric, 0))     AS reb,
           sum(coalesce((stats->>'AST')::numeric, 0))     AS ast,
           sum(coalesce((stats->>'3PM')::numeric, 0))     AS tpm,
           sum(coalesce((stats->>'STL')::numeric, 0))     AS stl,
           sum(coalesce((stats->>'BLK')::numeric, 0))     AS blk,
           sum(coalesce((stats->>'TO')::numeric, 0))      AS tov,
           sum(coalesce((stats->>'FGM')::numeric, 0))     AS fgm,
           sum(coalesce((stats->>'FGA')::numeric, 0))     AS fga,
           sum(coalesce((stats->>'FTM')::numeric, 0))     AS ftm,
           sum(coalesce((stats->>'FTA')::numeric, 0))     AS fta
    FROM bench
    WHERE rk <= open_slots
    GROUP BY season, matchup_id, team_id
),
totals AS (      -- actual weekly totals, one row per team per matchup
    SELECT season, matchup_id, team_id,
           max(value) FILTER (WHERE stat_name = 'PTS') AS pts,
           max(value) FILTER (WHERE stat_name = 'REB') AS reb,
           max(value) FILTER (WHERE stat_name = 'AST') AS ast,
           max(value) FILTER (WHERE stat_name = '3PM') AS tpm,
           max(value) FILTER (WHERE stat_name = 'STL') AS stl,
           max(value) FILTER (WHERE stat_name = 'BLK') AS blk,
           max(value) FILTER (WHERE stat_name = 'TO')  AS tov,
           max(value) FILTER (WHERE stat_name = 'FGM') AS fgm,
           max(value) FILTER (WHERE stat_name = 'FGA') AS fga,
           max(value) FILTER (WHERE stat_name = 'FTM') AS ftm,
           max(value) FILTER (WHERE stat_name = 'FTA') AS fta
    FROM core.matchup_categories
    GROUP BY season, matchup_id, team_id
),
weeks AS (
    SELECT tm.season, tm.matchup_period, tm.team_id, tm.result,
           coalesce(a.games_left_on_bench, 0) AS games_left_on_bench,
           c.wins, c.losses
    FROM core.team_matchups tm
    JOIN core.seasons s ON s.season = tm.season
    JOIN totals me      ON (me.season, me.matchup_id, me.team_id)    = (tm.season, tm.matchup_id, tm.team_id)
    JOIN totals opp     ON (opp.season, opp.matchup_id, opp.team_id) = (tm.season, tm.matchup_id, tm.opponent_id)
    LEFT JOIN added a   ON (a.season, a.matchup_id, a.team_id)       = (tm.season, tm.matchup_id, tm.team_id)
    CROSS JOIN LATERAL (
        -- the 9 categories again, with the bench stats added (higher is better except TO)
        SELECT count(*) FILTER (WHERE (mine > theirs AND NOT low) OR (mine < theirs AND low)) AS wins,
               count(*) FILTER (WHERE (mine < theirs AND NOT low) OR (mine > theirs AND low)) AS losses
        FROM (VALUES
            (me.pts + coalesce(a.pts, 0), opp.pts, false),
            (me.reb + coalesce(a.reb, 0), opp.reb, false),
            (me.ast + coalesce(a.ast, 0), opp.ast, false),
            (me.tpm + coalesce(a.tpm, 0), opp.tpm, false),
            (me.stl + coalesce(a.stl, 0), opp.stl, false),
            (me.blk + coalesce(a.blk, 0), opp.blk, false),
            (me.tov + coalesce(a.tov, 0), opp.tov, true),
            ((me.fgm + coalesce(a.fgm, 0)) / nullif(me.fga + coalesce(a.fga, 0), 0), opp.fgm / nullif(opp.fga, 0), false),
            ((me.ftm + coalesce(a.ftm, 0)) / nullif(me.fta + coalesce(a.fta, 0), 0), opp.ftm / nullif(opp.fta, 0), false)
        ) v(mine, theirs, low)
    ) c
    WHERE s.scoring_type IN ('H2H_CATEGORY', 'H2H_MOST_CATEGORIES')
      AND tm.result IS NOT NULL                 -- skip matchups still in progress
)
SELECT
    t.team_name                                                         AS team_name,   -- most recent name
    m.display_name                                                      AS manager,     -- most recent owner
    count(DISTINCT w.season)                                            AS seasons,
    count(*) FILTER (WHERE w.result = 'L')                              AS losses,
    count(*) FILTER (WHERE w.result = 'L' AND w.wins > w.losses)        AS losses_to_bench,
    count(*) FILTER (WHERE w.result = 'L' AND w.wins = w.losses)        AS losses_to_tie,
    sum(w.games_left_on_bench)                                          AS games_left_on_bench
FROM weeks w
JOIN (   -- each team's name and owner from its latest season
    SELECT DISTINCT ON (team_id) team_id, team_name, primary_owner_id
    FROM core.teams
    ORDER BY team_id, season DESC
) t ON t.team_id = w.team_id
LEFT JOIN core.members m ON m.member_id = t.primary_owner_id
GROUP BY t.team_id, t.team_name, m.display_name
ORDER BY losses_to_bench DESC, games_left_on_bench DESC;
