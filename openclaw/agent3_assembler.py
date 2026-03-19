"""
Agent 3: Faceless Video Assembler (CapCut Workflow)
====================================================
Takes voice.mp3 + script.json and builds a rough-cut master
ready for CapCut manual polish.

Pipeline:
  1. Parse script body[] → extract visual_cue + duration_sec per segment
  2. Build B-roll timeline:
       - Match visual_cue keywords → assets/b-roll/<keyword>.*
       - Trim / loop each clip to its duration_sec
       - Apply tone-based pacing effect ([FAST] / [SLOW] / [ENERGETIC])
       - Dark colour-card fallback when no asset matches
  3. Final composite (single FFmpeg pass):
       - Video : B-roll timeline (full frame, no avatar)
       - Audio : voice.mp3  +  background music (auto-ducked with sidechaincompress)
  4. Output: outputs/<run_id>_pre_edit_master.mp4

CapCut finishes:
  [ ] Auto-captions    [ ] Color grade    [ ] Transitions    [ ] Final mix

Usage:
  python agent3_assembler.py --script outputs/run001_script.json --voice outputs/run001_voice.mp3
  python agent3_assembler.py --script outputs/run001_script.json --voice outputs/run001_voice.mp3 --music assets/background_music/lo-fi.mp3
  python agent3_assembler.py --script outputs/run001_script.json --voice outputs/run001_voice.mp3 --resolution 1280x720

Output:
  outputs/<run_id>_pre_edit_master.mp4
"""
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent3-assembler")

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).resolve().parent
ASSETS_BASE = Path(os.environ.get("ASSETS_BASE", str(ROOT / "assets")))
MUSIC_DIR   = ASSETS_BASE / "background_music"
BROLL_DIR   = ASSETS_BASE / "b-roll"
OUTPUTS     = ROOT / "outputs"
OUTPUTS.mkdir(exist_ok=True)

# ── Video defaults (overridable via CLI) ───────────────────────────────────────
DEFAULT_RESOLUTION = "1920x1080"
DEFAULT_FPS        = "30"

# ── Audio ducking config ───────────────────────────────────────────────────────
DUCK_THRESHOLD = "-24dB"   # music ducks below this level when voice is present
DUCK_RATIO     = 8         # how aggressively to duck
DUCK_ATTACK    = 5         # ms — fade-down speed
DUCK_RELEASE   = 350       # ms — fade-back speed
MUSIC_WEIGHT   = 0.15      # final music volume relative to voice (0.0–1.0)

# ── B-roll file types ──────────────────────────────────────────────────────────
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
BROLL_EXTS = VIDEO_EXTS | IMAGE_EXTS

# ── Tone → FFmpeg pacing effect ────────────────────────────────────────────────
# {W} and {H} are replaced with actual width/height at render time
TONE_EFFECTS = {
    "[FAST]":           "setpts=0.85*PTS",
    "[SLOW]":           "setpts=1.20*PTS",
    "[ENERGETIC]":      "zoompan=z='1.03':d=25:s={W}x{H}",
    "[CALM]":           "null",
    "[DRAMATIC PAUSE]": "null",
}


# ── CLI ────────────────────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(description="Agent 3 — Faceless Video Assembler")
    p.add_argument("--script",     required=True,
                   help="Path to script.json from Agent 1")
    p.add_argument("--voice",      required=True,
                   help="Path to voice.mp3 from Agent 2")
    p.add_argument("--music",      default=None,
                   help="Background music file (auto-detected from assets/background_music/ if omitted)")
    p.add_argument("--resolution", default=DEFAULT_RESOLUTION,
                   help=f"Output resolution WxH (default: {DEFAULT_RESOLUTION})")
    p.add_argument("--fps",        default=DEFAULT_FPS,
                   help=f"Output frame rate (default: {DEFAULT_FPS})")
    p.add_argument("--run-id",     default=None,
                   help="Run ID for output filename (inferred from script filename if omitted)")
    p.add_argument("--out",        default=None,
                   help="Override full output path")
    return p.parse_args()


# ── Encoder detection ──────────────────────────────────────────────────────────

def _detect_encoder() -> tuple[str, list[str]]:
    """Try GPU encoders first, fall back to CPU libx264."""
    for codec, extra, label in [
        ("h264_nvenc", ["-preset", "p5",    "-b:v", "8M"], "NVIDIA NVENC"),
        ("h264_amf",   ["-quality", "speed","-b:v", "8M"], "AMD AMF"),
        ("libx264",    ["-preset", "fast",  "-crf", "20"], "CPU libx264"),
    ]:
        probe = subprocess.run(
            ["ffmpeg", "-f", "lavfi", "-i", "nullsrc", "-t", "0.1",
             "-c:v", codec, "-f", "null", "-"],
            capture_output=True,
        )
        if probe.returncode == 0:
            log.info(f"Encoder: {label} ({codec})")
            return codec, extra
    return "libx264", ["-preset", "fast", "-crf", "20"]


# ── Asset helpers ──────────────────────────────────────────────────────────────

def _find_music(override: Optional[str]) -> Optional[Path]:
    if override:
        p = Path(override)
        return p if p.exists() else None

    if not MUSIC_DIR.exists():
        return None
    tracks = sorted(
        [f for f in MUSIC_DIR.iterdir() if f.suffix.lower() in {".mp3", ".wav", ".aac", ".m4a"}],
        key=lambda f: f.stat().st_size, reverse=True,
    )
    if tracks:
        log.info(f"Music (auto): {tracks[0].name}")
        return tracks[0]
    log.warning("No background music found — assembling without music")
    return None


def _find_broll(visual_cue: str) -> Optional[Path]:
    """Score b-roll assets by keyword overlap with visual_cue."""
    if not BROLL_DIR.exists():
        return None

    tokens = {t.lower() for t in re.split(r"[\s,./_()\-]+", visual_cue) if len(t) >= 4}
    best_score, best_path = 0, None

    for asset in BROLL_DIR.iterdir():
        if asset.suffix.lower() not in BROLL_EXTS:
            continue
        stem  = asset.stem.lower()
        score = sum(1 for t in tokens if t in stem)
        if score > best_score:
            best_score, best_path = score, asset

    if best_path:
        log.info(f"B-roll [{best_score}pt]: '{visual_cue[:55]}' -> {best_path.name}")
    else:
        log.info(f"No b-roll match: '{visual_cue[:60]}' — colour card")
    return best_path


# ── FFmpeg runner ──────────────────────────────────────────────────────────────

def _ffmpeg(args: list[str], label: str) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args
    log.info(f"[{label}] running ...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"[{label}] FAILED:\n{result.stderr[-2000:]}")
        raise RuntimeError(f"FFmpeg error: {label}")
    log.info(f"[{label}] done")


# ── Pass 1: Build B-roll segment clips ────────────────────────────────────────

def _render_segment(
    asset: Optional[Path],
    duration: int,
    tone: str,
    seg_idx: int,
    tmpdir: Path,
    W: int,
    H: int,
    fps: str,
) -> Path:
    out    = tmpdir / f"seg_{seg_idx:02d}.mp4"
    scale  = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    effect = TONE_EFFECTS.get(tone, "null").format(W=W, H=H)
    vf     = f"{scale},{effect}" if effect != "null" else scale
    encode = ["-r", fps, "-c:v", "libx264", "-preset", "ultrafast",
              "-pix_fmt", "yuv420p", "-an"]

    if asset and asset.suffix.lower() in IMAGE_EXTS:
        _ffmpeg(
            ["-loop", "1", "-i", str(asset), "-t", str(duration), "-vf", vf] + encode + [str(out)],
            f"seg {seg_idx} image",
        )
    elif asset and asset.suffix.lower() in VIDEO_EXTS:
        _ffmpeg(
            ["-stream_loop", "-1", "-i", str(asset), "-t", str(duration), "-vf", vf] + encode + [str(out)],
            f"seg {seg_idx} video",
        )
    else:
        # Dark gradient colour card
        _ffmpeg(
            ["-f", "lavfi",
             "-i", f"color=c=0x0d0d1a:size={W}x{H}:rate={fps}:duration={duration}"]
            + encode + [str(out)],
            f"seg {seg_idx} colour card",
        )
    return out


def _concat_segments(segments: list[Path], tmpdir: Path) -> Path:
    concat_txt = tmpdir / "concat.txt"
    concat_txt.write_text(
        "\n".join(f"file '{s.as_posix()}'" for s in segments), encoding="utf-8"
    )
    out = tmpdir / "broll_timeline.mp4"
    _ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", str(concat_txt), "-c", "copy", str(out)],
        "concat b-roll",
    )
    mib = out.stat().st_size / (1024 * 1024)
    log.info(f"B-roll timeline: {out.name} ({mib:.1f} MiB)")
    return out


# ── Pass 2: Final composite ────────────────────────────────────────────────────

def _composite(
    broll:  Path,
    voice:  Path,
    music:  Optional[Path],
    output: Path,
    W: int, H: int, fps: str,
    codec: str, codec_args: list[str],
) -> None:
    """
    Inputs:
      0 = broll_timeline.mp4   (video only)
      1 = voice.mp3            (audio only — the narration)
      2 = music file           (audio only — optional background)

    Filtergraph:
      Video : B-roll passthrough [video_out]
      Audio : sidechaincompress ducks music under voice → amix → [audio_out]
    """
    inputs = ["-i", str(broll), "-i", str(voice)]
    if music:
        inputs += ["-i", str(music)]

    if music:
        filter_complex = (
            # Split voice: one copy goes to output, one is the sidechain detector
            "[1:a] asplit=2[voice_out][sc];"
            # Loop music so it covers the full video length
            "[2:a] asetpts=PTS-STARTPTS,aloop=loop=-1:size=2e9 [music_loop];"
            # Duck music whenever voice is above threshold
            f"[music_loop][sc] sidechaincompress="
            f"threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}:"
            f"attack={DUCK_ATTACK}:release={DUCK_RELEASE}:makeup=1 [ducked];"
            # Mix: voice at full volume, music at MUSIC_WEIGHT
            f"[voice_out][ducked] amix=inputs=2:normalize=0:"
            f"weights=1 {MUSIC_WEIGHT} [audio_out];"
            # Video: passthrough (add subtle vignette for polish)
            "[0:v] vignette=PI/6 [video_out]"
        )
        map_args = ["-map", "[video_out]", "-map", "[audio_out]"]
    else:
        filter_complex = (
            "[1:a] anull [audio_out];"
            "[0:v] vignette=PI/6 [video_out]"
        )
        map_args = ["-map", "[video_out]", "-map", "[audio_out]"]

    cmd = (
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info"]
        + inputs
        + ["-filter_complex", filter_complex]
        + map_args
        + [
            "-c:v", codec, *codec_args,
            "-r", fps, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",          # stop when the voice track ends
            "-movflags", "+faststart",
            str(output),
        ]
    )

    log.info("Rendering final composite ...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"Composite FAILED:\n{result.stderr[-2000:]}")
        raise RuntimeError("Final composite failed")

    mib = output.stat().st_size / (1024 * 1024)
    log.info(f"Output: {output.name}  ({mib:.1f} MiB)")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()

    if not shutil.which("ffmpeg"):
        log.error("FFmpeg not in PATH. Install: https://www.gyan.dev/ffmpeg/builds/")
        raise SystemExit(1)

    script_path = Path(args.script)
    voice_path  = Path(args.voice)
    for p, label in [(script_path, "script"), (voice_path, "voice")]:
        if not p.exists():
            log.error(f"{label} not found: {p}")
            raise SystemExit(1)

    script  = json.loads(script_path.read_text(encoding="utf-8"))
    run_id  = args.run_id or script_path.stem.replace("_script", "")
    output  = Path(args.out) if args.out else OUTPUTS / f"{run_id}_pre_edit_master.mp4"
    music   = _find_music(args.music)

    W, H    = map(int, args.resolution.split("x"))
    fps     = args.fps
    codec, codec_args = _detect_encoder()

    body = script.get("body", [])
    if not body:
        log.error("script.json has no body segments")
        raise SystemExit(1)

    total_dur = sum(max(int(s.get("duration_sec", 10)), 3) for s in body)
    log.info(f"Script: {len(body)} segments | total={total_dur}s | {args.resolution}@{fps}fps")

    with tempfile.TemporaryDirectory(prefix="assembler_") as _tmp:
        tmpdir = Path(_tmp)

        # ── Pass 1: B-roll segments ───────────────────────────────────────────
        segments = []
        for i, seg in enumerate(body):
            asset    = _find_broll(seg.get("visual_cue", ""))
            duration = max(int(seg.get("duration_sec", 10)), 3)
            tone     = seg.get("tone", "[CALM]")
            segments.append(_render_segment(asset, duration, tone, i, tmpdir, W, H, fps))

        broll = _concat_segments(segments, tmpdir)

        # ── Pass 2: Composite ─────────────────────────────────────────────────
        _composite(broll, voice_path, music, output, W, H, fps, codec, codec_args)

    log.info("")
    log.info("=" * 60)
    log.info("  AGENT 3 COMPLETE — Pre-Edit Master Ready for CapCut")
    log.info(f"  File   : {output}")
    log.info(f"  Size   : {output.stat().st_size / (1024*1024):.1f} MiB")
    log.info(f"  Length : ~{total_dur}s")
    log.info("")
    log.info("  CapCut checklist:")
    log.info("    [ ] Import pre_edit_master.mp4")
    log.info("    [ ] Auto-captions (set language to Hebrew)")
    log.info("    [ ] Color grade / apply LUT")
    log.info("    [ ] Add transitions between segments")
    log.info("    [ ] Final EQ + sound mix")
    log.info("    [ ] Export 1080p / 4K for YouTube")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
