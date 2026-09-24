# Whop clipper — cloud producer + publisher

Everything runs on GitHub Actions, so it works while your PC is off.

| Workflow | When | What |
|---|---|---|
| `produce.yml` | daily 08:00 UTC | scout Whop → read each brief + its reference docs/PDFs → join fitting campaigns → download footage → transcribe → AI picks moments → render → **compliance audit of every clip against every rule** → auto-approve only clips the audit clears for unattended posting → queue |
| `publish.yml` | each posting slot (cron-job.org) | post one queued clip per account → submit it on Whop inside the deadline |

Clips the audit doesn't clear are **held**, never posted. They're attached to the produce run
(artifact `clips-<run>`) and listed in the run summary with the reason. Queued videos live as assets of
the `queue` release, not in git; a clip posted to all its accounts is retired automatically.

The producer code is a copy of the PC's `clipper/` package in `producer/`. After changing code or
`config.yaml` on the PC, run `python -m clipper sync-producer`. The cloud owns the database
(`producer/data/state.db`); `python -m clipper pull-state` brings it back to the PC.

## Safety checks on every post (all accounts, all platforms)

1. **Never twice.** Before posting, the publisher asks the platform itself (YouTube uploads, Instagram media,
   TikTok feed) whether the clip is already on the account, and claims the slot in the repo before uploading,
   so overlapping runs can't double-post.
2. **Only what can be submitted.** Before posting, it opens the campaign's Whop Submit dialog (and cancels
   it). If Whop isn't reachable, or the campaign has ended, the clip is **held for a later slot** instead of
   posted, and you get a Telegram note. Nothing is posted that would need a hand submission.
3. **Submission retries** run for up to ~25 minutes of Whop's 30-minute window. If the first attempt fails
   you're told on Telegram straight away.

## Schedule (cron-job.org)

Create three jobs. Each one is a POST to:

```
https://api.github.com/repos/cloudstrive6/whop-clipper-publisher/dispatches
```

Headers:

```
Accept: application/vnd.github+json
Authorization: Bearer <GITHUB_PAT>
Content-Type: application/json
```

Body, per job:

```json
{"event_type": "publish-youtube"}      // or publish-instagram / publish-tiktok
```

Times (UTC) — spaced evenly inside US evening prime time, when a US-heavy first audience helps the
Tier-1 rules most campaigns impose:

| Job | Times (UTC) | US Eastern | Posts/account/day |
|---|---|---|---|
| publish-youtube | 16:00, 19:00, 22:00, 01:00 | 12:00, 15:00, 18:00, 21:00 | 4 |
| publish-instagram | 17:00, 20:00, 23:00 | 13:00, 16:00, 19:00 | 3 |
| publish-tiktok | 16:30, 19:30, 22:30, 01:30 | 12:30, 15:30, 18:30, 21:30 | 4 |

The workflow also has a GitHub `schedule:` safety net 30 minutes after each slot; it skips anything
already posted, so a double fire is harmless.

The PAT needs only `Contents: read/write` and `Metadata: read` on this repo (fine-grained token).

## Secrets

| Secret | What it is |
|---|---|
| `YOUTUBE_TOKEN_YT_POGINGPANDA` / `_YT_ERICKNOX` / `_YT_MANIFESTATION` | OAuth token JSON per channel |
| `INSTAGRAM_TOKENS` | `{"handle": {"token": "...", "user_id": "..."}}` |
| `POSTFORME_API_KEY` | Post for Me project key (TikTok drafts, X) |
| `WHOP_SESSION` | Playwright storage_state for whop.com, from `python -m clipper export-session` |
| `CLAUDE_CODE_OAUTH_TOKEN` | from `claude setup-token` on the PC: the producer's AI runs on your Claude subscription |
| `SECRETS_PAT` | fine-grained token, this repo only, **Secrets: read and write**: lets runs store renewed credentials |

## Credentials renew themselves

- **Whop login** — every run that uses Whop stores the refreshed cookies back into `WHOP_SESSION`, and the
  daily run checks the login first. If Whop ever logs the automation out, you get a GitHub issue (emailed)
  telling you to run `python -m clipper login` + `sync-secrets` on the PC. That is the only step that can
  need you.
- **Instagram** — the daily run refreshes each 60-day token once a week and stores the new one.
- **YouTube** — refresh tokens don't expire while the Google app is published and the channel posts.
- **Post for Me, Claude token** — don't expire (`claude setup-token` tokens last a year).

## Before an account can earn

Every account must be connected **inside the Content Rewards app**: Discover → Content Rewards →
Settings → Connected accounts → Connect account. The Whop profile's "Social accounts" list is separate
and is **not** what the "Posted from one of your linked accounts" check uses.

## When something breaks

- **Whop submission fails / "session is stale"** — run `python -m clipper login` then
  `python -m clipper sync-secrets` on the PC. An unsubmitted post earns nothing, so this is the alarm that matters.
- **YouTube stops working after ~7 days** — the Google OAuth app fell back to "Testing" mode. Publish it
  to production in the Google Auth Platform console.
- **TikTok posts appear as drafts** — Post for Me refused a direct post; open TikTok and publish the
  draft with the caption saved beside the clip.
- **"rejected: Posted from one of your linked accounts"** — that account isn't connected inside the
  Content Rewards app (see above). The post can't be rescued once 30 minutes pass.
- **"nothing queued"** — the producer made nothing that passed the audit. Read the latest produce run's
  summary: it lists held clips, campaigns that need a human, and download errors.
- **Download errors on YouTube sources** — YouTube sometimes blocks GitHub's servers. Drive/Dropbox
  footage is unaffected; for YouTube-only campaigns run `python -m clipper clip <campaign>` on the PC.

Proof screenshots of each Whop submission are attached to every workflow run as artifacts.
