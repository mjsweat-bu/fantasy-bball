# fantasy-bball

ESPN fantasy basketball league + NBA box scores → Postgres (Neon) → SQL models → analysis.
Public repo that doubles as a portfolio: keep secrets, connection strings and private plans out of it.

## Workflow
- Every change goes on a branch and through a PR; never commit to `main` (it's protected).
  The owner merges. At the start of a session: `git checkout main && git pull`.
- Python runs from the venv: `.venv\Scripts\python ...` on Windows, `.venv/bin/python ...` elsewhere.
  A bare `python` gives "No module named fbb".
- Cloud sessions install the slim set: `python3 -m venv .venv && .venv/bin/pip install -r requirements-analysis.txt`
  (the environment's setup script does this). Pins must match `requirements.txt`; when you bump a
  version in one, bump it in the other (`tests/test_requirements.py` fails otherwise).
- Tests: `python -m pytest` (offline, no database needed).
- Keep answers short and lead with the result.

## Database
- Hosted on Neon. `DATABASE_URL` in `.env` (git-ignored) selects it; never print, log or commit it.
  Without it, code falls back to local Docker Postgres (`POSTGRES_*`, `docker compose up -d`).
- Roles: the owner role (read/write) is only for `fbb.refresh`. Analysis machines and cloud
  sessions use the read-only `analyst` role.
- **Analysis is read-only.** Never run INSERT/UPDATE/DELETE/DDL unless the owner asks.
- Read with `fbb.db.read_sql(sql)` / `read_query("file.sql", **params)`, not
  `pd.read_sql(sql, engine)`. They switch to Neon's HTTPS endpoint where port 5432 is blocked
  (Claude cloud sessions); `FBB_DB_TRANSPORT=tcp|http|auto` overrides.
- Pipeline: `raw.*` (JSON from ESPN / stats.nba.com, filled by `src/fbb/ingest*.py`, never rebuilt)
  → `sql/build/NN_*.sql` rebuild `core.*` and `ref.*` on every `python -m fbb.refresh`
  → `src/fbb/validate.py` checks run at the end. Ad-hoc queries live in `sql/analysis/`.
- Table grains: README "Tables". Raw JSON paths and join keys: `docs/data_contracts.md`.

## League rules (2027 season)
- "BALL DONT LIE HOE": 12 teams, H2H most categories, 17 regular-season weeks, 6-team playoffs,
  $100 FAAB. The owner's team is Ligma Sugma, `team_id = 4`.
- 9 categories: PTS REB AST 3PM STL BLK FG% FT% TO (TO: lower is better).
- Daily lineup: PG SG SF PF C UT UT; 9 bench (BE), 2 IR.
- Seasons are named by the year they end: 2026-27 = `2027` (`config.current_season()`).

## Query gotchas
- FG%/FT%: sum makes and attempts, then divide. Never average percentages.
- Names: `core.players` / `core.rosters` / `core.player_days` use `player_name` (not `full_name`).
- Team abbreviations: ESPN tables use PHL / PHO; NBA tables (`core.player_games`,
  `core.team_schedule`) use PHI / PHX.
- Stat names differ by source: ESPN `stats` jsonb keys are `'PTS'`, `'3PM'`, `'TO'`, `'FGM'`...
  (`stats->>'3PM'`); NBA columns are lowercase `pts`, `fg3m`, `tov`, `fgm`.
- ESPN ↔ NBA player ids: join through `ref.player_xwalk` (`espn_player_id`, `nba_player_id`);
  hand fixes go in `ref.player_xwalk_manual`.
- `scoring_period` is a fantasy day, not a date; `matchup_period` is a fantasy week. Weeks are not
  always 7 days (short opening week, 2-week playoff rounds). Never derive a week from a weekday.
- `core.team_schedule` lists every game twice (once per team); don't double-count games.
- `core.player_days` is team × player × day: bench days are rows too (`lineup_slot = 'BE'`);
  filter on `is_starter` / `played` for stats that counted.
