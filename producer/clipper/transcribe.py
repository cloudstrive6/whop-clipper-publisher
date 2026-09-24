"""Word-level transcription with faster-whisper (cached next to the video)."""
import json
from pathlib import Path

from .config import cfg


def _cuda_dlls() -> None:
    """Expose pip-installed cuBLAS/cuDNN DLLs (nvidia-*-cu12 wheels) to ctranslate2 on Windows."""
    import os
    import site

    if not hasattr(os, "add_dll_directory"):  # Linux: nothing to do
        return
    for sp in site.getsitepackages():
        for bin_dir in Path(sp, "nvidia").glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def transcribe(video: Path, model_name: str | None = None) -> list[dict]:
    """Returns segments: [{start, end, text, words: [{start, end, word}]}]."""
    cache = video.with_suffix(".transcript.json")
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    _cuda_dlls()
    from faster_whisper import WhisperModel

    e = cfg()["editing"]
    device = e.get("whisper_device", "auto")
    model = None
    for dev, ctype in ([("cuda", "float16"), ("cpu", "int8")] if device == "auto" else [(device, "float16" if device == "cuda" else "int8")]):
        try:
            model = WhisperModel(model_name or e.get("whisper_model", "small"), device=dev, compute_type=ctype)
            segs, _ = model.transcribe(str(video), word_timestamps=True, vad_filter=True)
            out = [
                {"start": s.start, "end": s.end, "text": s.text.strip(),
                 "words": [{"start": w.start, "end": w.end, "word": w.word} for w in (s.words or [])]}
                for s in segs
            ]
            break
        except Exception as err:  # CUDA libs missing -> fall back to CPU
            if dev == "cpu":
                raise
            print(f"[transcribe] {dev} failed ({err.__class__.__name__}); falling back to CPU")
    cache.write_text(json.dumps(out), encoding="utf-8")
    return out


def as_timestamped_text(segments: list[dict]) -> str:
    return "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments)
