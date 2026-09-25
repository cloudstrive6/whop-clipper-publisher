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
              disclosure: str | None = None) -> str:
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
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(dur)},Hook,,0,0,0,,{_esc(hook.upper())}")
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


def talking_head_window(video: Path, start: float, end: float, src_w: int, src_h: int) -> int | None:
    """Left edge of a 4:5 crop that keeps the speaker's face, or None when the shot isn't a single talking
    head (no steady face, or people spread wider than 4:5 would allow): then the full frame is kept."""
    import cv2
    import numpy

    win = int(src_h * 4 / 5)
    if win >= src_w:
        return None
    sw, sh = 640, max(2, round(640 * src_h / src_w / 2) * 2)
    # YuNet (OpenCV's face model) also finds angled and profile faces, unlike the old Haar cascades
    det = cv2.FaceDetectorYN.create(str(ROOT / "assets" / "models" / "face_detection_yunet_2023mar.onnx"),
                                    "", (sw, sh), 0.6)
    spans, frames_with_face = [], 0
    for i in range(6):
        t = start + (end - start) * (i + 0.5) / 6
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                              "-vf", f"scale={sw}:{sh}", "-f", "image2pipe", "-vcodec", "png", "-"],
                             capture_output=True).stdout
        img = cv2.imdecode(numpy.frombuffer(raw, numpy.uint8), cv2.IMREAD_COLOR) if raw else None
        if img is None:
            continue
        k = src_w / sw
        _, faces = det.detect(img)
        faces = [] if faces is None else faces
        if len(faces):
            frames_with_face += 1
        spans += [(f[0] * k, (f[0] + f[2]) * k) for f in faces]
    if frames_with_face < 4:  # the speaker isn't on screen most of the time
        return None
    lo, hi = min(a for a, _ in spans), max(b for _, b in spans)
    if hi - lo > win * 0.85:  # two people side by side, or a wide shot: cropping would cut someone off
        return None
    return int(min(max((lo + hi) / 2 - win / 2, 0), src_w - win))


def render(video: Path, segments: list[dict], start: float, end: float, hook: str | None, out: Path,
           music: bool = True, disclosure: str | None = None) -> Path:
    e = cfg()["editing"]
    W, H = e["width"], e["height"]
    out.parent.mkdir(parents=True, exist_ok=True)
    v = next(s for s in _probe(video)["streams"] if s["codec_type"] == "video")
    vertical = v["height"] / v["width"] > 1.5  # already 9:16-ish (e.g. pre-cut portrait clips)
    # pre-edited vertical cuts usually carry burned-in captions already; don't double them
    captions = e["captions"] and not vertical
    ass = out.with_suffix(".ass")
    ass.write_text(build_ass(segments, start, end, hook if e["hook_text"] else None, captions, disclosure),
                   encoding="utf-8")
    x = None
    if not vertical and e["layout"] != "crop" and e.get("talking_head_4x5", True):
        try:
            x = talking_head_window(video, start, end, int(v["width"]), int(v["height"]))
        except Exception as err:  # face detection is a nicety: never lose a clip over it
            print(f"   (face detection unavailable: {err.__class__.__name__}; keeping the full frame)")
    if e["layout"] == "crop" or vertical:
        vf = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[base]"
    elif x is not None:  # talking head: a 4:5 crop on the speaker, big, on a 9:16 blurred canvas
        win = int(int(v["height"]) * 4 / 5)
        vf = (f"[0:v]split[a][b];"
              f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:2,eq=brightness=-0.15[bg];"
              f"[b]crop={win}:ih:{x}:0,scale={W}:-2[fg];"
              f"[bg][fg]overlay=(W-w)/2:{int(H * 0.19)},setsar=1[base]")
        print(f"   layout: 4:5 talking head (crop x={x})")
    else:  # blurred background + full frame, nudged up so captions sit below it
        vf = (f"[0:v]split[a][b];"
              f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:2,eq=brightness=-0.15[bg];"
              f"[b]scale={W}:-2[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2-{int(H*0.04)},setsar=1[base]")
    vf += f";[base]subtitles={ass.name},fps=30,format=yuv420p[v]"

    dur = end - start
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(video.resolve())]
    music = _music() if (music and e.get("bg_music_volume")) else None
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music.resolve())]
        af = f"[1:a]volume={e['bg_music_volume']}[m];[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]"
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
