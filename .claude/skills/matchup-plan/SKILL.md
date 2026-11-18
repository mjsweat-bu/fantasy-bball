---
name: matchup-plan
description: Plan a head-to-head week against this week's (or next week's) opponent. Says who to start each day to beat that specific opponent, and which free agents to add (when, and who to drop) to fill schedule and position gaps and swing close categories. Use when the user asks about their matchup or opponent, start/sit, lineups, streaming, the waiver wire, pickups, or adds and drops for a week.
---

# Matchup plan: lineups + waiver wire for one H2H week

The engine is `analysis/matchup_plan.py` (method in its docstring and in the README section
"Matchup planner"). This skill runs it, sanity-checks the output and turns it into a short plan.
**Read-only:** it never changes anything on ESPN. The user makes the moves.

## 1. Pick the week

- Default is the current matchup period. Also run `--week <current + 1>` when it's Friday to Sunday,
  when the current week is all but decided (P(win) above 90% or below 10%), or when the user asks
  about next week. Waiver moves for next week can be planned days in advance.
- The user's team is `--team 4` (the default). Never derive a week from a weekday. The script reads
  the days of a week from `raw.espn_matchup_days`.

## 2. Check that it can run

```bash
.venv/bin/python -c "from fbb import config; print('db ok' if config.DATABASE_URL else 'no DATABASE_URL')"
```

- With `no DATABASE_URL` (for example a cloud session with no `.env`), the script can't read the
  league. Say so and stop. Don't make up projections from the Neon connector. The fix is a
  `.env` with the read-only `analyst` connection string. Never print, log or commit it.
- The script reports the data's age. Rosters, injuries and the days already played only update
  when the owner runs `python -m fbb.refresh` (owner role, on their machine). If the header warns
  that data is stale or that past days are simulated, put that first in the answer. Recent adds
  and drops, by either team, won't be in the data.

## 3. Run it

```bash
.venv/bin/python analysis/matchup_plan.py [--week N]
```

| Flag | Use |
|---|---|
| `--avail "Name=0.5"` | Chance he plays each game. Use it for injury news newer than the data, or for a player listed OUT who is due back. Repeatable. |
| `--keep "A" "B"` | Never suggest dropping these players. |
| `--drop "A"` | Always consider dropping this player. |
| `--drop-pool 3` | How many of the lowest-value players can be dropped. |
| `--max-adds 2` | Adds chained greedily. Each is kept only if it gains ≥1pp P(win) or ≥0.15 expected categories. |
| `--no-waivers` | Lineups only (faster). |
| `--today YYYY-MM-DD` | Plan as of another day. Assumes it's before the first tip-off. |
| `--sims 2000` | More sims means less noise (about ±1pp on ΔP at 2000). |

It runs in a few seconds.

## 4. Sanity-check before answering

- **Drops in a keeper league.** The drop pool is the lowest 9-cat-value players, ignoring injury.
  A young or long-term player can still land there. Before you recommend a drop, ask yourself
  whether it gives up future value. If it might, name the player and ask the user, or rerun with
  `--keep`. Never drop a player only to stream unless the user is fine with it.
- **IR.** Two IR slots. If a bench player is OUT and an IR slot is free, moving him to IR opens a
  roster spot without a drop. Say so.
- **Timing rules** (this league's settings): a free agent added before the day's first tip-off can
  play that day. After it, he starts tomorrow (the script checks this). Players on waivers are
  claims and take about a day. Lineups lock one game at a time, so a start/sit can change up to
  that player's tip-off.
- **What the model assumes.** The opponent starts their best 9-cat lineup every day and makes no
  pickups. Per-game projections are recent form shrunk toward last season. Bench players never
  score. Treat a ΔP under 2pp as a coin flip.

## 5. Answer

Keep it short and lead with the result:

1. **Headline:** opponent, P(win week), expected categories. Also whether the tuned lineups or the
   adds move P(win), and by how much.
2. **Categories:** the SWING ones to play for, and the LOCKED / LOST ones to ignore. For example:
   "REB and TO decide this week. BLK is gone, so don't chase it."
3. **Lineups:** only the decisions that matter, day by day:
   - bench players who have games ("next up first" is the order to sub in),
   - starts and sits that differ from a plain 9-cat ranking (`vs 9-cat default`), and any
     "hurts more than helps" sit, which protects FG%, FT% or TO,
   - open slots, which are the gaps a pickup should fill,
   - `*` players, who are less than 80% to play. Check their status before lock.
4. **Moves:** each add as *day: add X (team, games left), drop Y*, with ΔP(win) and the
   categories he helps. Give the "no later than" day when there's slack.
5. **Caveats:** in one line, data age and anything you changed with `--avail` / `--keep`.

Offer to rerun with news the user gives you, such as "Garland is questionable" or "don't drop
Proctor".
