"""CLI:  python -m clipper <command>

  login                         log in to Whop in the automation browser (once)
  scout                         find + rank campaigns (YouTube pay, budget left)
  campaigns                     list campaigns and their status
  analyze CAMPAIGN_ID           turn the scraped brief into a checklist
  join CAMPAIGN_ID              join / draft application (you confirm in browser)
  clip CAMPAIGN_ID URL|FILE…    download source(s), pick moments, render, QA
  review                        open the batch review page
  approve all|ID…  /  reject ID…
  youtube-auth                  connect @PogingPanda (OAuth, once)
  publish                       upload approved clips to YouTube
  submit                        submit posted clips on Whop (asks y/N each)
  status                        pipeline overview
  produce                       daily unattended run: scout -> analyze -> join -> clip -> audit -> queue
  sync-producer [--with-state]  copy code/config/references to the cloud repo and push
  pull-state                    copy the cloud producer's database back to this PC
  sync-secrets                  renew + upload every credential to the cloud repo
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import campaign_dir, cfg


def cmd_clip(campaign_id: str, *sources: str, limit: int | None = None) -> int:
    """Returns how many clips were rendered. `limit` caps the number of new clips (cloud runs)."""
    from . import moments, qa, render, source, transcribe
    from .config import IN_CI

    camp = db.get("campaigns", campaign_id) or {}
    checklist = camp.get("checklist")
    if not sources:
        sources = tuple((checklist or {}).get("source_assets", []))
        if not sources:
            sys.exit("No source given and the checklist lists no assets. Pass a URL or file.")
    cdir = campaign_dir(campaign_id)
    videos: list[Path] = []
    for s in sources:
        videos += [Path(s)] if Path(s).exists() else source.download(s, cdir / "sources")
    videos = _dedupe(videos)
    # by file name: the same footage has a Windows path locally and a Linux path in the cloud
    already = {Path(c["source"]).name for c in db.rows("clips", "campaign_id=?", campaign_id)}
    videos = [v for v in videos if v.name not in already]
    durations = {v.name: render.probe_summary(v)["duration"] for v in videos}
    short = [v for v in videos if 7 <= durations[v.name] <= 180]   # pre-cut clips
    long_ = [v for v in videos if durations[v.name] > 180]         # podcasts / VODs
    use_music = not (checklist or {}).get("no_music")

    disclosure = _disclosure(checklist)
    made = 0

    def finish(video: Path, segs: list[dict], m) -> None:
        cid = f"{campaign_id[:24]}-{source.slug(video.stem)[:20]}-{int(m.start)}"
        out = cdir / "clips" / f"{cid}.mp4"
        render.render(video, segs, m.start, m.end, m.hook_text, out, music=use_music, disclosure=disclosure)
        meta = _enforce_caption(m.model_dump(), checklist)
        meta["disclosure"] = disclosure  # the audit states exactly what was burned in
        report = qa.check(out, meta, checklist)
        db.upsert("clips", {"id": cid, "campaign_id": campaign_id, "source": str(video), "start": m.start,
                            "end": m.end, "meta": meta, "file": str(out), "qa": report,
                            "status": "rendered" if report["ok"] else "qa_failed"})
        nonlocal made
        made += 1
        print(f"   [{'OK ' if report['ok'] else 'FIX'}] {cid}  {m.end - m.start:.0f}s  {m.hook_text!r}  {report['issues']}")

    if short:
        print(f"\n== batch of {len(short)} short clips: transcribing…")
        segs = {v.name: transcribe.transcribe(v) for v in short}
        n = min(len(short), int(cfg()["editing"].get("clips_per_batch", 8)), limit or 99)
        print(f"   picking the best {n} moments across the batch…")
        by_name = {v.name: v for v in short}
        for m in moments.pick_multi(segs, durations, checklist, n):
            finish(by_name[m.source], segs[m.source], m)
    for video in long_:
        if limit is not None and made >= limit:
            break
        print(f"\n== {video.name}")
        long_model = cfg()["editing"].get("whisper_model_long_ci") if IN_CI and durations[video.name] > 1200 else None
        segs = transcribe.transcribe(video, long_model)
        print(f"   transcript: {len(segs)} segments; picking moments…")
        for m in moments.pick(segs, checklist, None if limit is None else limit - made):
            finish(video, segs, m)
    from . import compliance
    print("\n== compliance audit (every clip vs every campaign rule)")
    compliance.audit_campaign(campaign_id, only_new=limit is not None)
    if limit is not None:
        return made
    print("\nNext: python -m clipper review")
    return made


def _dedupe(videos: list[Path]) -> list[Path]:
    """Drop 'name (1).mp4' copies, and 16:9 '_source' files when a '_portrait' cut of the same clip exists."""
    import re

    names = {v.name for v in videos}
    keep = []
    for v in videos:
        if re.search(r" \(\d+\)\.\w+$", v.name) and re.sub(r" \(\d+\)(\.\w+)$", r"\1", v.name) in names:
            continue
        base = v.stem.split("_source")[0].split("_portrait")[0]
        portraits = [p for p in videos if p.name.startswith(base + "_portrait")]
        sharp = [p for p in portraits if (render_probe(p).get("width") or 0) >= 700]
        if "_source" in v.stem and sharp:
            continue  # use the editor's sharp vertical cut instead
        if "_portrait" in v.stem and v not in sharp and any(n.startswith(base + "_source") for n in names):
            continue  # low-res vertical cut: the 16:9 source looks better
        keep.append(v)
    return keep


def _disclosure(checklist: dict | None) -> str | None:
    """Burn the paid-partnership tag on screen when the campaign wants disclosures visible in the video."""
    ck = checklist or {}
    blob = " ".join(ck.get("required_in_caption", []) + ck.get("required_on_screen", [])).lower()
    if "disclosure" in blob or "#ad" in blob:
        for tag in ("#ad", "#sponsored"):
            if tag in blob:
                return tag
        return "#ad"
    return None


def render_probe(p: Path) -> dict:
    from . import render

    return render.probe_summary(p)


def _enforce_caption(meta: dict, checklist: dict | None) -> dict:
    """Required caption items (disclosures, tags, links) are added in code, never left to the model."""
    import re

    def text() -> str:
        return f"{meta.get('title', '')} {meta.get('description', '')} {' '.join(meta.get('hashtags', []))}".lower()

    for req in (checklist or {}).get("required_in_caption", []):
        # a requirement is often phrased as an instruction ("Tag @x in every post"): add only the literal
        # tags/handles/links it names, never the instruction itself
        tokens = re.findall(r"https?://\S+|[@#][\w.]+", req) if " " in req.strip() else [req.strip()]
        for tok in (t.rstrip(".,;:)") for t in tokens):
            if not tok or tok.lower() in text():
                continue
            if tok.startswith("#"):
                meta["hashtags"] = [tok] + [h for h in meta.get("hashtags", []) if h.lower() != tok.lower()]
            else:
                meta["description"] = f"{meta.get('description', '').rstrip()}\n\n{tok}"
    return meta


def cmd_publish(force_now: bool = False) -> None:
    from zoneinfo import ZoneInfo

    from . import youtube

    start, end = cfg()["youtube"].get("post_window_et", [0, 24])
    et = datetime.now(ZoneInfo("America/New_York"))
    if not force_now and not (start <= et.hour < end):
        print(f"Outside the US posting window ({start}:00-{end}:00 ET; it's {et:%H:%M} ET). "
              "Run again then, or use: publish --now")
        return
    from . import targets

    cap = cfg()["youtube"].get("max_uploads_per_day", 3)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    queue = db.rows("clips", "status='approved' ORDER BY json_extract(meta, '$.score') DESC")
    if not queue:
        print("Nothing approved. Run: python -m clipper review")
        return
    for c in queue:
        campaign = db.get("campaigns", c["campaign_id"])
        posted_any = False
        for post in targets.plan(c, campaign):
            if post["status"] != "pending":
                continue
            t = targets.get(post["target_id"])
            # the per-day cap applies per account, to keep each channel looking human
            cadence = cfg().get("cadence", {}).get(t["platform"], {})
            cap = cadence.get("per_day", cfg()["youtube"].get("max_uploads_per_day", 3))
            done = len([p for p in db.rows("posts", "target_id=? AND posted_at >= ?", t["id"], since)])
            if t["platform"] != "manual" and done >= cap:
                print(f"  {t['label']}: daily cap reached ({done}/{cap}), leaving {c['id'][-20:]} queued")
                continue
            row = targets.publish_one(post, c)
            posted_any = posted_any or row["status"] in ("posted", "draft_uploaded")
            print(f"  {t['label']}: {row['status']} {row['url'] or ''}".rstrip())
            if row["note"]:
                print(f"      {row['note'][:200]}")
        if posted_any:
            first = next((p for p in db.rows("posts", "clip_id=? AND url!=''", c["id"])), None)
            db.upsert("clips", {"id": c["id"], "status": "posted", "posted_at": db.now(),
                                "youtube_url": (first or {}).get("url", "")})
            print(f"{c['id']}: posted. Submit on Whop within {cfg()['whop']['submit_deadline_minutes']} min.")


def cmd_submit() -> None:
    from . import whop

    for c in db.rows("clips", "status='posted' ORDER BY posted_at"):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(c["posted_at"])).total_seconds() / 60
        if age > cfg()["whop"]["submit_deadline_minutes"]:
            print(f"! {c['id']} posted {age:.0f} min ago, past the 1h window; submitting anyway")
        if whop.submit(c, db.get("campaigns", c["campaign_id"])):
            db.upsert("clips", {"id": c["id"], "status": "submitted", "submitted_at": db.now()})


def cmd_status() -> None:
    with db.conn() as con:
        for t in ("campaigns", "clips"):
            counts = con.execute(f"SELECT status, COUNT(*) FROM {t} GROUP BY status").fetchall()
            print(f"{t}: " + (", ".join(f"{s}={n}" for s, n in counts) or "none"))


def main(argv: list[str]) -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, *args = argv
    if cmd == "login":
        from . import whop; whop.login()
    elif cmd == "scout":
        from . import whop
        queries = [None] + (cfg()["scout"].get("search_queries", []) if "--search" in args else [])
        if args and args[0] not in ("--search",):
            queries = [" ".join(args)]  # scout "some search terms"
        results = []
        for q in queries:
            print(f"\n== scouting {'featured' if q is None else repr(q)}")
            results += whop.scout(query=q, max_details=12 if q is None else 6)
        for r in sorted(results, key=lambda r: -r["score"]):
            print(f"{r['score']:6.2f}  {r['status']:11}  YT ${r['youtube_cpm'] or 0:.2f}/1K  "
                  f"${r['budget_remaining'] or 0:,.0f} left  {r['id']}")
    elif cmd == "campaigns":
        for c in db.rows("campaigns", "1=1 ORDER BY score DESC"):
            print(f"{c['score'] or 0:6.2f}  {c['status']:11}  {c['id']}")
    elif cmd == "analyze":
        from . import brief
        c = db.get("campaigns", args[0])
        text = args[1] if len(args) > 1 else (c or {}).get("data", {}).get("brief_text")
        if not text:
            sys.exit("No brief text stored. Run scout first, or pass the brief text as a 2nd argument.")
        print(json.dumps(brief.analyze(args[0], text).model_dump(), indent=2))
    elif cmd == "join":
        from . import whop; whop.join(args[0])
    elif cmd == "clip":
        cmd_clip(*args)
    elif cmd == "review":
        from . import review; print(review.build())
    elif cmd in ("approve", "reject"):
        new = "approved" if cmd == "approve" else "rejected"
        force = "--force" in args
        args = [a for a in args if a != "--force"]
        ids = [c["id"] for c in db.rows("clips", "status='rendered'")] if args == ["all"] else args
        done = 0
        for i in ids:
            c = db.get("clips", i)
            audit = (c.get("qa") or {}).get("audit") if c else None
            if new == "approved" and not force:
                if not audit:
                    print(f"skip {i}: not audited yet (run: python -m clipper audit {c['campaign_id']})")
                    continue
                if not audit.get("overall_pass"):
                    print(f"skip {i}: failed compliance audit -> {'; '.join(audit.get('blocking_issues', []))[:160]}")
                    continue
            db.upsert("clips", {"id": i, "status": new})
            done += 1
        print(f"{new}: {done}" + (" (use --force to override the audit)" if done < len(ids) and new == "approved" else ""))
    elif cmd == "export-queue":
        from . import export
        export.publisher_config()
        export.export_queue()
    elif cmd == "export-session":
        from . import export
        export.export_session()
    elif cmd == "sync-secrets":
        from . import export
        export.sync_secrets()
    elif cmd == "produce":  # the daily unattended run (GitHub Actions)
        import os

        from . import autopilot
        report = autopilot.produce(discover="--discover" in args)
        md = autopilot.summary_md(report)
        print("\n" + md)
        (Path(__file__).parent.parent / "data" / "last_run.md").write_text(md, encoding="utf-8")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
                fh.write(md)
    elif cmd == "sync-producer":
        from . import export
        export.sync_producer(with_state="--with-state" in args, push="--no-push" not in args)
    elif cmd == "pull-state":
        from . import export
        export.pull_state()
    elif cmd == "audit":
        from . import compliance
        compliance.audit_campaign(args[0] if args else None) if args else [
            compliance.audit_campaign(c["id"]) for c in db.rows("campaigns", "status IN ('joined','analyzed')")]
    elif cmd == "youtube-auth":
        from . import targets, youtube
        for t in targets.all_targets():
            if t["platform"] == "youtube" and (not args or t["id"] in args or t["handle"] in args):
                print(f"{t['label']} -> connected:", youtube.whoami(t.get("token")))
    elif cmd == "instagram-auth":
        from . import instagram, targets
        for t in targets.all_targets():
            if t["platform"] != "instagram":
                continue
            try:
                me = instagram.whoami(t)
                print(f"{t['label']} -> connected as @{me.get('username')} "
                      f"({me.get('account_type')}, {me.get('media_count')} posts, user_id {me.get('user_id')})")
            except Exception as err:
                print(f"{t['label']} -> NOT connected: {err}")
    elif cmd == "targets":
        from . import targets
        for t in targets.all_targets():
            print(f"{t['id']:20} {t['platform']:10} auto={str(t.get('auto_post', False)):5} "
                  f"niches={','.join(t.get('niches', [])) or '-'}")
        for c in db.rows("campaigns", "status='joined'"):
            names = [x["label"] for x in targets.for_campaign(c)]
            print(f"\n{c['title'][:40]}: {', '.join(names) or 'no matching target'}")
    elif cmd == "publish":
        cmd_publish(force_now="--now" in args)
    elif cmd == "submit":
        cmd_submit()
    elif cmd == "status":
        cmd_status()
    else:
        sys.exit(f"unknown command {cmd!r}\n{__doc__}")


if __name__ == "__main__":
    main(sys.argv[1:])
