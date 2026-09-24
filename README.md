# Whop clipper — cloud publisher

Posts already-approved clips to YouTube / Instagram / TikTok on a schedule, then submits each post
on Whop inside the campaign's deadline. Runs on GitHub Actions, so it works while your PC is off.

Clips are **produced locally** (GPU transcription, rendering, AI moment picking and the compliance
audit all run on your machine and your Claude subscription). Only approved, audited clips reach the
`queue/` folder here. This repo does no editing and no AI.

## Daily loop

| Where | What | Command |
|---|---|---|
| Your PC | make + audit clips, approve them | `python -m clipper clip …`, `review`, `approve` |
| Your PC | push the batch to the cloud | `python -m clipper export-queue` then commit + push this repo |
| Cloud | post + submit on schedule | fired by cron-job.org, nothing to do |

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

## When something breaks

- **Whop submission fails / "session is stale"** — run `python -m clipper export-session` locally and
  update the `WHOP_SESSION` secret. An unsubmitted post earns nothing, so this is the alarm that matters.
- **YouTube stops working after ~7 days** — the Google OAuth app fell back to "Testing" mode. Publish it
  to production in the Google Auth Platform console.
- **TikTok posts appear as drafts** — Post for Me refused a direct post; open TikTok and publish the
  draft with the caption saved beside the clip.
- **"nothing queued"** — the local producer hasn't pushed new clips. Run a local batch.

Proof screenshots of each Whop submission are attached to every workflow run as artifacts.
