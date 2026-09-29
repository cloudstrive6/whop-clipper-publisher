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


ZOOM_FACE = 3.5      # the window is this many face-heights tall: head and shoulders
MIN_WINDOW = 0.34    # never zoom tighter than this share of the frame height (tiny insets would turn to mush)


SAME_PERSON = 0.36   # SFace cosine similarity at or above which two faces are the same person


def face_embedding(image: Path):
    """SFace fingerprint of the biggest face in a photo (e.g. a creator's channel avatar), or None."""
    import cv2
    import numpy

    img = cv2.imread(str(image))
    if img is None:
        return None
    scale = 640 / max(img.shape[:2])
    if scale < 1:
        img = cv2.resize(img, (round(img.shape[1] * scale), round(img.shape[0] * scale)))
    det = cv2.FaceDetectorYN.create(str(ROOT / "assets" / "models" / "face_detection_yunet_2023mar.onnx"),
                                    "", (img.shape[1], img.shape[0]), 0.6)
    _, faces = det.detect(img)
    if faces is None or not len(faces):
        return None
    f = max(faces, key=lambda f: f[2] * f[3])
    rec = cv2.FaceRecognizerSF.create(str(ROOT / "assets" / "models" / "face_recognition_sface_2021dec.onnx"), "")
    e = rec.feature(rec.alignCrop(img, f)).flatten()
    return e / (numpy.linalg.norm(e) + 1e-9)


def speaker_track(video: Path, start: float, end: float, src_w: int, src_h: int,
                  segments: list[dict] | None = None, featured=None, stats: dict | None = None) -> list[dict] | None:
    """A "camera" that follows whoever is talking, like a TV director: it cuts to the speaker and zooms to
    fit them - the full frame height for a big face, a punch-in for a small one (a reaction-video
    picture-in-picture, a guest far from the camera). Returns shots [{t, x, y, w, h}, ...] as 4:5 boxes in
    source pixels, or None to keep the full frame (nobody's face on screen most of the time).

    Faces come from YuNet (OpenCV's face model, handles angles and profiles). Who's talking is read from
    mouth movement: the face whose mouth area changes most while the transcript says someone is speaking.
    A cut only happens when the other person clearly takes over for a while, so it never flickers.

    featured: the fingerprint of the person the campaign is about. `stats` then gets featured_share - the part of
    the speech where the speaker is recognisably them (None when too few speaking faces could be recognised)."""
    import cv2
    import numpy

    if src_h * 4 / 5 >= src_w * 0.98:  # already narrower than 4:5: nothing to follow
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
    sface = ROOT / "assets" / "models" / "face_recognition_sface_2021dec.onnx"
    rec = cv2.FaceRecognizerSF.create(str(sface), "") if sface.exists() else None  # who is who, across cuts

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
            # the eye area moves with the head but not with speech: subtracting its change isolates the mouth
            ex, ey = (f[4] + f[6]) / 2, (f[5] + f[7]) / 2
            eye = gray[int(max(ey - mh / 2, 0)):int(min(ey + mh / 2, sh)), int(max(ex - mw / 2, 0)):int(min(ex + mw / 2, sw))]
            eye = cv2.resize(eye, (32, 16)).astype(numpy.float32) if eye.size else None
            cy = y + h / 2
            # a face fingerprint for faces big enough to recognise: shows often put different people in the
            # same close-up spot, so position alone can't tell who is who
            e = None
            if rec is not None and h >= 20:
                try:
                    e = rec.feature(rec.alignCrop(img, f)).flatten()
                    e = e / (numpy.linalg.norm(e) + 1e-9)
                except Exception:
                    e = None
            same = lambda t: e is None or t.get("emb") is None or float(t["emb"] @ e) >= 0.3
            # the same person = the nearest track of a similar face size that is recognisably them
            near = [t for t in tracks if abs(t["cx"] - cx) <= max(w, sw * 0.04) * 1.5 and abs(t["cy"] - cy) <= h * 1.5
                    and 0.5 <= t["h"] / h <= 2 and same(t)]
            track = min(near, key=lambda t: abs(t["cx"] - cx) + abs(t["cy"] - cy), default=None)
            if track is None:
                track = {"cx": cx, "cy": cy, "h": h, "hits": {}, "mouth": None, "eye": None, "last": -9}
                tracks.append(track)
            motion, head = 0.0, 0.0
            face = gray[int(max(y, 0)):int(min(y + h, sh)), int(max(x, 0)):int(min(x + w, sw))]
            face = cv2.resize(face, (24, 24)).astype(numpy.float32) if face.size else None
            if face is not None and track.get("face") is not None and track["last"] == i - 1:
                head = float(numpy.mean(numpy.abs(face - track["face"])))
            if e is not None:
                track["emb"] = e if track.get("emb") is None else (track["emb"] * 0.8 + e * 0.2)
                track["emb"] = track["emb"] / (numpy.linalg.norm(track["emb"]) + 1e-9)
            if patch is not None and track["mouth"] is not None and track["last"] == i - 1:
                motion = float(numpy.mean(numpy.abs(patch - track["mouth"])))
                if eye is not None and track.get("eye") is not None:
                    motion = max(0.0, motion - float(numpy.mean(numpy.abs(eye - track["eye"]))))
            track.update(cx=cx, cy=cy, h=h, mouth=patch, eye=eye, face=face, last=i)
            track["hits"][i] = (cx, cy, h, motion, head)
    if with_face < len(frames) * 0.5:
        return None

    talking = _speaking_times(segments or [], start, end)
    def speech_at(i: int) -> bool:
        t = i / fps
        return not talking or any(a - 0.2 <= t <= b + 0.2 for a, b in talking)

    # who's talking = whose mouth moves in time with the voice (lip-sync). Loudness of the audio, per frame:
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(video),
                          "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True).stdout
    audio = numpy.frombuffer(pcm[: len(pcm) // 2 * 2], numpy.int16).astype(numpy.float32)
    per = int(16000 / fps)
    loud = numpy.array([numpy.sqrt(numpy.mean(audio[i * per:(i + 1) * per] ** 2)) if (i + 1) * per <= len(audio) else 0.0
                        for i in range(len(frames))])

    def sync(track, i, half=12, ahead=False):
        """Correlation between this face's mouth movement and the audio loudness around frame i (~3 s).
        A talking face scores high; a listener, a photo, or compression noise on a tiny face does not."""
        lo, hi = (i, i + 2 * half) if ahead else (i - half, i + half)
        js = [j for j in range(lo, hi + 1) if j in track["hits"] and 0 <= j < len(loud)]
        if len(js) < 10:
            return 0.0
        a = loud[js]
        if a.std() < 1e-6:
            return 0.0
        best = 0.0
        for col, weight in ((3, 1.0), (4, 0.8)):  # lips, then whole head (a bit less telling)
            m = numpy.array([track["hits"][j][col] for j in js])
            if m.std() > 1e-6:
                best = max(best, weight * float(numpy.corrcoef(m, a)[0, 1]))
        return best

    k = src_w / sw
    current, since, cuts = None, 0, []
    anchor = None  # the last person we have real evidence is speaking (not just whoever is on screen)
    min_shot = int(fps * 1.2)
    speaker_at: dict[int, dict] = {}  # frame -> who the camera is on while someone speaks
    for i in range(len(frames)):
        present = [t for t in tracks if i in t["hits"]]
        if not present:
            continue
        if current is not None and speech_at(i):
            speaker_at[i] = current
        # ties go to the bigger face (the main subject), so a quiet moment doesn't cut to a background face
        rank = lambda t: sync(t, i) + 0.3 * (t["h"] / sh)
        if current is None or not any(t is current for t in present):
            # a new scene. If the speech carries straight on through the cut, the speaker hasn't changed:
            # recognise them in the new shot. (A reaction close-up of a listener is shown - he may be the only
            # face - but never becomes "the speaker".) Otherwise look ahead to see who talks in the new shot.
            best = None
            if anchor is not None and anchor.get("emb") is not None and speech_at(i) and speech_at(i - 2):
                simi = lambda t: float(t["emb"] @ anchor["emb"]) if t.get("emb") is not None else -1.0
                closest = max(present, key=simi)
                others = [simi(t) for t in present if t is not closest]
                if simi(closest) >= 0.36 and (not others or simi(closest) - max(others) >= 0.08):
                    best = closest
            if best is None:
                best = max(present, key=lambda t: sync(t, i, ahead=True) + 0.1 * (t["h"] / sh))
                if sync(best, i, ahead=True) >= 0.3:
                    anchor = best  # clearly talking in the new shot
        else:
            best = current
            if speech_at(i) and len(present) > 1:
                rival = max((t for t in present if t is not current), key=rank)
                bar = 0.5 if rival["h"] < current["h"] * 0.5 else 0.35  # cutting to a much smaller face needs more proof
                # the evidence must hold for ~2 s (a laugh or a nod in rhythm isn't someone taking over)
                held = all(sync(rival, j) > bar and sync(rival, j) > sync(current, j) + 0.25
                           for j in (i, i + 5, i + 10) if j < len(frames))
                if held and i - since >= min_shot:
                    best = anchor = rival
            if anchor is None and sync(current, i) >= 0.3:
                anchor = current
        if best is not current:
            current, since = best, i
            cuts.append((i / fps, best))
    if featured is not None and stats is not None:
        # is the person talking the one the campaign is about? (a reaction video on their channel can be carried
        # by someone else entirely, with them in a corner inset)
        known = [t for t in speaker_at.values() if t.get("emb") is not None]
        match = sum(float(t["emb"] @ featured) >= SAME_PERSON for t in known)
        stats["featured_share"] = round(match / len(known), 2) if len(known) >= max(10, len(speaker_at) * 0.5) else None
        stats["featured_on_screen"] = any(t.get("emb") is not None and float(t["emb"] @ featured) >= SAME_PERSON
                                          for t in tracks)
    # each shot: a steady 4:5 box around its speaker (median position and size over the shot, no drifting)
    med = lambda v: sorted(v)[len(v) // 2]
    even = lambda n: max(2, int(n) // 2 * 2)
    out = []
    for n, (t, track) in enumerate(cuts):
        t_end = cuts[n + 1][0] if n + 1 < len(cuts) else (end - start)
        hits = [v for j, v in track["hits"].items() if t <= j / fps < t_end] or [(track["cx"], track["cy"], track["h"], 0)]
        cx, cy, fh = med([v[0] for v in hits]) * k, med([v[1] for v in hits]) * k, med([v[2] for v in hits]) * k
        h = min(max(fh * ZOOM_FACE, src_h * MIN_WINDOW), src_h)
        w = h * 4 / 5
        if w > src_w:
            w, h = src_w, src_w * 5 / 4
        w, h = even(w), even(h)
        x = even(min(max(cx - w / 2, 0), src_w - w))
        y = even(min(max(cy - h * 0.40, 0), src_h - h))  # face in the upper part of the frame
        box = {"t": round(t if out else 0.0, 2), "x": x, "y": y, "w": w, "h": h}
        if out and all(abs(out[-1][key] - box[key]) <= src_w * 0.03 for key in ("x", "y", "w", "h")):
            continue  # same framing as the previous shot: no cut
        out.append(box)
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
           full_frame: bool = False, verbatim_hook: bool = False, featured=None, stats: dict | None = None) -> Path:
    """captions=False: no word captions (brand forbids our own text). full_frame: never crop (branded
    footage whose logos, UI or legal lines must stay visible). verbatim_hook: approved line, exact case.
    featured + stats: see speaker_track (is the person talking the one the campaign is about?)."""
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
            track = speaker_track(video, start, end, src_w, src_h, segments, featured, stats)
        except Exception as err:  # face tracking is a nicety: never lose a clip over it
            print(f"   (face tracking unavailable: {err.__class__.__name__}; keeping the full frame)")
    bg = (f"[0:v]split[a][b];"
          f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:2,eq=brightness=-0.15[bg];")
    extra_inputs = []
    if e["layout"] == "crop" or vertical:
        vf = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[base]"
    else:
        if track:  # faces: a 4:5 "camera" on whoever is speaking, cutting and zooming between speakers
            fg_w, fg_h, top = W, round(W * 5 / 4 / 2) * 2, int(H * 0.19)
            dur_total = end - start
            shots = []
            for i, b in enumerate(track):
                t1 = track[i + 1]["t"] if i + 1 < len(track) else dur_total + 1
                src = f"[s{i}]" if len(track) > 1 else "[b]"
                shots.append(f"{src}trim=start={b['t']}:end={t1:.2f},setpts=PTS-STARTPTS,"
                             f"crop={b['w']}:{b['h']}:{b['x']}:{b['y']},scale={fg_w}:{fg_h},setsar=1"
                             + (f"[c{i}]" if len(track) > 1 else ""))
            if len(track) > 1:
                fg = (f"[b]split={len(track)}" + "".join(f"[s{i}]" for i in range(len(track))) + ";"
                      + ";".join(shots) + ";" + "".join(f"[c{i}]" for i in range(len(track)))
                      + f"concat=n={len(track)}:v=1:a=0")
            else:
                fg = shots[0]
            zooms = sum(1 for b in track if b["h"] < src_h * 0.95)
            print(f"   layout: 4:5 camera on the speaker ({len(track)} shot{'s' * (len(track) > 1)}, {zooms} zoomed in)")
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
