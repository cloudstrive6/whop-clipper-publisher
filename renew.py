"""Keeps the cloud's credentials alive without the PC.

  python renew.py whop-check                 # is the Whop login still valid? opens an issue if not
  python renew.py whop-save FILE             # store the refreshed Whop session (rotated cookies)
  python renew.py save SECRET FILE           # store any refreshed credential file (e.g. YT_COOKIES)
  python renew.py instagram [--min-age 7]    # refresh the 60-day Instagram tokens, store the new ones

Storing needs the SECRETS_PAT secret (fine-grained token: this repo, "Secrets: read and write").
Without it the checks still run and warn, but nothing is written back.
Values are piped straight into `gh secret set`; they are never printed.
"""
import datetime
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY", "cloudstrive6/whop-clipper-publisher")


def set_secret(name: str, value: str) -> bool:
    pat = os.environ.get("SECRETS_PAT")
    if not pat:
        print(f"::warning::SECRETS_PAT not set - can't store the renewed {name}")
        return False
    r = subprocess.run(["gh", "secret", "set", name, "--repo", REPO], input=value.encode(),
                       env={**os.environ, "GH_TOKEN": pat}, capture_output=True)
    if r.returncode:  # gh's error text never contains the secret value
        print(f"::warning::could not store {name}: {r.stderr.decode(errors='replace').strip()[:300]} "
              "(SECRETS_PAT needs access to this repo with 'Secrets: Read and write')")
        return False
    print(f"stored renewed {name}")
    return True


def alert(title: str, body: str) -> None:
    """Telegram, or a GitHub issue if Telegram isn't set up."""
    from notify import alert as send

    send(title, body)


def whop_check() -> int:
    from playwright.sync_api import sync_playwright

    session = os.environ.get("WHOP_SESSION")
    if not session:
        alert("Whop session missing", "Run `python -m clipper sync-secrets` on the PC.")
        return 1
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(storage_state=json.loads(session))
        page = ctx.new_page()
        page.goto("https://whop.com/home/", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)
        ok = "/login" not in page.url
        if ok:
            fresh = json.dumps(ctx.storage_state())
        b.close()
    if not ok:
        print("::error::Whop session expired")
        alert("Whop session expired - posts can't be submitted",
              "Whop logged the automation out. On the PC run:\n\n"
              "```\n.venv\\Scripts\\python -m clipper login\n.venv\\Scripts\\python -m clipper sync-secrets\n```\n"
              "Until then clips still post, but Whop submissions fail (they earn nothing).")
        return 1
    print("Whop session valid")
    set_secret("WHOP_SESSION", fresh)  # rolling refresh: the newest cookies extend the login
    return 0


def save(name: str, file: str) -> int:
    if os.path.exists(file) and os.path.getsize(file) > 100:
        set_secret(name, open(file, encoding="utf-8").read())
    return 0


def instagram(min_age_days: int = 7) -> int:
    store = json.loads(os.environ.get("INSTAGRAM_TOKENS") or "{}")
    changed, failed = False, []
    now = datetime.datetime.now(datetime.timezone.utc)
    for handle, entry in store.items():
        last = entry.get("refreshed_at")
        if last and (now - datetime.datetime.fromisoformat(last)).days < min_age_days:
            continue
        try:
            q = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": entry["token"]})
            with urllib.request.urlopen(f"https://graph.instagram.com/refresh_access_token?{q}", timeout=60) as r:
                data = json.loads(r.read())
            entry.update(token=data["access_token"], refreshed_at=now.isoformat(timespec="seconds"),
                         expires_in_days=round(data.get("expires_in", 0) / 86400))
            changed = True
            print(f"refreshed @{handle} (valid ~{entry['expires_in_days']} days)")
        except Exception as err:
            failed.append(handle)
            print(f"::warning::could not refresh @{handle}: {err.__class__.__name__}")
    if changed:
        set_secret("INSTAGRAM_TOKENS", json.dumps(store, indent=2))
    if failed:
        alert(f"Instagram token refresh failed: {', '.join(failed)}",
              "Regenerate the token in the Meta app (Use cases > Instagram > API setup > Generate access tokens), "
              "paste it into secrets\\instagram_tokens.json on the PC, then run `python -m clipper sync-secrets`.")
    return 0


if __name__ == "__main__":
    cmd, *rest = sys.argv[1:] or ["whop-check"]
    if cmd == "whop-check":
        sys.exit(whop_check())
    if cmd == "whop-save":
        sys.exit(save("WHOP_SESSION", rest[0]))
    if cmd == "save":
        sys.exit(save(rest[0], rest[1]))
    if cmd == "instagram":
        sys.exit(instagram(int(rest[1]) if rest[:1] == ["--min-age"] else 7))
    sys.exit(f"unknown: {cmd}")
