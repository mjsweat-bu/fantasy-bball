-- core.player_form: each player's recent form as it stood BEFORE each game.
-- Why it exists: the model learns "here's what we knew before game G -> here's what he did in G".
--   It feeds core.training_player_games, and a rolling average on its own is the step-2 baseline.
-- Destination: sql/build/11_core_player_form.sql, primary key = your answer to decision A.
--
-- ============================================================================================
-- DECIDE BEFORE YOU WRITE: answer each one in a comment right here, in a sentence.
--
--   A. Grain. One row per ___ per ___. (Hint: how many different "last 5" averages does
--      Brunson have in one season?)
--
--   B. Season boundaries. Should a window reach back into last season?
--      Think about it separately for "last 5 games" and "season so far". You may decide
--      differently for each, and that's fine, as long as you can say why.
--
--   C. Trades. 399 player-seasons have games for more than one team. If a player is traded
--      in January, should his "last 5 games" reset? What does that tell you about whether
--      `team` belongs in the PARTITION BY?
--
--   D. The window for game G. Before writing any frame, say in words which games belong in
--      game G's "last 5". Is game G itself one of them? (What do you know at 6pm on game day?)
-- ============================================================================================

-- STEP 1 (done for you): the games to roll over.
WITH games AS (
    SELECT
        season, game_id, game_date,
        nba_player_id, espn_player_id, player_name,
        team, opponent, is_home,
        minutes, pts, reb, ast, stl, blk, tov, fg3m, fgm, fga, ftm, fta
    FROM core.player_games
    -- Regular season only: it's what fantasy counts, and playoff minutes and intensity differ.
    -- Remember WHERE runs BEFORE window functions: playoff games are removed first, so they
    -- never land in anyone's window. Here that's on purpose. (Same rule that turned NYK's
    -- Nov 22 into NULL in the visualization.)
    WHERE season_type = 'Regular Season'
    -- Note: a player only has a row when he appeared. Missed games (injury, rest, DNP) are
    -- not rows, so "last 5 games" means his last 5 games PLAYED, not his team's last 5.
),

-- YOUR TURN, building on games. Hints name the concept, not the code.
--
--   games_before       how many games he has already played this season.
--                      Opening night should be 0, not 1. Which frame gives that?
--
--   pts_last_5         average points over the previous 5 games.
--                      Frame: ROWS BETWEEN ? PRECEDING AND ? PRECEDING. Fill in the numbers by
--                      counting rows on your fingers. Off-by-one here is the classic bug.
--   pts_last_10        same idea, a longer window.
--   pts_season         season-so-far average before this game. Where does that frame START?
--
--   min_last_5         minutes. Why might minutes matter more than any other stat? (Every
--                      counting stat depends on time on the floor.)
--
--   reb / ast / stl / blk / tov / fg3m, last 5
--                      the same pattern as pts. Once pts works, look up the named WINDOW clause
--                      in Postgres so you write the PARTITION BY / ORDER BY / frame only once.
--
--   fg_pct_last_5      careful. Is the AVERAGE of 5 nightly FG% the same as total makes divided
--                      by total attempts? Compare a 1-for-1 night with a 10-for-25 night.
--                      Hint: two sums over the same frame. What if he took zero shots?
--   ft_pct_last_5      same.
--
--   DIAGNOSTIC (delete before production):
--   games_in_last_5    count(*) over the same frame as pts_last_5. Look at it on a player's
--                      1st, 2nd and 3rd games. What does it tell you about how "last 5" behaves
--                      early in a season?
form AS (
    SELECT
        g.*
        -- your columns here
    FROM games g
)

-- Preview: Jalen Brunson's 2026-27 season.
SELECT *
FROM form
WHERE nba_player_id = 1628973 AND season = 2027
ORDER BY game_date
LIMIT 50;

-- KNOWN-ANSWER CHECK (verified against the raw game logs):
--   Brunson, game 0022600218 (2026-11-11 vs CLE), his 11th game of the season:
--     games_before   = 10
--     pts_last_5     = 26.00     (games 6-10: 24, 19, 33, 31, 23)
--     pts_season     = 25.90
--     fg_pct_last_5  = 0.439     (43 makes / 98 attempts)
--     His points in that game were 27. If pts_last_5 includes the 27, what number do you get?
--
--   Brunson, opening night 0022600002 (2026-10-20 vs PHI):
--     pts_last_5 is either NULL or 22.60 (his last 5 games of 2025-26), depending on your
--     decision B. Write down which one you expect, and why, BEFORE you run it.
