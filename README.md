# Fantasy basketball league database

ESPN fantasy basketball league data → Postgres (Docker) → SQL tables for notebooks and analysis.
Data comes from ESPN's fantasy API through [espn_api](https://github.com/cwendt94/espn-api).
Layout follows `cfb-project`: raw JSON tables, numbered `sql/build` scripts that rebuild `core.*`, and a `src/fbb` package.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set a password
cp .api.env.example .api.env    # league id, and cookies for a private league
docker compose up -d            # Postgres 16 on localhost:5433
```

**Private leagues** need two cookies from a logged-in browser session on fantasy.espn.com
(dev tools → Application → Cookies): `espn_s2` and `SWID` (keep the `{}` braces). Put them in `.api.env`.
They expire every few months; refresh them if loads start returning 401.

## Loading data

```bash
python -m fbb.refresh --history     # first time: every season the league has existed
python -m fbb.refresh               # daily: current season (new days + last 3 refetched)
python -m fbb.refresh --skip-load   # rebuild tables only
```

Seasons are named by the year they end (2026-27 = **2027**). Full detail (daily box scores,
transactions, player pool) is available from 2019; earlier seasons have league, schedule and draft only.
A full season is ~350 requests (one per day for box scores, one per day for transactions).

## Tables

| Table | Grain |
|---|---|
| `core.seasons` | season: league name, scoring type, team count, playoff settings |
| `core.scoring_items` | season × stat: categories or point values |
| `core.members` | ESPN user (manager) |
| `core.teams` | season × fantasy team: record, PF/PA, seed, final rank, moves, FAAB |
| `core.team_owners` | season × team × manager |
| `core.rosters` | roster as of the latest load |
| `core.matchups` / `core.team_matchups` | matchup / team × matchup (W/L, points, cats) |
| `core.matchup_categories` | team × matchup × category (category leagues) |
| `core.team_days` | team × day fantasy points |
| `core.player_days` | **team × player × day**: lineup slot, starter flag, fantasy points, stat line |
| `core.players` / `core.player_seasons` | NBA player / player × season (owner, % owned, ADP, ranks) |
| `core.player_stat_splits` | player × season × split (total, last 7/15/30, projected) |
| `core.draft_picks` | pick: round, team, player, auction bid, keeper |
| `core.transactions` / `core.transaction_items` | adds, drops, waivers (with FAAB bids), trades, lineup moves |

Stat lines are `jsonb` keyed by stat name (`stats->>'PTS'`); `ref.*` holds the ESPN id lookups.

```python
from fbb.db import read_query
read_query("standings.sql", season=2027)
```
