-- Draft picks (raw.espn_league, kind = 'draft') and transactions (raw.espn_transactions).

DROP TABLE IF EXISTS core.draft_picks CASCADE;
CREATE TABLE core.draft_picks AS
SELECT
    l.season,
    (pk ->> 'overallPickNumber')::int      AS overall_pick,
    (pk ->> 'roundId')::int                AS round,
    (pk ->> 'roundPickNumber')::int        AS round_pick,
    (pk ->> 'teamId')::int                 AS team_id,
    (pk ->> 'playerId')::int               AS player_id,
    pl.player_name,
    (pk ->> 'bidAmount')::int              AS bid_amount,       -- auction drafts
    (pk ->> 'keeper')::boolean             AS is_keeper,
    nullif((pk ->> 'nominatingTeamId')::int, 0) AS nominating_team_id
FROM raw.espn_league l
CROSS JOIN LATERAL jsonb_array_elements(l.payload #> '{draftDetail,picks}') pk
LEFT JOIN core.players pl ON pl.player_id = (pk ->> 'playerId')::int
WHERE l.kind = 'draft';

DROP TABLE IF EXISTS core.transactions CASCADE;
CREATE TABLE core.transactions AS
SELECT DISTINCT ON (t ->> 'id')
    r.season,
    t ->> 'id'                                         AS transaction_id,
    (t ->> 'scoringPeriodId')::int                     AS scoring_period,
    (t ->> 'teamId')::int                              AS team_id,
    t ->> 'type'                                       AS type,     -- FREEAGENT, WAIVER, TRADE_ACCEPT, ROSTER, ...
    t ->> 'status'                                     AS status,   -- EXECUTED, CANCELED, ...
    (t ->> 'bidAmount')::int                           AS bid_amount,
    core.from_epoch_ms(coalesce((t ->> 'processDate')::bigint,
                                (t ->> 'acceptedDate')::bigint,
                                (t ->> 'proposedDate')::bigint)) AS happened_at,
    t ->> 'relatedTransactionId'                       AS related_transaction_id,
    t -> 'items'                                       AS items
FROM raw.espn_transactions r
CROSS JOIN LATERAL jsonb_array_elements(r.payload -> 'transactions') t
ORDER BY t ->> 'id', r.fetched_at DESC;
ALTER TABLE core.transactions ADD PRIMARY KEY (transaction_id);

DROP TABLE IF EXISTS core.transaction_items CASCADE;
CREATE TABLE core.transaction_items AS
SELECT
    t.season,
    t.transaction_id,
    t.type                                            AS transaction_type,
    t.status,
    t.happened_at,
    i ->> 'type'                                      AS item_type,   -- ADD, DROP, LINEUP, TRADE
    (i ->> 'playerId')::int                           AS player_id,
    pl.player_name,
    nullif((i ->> 'fromTeamId')::int, 0)              AS from_team_id,  -- 0 / null = free agency
    nullif((i ->> 'toTeamId')::int, 0)                AS to_team_id,
    fs.slot_name                                      AS from_slot,
    ts.slot_name                                      AS to_slot
FROM core.transactions t
CROSS JOIN LATERAL jsonb_array_elements(coalesce(t.items, '[]'::jsonb)) i
LEFT JOIN core.players pl      ON pl.player_id = (i ->> 'playerId')::int
LEFT JOIN ref.lineup_slots fs  ON fs.slot_id = (i ->> 'fromLineupSlotId')::int
LEFT JOIN ref.lineup_slots ts  ON ts.slot_id = (i ->> 'toLineupSlotId')::int;
