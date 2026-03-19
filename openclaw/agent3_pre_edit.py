"""
Agent 3: Pre-Edit Assembler (Hybrid / CapCut Workflow)
=======================================================
Takes the HeyGen avatar video + script.json and produces a rough-cut
master file ready for CapCut manual polish.

What this agent does:
  - Chromakey the HeyGen green-screen avatar (#00B140)
  - Build a B-roll timeline from script body[] visual_cue + duration_sec
  - Overlay chromakeyed avatar over b-roll
  - Apply background music with auto-ducking (sidechaincompress)
  - Output: pre_edit_master.mp4

What this agent does NOT do (CapCut handles these):
  - Subtitles / captions
  - Text animations / lower thirds
  - Color grading / LUTs
  - Transitions between segments
  - Final sound mix / EQ

Usage:
  python agent3_pre_edit.py --script outputs/run001_script.json --avatar outputs/run001_avatar.mp4
  python agent3_pre_edit.py --script outputs/run001_script.json --avatar outputs/run001_avatar.mp4 --music assets/background_music/lo-fi.mp3

Output:
  outputs/<run_id>_pre_edit_master.mp4
"""
import argparse
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent3-pre-edit")

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT         = Path(__file__).resolve().parent
ASSETS_BASE  = Path(os.environ.get("ASSETS_BASE", str(ROOT / "assets")))
MUSIC_DIR    = ASSETS_BASE / "background_music"
BROLL_DIR    = ASSETS_BASE / "b-roll"
OUTPUTS      = ROOT / "outputs"
OUTPUTS.mkdir(exist_ok=True)

# ── Video settings ─────────────────────────────────────────────────────────────
RESOLUTION         = os.environ.get("TARGET_RESOLUTION", "1920x1080")
FPS                = os.environ.get("TARGET_FPS", "30")
WIDTH, HEIGHT      = map(int, RESOLUTION.split("x"))

# Avatar compositing: centered, lower-third position
AVATAR_W           = int(WIDTH  * 0.45)
AVATAR_H           = int(HEIGHT * 0.80)
AVATAR_X           = (WIDTH - AVATAR_W) // 2
AVATAR_Y           = HEIGHT - AVATAR_H - 40

# Chromakey settings — tuned for HeyGen #00B140 green
CHROMA_COLOR       = "0x00B140"
CHROMA_SIMILARITY  = float(os.environ.get("CHROMA_SIMILARITY", "0.30"))
CHROMA_BLEND       = float(os.environ.get("CHROMA_BLEND",       "0.05"))

# Music ducking: lowers music when avatar is speaking
DUCK_THRESHOLD     = "-25dB"
DUCK_RATIO         = 8
DUCK_ATTACK        = 5    # ms — how fast to duck
DUCK_RELEASE       = 300  # ms — how fast to restore


# ── CLI ────────────────────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(description="Agent 3 — Pre-Edit Assembler for CapCut")
    p.add_argument("--script",  required=True, help="Path to script.json (from Agent 1)")
    p.add_argument("--avatar",  required=True, help="Path to avatar.mp4  (from Agent 2 / HeyGen)")
    p.add_argument("--music",   default=None,  help="Background music file (auto-detected if omitted)")
    p.add_argument("--run-id",  default=None,  help="Run ID for output filename")
    p.add_argument("--out",     default=None,  help="Override full output path")
    return p.parse_args()


# ── Asset resolution ───────────────────────────────────────────────────────────

BROLL_EXTENSIONS = {".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg"}


def _find_music(override: Optional[str]) -> Optional[Path]:
    if override:
        p = Path(override)
        if p.exists():
            return p
        log.warning(f"--music file not found: {p}")
        return None

    candidates = sorted(
        [f for f in MUSIC_DIR.iterdir() if f.suffix.lower() in {".mp3", ".wav", ".aac", ".m4a"}],
        key=lambda f: f.stat().st_size,
        reverse=True,
    ) if MUSIC_DIR.exists() else []

    if candidates:
        log.info(f"Music (auto): {candidates[0].name}")
        return candidates[0]

    log.warning("No background music found — assembling without music")
    return None


def _find_broll(visual_cue: str) -> Optional[Path]:
    """Match keywords from visual_cue against filenames in assets/b-roll/."""
    if not BROLL_DIR.exists():
        return None

    tokens = {t.lower() for t in re.split(r"[\s,./_()\-]+", visual_cue) if len(t) >= 4}
    best_score, best_path = 0, None

    for asset in BROLL_DIR.iterdir():
        if asset.suffix.lower() not in BROLL_EXTENSIONS:
            continue
        stem   = asset.stem.lower()
        score  = sum(1 for t in tokens if t in stem)
        if score > best_score:
            best_score, best_path = score, asset

    if best_path:
        log.info(f"B-roll match (score={best_score}): '{visual_cue[:50]}' -> {best_path.name}")
    else:
        log.info(f"No b-roll match: '{visual_cue[:60]}' — using colour card")
    return best_path


# ── FFmpeg helpers ─────────────────────────────────────────────────────────────

def _run_ffmpeg(cmd: list[str], label: str) -> None:
    log.info(f"[{label}] FFmpeg: {' '.join(cmd[:8])} ...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"[{label}] FAILED:\n{result.stderr[-2000:]}")
        raise RuntimeError(f"FFmpeg failed: {label}")
    log.info(f"[{label}] complete")


def _detect_encoder() -> tuple[str, list[str]]:
    """Try NVENC, then AMF, fall back to libx264."""
    for codec, args, label in [
        ("h264_nvenc",  ["-preset", "p5", "-b:v", "8M"], "NVENC"),
        ("h264_amf",    ["-quality", "speed","-b:v","8M"], "AMF"),
        ("libx264",     ["-preset", "fast", "-crf", "20"], "CPU"),
    ]:
        probe = subprocess.run(
            ["ffmpeg", "-f", "lavfi", "-i", "nullsrc", "-t", "0.1",
             "-c:v", codec, "-f", "null", "-"],
            capture_output=True,
        )
        if probe.returncode == 0:
            log.info(f"Hardware encoder: {label} ({codec})")
            return codec, args
    log.info("Hardware encoder: libx264 (CPU fallback)")
    return "libx264", ["-preset", "fast", "-crf", "20"]


# ── Pass 1: B-roll timeline ────────────────────────────────────────────────────

TONE_EFFECTS = {
    "[FAST]":            "setpts=0.85*PTS",
    "[SLOW]":            "setpts=1.20*PTS",
    "[ENERGETIC]":       "zoompan=z='1.03':d=25:s={w}x{h}",
    "[CALM]":            "null",
    "[DRAMATIC PAUSE]":  "null",   # freeze handled separately
}


def _build_broll_segment(
    asset: Optional[Path],
    duration_sec: int,
    tone: str,
    seg_idx: int,
    tmpdir: Path,
) -> Path:
    out = tmpdir / f"seg_{seg_idx:02d}.mp4"
    w, h = WIDTH, HEIGHT

    if asset and asset.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        # Image → looped video
        base_vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
        effect  = TONE_EFFECTS.get(tone, "null").format(w=w, h=h)
        vf      = f"{base_vf},{effect}" if effect != "null" else base_vf
        _run_ffmpeg([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-i", str(asset),
            "-t", str(duration_sec),
            "-vf", vf,
            "-r", FPS, "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-an",
            str(out),
        ], f"b-roll seg {seg_idx} (image)")

    elif asset and asset.suffix.lower() in {".mp4", ".mov", ".webm"}:
        # Video clip — loop to duration
        effect = TONE_EFFECTS.get(tone, "null").format(w=w, h=h)
        scale  = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
        vf     = f"{scale},{effect}" if effect != "null" else scale
        _run_ffmpeg([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-stream_loop", "-1", "-i", str(asset),
            "-t", str(duration_sec),
            "-vf", vf,
            "-r", FPS, "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-an",
            str(out),
        ], f"b-roll seg {seg_idx} (video)")

    else:
        # No asset — dark gradient colour card
        colour = "0x0d0d1a"
        _run_ffmpeg([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"color=c={colour}:size={w}x{h}:rate={FPS}:duration={duration_sec}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an",
            str(out),
        ], f"b-roll seg {seg_idx} (colour card)")

    return out


def _concat_broll(segments: list[Path], tmpdir: Path) -> Path:
    concat_list = tmpdir / "concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{s.as_posix()}'" for s in segments), encoding="utf-8"
    )
    out = tmpdir / "broll_timeline.mp4"
    _run_ffmpeg([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(out),
    ], "b-roll concat")
    size_mib = out.stat().st_size / (1024 * 1024)
    log.info(f"B-roll timeline ready: {out.name} ({size_mib:.1f} MiB)")
    return out


# ── Pass 2: Final composite ────────────────────────────────────────────────────

def _composite(
    broll_timeline: Path,
    avatar: Path,
    music: Optional[Path],
    output: Path,
    codec: str,
    codec_args: list[str],
) -> None:
    """
    Inputs:
      0 = broll_timeline (video only)
      1 = avatar.mp4     (green-screen video + voice audio)
      2 = music          (optional audio)

    Filtergraph:
      Video: chromakey avatar → overlay on broll → [video_out]
      Audio: sidechain-duck music under voice → [audio_out]
    """
    inputs = ["-i", str(broll_timeline), "-i", str(avatar)]
    music_idx = None
    if music:
        inputs += ["-i", str(music)]
        music_idx = 2

    # Video chain: chromakey → scale → overlay
    video_chain = (
        f"[1:v] chromakey=color={CHROMA_COLOR}:"
        f"similarity={CHROMA_SIMILARITY}:blend={CHROMA_BLEND} [keyed];"
        f"[keyed] scale={AVATAR_W}:{AVATAR_H} [avatar_scaled];"
        f"[0:v] [avatar_scaled] overlay=x={AVATAR_X}:y={AVATAR_Y}:shortest=1 [video_out];"
    )

    # Audio chain
    if music_idx:
        audio_chain = (
            "[1:a] asplit=2[voice][sc];"
            f"[{music_idx}:a] asetpts=PTS-STARTPTS [music_raw];"
            f"[music_raw][sc] sidechaincompress="
            f"threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}:"
            f"attack={DUCK_ATTACK}:release={DUCK_RELEASE}:makeup=1 [ducked];"
            "[voice][ducked] amix=inputs=2:normalize=0:weights=1 0.15 [audio_out]"
        )
    else:
        audio_chain = "[1:a] anull [audio_out]"

    filter_complex = video_chain + audio_chain

    cmd = (
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info"]
        + inputs
        + [
            "-filter_complex", filter_complex,
            "-map", "[video_out]",
            "-map", "[audio_out]",
            "-c:v", codec, *codec_args,
            "-r", FPS,
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(output),
        ]
    )
    _run_ffmpeg(cmd, "final composite")
    size_mib = output.stat().st_size / (1024 * 1024)
    log.info(f"Pre-edit master ready: {output.name} ({size_mib:.1f} MiB)")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()

    script_path = Path(args.script)
    avatar_path = Path(args.avatar)

    for p, label in [(script_path, "script"), (avatar_path, "avatar")]:
        if not p.exists():
            log.error(f"{label} file not found: {p}")
            raise SystemExit(1)

    script  = json.loads(script_path.read_text(encoding="utf-8"))
    run_id  = args.run_id or script_path.stem.replace("_script", "")
    out     = Path(args.out) if args.out else OUTPUTS / f"{run_id}_pre_edit_master.mp4"
    music   = _find_music(args.music)
    codec, codec_args = _detect_encoder()

    body = script.get("body", [])
    log.info(f"Script loaded | segments={len(body)} | run_id={run_id}")

    # ── Pass 1: B-roll timeline ──────────────────────────────────────────────
    with tempfile.TemporaryDirectory(prefix="pre_edit_") as _tmp:
        tmpdir = Path(_tmp)

        segments = []
        for i, seg in enumerate(body):
            asset    = _find_broll(seg.get("visual_cue", ""))
            duration = max(int(seg.get("duration_sec", 10)), 3)
            tone     = seg.get("tone", "[CALM]")
            segments.append(_build_broll_segment(asset, duration, tone, i, tmpdir))

        broll_timeline = _concat_broll(segments, tmpdir)

        # ── Pass 2: Composite ────────────────────────────────────────────────
        _composite(broll_timeline, avatar_path, music, out, codec, codec_args)

    log.info("")
    log.info("=" * 60)
    log.info("  AGENT 3 COMPLETE — Pre-Edit Master Ready for CapCut")
    log.info(f"  Output : {out}")
    log.info("  CapCut checklist:")
    log.info("    [ ] Import pre_edit_master.mp4")
    log.info("    [ ] Add captions (Auto-caption feature)")
    log.info("    [ ] Color grade / apply LUT")
    log.info("    [ ] Add transitions between segments")
    log.info("    [ ] Final sound mix / EQ")
    log.info("    [ ] Export at 1080p / 4K")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
