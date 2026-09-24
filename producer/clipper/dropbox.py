"""Download individual files from a public Dropbox shared folder (skips files over a size cap)."""
import re
import sys
import urllib.request
from pathlib import Path

from .config import pw_channel

SIZE = re.compile(r"^([\d.]+)\s*(KB|MB|GB)$")


def list_files(folder_url: str) -> list[tuple[str, float, str]]:
    """Returns [(name, size_mb, direct_download_url)]."""
    from playwright.sync_api import sync_playwright

    out = []
    with sync_playwright() as p:
        b = p.chromium.launch(channel=pw_channel(), headless=True)
        pg = b.new_page(viewport={"width": 1400, "height": 1400})
        pg.goto(folder_url, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(8000)
        for _ in range(10):
            pg.mouse.wheel(0, 3000)
            pg.wait_for_timeout(500)
        rows = pg.evaluate("""() => [...document.querySelectorAll('a[href*="/scl/fo/"]')]
            .map(a => ({href: a.href, text: (a.closest('[role=row],tr,li') || a).innerText}))""")
        b.close()
    seen = set()
    for r in rows:
        lines = [l.strip() for l in r["text"].splitlines() if l.strip()]
        if not lines or r["href"] in seen:
            continue
        name = lines[0]
        size = next((l for l in lines if SIZE.match(l)), None)
        if not size or "." not in name:
            continue
        num, unit = SIZE.match(size).groups()
        mb = float(num) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[unit]
        seen.add(r["href"])
        out.append((name, mb, re.sub(r"dl=0", "dl=1", r["href"]) if "dl=0" in r["href"] else r["href"] + "&dl=1"))
    return out


def download(folder_url: str, dest: Path, max_mb: float = 200) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    got = []
    for name, mb, url in list_files(folder_url):
        target = dest / re.sub(r'[<>:"/\\|?*]', "_", name)
        if mb > max_mb:
            print(f"  skip (too big, {mb:,.0f} MB): {name}")
            continue
        if target.exists() and target.stat().st_size > 0:
            got.append(target)
            continue
        print(f"  downloading {name} ({mb:.1f} MB)")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=300) as r, open(target, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        got.append(target)
    return got


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for p in download(sys.argv[1], Path(sys.argv[2]), float(sys.argv[3]) if len(sys.argv) > 3 else 200):
        print(p)
