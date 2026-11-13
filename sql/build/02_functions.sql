-- Helpers used by the core build scripts.

-- ESPN keys stats by numeric id ({"0": 21, "6": 9, ...}). Rename the keys to stat
-- names ({"PTS": 21, "REB": 9, ...}) using ref.stats; unknown ids keep their number.
CREATE OR REPLACE FUNCTION core.stat_names(stats jsonb) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    SELECT coalesce(jsonb_object_agg(coalesce(s.stat_name, e.key), e.value), '{}'::jsonb)
    FROM jsonb_each(coalesce(stats, '{}'::jsonb)) e
    LEFT JOIN ref.stats s ON s.stat_id = e.key
$$;

-- ESPN timestamps are epoch milliseconds.
CREATE OR REPLACE FUNCTION core.from_epoch_ms(ms bigint) RETURNS timestamptz
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN ms IS NULL OR ms = 0 THEN NULL ELSE to_timestamp(ms / 1000.0) END
$$;
