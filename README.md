# Overwatch Meta Tracker

Tracks which Overwatch heroes are strongest right now, and which are rising or falling.

Each run pulls hero **pick rate, win rate and ban rate** from the open-source
[OverFast API](https://overfast-api.tekrop.fr), which mirrors Blizzard's public hero statistics. It saves a dated
snapshot into SQLite (`data/meta.db`) and rebuilds a static dashboard at `docs/index.html` with:

- top heroes per role, with S–D tiers
- biggest win-rate risers and fallers versus about a week earlier
- a sortable, filterable table of every hero

**Stack:** Python 3.10+ (standard library only), SQLite, HTML/CSS, GitHub Actions

## Run it

```bash
python tracker.py                          # competitive · PC · Americas · all ranks
python tracker.py --region europe
python tracker.py --division grandmaster   # bronze … grandmaster
python tracker.py --gamemode quickplay --platform console
python tracker.py --no-fetch               # rebuild the dashboard from saved data only
```

Then open `docs/index.html`.

## How the meta score works

```
score = (win rate − 50) × 2  +  10 × √(popularity ÷ role-average popularity)
popularity = pick rate + ½ × ban rate
```

Win rate is the main signal. The popularity term rewards heroes that are winning **and** being played or banned
a lot, so a niche hero with a high win rate over a tiny sample doesn't top the list. Tiers are percentiles within each role.

## Automatic daily snapshots

`.github/workflows/update.yml` runs the tracker every day on GitHub Actions and commits the new data. To publish the
dashboard, turn on **Settings → Pages → Deploy from branch → `main` / `docs`**. The dashboard will then be live at
`https://ryanstormrage.github.io/ow-meta-tracker/`.

## Database

| table | what's in it |
|---|---|
| `heroes` | key, name, role, portrait URL |
| `snapshots` | one row per hero per day per scope (platform, gamemode, region, division) |

```sql
-- Ana's win rate over time
SELECT taken_on, winrate FROM snapshots WHERE hero = 'ana' AND division = 'all' ORDER BY taken_on;
```
