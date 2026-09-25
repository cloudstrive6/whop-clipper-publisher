"""ffmpeg renderer: cut -> 9:16 layout -> word-highlight captions -> hook text -> optional music."""
import json
import random
import subprocess
from pathlib import Path

from .config import IN_CI, ROOT, cfg

FONT = cfg()["editing"].get("font_ci" if IN_CI else "font", "Arial Black")


def _ts(t: float) -> str:
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(segments: list[dict], start: float, end: float, hook: str | None, captions: bool,
              disclosure: str | None = None, verbatim: bool = False) -> str:
    e = cfg()["editing"]
    W, H = e["width"], e["height"]
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT},78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,5,60,60,0,1
Style: Hook,{FONT},64,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,70,70,{int(H*0.14)},1
Style: Disc,{FONT},44,&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,3,10,0,9,40,40,{int(H*0.055)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    dur = end - start
    if hook:
        # a brand-approved line is shown exactly as written (case included)
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(dur)},Hook,,0,0,0,,{_esc(hook if verbatim else hook.upper())}")
    if disclosure:  # visible paid-partnership disclosure, kept on screen for the whole clip
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(dur)},Disc,,0,0,0,,{_esc(disclosure)}")
    if captions:
        words = [w for s in segments for w in s.get("words", []) if w["end"] > start and w["start"] < end]
        cap_y = int(H * 0.68)
        for i in range(0, len(words), 3):
            chunk = words[i:i + 3]
            c0, c1 = chunk[0]["start"] - start, (words[i + 3]["start"] if i + 3 < len(words) else chunk[-1]["end"]) - start
            for j, w in enumerate(chunk):  # highlight the word being spoken
                ws = w["start"] - start
                we = (chunk[j + 1]["start"] - start) if j + 1 < len(chunk) else c1
                text = " ".join(
                    ("{\\c&H00E5FF&}" + _esc(x["word"].strip().upper()) + "{\\c&HFFFFFF&}") if k == j else _esc(x["word"].strip().upper())
                    for k, x in enumerate(chunk)
                )
                lines.append(f"Dialogue: 0,{_ts(max(ws, c0 if j == 0 else ws))},{_ts(we)},Cap,,0,0,0,,{{\\pos({W//2},{cap_y})}}{text}")
    return head + "\n".join(lines) + "\n"


def _probe(video: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(video)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _music() -> Path | None:
    tracks = [p for p in (ROOT / "assets" / "music").glob("*") if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".ogg"}]
    return random.choice(tracks) if tracks else None


def _speaking_times(segments: list[dict], start: float, end: float) -> list[tuple[float, float]]:
    """Clip-relative spans where someone is talking (from the transcript's word timings)."""
    return [(max(w["start"], start) - start, min(w["end"], end) - start)
            for s in segments for w in s.get("words", []) if w["end"] > start and w["start"] < end]


def speaker_track(video: Path, start: float, end: float, src_w: int, src_h: int,
                  segments: list[dict] | None = None) -> list[tuple[float, int]] | None:
    """Where a 4:5 window should sit over time so it shows whoever is talking, like a TV director cutting
    between cameras. Returns [(clip_time, left_x), ...] cut points, or None to keep the full frame
    (nobody's face on screen most of the time: gameplay, B-roll, screens).

    Faces come from YuNet (OpenCV's face model, handles angles and profiles). Who's talking is read from
    mouth movement: the face whose mouth area changes most while the transcript says someone is speaking.
    A cut only happens when the other person clearly takes over for a while, so it never flickers."""
    import cv2
    import numpy

    win = int(src_h * 4 / 5)
    if win >= src_w:
        return None
    fps, sw = 5, 640
    sh = max(2, round(sw * src_h / src_w / 2) * 2)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(video),
                          "-vf", f"fps={fps},scale={sw}:{sh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                         capture_output=True).stdout
    frames = numpy.frombuffer(raw, numpy.uint8)[: len(raw) // (sw * sh * 3) * sw * sh * 3].reshape(-1, sh, sw, 3)
    if not len(frames):
        return None
    det = cv2.FaceDetectorYN.create(str(ROOT / "assets" / "models" / "face_detection_yunet_2023mar.onnx"),
                                    "", (sw, sh), 0.6)

    tracks: list[dict] = []  # each: {"cx": last centre x, "hits": {frame: (cx, motion)}, "mouth": last mouth patch}
    with_face = 0
    for i, img in enumerate(frames):
        _, faces = det.detect(img)
        faces = [] if faces is None else faces
        with_face += bool(len(faces))
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for f in faces:
            x, y, w, h = f[:4]
            cx = x + w / 2
            mx1, my1, mx2, my2 = f[10], f[11], f[12], f[13]  # mouth corners
            mcx, mcy = (mx1 + mx2) / 2, (my1 + my2) / 2
            mw, mh = max(abs(mx2 - mx1) * 1.8, w * 0.35), h * 0.3
            x0, y0 = int(max(mcx - mw / 2, 0)), int(max(mcy - mh / 2, 0))
            patch = gray[y0:int(min(mcy + mh / 2, sh)), x0:int(min(mcx + mw / 2, sw))]
            patch = cv2.resize(patch, (32, 16)).astype(numpy.float32) if patch.size else None
            track = min(tracks, key=lambda t: abs(t["cx"] - cx), default=None)
            if track is None or abs(track["cx"] - cx) > sw * 0.12:
                track = {"cx": cx, "hits": {}, "mouth": None, "last": -9}
                tracks.append(track)
            motion = 0.0
            if patch is not None and track["mouth"] is not None and track["last"] == i - 1:
                motion = float(numpy.mean(numpy.abs(patch - track["mouth"])))
            track.update(cx=cx, mouth=patch, last=i)
            track["hits"][i] = (cx, motion)
    if with_face < len(frames) * 0.5:
        return None

    talking = _speaking_times(segments or [], start, end)
    def speech_at(i: int) -> bool:
        t = i / fps
        return not talking or any(a - 0.2 <= t <= b + 0.2 for a, b in talking)

    # who's talking, frame by frame: smoothed mouth motion (1 s window) among the faces on screen
    def score(track, i):
        vals = [track["hits"][j][1] for j in range(i - 2, i + 3) if j in track["hits"]]
        return sum(vals) / max(len(vals), 1)

    k = src_w / sw
    current, since, cuts = None, 0, []
    min_shot = int(fps * 1.2)
    for i in range(len(frames)):
        present = [t for t in tracks if i in t["hits"]]
        if not present:
            continue
        if current is None or not any(t is current for t in present):
            best = max(present, key=lambda t: score(t, i))
        else:
            best = current
            if speech_at(i) and len(present) > 1:
                rival = max((t for t in present if t is not current), key=lambda t: score(t, i))
                if score(rival, i) > score(current, i) * 1.4 + 0.5 and i - since >= min_shot:
                    best = rival
        if best is not current:
            current, since = best, i
            cuts.append((i / fps, best))
    # each shot sits on the median position of its speaker during that shot (no drifting)
    out = []
    for n, (t, track) in enumerate(cuts):
        t_end = cuts[n + 1][0] if n + 1 < len(cuts) else (end - start)
        xs = [cx for j, (cx, _) in track["hits"].items() if t <= j / fps < t_end] or [track["cx"]]
        centre = sorted(xs)[len(xs) // 2] * k
        left = int(min(max(centre - win / 2, 0), src_w - win))
        if not out or abs(out[-1][1] - left) > src_w * 0.03:
            out.append((round(t, 2), left))
    return out or None


def _feather_mask(w: int, h: int, folder: Path, edge: int = 90) -> Path:
    """Alpha mask that fades the sharp video into the blurred background over `edge` px at top and bottom."""
    mask = folder / f"_feather_{w}x{h}_{edge}.png"
    if not mask.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=white:s={w}x{h}",
                        "-frames:v", "1", "-vf", f"format=gray,geq=lum='255*min(1,min(Y,H-1-Y)/{edge})'",
                        str(mask)], check=True)
    return mask


def render(video: Path, segments: list[dict], start: float, end: float, hook: str | None, out: Path,
           music: bool = True, disclosure: str | None = None, captions: bool | None = None,
           full_frame: bool = False, verbatim_hook: bool = False) -> Path:
    """captions=False: no word captions (brand forbids our own text). full_frame: never crop (branded
    footage whose logos, UI or legal lines must stay visible). verbatim_hook: approved line, exact case."""
    e = cfg()["editing"]
    W, H = e["width"], e["height"]
    out.parent.mkdir(parents=True, exist_ok=True)
    v = next(s for s in _probe(video)["streams"] if s["codec_type"] == "video")
    vertical = v["height"] / v["width"] > 1.5  # already 9:16-ish (e.g. pre-cut portrait clips)
    # pre-edited vertical cuts usually carry burned-in captions already; don't double them
    captions = (e["captions"] and not vertical) if captions is None else captions
    ass = out.with_suffix(".ass")
    show_hook = hook if (e["hook_text"] or verbatim_hook) else None
    ass.write_text(build_ass(segments, start, end, show_hook, captions, disclosure, verbatim_hook),
                   encoding="utf-8")
    src_w, src_h = int(v["width"]), int(v["height"])
    track = None
    if not vertical and not full_frame and e["layout"] != "crop" and e.get("talking_head_4x5", True):
        try:
            track = speaker_track(video, start, end, src_w, src_h, segments)
        except Exception as err:  # face tracking is a nicety: never lose a clip over it
            print(f"   (face tracking unavailable: {err.__class__.__name__}; keeping the full frame)")
    bg = (f"[0:v]split[a][b];"
          f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:2,eq=brightness=-0.15[bg];")
    extra_inputs = []
    if e["layout"] == "crop" or vertical:
        vf = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[base]"
    else:
        if track:  # talking head: a 4:5 window on whoever is speaking, cutting between speakers
            win = int(src_h * 4 / 5)
            x = str(track[-1][1])
            for t, left in reversed(track[:-1]):
                x = f"if(lt(t,{track[track.index((t, left)) + 1][0]}),{left},{x})"
            fg_w, fg_h, top = W, round(W * 5 / 4 / 2) * 2, int(H * 0.19)
            fg = f"[b]crop={win}:ih:'{x}':0,scale={fg_w}:{fg_h}"
            print(f"   layout: 4:5 following the speaker ({len(track)} shot{'s' * (len(track) > 1)})")
        else:  # full frame over the blurred copy, nudged up so captions sit below it
            fg_w = W
            fg_h = round(W * src_h / src_w / 2) * 2
            top = (H - fg_h) // 2 - int(H * 0.04)
            fg = f"[b]scale={fg_w}:{fg_h}"
        # feathered top/bottom edges: the sharp video fades into the blurred copy instead of a hard line
        extra_inputs = ["-loop", "1", "-i", str(_feather_mask(fg_w, fg_h, out.parent))]
        vf = (bg + f"{fg},format=yuva420p[fgv];[fgv][1:v]alphamerge[fga];"
              f"[bg][fga]overlay=(W-w)/2:{top}:shortest=1,setsar=1[base]")
    vf += f";[base]subtitles={ass.name},fps=30,format=yuv420p[v]"

    dur = end - start
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
           "-i", str(video.resolve()), *extra_inputs]
    music_in = 1 + bool(extra_inputs)
    music = _music() if (music and e.get("bg_music_volume")) else None
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music.resolve())]
        af = (f"[{music_in}:a]volume={e['bg_music_volume']}[m];"
              f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]")
        fc, amap = f"{vf};{af}", "[a]"
    else:
        fc, amap = vf, "0:a?"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", amap,
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", "-t", f"{dur:.2f}", out.name]
    subprocess.run(cmd, cwd=out.parent, check=True)  # cwd lets the subtitles filter use a bare filename
    return out


def probe_summary(video: Path) -> dict:
    p = _probe(video)
    v = next((s for s in p["streams"] if s["codec_type"] == "video"), {})
    return {
        "duration": float(p["format"]["duration"]),
        "width": v.get("width"), "height": v.get("height"),
        "has_audio": any(s["codec_type"] == "audio" for s in p["streams"]),
        "size_mb": round(int(p["format"]["size"]) / 1e6, 1),
    }
