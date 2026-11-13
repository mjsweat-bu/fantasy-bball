-- Standings for a season with manager names.
WITH params AS (
    SELECT 2027 AS season
)
SELECT
    t.team_name,
    m.display_name                    AS manager,
    t.wins, t.losses, t.ties,
    round(t.points_for, 1)            AS points_for,
    round(t.points_against, 1)        AS points_against,
    t.playoff_seed,
    t.final_rank,
    t.acquisitions, t.trades, t.faab_spent
FROM core.teams t
JOIN params p ON p.season = t.season
LEFT JOIN core.members m ON m.member_id = t.primary_owner_id
ORDER BY coalesce(t.final_rank, t.playoff_seed);
