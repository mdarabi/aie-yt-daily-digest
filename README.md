# aie-yt-daily-digest

Daily email digest of new uploads on the [AI Engineer YouTube channel](https://www.youtube.com/@aiDotEngineer/videos).

Every morning at 6:00 (cron on an always-on Mac) it finds videos uploaded since the
last run, pulls each transcript, summarizes them with `claude -p` (your Claude
subscription — no API key), groups them by theme, and emails the digest via
[Resend](https://resend.com).

Each video entry in the email:

1. **Title** (linked)
2. **Problem** — what the talk sets out to solve
3. **Solution** — the proposed ideas/approaches/technologies, high level
4. **▶ Watch** link (with duration)
5. **Links from the description** (channel boilerplate filtered out)

## How it works

```
RSS feed ──► new-video discovery ──► yt-dlp (metadata + captions) ──► claude -p
(15 newest,     │ seen-set dedup         one call per video             summaries +
 exact times)   │ uploads-playlist                                      theme grouping
                │ fallback when all 15                                       │
                │ entries are new                                            ▼
                └─ deferred retries                                   render ──► Resend
                   (live / captions pending)                                   │
                                                                    state committed only
                                                                    after a successful send
```

Robustness properties:

- **No video is silently lost.** A video is marked *seen* only after the digest
  containing it was sent. Crashed run → next run redoes it.
- **>15 uploads or missed days** — if every RSS entry is new, the channel's uploads
  playlist (uncapped) is walked until known territory is reached. The discovery
  window resumes from the last successful run (capped at 7 days of catch-up).
- **Caption lag** — brand-new videos without auto-captions yet are deferred by id
  and retried on later runs (up to 48h, then summarized from the description).
- **Live streams / premieres** are deferred until they've finished and processed.
- **Failures email you** (`ERROR_EMAILS=true`) and are logged to `logs/digest.log`.

No YouTube Data API is used — discovery is the public RSS feed; metadata and
captions come from `yt-dlp` (managed as a project dependency by uv).

## Setup

Requirements: [uv](https://docs.astral.sh/uv/), the `claude` CLI logged in to your
subscription, a Resend account with a verified sending domain.

```sh
uv sync                                # install deps into .venv
cp .env.example .env                   # if .env doesn't exist yet
$EDITOR .env                           # paste your RESEND_API_KEY
uv run pytest                          # sanity check (no network)
uv run aie-digest --test-latest 3 --dry-run   # full pipeline, prints preview, no email
uv run aie-digest --test-latest 3      # sends a real test digest
```

## Scheduling (daily 6:00)

```sh
scripts/install_launchd.sh            # installs a LaunchAgent firing daily at 6:00
scripts/install_launchd.sh 7 30      # ...or at a custom time (7:30)
```

The LaunchAgent (`~/Library/LaunchAgents/com.mdarabi.aie-yt-daily-digest.plist`)
is the macOS-native equivalent of cron; it fires at 6:00 local time, and if the
Mac is asleep at 6:00 the job runs on wake instead of being skipped. Manage it:

```sh
launchctl print "gui/$(id -u)/com.mdarabi.aie-yt-daily-digest"   # status
launchctl bootout "gui/$(id -u)/com.mdarabi.aie-yt-daily-digest" # disable
scripts/install_launchd.sh                                        # re-enable/update
```

Prefer classic cron instead? `scripts/install_cron.sh` installs a crontab entry
(`scripts/install_cron.sh "30 7 * * *"` for a custom schedule). The two installers
remove each other's entry, so the digest never runs twice. Note: modifying crontab
on macOS requires your terminal app to have Full Disk Access; the LaunchAgent does not.

**Run this on exactly one machine.** Each machine keeps its own local state,
so two machines running the schedule will both email the same videos daily.

### Setting up on a new machine

```sh
git clone https://github.com/mdarabi/aie-yt-daily-digest.git
cd aie-yt-daily-digest
uv sync
cp .env.example .env && $EDITOR .env    # paste RESEND_API_KEY (not in git)
# migrating? copy state/state.json from the old machine into state/
claude                                   # log the CLI in to your subscription once
uv run aie-digest --test-latest 1 --dry-run   # verify the plumbing
scripts/install_launchd.sh              # arm the daily 6:00
# ...then disable the schedule on the old machine (launchctl bootout ...)
```

## Commands

| Command | What it does |
|---|---|
| `uv run aie-digest` | Normal incremental run (what cron executes) |
| `uv run aie-digest --dry-run` | Full pipeline; writes `preview.html`/`preview.txt`, no email, no state change |
| `uv run aie-digest --test-latest N` | Process the N most recent uploads regardless of state; sends email; no state change |
| `uv run aie-digest --backfill-hours H` | Widen the discovery window to the last H hours |
| `uv run aie-digest --no-send` | Run without sending or saving state |

## Configuration (.env)

| Variable | Default | Meaning |
|---|---|---|
| `RESEND_API_KEY` | — | Required for sending |
| `EMAIL_FROM` | `AIE Digest <digest@example.com>` | Must be on a Resend-verified domain |
| `EMAIL_TO` | — (required) | Comma-separate for multiple recipients |
| `CHANNEL_ID` | `UCLKPca3kwwd-B59HNr-_lvA` | @aiDotEngineer |
| `CLAUDE_MODEL` | `sonnet` | Passed to `claude -p --model` (`sonnet`/`opus`/`haiku`/full id) |
| `CLAUDE_BIN` | auto-detect | Absolute path to the claude binary |
| `LOOKBACK_HOURS` | `26` | Window for the first run |
| `MAX_CATCHUP_HOURS` | `168` | Cap on catch-up after missed days |
| `CAPTION_GRACE_HOURS` | `48` | How long to wait for auto-captions before going description-only |
| `SEND_EMPTY_DIGEST` | `false` | Email a short note on days with no new videos |
| `ERROR_EMAILS` | `true` | Email you when a run fails |
| `INCLUDE_SHORTS` | `false` | Include videos shorter than 75s |

## Runtime state

`state/state.json` (which videos were already emailed + deferred retries) is
**local and gitignored** — the system is designed to run on a single machine.
Deleting it resets the digest: the next run treats the last `LOOKBACK_HOURS`
as new. When moving to a new machine, copy the file over (or accept one
digest's worth of re-sent videos).

Other runtime files (also gitignored): `logs/digest.log`, `logs/cron.log`,
`preview.html`/`preview.txt`.

## Troubleshooting

- **No email arrived** — check `logs/cron.log` and `logs/digest.log`. If
  `ERROR_EMAILS=true` and Resend itself works, failures also arrive by email.
- **`claude -p` auth errors** — the CLI login can expire; run `claude` once
  interactively to re-authenticate. Cron uses the same stored login. (A stray
  `ANTHROPIC_API_KEY` is deliberately ignored so runs always bill the subscription.)
- **yt-dlp errors after months of working** — YouTube changed something;
  upgrade with `uv lock --upgrade-package yt-dlp && uv sync`.
- **Resend 403 about the from address** — the sending domain isn't verified (or the
  key belongs to another team). Verify your sending domain in the Resend dashboard.
- **Duplicate or missing videos** — inspect `state/state.json`; deleting it makes
  the next run treat the last `LOOKBACK_HOURS` as new.
