"""Cut the waits out of the raw recording and export demo.mp4 and a README GIF (ffmpeg from imageio-ffmpeg).

  demo/.venv/Scripts/python demo/make_video.py [--gif START DURATION]

START is a time in the cut video (demo.mp4), in seconds. Waits recorded by record.py keep KEEP seconds at each end.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import imageio_ffmpeg

HERE = Path(__file__).parent
RAW = HERE / "raw"
KEEP = 1.0
FF = imageio_ffmpeg.get_ffmpeg_exe()


def keep_segments(end: float, waits: list[list[float]]) -> list[tuple[float, float]]:
    segs, t = [], 0.0
    for a, b in waits:
        if b - a > 2 * KEEP:
            segs.append((t, a + KEEP))
            t = b - KEEP
    segs.append((t, end))
    return [(a, b) for a, b in segs if b - a > 0.05]


def to_cut(t: float, segs: list[tuple[float, float]]) -> float:
    """A time in the raw recording -> the same moment in demo.mp4."""
    out = 0.0
    for a, b in segs:
        if t < a:
            break
        out += min(t, b) - a
    return out


def run(args: list[str]) -> None:
    subprocess.run([FF, "-loglevel", "error", "-y", *args], check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gif", nargs=2, type=float, metavar=("START", "DURATION"))
    a = ap.parse_args()
    marks = json.loads((RAW / "marks.json").read_text(encoding="utf-8"))
    src = RAW / marks["video"]
    mp4 = HERE / "demo.mp4"
    if not a.gif:
        segs = keep_segments(marks["end"], marks["waits"])
        parts = "".join(f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[v{i}];" for i, (s, e) in enumerate(segs))
        concat = (
            "".join(f"[v{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0,fps=30,format=yuv420p[out]"
        )
        run(["-i", str(src), "-filter_complex", parts + concat, "-map", "[out]", "-c:v", "libx264", "-preset", "slow",
             "-crf", "22", "-movflags", "+faststart", str(mp4)])  # fmt: skip
        total = sum(e - s for s, e in segs)
        print(f"{mp4.name}: {total:.1f} s from {marks['end']:.1f} s raw ({len(segs)} segments), "
              f"{mp4.stat().st_size / 1e6:.1f} MB")  # fmt: skip
        for name, t in marks.get("events", {}).items():
            print(f"  {name}: {to_cut(t, segs):.1f} s in {mp4.name}")
    else:
        start, dur = a.gif
        gif = HERE / "demo.gif"
        vf = "fps=12,scale=1100:-1:flags=lanczos"
        palette = RAW / "palette.png"
        run(["-ss", str(start), "-t", str(dur), "-i", str(mp4), "-vf", f"{vf},palettegen=max_colors=128", str(palette)])
        run(["-ss", str(start), "-t", str(dur), "-i", str(mp4), "-i", str(palette), "-lavfi",
             f"{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4", str(gif)])  # fmt: skip
        print(f"{gif.name}: {dur:.0f} s, {gif.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
