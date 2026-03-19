"""
Agent 3: Faceless Video Assembler — High-Retention Edition
===========================================================
Takes voice.mp3 + script.json and builds a rough-cut master
ready for CapCut manual polish.

High-Retention Features:
  1. Jump Cuts     — Silences > 0.25s removed from voiceover automatically
  2. Ken Burns     — Slow zoom-in applied to ALL static B-roll images
  3. Whoosh SFX    — Transition sound plays at every B-roll cut point

Pipeline:
  1. Silence removal  → voice_jumpcut.mp3  (FFmpeg silenceremove)
  2. B-roll timeline  → per-segment clips (Ken Burns on images, tone effects on videos)
  3. Final composite  → B-roll + jump-cut voice + auto-ducked music + whoosh SFX
  4. Output          → outputs/<run_id>_pre_edit_master.mp4

CapCut finishes:
  [ ] Auto-captions    [ ] Color grade    [ ] Transitions    [ ] Final mix

Usage:
  python agent3_assembler.py --script outputs/run001_script.json --voice outputs/run001_voice.mp3
  python agent3_assembler.py --script outputs/run001_script.json --voice outputs/run001_voice.mp3 --music assets/background_music/lo-fi.mp3
  python agent3_assembler.py --script outputs/run001_script.json --voice outputs/run001_voice.mp3 --no-jump-cuts

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
SFX_DIR     = ASSETS_BASE / "sfx"
OUTPUTS     = ROOT / "outputs"
OUTPUTS.mkdir(exist_ok=True)

# ── Video defaults ─────────────────────────────────────────────────────────────
DEFAULT_RESOLUTION = "1920x1080"
DEFAULT_FPS        = "30"

# ── Audio: music ducking ───────────────────────────────────────────────────────
DUCK_THRESHOLD = "-24dB"
DUCK_RATIO     = 8
DUCK_ATTACK    = 5      # ms
DUCK_RELEASE   = 350    # ms
MUSIC_WEIGHT   = 0.15   # background music level under voice

# ── Audio: SFX ────────────────────────────────────────────────────────────────
SFX_WEIGHT     = 0.45   # whoosh volume relative to voice

# ── Jump cut: silence removal ─────────────────────────────────────────────────
SILENCE_DURATION  = 0.25   # seconds — silences longer than this get cut
SILENCE_THRESHOLD = "-35dB"

# ── Ken Burns: zoom parameters ─────────────────────────────────────────────────
KB_ZOOM_SPEED  = 0.0012   # zoom increment per frame (total ~1.07x over 60s)
KB_MAX_ZOOM    = 1.10     # maximum zoom level (1.0 = no zoom, 1.1 = 10% zoom)

# ── B-roll file types ──────────────────────────────────────────────────────────
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
BROLL_EXTS = VIDEO_EXTS | IMAGE_EXTS

# ── Tone effects (videos only — images always get Ken Burns instead) ───────────
TONE_EFFECTS = {
    "[FAST]":           "setpts=0.85*PTS",
    "[SLOW]":           "setpts=1.20*PTS",
    "[ENERGETIC]":      "zoompan=z='1.05':d=25:s={W}x{H}",
    "[CALM]":           "null",
    "[DRAMATIC PAUSE]": "null",
}


# ── CLI ────────────────────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(description="Agent 3 — High-Retention Video Assembler")
    p.add_argument("--script",        required=True)
    p.add_argument("--voice",         required=True)
    p.add_argument("--music",         default=None)
    p.add_argument("--resolution",    default=DEFAULT_RESOLUTION)
    p.add_argument("--fps",           default=DEFAULT_FPS)
    p.add_argument("--run-id",        default=None)
    p.add_argument("--out",           default=None)
    p.add_argument("--no-jump-cuts",  action="store_true",
                   help="Skip silence removal (keep all pauses)")
    p.add_argument("--no-sfx",        action="store_true",
                   help="Skip whoosh sound effects")
    return p.parse_args()


# ── Encoder detection ──────────────────────────────────────────────────────────

def _detect_encoder() -> tuple[str, list[str]]:
    for codec, extra, label in [
        ("h264_nvenc", ["-preset", "p5",     "-b:v", "8M"], "NVIDIA NVENC"),
        ("h264_amf",   ["-quality", "speed", "-b:v", "8M"], "AMD AMF"),
        ("libx264",    ["-preset", "fast",   "-crf", "20"], "CPU libx264"),
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


# ── Audio helpers ──────────────────────────────────────────────────────────────

def _get_duration(path: Path) -> float:
    """Return media duration in seconds using ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def _remove_silences(voice: Path, tmpdir: Path) -> Path:
    """
    Feature 1: Jump Cuts
    Remove pauses longer than SILENCE_DURATION from the voiceover.
    Creates aggressive jump-cut rhythm identical to MrBeast / Ali Abdaal style.
    """
    out = tmpdir / "voice_jumpcut.mp3"
    _ffmpeg([
        "-i", str(voice),
        "-af", (
            f"silenceremove="
            f"stop_periods=-1:"
            f"stop_duration={SILENCE_DURATION}:"
            f"stop_threshold={SILENCE_THRESHOLD}"
        ),
        str(out),
    ], "jump-cut silence removal")

    orig_dur = _get_duration(voice)
    new_dur  = _get_duration(out)
    removed  = orig_dur - new_dur
    pct      = removed / orig_dur * 100 if orig_dur > 0 else 0
    log.info(f"Jump cuts: {removed:.1f}s removed ({pct:.0f}% of audio — tighter pacing)")
    return out


# ── SFX helpers ────────────────────────────────────────────────────────────────

def _find_or_generate_sfx(tmpdir: Path) -> Optional[Path]:
    """
    Feature 3: Whoosh / Pop SFX
    Looks for existing SFX in assets/sfx/ first.
    Falls back to generating a synthetic frequency-sweep whoosh via FFmpeg.
    To use a custom sound, place whoosh.mp3 in assets/sfx/.
    """
    # Prefer named files first
    if SFX_DIR.exists():
        for name in ["whoosh", "pop", "swoosh", "transition", "snap"]:
            for ext in [".mp3", ".wav", ".aac", ".ogg"]:
                c = SFX_DIR / f"{name}{ext}"
                if c.exists():
                    log.info(f"SFX: using {c.name} from assets/sfx/")
                    return c
        # Fall back to any audio in sfx/
        for f in SFX_DIR.iterdir():
            if f.suffix.lower() in {".mp3", ".wav", ".aac"}:
                log.info(f"SFX: using {f.name} from assets/sfx/")
                return f

    # Generate a synthetic chirp-sweep whoosh (no external files needed)
    log.info("SFX: generating synthetic whoosh (place assets/sfx/whoosh.mp3 to customise)")
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    out = tmpdir / "whoosh_gen.mp3"
    # Frequency sweep 300→1200 Hz with smooth amplitude envelope
    expr = "0.4*sin(2*PI*(300*t+750*t*t))*sin(PI*t/0.4)"
    _ffmpeg([
        "-f", "lavfi",
        "-i", f"aevalsrc='{expr}:s=44100:c=stereo'",
        "-t", "0.4",
        "-af", "afade=t=in:st=0:d=0.05,afade=t=out:st=0.3:d=0.1,volume=0.7",
        "-c:a", "libmp3lame", "-b:a", "128k",
        str(out),
    ], "generate whoosh SFX")
    return out


# ── B-roll helpers ─────────────────────────────────────────────────────────────

def _find_music(override: Optional[str]) -> Optional[Path]:
    if override:
        p = Path(override)
        return p if p.exists() else None
    if not MUSIC_DIR.exists():
        return None
    tracks = sorted(
        [f for f in MUSIC_DIR.iterdir()
         if f.suffix.lower() in {".mp3", ".wav", ".aac", ".m4a"}],
        key=lambda f: f.stat().st_size, reverse=True,
    )
    if tracks:
        log.info(f"Music (auto): {tracks[0].name}")
        return tracks[0]
    log.warning("No background music — assembling without music")
    return None


def _find_broll(visual_cue: str) -> Optional[Path]:
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
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.error(f"[{label}] FAILED:\n{r.stderr[-2000:]}")
        raise RuntimeError(f"FFmpeg error: {label}")
    log.info(f"[{label}] done")


# ── Pass 1: B-roll segments ────────────────────────────────────────────────────

def _render_segment(
    asset: Optional[Path],
    duration: int,
    tone: str,
    seg_idx: int,
    tmpdir: Path,
    W: int, H: int, fps: str,
) -> Path:
    out    = tmpdir / f"seg_{seg_idx:02d}.mp4"
    encode = ["-r", fps, "-c:v", "libx264", "-preset", "ultrafast",
              "-pix_fmt", "yuv420p", "-an"]

    if asset and asset.suffix.lower() in IMAGE_EXTS:
        # ── Feature 2: Ken Burns effect on ALL static images ─────────────────
        # Upscale first so zoompan has headroom without pixelation
        frames = int(duration) * int(fps)
        ken_burns = (
            f"scale=7680:-1,"
            f"zoompan="
            f"z='min(zoom+{KB_ZOOM_SPEED},{KB_MAX_ZOOM})':"
            f"x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"d={frames}:"
            f"s={W}x{H},"
            f"fps={fps}"
        )
        _ffmpeg(
            ["-loop", "1", "-i", str(asset),
             "-t", str(duration), "-vf", ken_burns]
            + encode + [str(out)],
            f"seg {seg_idx} image+KenBurns",
        )

    elif asset and asset.suffix.lower() in VIDEO_EXTS:
        # Videos have natural motion — apply tone effect only
        scale  = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
        effect = TONE_EFFECTS.get(tone, "null").format(W=W, H=H)
        vf     = f"{scale},{effect}" if effect != "null" else scale
        _ffmpeg(
            ["-stream_loop", "-1", "-i", str(asset),
             "-t", str(duration), "-vf", vf]
            + encode + [str(out)],
            f"seg {seg_idx} video",
        )

    else:
        # Dark colour card fallback
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
    log.info(f"B-roll timeline: {out.stat().st_size / (1024*1024):.1f} MiB")
    return out


# ── Pass 2: Final composite ────────────────────────────────────────────────────

def _build_sfx_chain(
    sfx_input_idx: int,
    transition_ms: list[int],
) -> tuple[str, str]:
    """
    Feature 3: Whoosh at every visual cut.
    Returns (sfx_filter_chain, output_label) to embed in filter_complex.
    transition_ms: list of millisecond timestamps where each new segment starts.
    """
    n = len(transition_ms)
    if n == 0:
        return "", ""

    split_labels = "".join(f"[sfx_r{i}]" for i in range(n))
    delay_chain  = "".join(
        f"[sfx_r{i}] adelay={ms}|{ms} [sfx_d{i}];"
        for i, ms in enumerate(transition_ms)
    )
    mix_inputs   = "".join(f"[sfx_d{i}]" for i in range(n))

    chain = (
        f"[{sfx_input_idx}:a] asplit={n}{split_labels};"
        f"{delay_chain}"
        f"{mix_inputs} amix=inputs={n}:normalize=0 [sfx_track];"
    )
    return chain, "[sfx_track]"


def _composite(
    broll:          Path,
    voice:          Path,
    music:          Optional[Path],
    sfx:            Optional[Path],
    transition_ms:  list[int],
    output:         Path,
    W: int, H: int, fps: str,
    codec: str, codec_args: list[str],
) -> None:
    """
    Input index map:
      0 = broll_timeline   (video)
      1 = voice.mp3        (audio — narration)
      2 = music.mp3        (audio — optional)
      2 or 3 = sfx.mp3    (audio — optional whoosh)

    Filter chains:
      Video  : b-roll → vignette
      Audio  : voice  → sidechain-duck music → mix SFX → [audio_out]
    """
    inputs     = ["-i", str(broll), "-i", str(voice)]
    music_idx  = None
    sfx_idx    = None

    if music:
        inputs    += ["-i", str(music)]
        music_idx  = 2
    if sfx and transition_ms:
        inputs   += ["-i", str(sfx)]
        sfx_idx   = 3 if music else 2

    # ── SFX chain ─────────────────────────────────────────────────────────────
    sfx_chain, sfx_label = ("", "")
    if sfx_idx is not None:
        sfx_chain, sfx_label = _build_sfx_chain(sfx_idx, transition_ms)

    # ── Video chain ───────────────────────────────────────────────────────────
    video_chain = "[0:v] vignette=PI/6 [video_out];"

    # ── Audio chain ───────────────────────────────────────────────────────────
    if music_idx and sfx_label:
        # Voice + ducked music + SFX
        audio_chain = (
            "[1:a] asplit=2[voice_out][sc];"
            f"[{music_idx}:a] asetpts=PTS-STARTPTS,aloop=loop=-1:size=2000000000 [music_loop];"
            f"[music_loop][sc] sidechaincompress="
            f"threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}:"
            f"attack={DUCK_ATTACK}:release={DUCK_RELEASE}:makeup=1 [ducked];"
            f"[voice_out][ducked]{sfx_label} amix=inputs=3:normalize=0:"
            f"weights=1 {MUSIC_WEIGHT} {SFX_WEIGHT} [audio_out]"
        )
    elif music_idx:
        # Voice + ducked music, no SFX
        audio_chain = (
            "[1:a] asplit=2[voice_out][sc];"
            f"[{music_idx}:a] asetpts=PTS-STARTPTS,aloop=loop=-1:size=2000000000 [music_loop];"
            f"[music_loop][sc] sidechaincompress="
            f"threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}:"
            f"attack={DUCK_ATTACK}:release={DUCK_RELEASE}:makeup=1 [ducked];"
            f"[voice_out][ducked] amix=inputs=2:normalize=0:"
            f"weights=1 {MUSIC_WEIGHT} [audio_out]"
        )
    elif sfx_label:
        # Voice + SFX, no music
        audio_chain = (
            f"[1:a]{sfx_label} amix=inputs=2:normalize=0:"
            f"weights=1 {SFX_WEIGHT} [audio_out]"
        )
    else:
        # Voice only
        audio_chain = "[1:a] anull [audio_out]"

    filter_complex = sfx_chain + video_chain + audio_chain

    cmd = (
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info"]
        + inputs
        + ["-filter_complex", filter_complex,
           "-map", "[video_out]", "-map", "[audio_out]",
           "-c:v", codec, *codec_args,
           "-r", fps, "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k",
           "-shortest",
           "-movflags", "+faststart",
           str(output)]
    )

    log.info("Rendering final composite ...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.error(f"Composite FAILED:\n{r.stderr[-3000:]}")
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

    script = json.loads(script_path.read_text(encoding="utf-8"))
    run_id = args.run_id or script_path.stem.replace("_script", "")
    output = Path(args.out) if args.out else OUTPUTS / f"{run_id}_pre_edit_master.mp4"
    music  = _find_music(args.music)
    W, H   = map(int, args.resolution.split("x"))
    fps    = args.fps
    codec, codec_args = _detect_encoder()

    body = script.get("body", [])
    if not body:
        log.error("script.json has no body segments")
        raise SystemExit(1)

    # Calculate segment durations and visual cut timestamps
    durations      = [max(int(s.get("duration_sec", 10)), 3) for s in body]
    total_dur      = sum(durations)

    # Transition timestamps (ms) — where each new segment starts, skip t=0
    transition_ms: list[int] = []
    t = 0
    for dur in durations[:-1]:   # N-1 transitions for N segments
        t += dur
        transition_ms.append(t * 1000)

    log.info(f"Script: {len(body)} segments | {total_dur}s total | "
             f"{len(transition_ms)} visual cuts | {args.resolution}@{fps}fps")

    with tempfile.TemporaryDirectory(prefix="assembler_") as _tmp:
        tmpdir = Path(_tmp)

        # ── Feature 1: Jump cuts ─────────────────────────────────────────────
        if args.no_jump_cuts:
            log.info("Jump cuts: disabled (--no-jump-cuts)")
            voice_final = voice_path
        else:
            voice_final = _remove_silences(voice_path, tmpdir)

        # ── Feature 3: SFX setup ─────────────────────────────────────────────
        sfx = None
        if not args.no_sfx and transition_ms:
            sfx = _find_or_generate_sfx(tmpdir)

        # ── Pass 1: B-roll segments (Feature 2: Ken Burns inside) ─────────────
        segments = []
        for i, seg in enumerate(body):
            asset = _find_broll(seg.get("visual_cue", ""))
            segments.append(
                _render_segment(asset, durations[i], seg.get("tone", "[CALM]"),
                                i, tmpdir, W, H, fps)
            )

        broll = _concat_segments(segments, tmpdir)

        # ── Pass 2: Final composite ────────────────────────────────────────────
        _composite(
            broll         = broll,
            voice         = voice_final,
            music         = music,
            sfx           = sfx,
            transition_ms = transition_ms,
            output        = output,
            W=W, H=H, fps=fps,
            codec=codec, codec_args=codec_args,
        )

    log.info("")
    log.info("=" * 60)
    log.info("  AGENT 3 COMPLETE — Pre-Edit Master Ready for CapCut")
    log.info(f"  File  : {output}")
    log.info(f"  Size  : {output.stat().st_size / (1024*1024):.1f} MiB")
    log.info(f"  Cuts  : {len(transition_ms)} whoosh SFX placed")
    log.info("")
    log.info("  CapCut checklist:")
    log.info("    [ ] Import pre_edit_master.mp4")
    log.info("    [ ] Auto-captions (Hebrew)")
    log.info("    [ ] Color grade")
    log.info("    [ ] Add text animations")
    log.info("    [ ] Final EQ + loudness normalization")
    log.info("    [ ] Export 1080p for YouTube")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
