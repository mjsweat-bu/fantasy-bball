---
name: matchup-plan
description: Plan a head-to-head week against this week's (or next week's) opponent. Says who to start each day to beat that specific opponent, and which free agents to add (when, and who to drop) to fill schedule and position gaps and swing close categories. Use when the user asks about their matchup or opponent, start/sit, lineups, streaming, the waiver wire, pickups, or adds and drops for a week.
---

# Matchup plan: lineups + waiver wire for one H2H week

The engine is `analysis/daily_lineup.py` (method in its docstring and in the README section
"Daily lineup report"). It reads ESPN live, so lineups, injury tags, the week's totals and free
agents are current. This skill runs it, sanity-checks the output and turns it into a short plan.
**Read-only:** it never changes anything on ESPN. The user makes the moves.

## 1. Pick the week

- Default is the current matchup. Also preview `--week <current + 1>` when it's Friday to Sunday,
  when this week is all but decided (P(win week) above 90% or below 10%), or when the user asks
  about next week. Next week's pickups can be planned days ahead.
- Never derive a week from a weekday. The script asks ESPN which days belong to which week.

## 2. Run it

```bash
.venv/bin/python analysis/daily_lineup.py --team 4 [--week N]
```

On Windows: `.venv\Scripts\python analysis\daily_lineup.py ...`. The league id comes from
`ESPN_LEAGUE_ID` in `.api.env`. Where that file doesn't exist (a cloud session), pass
`--league <id>`. Ask the user for the id; don't commit it.

| Flag | Use |
|---|---|
| `--week N` | Preview a later matchup: that opponent, all its days, totals from zero, today's rosters. |
| `--avail "Name=0.5"` | Chance he plays each game. Use it for injury news newer than ESPN's tag. Repeatable. |
| `--keep "A" "B"` | Never suggest dropping these players. |
| `--out file.md` | Also write the report to a file. |

- It takes about 10 seconds, mostly ESPN requests.
- If an ESPN request fails, retry once. A cloud environment must allow
  `lm-api-reads.fantasy.espn.com`.
- `NO_GAMES: ...` means there are no NBA games today, or no matchup. To plan the rest of the
  week anyway, pass `--week` with the current week number.

## 3. Sanity-check before answering

- **Drops in a keeper league.** The drop pool is the weakest players by season-long value (injury
  ignored). A young or long-term player can still land there. Before you recommend a drop, ask
  yourself whether it gives up future value. If it might, name the player and ask the user, or
  rerun with `--keep`. Never drop a player only to stream unless the user is fine with it.
- **IR.** Two IR slots. If a bench player is OUT and an IR slot is free, moving him there opens a
  roster spot without a drop. "Healthy on IR" in the report means a player should be activated.
- **Timing.** "When" is the best day to make the move; "(or by ...)" is the latest day that's just
  as good. Waiting keeps options open, but someone else may claim the player. After today's
  first tip, adds count from tomorrow. Players on waivers aren't planned (their claim clears at
  an unknown time). The league note gives the add limit. Lineups lock one game at a time.
- **What the model assumes.** The opponent starts their best 9-cat lineup every day and makes no
  pickups. Categories are treated as independent. Treat a change in P(win week) under 2 points as
  a coin flip.

## 4. Answer

Keep it short and lead with the result:

1. **Headline:** opponent, P(win week), projected categories.
2. **Categories:** the high-swing ones to play for, and the decided ones to ignore. For example:
   "REB, FG% and TO decide this week. BLK is gone, so don't chase it."
3. **Today's lineup:** the moves only ("move in", "move to bench", optional sits that protect
   FG%, FT% or TO), plus DTD/Q players to recheck before tip-off.
4. **Rest of the week:** days with open slots (the gaps a pickup fills) and days with players
   who have games but sit on the bench.
5. **Moves:** each add as *day: add X, drop Y*, with P(win week) before → after and the
   categories he helps. Give the "or by" day when there's slack.
6. **Caveats:** in one line, anything you changed with `--avail` / `--keep`.

Offer to rerun with news the user gives you, such as "Garland is questionable" or "don't drop
Proctor".
