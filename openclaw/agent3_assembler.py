"""
Agent 3: Faceless Video Assembler — High-Retention Edition
===========================================================
Fully automated pipeline: DALL-E 3 generates cinematic B-roll images
from visual_cue text, then FFmpeg assembles everything into a polished
rough-cut ready for CapCut.

High-Retention Features:
  1. Jump Cuts      — Silences > 0.25s removed automatically
  2. Ken Burns      — Slow zoom-in on every static image
  3. Whoosh SFX     — Transition sound at every B-roll cut
  4. DALL-E 3       — Auto-generates cinematic B-roll from visual cues
  5. Crossfade      — Smooth 0.5s blend between every clip

Usage:
  # Full auto — DALL-E generates all images:
  $env:OPENAI_API_KEY = "sk-..."
  python agent3_assembler.py --script outputs/run001_script.json --voice outputs/run001_voice.mp3 --dalle

  # Use existing assets folder:
  python agent3_assembler.py --script outputs/run001_script.json --voice outputs/run001_voice.mp3

  # Disable features:
  python agent3_assembler.py ... --no-jump-cuts --no-sfx --no-crossfade

Output:
  outputs/<run_id>_pre_edit_master.mp4
  assets/b-roll/<slug>.png  (DALL-E images saved here, reused on future runs)
"""
import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
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

# ── Feature: crossfade ─────────────────────────────────────────────────────────
CROSSFADE_DUR  = 0.5          # seconds of blend between clips

# ── Feature: DALL-E 3 ─────────────────────────────────────────────────────────
DALLE_STYLE    = (
    "Cinematic lighting, hyper-realistic, highly detailed, 8k resolution, "
    "dramatic composition, no text, no watermark, widescreen 16:9"
)
DALLE_SIZE     = "1792x1024"  # native 16:9 from DALL-E 3
DALLE_QUALITY  = "hd"
DALLE_RETRY    = 3            # retries on rate-limit

# ── Feature: audio ducking ─────────────────────────────────────────────────────
DUCK_THRESHOLD = "-24dB"
DUCK_RATIO     = 8
DUCK_ATTACK    = 5
DUCK_RELEASE   = 350
MUSIC_WEIGHT   = 0.15

# ── Feature: SFX ──────────────────────────────────────────────────────────────
SFX_WEIGHT     = 0.45

# ── Feature: jump cuts ────────────────────────────────────────────────────────
SILENCE_DUR    = 0.25
SILENCE_DB     = "-35dB"

# ── Feature: Ken Burns ────────────────────────────────────────────────────────
KB_SPEED       = 0.0012
KB_MAX         = 1.10

# ── B-roll types ──────────────────────────────────────────────────────────────
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
BROLL_EXTS = VIDEO_EXTS | IMAGE_EXTS

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
    p.add_argument("--dalle",         action="store_true",
                   help="Auto-generate B-roll with DALL-E 3 (requires OPENAI_API_KEY)")
    p.add_argument("--no-jump-cuts",  action="store_true")
    p.add_argument("--no-sfx",        action="store_true")
    p.add_argument("--no-crossfade",  action="store_true")
    return p.parse_args()


# ── Encoder ────────────────────────────────────────────────────────────────────

def _detect_encoder() -> tuple[str, list[str]]:
    for codec, extra, label in [
        ("h264_nvenc", ["-preset", "p5",     "-b:v", "8M"], "NVIDIA NVENC"),
        ("h264_amf",   ["-quality", "speed", "-b:v", "8M"], "AMD AMF"),
        ("libx264",    ["-preset", "fast",   "-crf", "20"], "CPU libx264"),
    ]:
        if subprocess.run(
            ["ffmpeg", "-f", "lavfi", "-i", "nullsrc", "-t", "0.1",
             "-c:v", codec, "-f", "null", "-"],
            capture_output=True,
        ).returncode == 0:
            log.info(f"Encoder: {label}")
            return codec, extra
    return "libx264", ["-preset", "fast", "-crf", "20"]


# ── FFmpeg runner ──────────────────────────────────────────────────────────────

def _ffmpeg(args: list[str], label: str) -> None:
    r = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args,
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        log.error(f"[{label}] FAILED:\n{r.stderr[-2000:]}")
        raise RuntimeError(f"FFmpeg error: {label}")
    log.info(f"[{label}] done")


def _get_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True,
    )
    return float(json.loads(r.stdout)["format"]["duration"])


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE 4: DALL-E 3 B-roll generation
# ══════════════════════════════════════════════════════════════════════════════

def _broll_slug(visual_cue: str) -> str:
    """Convert visual_cue to a clean filename slug (used for caching)."""
    words = [w.lower() for w in re.split(r"[\s,./_()\-]+", visual_cue) if len(w) >= 4]
    slug  = "_".join(words[:4])                    # first 4 meaningful words
    slug  = re.sub(r"[^a-z0-9_]", "", slug)[:60]  # safe filename
    return slug or hashlib.md5(visual_cue.encode()).hexdigest()[:12]


def _generate_broll_dalle(
    body:    list[dict],
    api_key: str,
) -> dict[int, Path]:
    """
    Call DALL-E 3 for each segment's visual_cue.
    Saves images to assets/b-roll/<slug>.png.
    Returns {seg_idx: image_path}.
    Skips segments where a matching asset already exists in BROLL_DIR.
    """
    try:
        from openai import OpenAI
    except ImportError:
        log.error("Missing: pip install openai")
        raise SystemExit(1)

    client   = OpenAI(api_key=api_key)
    BROLL_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[int, Path] = {}

    for i, seg in enumerate(body):
        cue  = seg.get("visual_cue", "").strip()
        if not cue:
            continue

        slug = _broll_slug(cue)
        dest = BROLL_DIR / f"{slug}.png"

        # Cache hit — reuse existing image
        if dest.exists():
            log.info(f"[DALL-E seg {i}] cache hit: {dest.name}")
            results[i] = dest
            continue

        prompt = f"{cue}. {DALLE_STYLE}"
        log.info(f"[DALL-E seg {i}] generating: '{cue[:60]}'")

        for attempt in range(DALLE_RETRY):
            try:
                resp = client.images.generate(
                    model   = "dall-e-3",
                    prompt  = prompt,
                    size    = DALLE_SIZE,
                    quality = DALLE_QUALITY,
                    n       = 1,
                )
                url       = resp.data[0].url
                import urllib.request
                urllib.request.urlretrieve(url, dest)
                size_kb = dest.stat().st_size // 1024
                log.info(f"[DALL-E seg {i}] saved: {dest.name} ({size_kb} KB)")
                results[i] = dest
                break

            except Exception as exc:
                if attempt < DALLE_RETRY - 1:
                    wait = 2 ** attempt * 5
                    log.warning(f"[DALL-E seg {i}] attempt {attempt+1} failed ({exc}) — retry in {wait}s")
                    time.sleep(wait)
                else:
                    log.warning(f"[DALL-E seg {i}] all retries failed — will use local fallback")

    log.info(f"DALL-E: {len(results)}/{len(body)} images generated")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE 1: Jump Cuts
# ══════════════════════════════════════════════════════════════════════════════

def _remove_silences(voice: Path, tmpdir: Path) -> Path:
    out = tmpdir / "voice_jumpcut.mp3"
    _ffmpeg([
        "-i", str(voice),
        "-af", (f"silenceremove=stop_periods=-1:"
                f"stop_duration={SILENCE_DUR}:"
                f"stop_threshold={SILENCE_DB}"),
        str(out),
    ], "jump-cut silence removal")
    orig = _get_duration(voice)
    new  = _get_duration(out)
    log.info(f"Jump cuts: removed {orig-new:.1f}s ({(orig-new)/orig*100:.0f}% of audio)")
    return out


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE 3: SFX
# ══════════════════════════════════════════════════════════════════════════════

def _find_or_generate_sfx(tmpdir: Path) -> Optional[Path]:
    if SFX_DIR.exists():
        for name in ["whoosh", "pop", "swoosh", "transition", "snap"]:
            for ext in [".mp3", ".wav", ".aac"]:
                c = SFX_DIR / f"{name}{ext}"
                if c.exists():
                    log.info(f"SFX: {c.name}")
                    return c
        for f in SFX_DIR.iterdir():
            if f.suffix.lower() in {".mp3", ".wav", ".aac"}:
                log.info(f"SFX: {f.name}")
                return f

    log.info("SFX: generating synthetic whoosh (add assets/sfx/whoosh.mp3 to customise)")
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    out  = tmpdir / "whoosh_gen.mp3"
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


# ══════════════════════════════════════════════════════════════════════════════
# Asset helpers
# ══════════════════════════════════════════════════════════════════════════════

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
    log.warning("No background music found")
    return None


def _find_broll(visual_cue: str, dalle_map: dict[int, Path], seg_idx: int) -> Optional[Path]:
    """Check DALL-E map first, then keyword-match assets/b-roll/."""
    if seg_idx in dalle_map:
        return dalle_map[seg_idx]

    if not BROLL_DIR.exists():
        return None
    tokens     = {t.lower() for t in re.split(r"[\s,./_()\-]+", visual_cue) if len(t) >= 4}
    best_score, best_path = 0, None
    for asset in BROLL_DIR.iterdir():
        if asset.suffix.lower() not in BROLL_EXTS:
            continue
        score = sum(1 for t in tokens if t in asset.stem.lower())
        if score > best_score:
            best_score, best_path = score, asset
    if best_path:
        log.info(f"B-roll [{best_score}pt]: '{visual_cue[:50]}' -> {best_path.name}")
    else:
        log.info(f"No b-roll match: '{visual_cue[:55]}' — colour card")
    return best_path


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE 2: Ken Burns  +  Pass 1: B-roll segments
# ══════════════════════════════════════════════════════════════════════════════

def _render_segment(
    asset: Optional[Path], duration: int, tone: str,
    seg_idx: int, tmpdir: Path, W: int, H: int, fps: str,
) -> Path:
    out    = tmpdir / f"seg_{seg_idx:02d}.mp4"
    encode = ["-r", fps, "-c:v", "libx264", "-preset", "ultrafast",
              "-pix_fmt", "yuv420p", "-an"]

    if asset and asset.suffix.lower() in IMAGE_EXTS:
        # Ken Burns: slow zoom-in, always applied to static images
        frames    = int(duration) * int(fps)
        ken_burns = (
            f"scale=7680:-1,"
            f"zoompan=z='min(zoom+{KB_SPEED},{KB_MAX})':"
            f"x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"d={frames}:s={W}x{H},fps={fps}"
        )
        _ffmpeg(
            ["-loop", "1", "-i", str(asset),
             "-t", str(duration), "-vf", ken_burns] + encode + [str(out)],
            f"seg {seg_idx} image+KenBurns",
        )

    elif asset and asset.suffix.lower() in VIDEO_EXTS:
        scale  = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
        effect = TONE_EFFECTS.get(tone, "null").format(W=W, H=H)
        vf     = f"{scale},{effect}" if effect != "null" else scale
        _ffmpeg(
            ["-stream_loop", "-1", "-i", str(asset),
             "-t", str(duration), "-vf", vf] + encode + [str(out)],
            f"seg {seg_idx} video",
        )

    else:
        _ffmpeg(
            ["-f", "lavfi",
             "-i", f"color=c=0x0d0d1a:size={W}x{H}:rate={fps}:duration={duration}"]
            + encode + [str(out)],
            f"seg {seg_idx} colour card",
        )
    return out


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE 5: Crossfade transitions
# ══════════════════════════════════════════════════════════════════════════════

def _crossfade_segments(
    segments:    list[Path],
    durations:   list[int],
    xfade_sec:   float,
    tmpdir:      Path,
) -> Path:
    """
    Chain clips together with smooth xfade blends.
    Each transition is xfade_sec seconds of cross-dissolve.

    xfade offset formula: offset[i] = sum(dur[0..i-1]) - xfade_sec * i
    """
    n = len(segments)

    # Single clip or crossfade disabled
    if n == 1 or xfade_sec <= 0:
        if n == 1:
            return segments[0]
        concat_txt = tmpdir / "concat.txt"
        concat_txt.write_text(
            "\n".join(f"file '{s.as_posix()}'" for s in segments), encoding="utf-8"
        )
        out = tmpdir / "broll_timeline.mp4"
        _ffmpeg(
            ["-f", "concat", "-safe", "0", "-i", str(concat_txt),
             "-c", "copy", str(out)],
            "concat (no crossfade)",
        )
        return out

    # Build xfade filter chain
    inputs = []
    for s in segments:
        inputs += ["-i", str(s)]

    parts   = []
    cur_in  = "[0:v]"
    cumsum  = 0.0

    for i in range(1, n):
        cumsum += durations[i - 1]
        offset  = max(cumsum - xfade_sec * i, 0.001)
        out_lbl = f"[xf{i}]" if i < n - 1 else "[vout]"
        parts.append(
            f"{cur_in}[{i}:v] xfade=transition=fade:"
            f"duration={xfade_sec}:offset={offset:.3f} {out_lbl}"
        )
        cur_in = out_lbl

    out = tmpdir / "broll_timeline.mp4"
    _ffmpeg(
        inputs + [
            "-filter_complex", ";".join(parts),
            "-map", "[vout]",
            "-r", DEFAULT_FPS,
            "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-an",
            str(out),
        ],
        f"crossfade merge ({n} clips, {xfade_sec}s blend)",
    )
    mib = out.stat().st_size / (1024 * 1024)
    log.info(f"B-roll timeline: {out.name} ({mib:.1f} MiB)")
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Pass 2: Final composite
# ══════════════════════════════════════════════════════════════════════════════

def _build_sfx_chain(sfx_idx: int, transition_ms: list[int]) -> tuple[str, str]:
    n = len(transition_ms)
    if n == 0:
        return "", ""
    split  = "".join(f"[sfx_r{i}]" for i in range(n))
    delays = "".join(f"[sfx_r{i}] adelay={ms}|{ms} [sfx_d{i}];" for i, ms in enumerate(transition_ms))
    mix_in = "".join(f"[sfx_d{i}]" for i in range(n))
    chain  = f"[{sfx_idx}:a] asplit={n}{split};{delays}{mix_in} amix=inputs={n}:normalize=0 [sfx_track];"
    return chain, "[sfx_track]"


def _composite(
    broll: Path, voice: Path, music: Optional[Path],
    sfx: Optional[Path], transition_ms: list[int],
    output: Path, W: int, H: int, fps: str,
    codec: str, codec_args: list[str],
) -> None:
    inputs    = ["-i", str(broll), "-i", str(voice)]
    music_idx = sfx_idx = None

    if music:
        inputs    += ["-i", str(music)]
        music_idx  = 2
    if sfx and transition_ms:
        inputs += ["-i", str(sfx)]
        sfx_idx = 3 if music else 2

    sfx_chain, sfx_label = ("", "")
    if sfx_idx is not None:
        sfx_chain, sfx_label = _build_sfx_chain(sfx_idx, transition_ms)

    video_chain = "[0:v] vignette=PI/6 [video_out];"

    if music_idx and sfx_label:
        audio = (
            "[1:a] asplit=2[voice_out][sc];"
            f"[{music_idx}:a] asetpts=PTS-STARTPTS,aloop=loop=-1:size=2000000000 [ml];"
            f"[ml][sc] sidechaincompress=threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}:"
            f"attack={DUCK_ATTACK}:release={DUCK_RELEASE}:makeup=1 [ducked];"
            f"[voice_out][ducked]{sfx_label} amix=inputs=3:normalize=0:"
            f"weights=1 {MUSIC_WEIGHT} {SFX_WEIGHT} [audio_out]"
        )
    elif music_idx:
        audio = (
            "[1:a] asplit=2[voice_out][sc];"
            f"[{music_idx}:a] asetpts=PTS-STARTPTS,aloop=loop=-1:size=2000000000 [ml];"
            f"[ml][sc] sidechaincompress=threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}:"
            f"attack={DUCK_ATTACK}:release={DUCK_RELEASE}:makeup=1 [ducked];"
            f"[voice_out][ducked] amix=inputs=2:normalize=0:weights=1 {MUSIC_WEIGHT} [audio_out]"
        )
    elif sfx_label:
        audio = (
            f"[1:a]{sfx_label} amix=inputs=2:normalize=0:"
            f"weights=1 {SFX_WEIGHT} [audio_out]"
        )
    else:
        audio = "[1:a] anull [audio_out]"

    filter_complex = sfx_chain + video_chain + audio

    r = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info"]
        + inputs
        + ["-filter_complex", filter_complex,
           "-map", "[video_out]", "-map", "[audio_out]",
           "-c:v", codec, *codec_args,
           "-r", fps, "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k",
           "-shortest", "-movflags", "+faststart",
           str(output)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        log.error(f"Composite FAILED:\n{r.stderr[-3000:]}")
        raise RuntimeError("Final composite failed")
    log.info(f"Output: {output.name}  ({output.stat().st_size/(1024*1024):.1f} MiB)")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    args = _parse_args()

    if not shutil.which("ffmpeg"):
        log.error("FFmpeg not in PATH.")
        raise SystemExit(1)

    script_path = Path(args.script)
    voice_path  = Path(args.voice)
    for p, lbl in [(script_path, "script"), (voice_path, "voice")]:
        if not p.exists():
            log.error(f"{lbl} not found: {p}")
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

    durations  = [max(int(s.get("duration_sec", 10)), 3) for s in body]
    total_dur  = sum(durations)
    xfade_sec  = 0.0 if args.no_crossfade else CROSSFADE_DUR

    # SFX timestamps: account for crossfade overlap at each transition
    transition_ms: list[int] = []
    t = 0
    for i, dur in enumerate(durations[:-1]):
        t += dur
        adjusted_ms = int((t - xfade_sec * (i + 1)) * 1000)
        transition_ms.append(max(adjusted_ms, 0))

    log.info(
        f"Pipeline | segments={len(body)} | total={total_dur}s | "
        f"xfade={xfade_sec}s | cuts={len(transition_ms)} | {args.resolution}@{fps}"
    )

    with tempfile.TemporaryDirectory(prefix="assembler_") as _tmp:
        tmpdir = Path(_tmp)

        # ── DALL-E 3 B-roll generation ────────────────────────────────────────
        dalle_map: dict[int, Path] = {}
        if args.dalle:
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not api_key:
                log.error("--dalle requires OPENAI_API_KEY env var")
                raise SystemExit(1)
            log.info(f"DALL-E 3: generating {len(body)} cinematic images ...")
            dalle_map = _generate_broll_dalle(body, api_key)

        # ── Jump cuts ─────────────────────────────────────────────────────────
        voice_final = voice_path
        if not args.no_jump_cuts:
            voice_final = _remove_silences(voice_path, tmpdir)

        # ── SFX ───────────────────────────────────────────────────────────────
        sfx = None
        if not args.no_sfx and transition_ms:
            sfx = _find_or_generate_sfx(tmpdir)

        # ── Pass 1: B-roll segments (Ken Burns inside) ────────────────────────
        segments = []
        for i, seg in enumerate(body):
            asset = _find_broll(seg.get("visual_cue", ""), dalle_map, i)
            segments.append(
                _render_segment(asset, durations[i], seg.get("tone", "[CALM]"),
                                i, tmpdir, W, H, fps)
            )

        # ── Pass 1b: Crossfade merge ──────────────────────────────────────────
        broll = _crossfade_segments(segments, durations, xfade_sec, tmpdir)

        # ── Pass 2: Final composite ───────────────────────────────────────────
        _composite(
            broll=broll, voice=voice_final, music=music,
            sfx=sfx, transition_ms=transition_ms,
            output=output, W=W, H=H, fps=fps,
            codec=codec, codec_args=codec_args,
        )

    log.info("")
    log.info("=" * 60)
    log.info("  AGENT 3 COMPLETE")
    log.info(f"  File   : {output}")
    log.info(f"  Size   : {output.stat().st_size/(1024*1024):.1f} MiB")
    log.info(f"  DALL-E : {len(dalle_map)}/{len(body)} images generated")
    log.info(f"  Cuts   : {len(transition_ms)} SFX whooshes")
    log.info(f"  Xfade  : {xfade_sec}s crossfade between clips")
    log.info("")
    log.info("  CapCut checklist:")
    log.info("    [ ] Auto-captions (Hebrew)")
    log.info("    [ ] Color grade")
    log.info("    [ ] Text animations / titles")
    log.info("    [ ] Final loudness normalization")
    log.info("    [ ] Export 1080p/4K for YouTube")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
