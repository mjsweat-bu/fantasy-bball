# Data contracts

What each raw table promises, so you can build `core.*` tables on top of it.
Raw tables are filled by the loaders in `src/fbb/` and are never rebuilt by `sql/build`;
`core.*` tables are rebuilt from them on every refresh.

---

## raw.espn_matchup_days

**Grain:** one row per fantasy day (ESPN "scoring period") per season, for the whole season,
including days not played yet.

| Column | Type | Notes |
|---|---|---|
| `season` | int | **PK.** ESPN season = year the NBA season ends (2026-27 → 2027) |
| `scoring_period` | int | **PK.** Day number, 1 = opening night |
| `matchup_period` | int | The fantasy week that day belongs to. Null = a past day with no fantasy games |
| `fetched_at` | timestamptz | When the row was last written |

**Joins to**
- `raw.espn_pro_schedule` / your `core.team_schedule`: on `(season, scoring_period)`
- `core.matchups`, `core.team_matchups`: on `(season, matchup_period)`
- `core.player_days`: on `(season, scoring_period)`

**Freshness**
- Played days are copied from the box-score load; future days are asked of ESPN once.
- Future days ESPN can't place yet (2027: days 119-146, the playoff rounds before the
  bracket exists) are missing, not null, and are re-asked on every refresh until they appear.

**Gotchas**
- Weeks are not always 7 days: opening week is short, and playoff rounds span 2 weeks
  (`settings.scheduleSettings.playoffMatchupPeriodLength`). Never derive a week from a weekday.
- `scoring_period` is a fantasy day, not a calendar date. Get the date from the pro schedule.

---

## raw.espn_pro_schedule (input for core.team_schedule)

**Grain:** one row per season; `payload` holds the full NBA schedule as JSON, future games included.

| Column | Notes |
|---|---|
| `season` | **PK** |
| `payload` | JSON, see paths below |

**JSON paths**
```
payload -> 'settings' -> 'proTeams'                    array, one element per NBA team (plus FA, id 0)
  team ->> 'id'                                        ESPN pro team id (join ref.pro_teams.pro_team_id)
  team -> 'proGamesByScoringPeriod'                    object: { "<scoring period>": [game, ...] }
    game ->> 'id'                                      ESPN game id
    game ->> 'date'                                    start time, epoch milliseconds (UTC)
    game ->> 'homeProTeamId' / 'awayProTeamId'
    game ->> 'scoringPeriodId'                         the fantasy day
```

**Gotchas**
- Each game is listed under **both** teams. Unnesting gives one row per team per game,
  which is what a team schedule wants, but counting games across all rows doubles them.
- `date` is UTC. Convert to Eastern before taking the date, or late West-coast games land
  on the next day: `(to_timestamp((g->>'date')::bigint / 1000.0) AT TIME ZONE 'America/New_York')::date`
- Unnesting pattern (same one `sql/build/09_core_player_games.sql` uses):
  `jsonb_array_elements(payload #> '{settings,proTeams}')` → `jsonb_each(team -> 'proGamesByScoringPeriod')` → `jsonb_array_elements(each.value)`
- ESPN team abbreviations differ from the NBA's for two teams (PHL/PHI, PHO/PHX).
  Use `core.nba_team_abbrev()` when joining to `core.player_games`.
