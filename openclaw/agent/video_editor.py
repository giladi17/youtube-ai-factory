"""
Agent 3: Elite Video Editor  ★ HEAVY ★
========================================
Runs on a Karpenter-provisioned c5.2xlarge node (tainted: workload=video-editor:NoSchedule).

Asset resolution order (local-first, S3 fallback):
  assets/background_music/   — ambient music track
  assets/b-roll/             — keyword-matched clips (e.g. openai.mp4 for "OpenAI" cues)
  assets/fonts/              — Hebrew/RTL font for libass subtitle rendering
  assets/overlays/           — optional static overlays

Pipeline:
  1. Download primary inputs from S3
       - avatar.mp4       (green-screen from HeyGen)
       - script.json      (body[] with visual_cue, tone, duration_sec per segment)
  2. Resolve assets  (local assets/ → S3 fallback)
       - background music from assets/background_music/
       - b-roll per segment: keyword match visual_cue → assets/b-roll/<keyword>.*
       - Hebrew font from assets/fonts/ for RTL subtitles
  3. Pass 1 — B-roll timeline (FFmpeg concat)
       - Trim/loop each b-roll clip to segment's duration_sec
       - Apply pacing effect based on tone cue:
           [FAST]            → setpts=0.85*PTS
           [SLOW]            → setpts=1.20*PTS
           [DRAMATIC PAUSE]  → 1.5s freeze prepended
           [ENERGETIC]       → subtle zoom-in via zoompan
           [CALM]            → no effect
  4. Pass 2 — Final composite
       a. Chromakey avatar  (#00B140 green screen removal)
       b. Overlay keyed avatar over b-roll timeline
       c. Auto-duck music under voice via sidechaincompress
       d. Burn SRT subtitles with libass + Hebrew RTL font
       e. Segment title cards via drawtext
       f. Hardware-accelerated encode: NVENC → AMF → libx264 fallback
  5. Save to outputs/{RUN_ID}.mp4  (local)
  6. Upload → s3://yt-final-video/{RUN_ID}/final.mp4
  7. Update Redis: run:{RUN_ID}:stage = "video_ready"
"""
import json
import logging
import os
import re
import random
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import boto3
import redis

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
RUN_ID                = os.environ["RUN_ID"]
S3_SCRIPTS_BUCKET     = os.environ["S3_SCRIPTS_BUCKET"]
S3_RAW_VIDEO_BUCKET   = os.environ["S3_RAW_VIDEO_BUCKET"]
S3_ASSETS_BUCKET      = os.environ.get("S3_ASSETS_BUCKET", "")
S3_FINAL_VIDEO_BUCKET = os.environ["S3_FINAL_VIDEO_BUCKET"]
AWS_REGION            = os.environ.get("AWS_REGION", "eu-north-1")
REDIS_HOST            = os.environ.get("REDIS_HOST", "redis-service")
RESOLUTION            = os.environ.get("VIDEO_EDITOR_TARGET_RESOLUTION", "1920x1080")
FPS                   = os.environ.get("VIDEO_EDITOR_TARGET_FPS", "30")

# Local asset directories (override with ASSETS_BASE env var for K8s volume mounts)
_HERE        = Path(__file__).resolve().parent
ASSETS_BASE  = Path(os.environ.get("ASSETS_BASE", str(_HERE.parent / "assets")))
MUSIC_DIR    = ASSETS_BASE / "background_music"
BROLL_DIR    = ASSETS_BASE / "b-roll"
FONTS_DIR    = ASSETS_BASE / "fonts"
OVERLAYS_DIR = ASSETS_BASE / "overlays"

# Rendered outputs land here (local) before S3 upload
OUTPUTS_DIR = Path(os.environ.get("OUTPUTS_DIR", str(_HERE.parent / "outputs")))

WORKSPACE = Path("/tmp/render")
WIDTH, HEIGHT = map(int, RESOLUTION.split("x"))

# Avatar compositing position: centered horizontally, bottom-aligned with 40px padding
AVATAR_W   = int(WIDTH * 0.45)   # avatar takes 45% of frame width
AVATAR_H   = int(HEIGHT * 0.80)  # 80% of frame height
AVATAR_X   = (WIDTH - AVATAR_W) // 2
AVATAR_Y   = HEIGHT - AVATAR_H - 40


# ─────────────────────────────────────────────────────────────────────────────
# S3 helpers
# ─────────────────────────────────────────────────────────────────────────────

def _s3():
    return boto3.client("s3", region_name=AWS_REGION)


def _download(bucket: str, key: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _s3().download_file(bucket, key, str(dest))
    logger.info(f"↓ s3://{bucket}/{key} → {dest} ({dest.stat().st_size // 1024} KB)")
    return dest


def _list_prefix(bucket: str, prefix: str) -> list[str]:
    resp = _s3().list_objects_v2(Bucket=bucket, Prefix=prefix)
    return [obj["Key"] for obj in resp.get("Contents", [])]


def _upload(local_path: Path, bucket: str, key: str) -> None:
    _s3().upload_file(str(local_path), bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    logger.info(f"↑ {local_path} → s3://{bucket}/{key} ({local_path.stat().st_size // (1024*1024)} MiB)")


# ─────────────────────────────────────────────────────────────────────────────
# Hardware encoder detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_hw_encoder() -> tuple[str, list[str]]:
    """
    Probe available FFmpeg encoders and return the fastest available option.
    Priority: NVENC (NVIDIA) → AMF (AMD) → libx264 (CPU fallback).
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders", "-hide_banner"],
            capture_output=True, text=True, timeout=10,
        )
        if "h264_nvenc" in result.stdout:
            logger.info("Hardware encoder: h264_nvenc (NVIDIA)")
            return "h264_nvenc", ["-preset", "p4", "-rc", "vbr", "-cq", "22", "-b:v", "0"]
        if "h264_amf" in result.stdout:
            logger.info("Hardware encoder: h264_amf (AMD)")
            return "h264_amf", ["-quality", "balanced", "-rc", "vbr_latency", "-qp_i", "22"]
    except Exception as exc:
        logger.warning(f"Encoder probe failed: {exc}")
    logger.info("Hardware encoder: libx264 (CPU fallback)")
    return "libx264", ["-preset", "medium", "-crf", "22"]


# ─────────────────────────────────────────────────────────────────────────────
# Asset resolution
# ─────────────────────────────────────────────────────────────────────────────

def _get_background() -> Optional[Path]:
    # Background images live in assets/b-roll/ root or assets/overlays/
    for search_dir in (OVERLAYS_DIR, BROLL_DIR):
        if search_dir.exists():
            candidates = [
                p for p in search_dir.iterdir()
                if p.suffix.lower() in {".png", ".jpg", ".jpeg"}
                and "background" in p.stem.lower()
            ]
            if candidates:
                logger.info(f"Background: {candidates[0]}")
                return candidates[0]
    # S3 fallback
    if S3_ASSETS_BUCKET:
        keys = _list_prefix(S3_ASSETS_BUCKET, "backgrounds/")
        image_keys = [k for k in keys if k.lower().endswith((".png", ".jpg", ".jpeg"))]
        if image_keys:
            dest = WORKSPACE / "background" / Path(image_keys[0]).name
            return _download(S3_ASSETS_BUCKET, image_keys[0], dest)
    logger.warning("No background found — using dark colour fallback")
    return None


def _get_music() -> Optional[Path]:
    """
    Scan assets/background_music/ for any audio file.
    Falls back to S3 assets/music/ if local dir is empty.
    """
    audio_exts = {".mp3", ".wav", ".aac", ".m4a", ".ogg"}
    if MUSIC_DIR.exists():
        tracks = [p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in audio_exts]
        if tracks:
            chosen = random.choice(tracks)
            logger.info(f"Music (local): {chosen.name}")
            return chosen
    # S3 fallback
    if S3_ASSETS_BUCKET:
        keys = _list_prefix(S3_ASSETS_BUCKET, "music/")
        mp3_keys = [k for k in keys if k.lower().endswith((".mp3", ".wav", ".aac"))]
        if mp3_keys:
            key  = random.choice(mp3_keys)
            dest = WORKSPACE / "music" / Path(key).name
            return _download(S3_ASSETS_BUCKET, key, dest)
    logger.warning("No music found — output will be voice-only")
    return None


def _get_font() -> Optional[Path]:
    """
    Return the first font file found in assets/fonts/.
    Used for Hebrew RTL subtitle rendering via libass fontsdir.
    """
    font_exts = {".ttf", ".otf", ".woff", ".woff2"}
    if FONTS_DIR.exists():
        fonts = [p for p in FONTS_DIR.iterdir() if p.suffix.lower() in font_exts]
        if fonts:
            logger.info(f"Font (local): {fonts[0].name}")
            return fonts[0]
    logger.warning("No font found in assets/fonts/ — libass will use system default")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Visual cue → B-roll asset mapping
# ─────────────────────────────────────────────────────────────────────────────

_MEDIA_EXTS = {".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png"}


def _build_local_asset_index() -> dict[str, Path]:
    """
    Scan assets/b-roll/ and return {normalised_stem: local_path}.
    Stem is lowercased with hyphens/underscores replaced by spaces.
    Example: "openai_demo.mp4" → key "openai demo"
    """
    index: dict[str, Path] = {}
    if not BROLL_DIR.exists():
        return index
    for p in BROLL_DIR.iterdir():
        if p.suffix.lower() in _MEDIA_EXTS:
            stem = p.stem.lower().replace("-", " ").replace("_", " ")
            index[stem] = p
    logger.info(f"Local b-roll index: {len(index)} assets in {BROLL_DIR}")
    return index


def _build_s3_asset_index() -> dict[str, str]:
    """S3 fallback index: {normalised_stem: s3_key}."""
    if not S3_ASSETS_BUCKET:
        return {}
    keys = _list_prefix(S3_ASSETS_BUCKET, "broll/")
    return {
        Path(k).stem.lower().replace("-", " ").replace("_", " "): k
        for k in keys if Path(k).suffix.lower() in _MEDIA_EXTS
    }


def _asset_mapper(visual_cue: str, local_index: dict[str, Path],
                  s3_index: dict[str, str]) -> Optional[Path]:
    """
    Score-based keyword match: visual_cue text vs asset filename stems.
    Checks local assets first; S3 index as fallback.

    Special case: if the cue contains a brand keyword that exactly matches
    a filename stem (e.g. 'OpenAI' → stem 'openai'), that wins outright.
    Returns a local Path (already downloaded if from S3), or None.
    """
    cue_words = set(re.findall(r"\b\w{3,}\b", visual_cue.lower()))

    def _score(stem: str) -> int:
        stem_words = set(re.findall(r"\b\w{3,}\b", stem))
        return len(cue_words & stem_words)

    # Local lookup
    best_local = max(local_index.keys(), key=_score, default=None)
    local_score = _score(best_local) if best_local else 0

    if local_score > 0:
        path = local_index[best_local]
        logger.info(f"B-roll match (local, score={local_score}): "
                    f"'{visual_cue[:55]}' → {path.name}")
        return path

    # S3 fallback
    best_s3 = max(s3_index.keys(), key=_score, default=None)
    s3_score = _score(best_s3) if best_s3 else 0

    if s3_score > 0:
        s3_key = s3_index[best_s3]
        dest   = WORKSPACE / "broll" / Path(s3_key).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            _download(S3_ASSETS_BUCKET, s3_key, dest)
            logger.info(f"B-roll match (S3, score={s3_score}): "
                        f"'{visual_cue[:55]}' → {s3_key}")
            return dest
        except Exception as exc:
            logger.warning(f"S3 b-roll download failed for '{s3_key}': {exc}")

    logger.info(f"No b-roll match for: '{visual_cue[:55]}'")
    return None


def _resolve_broll_assets(
    body: list[dict],
    local_index: dict[str, Path],
    s3_index: dict[str, str],
) -> dict[int, Optional[Path]]:
    """Map each segment index → resolved local Path (or None)."""
    return {
        i: _asset_mapper(seg.get("visual_cue", ""), local_index, s3_index)
        for i, seg in enumerate(body)
    }


# ─────────────────────────────────────────────────────────────────────────────
# Subtitle generation (segment-accurate timing)
# ─────────────────────────────────────────────────────────────────────────────

def _ts(seconds: float) -> str:
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _generate_srt(body: list[dict], words_per_line: int = 8) -> str:
    """
    Build SRT using actual duration_sec per segment for accurate timing.
    Falls back to 150 WPM if duration_sec is missing.
    """
    entries, idx, cursor = [], 1, 0.0
    for seg in body:
        text     = seg.get("text", "")
        duration = float(seg.get("duration_sec", 60))
        words    = text.split()
        if not words:
            cursor += duration
            continue
        secs_per_word = duration / len(words)
        for i in range(0, len(words), words_per_line):
            chunk = words[i : i + words_per_line]
            start = cursor + i * secs_per_word
            end   = cursor + (i + len(chunk)) * secs_per_word
            entries.append(f"{idx}\n{_ts(start)} --> {_ts(end)}\n{' '.join(chunk)}\n")
            idx += 1
        cursor += duration
    return "\n".join(entries)


# ─────────────────────────────────────────────────────────────────────────────
# Pacing effects
# ─────────────────────────────────────────────────────────────────────────────

PACING_FILTERS: dict[str, str] = {
    "[FAST]":            "setpts=0.85*PTS",
    "[SLOW]":            "setpts=1.20*PTS",
    "[ENERGETIC]":       "zoompan=z='min(zoom+0.0015,1.15)':d=1:s={W}x{H}",
    "[CALM]":            "null",
    "[DRAMATIC PAUSE]":  "null",   # handled separately via tpad prepend
}


def _pacing_filter(tone: str) -> str:
    base = PACING_FILTERS.get(tone.strip(), "null")
    return base.replace("{W}", str(WIDTH)).replace("{H}", str(HEIGHT))


# ─────────────────────────────────────────────────────────────────────────────
# Pass 1 — B-roll timeline
# ─────────────────────────────────────────────────────────────────────────────

def _render_broll_timeline(
    body: list[dict],
    broll_map: dict[int, Optional[Path]],
    background: Optional[Path],
    output: Path,
) -> None:
    """
    Build a single video that concatenates all segments' b-roll/fallback clips,
    each trimmed (or looped) to its segment's duration_sec.
    Writes to `output`.
    """
    segment_clips: list[Path] = []
    tmp_dir = WORKSPACE / "tmp_segments"
    tmp_dir.mkdir(exist_ok=True)

    for i, seg in enumerate(body):
        duration = float(seg.get("duration_sec", 60))
        tone     = seg.get("tone", "[CALM]")
        broll    = broll_map.get(i)
        out      = tmp_dir / f"seg_{i:02d}.mp4"

        # Determine source input flags
        if broll and broll.suffix.lower() in {".jpg", ".jpeg", ".png"}:
            # Still image → loop to fill duration
            src_flags = ["-loop", "1", "-t", str(duration), "-i", str(broll)]
        elif broll:
            # Video clip → seek to 0, trim to duration (loop if too short)
            src_flags = ["-stream_loop", "-1", "-t", str(duration), "-i", str(broll)]
        elif background:
            src_flags = ["-loop", "1", "-t", str(duration), "-i", str(background)]
        else:
            # Colour fallback
            src_flags = ["-f", "lavfi", "-t", str(duration),
                         "-i", f"color=c=0x1a1a2e:size={WIDTH}x{HEIGHT}:rate={FPS}"]

        pacing = _pacing_filter(tone)
        tpad   = ""
        if tone.strip() == "[DRAMATIC PAUSE]":
            # Freeze 1.5 s before the segment (tpad prepend blank)
            tpad = f",tpad=start_duration=1.5:start_mode=clone"

        vf = f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease," \
             f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},{pacing}{tpad}"

        cmd = (
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
            + src_flags
            + ["-vf", vf, "-an",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
               "-pix_fmt", "yuv420p", str(out)]
        )
        _run_ffmpeg(cmd, label=f"b-roll seg {i}")
        segment_clips.append(out)

    # Write concat list
    concat_list = tmp_dir / "concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in segment_clips),
        encoding="utf-8",
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(output),
    ]
    _run_ffmpeg(cmd, label="b-roll concat")
    logger.info(f"B-roll timeline: {output} ({output.stat().st_size // (1024*1024)} MiB)")


# ─────────────────────────────────────────────────────────────────────────────
# Pass 2 — Final composite
# ─────────────────────────────────────────────────────────────────────────────

def _build_subtitle_filter(srt_escaped: str, font: Optional[Path]) -> str:
    """
    Build the libass subtitles filter string.
    When a font is provided (Hebrew/RTL support):
      - fontsdir points to assets/fonts/
      - FontName is set to the stem of the font file
      - Direction is not a libass ASS style option; RTL is handled automatically
        by the Unicode BiDi algorithm inside libass when the font covers Hebrew glyphs.
    """
    font_size = max(20, WIDTH // 80)
    base_style = (
        f"FontSize={font_size},"
        f"PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,"
        f"Outline=2,Shadow=1,MarginV=40,"
        f"Alignment=2"          # bottom-center
    )
    if font:
        font_name   = font.stem
        fonts_dir   = str(font.parent).replace("\\", "/")
        style       = f"{base_style},FontName={font_name}"
        sub_filter  = (
            f"[composed] subtitles='{srt_escaped}'"
            f":fontsdir='{fonts_dir}'"
            f":force_style='{style}' [subbed];"
        )
    else:
        sub_filter = (
            f"[composed] subtitles='{srt_escaped}'"
            f":force_style='{base_style}' [subbed];"
        )
    return sub_filter


def _build_drawtext_chain(body: list[dict]) -> str:
    """
    Build a chained drawtext filter string that renders each segment's title
    for the first 2.5 seconds of that segment.
    """
    filters, cursor = [], 0.0
    for seg in body:
        title    = seg.get("title", "").replace("'", "\\'").replace(":", "\\:")
        duration = float(seg.get("duration_sec", 60))
        if title:
            start, end = cursor, cursor + 2.5
            filters.append(
                f"drawtext=text='{title}'"
                f":fontcolor=white:fontsize={max(28, WIDTH // 55)}"
                f":borderw=2:bordercolor=black"
                f":x=(w-text_w)/2:y=h*0.08"
                f":enable='between(t,{start:.2f},{end:.2f})'"
            )
        cursor += duration
    return ",".join(filters) if filters else "null"


def _render_final(
    avatar: Path,
    broll_timeline: Path,
    music: Optional[Path],
    srt: Path,
    body: list[dict],
    output: Path,
    codec: str,
    codec_args: list[str],
    font: Optional[Path] = None,
) -> None:
    """
    Full composite:
      - avatar chromakey overlay on b-roll timeline
      - auto-ducking music via sidechaincompress
      - subtitle burn-in (libass) with optional Hebrew RTL font
      - segment title cards (drawtext)
      - hardware-accelerated encode
    """
    srt_escaped = str(srt.resolve()).replace("\\", "/").replace(":", "\\:")

    inputs = ["-i", str(avatar), "-i", str(broll_timeline)]
    music_idx = None
    if music:
        inputs += ["-i", str(music)]
        music_idx = 2  # 0=avatar, 1=broll_timeline, 2=music

    # ── Video chain ─────────────────────────────────────────────────────────
    # B-roll timeline is already 1920×1080 from pass 1
    # Avatar: chromakey → scale to AVATAR_WxAVATAR_H → overlay at (AVATAR_X, AVATAR_Y)
    drawtext = _build_drawtext_chain(body)

    filter_complex = (
        # Chromakey avatar
        f"[0:v] chromakey=0x00B140:0.3:0.05,"
        f"scale={AVATAR_W}:{AVATAR_H}:force_original_aspect_ratio=decrease,"
        f"pad={AVATAR_W}:{AVATAR_H}:(ow-iw)/2:(oh-ih)/2,setsar=1 [keyed];"
        # Overlay keyed avatar on b-roll timeline
        f"[1:v][keyed] overlay={AVATAR_X}:{AVATAR_Y} [composed];"
        # Subtitle burn-in (Hebrew RTL: fontsdir + libass BiDi)
        + _build_subtitle_filter(srt_escaped, font)
        # Title cards per segment
        + f"[subbed] {drawtext} [video_out];"
    )

    # ── Audio chain (auto-ducking) ───────────────────────────────────────────
    if music_idx:
        filter_complex += (
            # Split voice for sidechain
            f"[0:a] asplit=2[voice][sc];"
            # Sidechain compress music under voice
            f"[{music_idx}:a] asetpts=PTS-STARTPTS [music_raw];"
            f"[music_raw][sc] sidechaincompress="
            f"threshold=-25dB:ratio=8:attack=5:release=300:makeup=1 [ducked];"
            # Final mix: voice at full, ducked music at 0.5 relative
            f"[voice][ducked] amix=inputs=2:normalize=0:weights=1 0.5 [audio_out]"
        )
    else:
        filter_complex += "[0:a] anull [audio_out]"

    cmd = (
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info"]
        + inputs
        + [
            "-filter_complex", filter_complex,
            "-map", "[video_out]",
            "-map", "[audio_out]",
            # Video
            "-c:v", codec,
            *codec_args,
            "-r", FPS,
            "-pix_fmt", "yuv420p",
            # Audio
            "-c:a", "aac",
            "-b:a", "192k",
            # Container
            "-movflags", "+faststart",
            str(output),
        ]
    )
    _run_ffmpeg(cmd, label="final composite")


# ─────────────────────────────────────────────────────────────────────────────
# FFmpeg runner
# ─────────────────────────────────────────────────────────────────────────────

def _run_ffmpeg(cmd: list[str], label: str = "") -> None:
    tag = f"[{label}] " if label else ""
    logger.info(f"{tag}FFmpeg: {' '.join(cmd[:6])} ...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"{tag}FFmpeg stderr (last 3000 chars):\n{result.stderr[-3000:]}")
        raise RuntimeError(f"{tag}FFmpeg exited with code {result.returncode}")
    # Log progress lines (frame= lines) from stderr
    for line in result.stderr.splitlines():
        if line.startswith("frame="):
            logger.debug(f"{tag}{line.strip()}")
    logger.info(f"{tag}FFmpeg complete ✓")


# ─────────────────────────────────────────────────────────────────────────────
# Redis
# ─────────────────────────────────────────────────────────────────────────────

def _update_redis(stage: str) -> None:
    r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
    r.setex(f"run:{RUN_ID}:stage", 86400, stage)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run() -> None:
    logger.info(f"Elite Video Editor starting | run_id={RUN_ID} | {RESOLUTION}@{FPS}fps")
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. Detect hardware encoder ────────────────────────────────────────────
    codec, codec_args = _detect_hw_encoder()

    # ── 2. Download primary inputs from S3 ───────────────────────────────────
    avatar_path = _download(S3_RAW_VIDEO_BUCKET, f"{RUN_ID}/avatar.mp4",  WORKSPACE / "avatar.mp4")
    script_path = _download(S3_SCRIPTS_BUCKET,   f"{RUN_ID}/script.json", WORKSPACE / "script.json")

    # ── 3. Resolve assets (local-first, S3 fallback) ─────────────────────────
    background = _get_background()
    music      = _get_music()
    font       = _get_font()

    logger.info(
        f"Assets resolved | music={'✓' if music else '✗'} "
        f"background={'✓' if background else '✗ (colour fallback)'} "
        f"font={'✓ ' + font.name if font else '✗ (system default)'}"
    )

    # ── 4. Parse script ───────────────────────────────────────────────────────
    with open(script_path, encoding="utf-8") as f:
        script = json.load(f)

    body: list[dict] = script.get("body", [])
    if not body:
        raise ValueError("script.json missing 'body' segments")

    logger.info(
        f"Script loaded | segments={len(body)} | "
        f"total_duration={sum(s.get('duration_sec', 60) for s in body):.0f}s"
    )

    # ── 5. B-roll asset mapping ───────────────────────────────────────────────
    local_index = _build_local_asset_index()
    s3_index    = _build_s3_asset_index()
    broll_map   = _resolve_broll_assets(body, local_index, s3_index)
    matched     = sum(1 for v in broll_map.values() if v is not None)
    logger.info(f"B-roll coverage: {matched}/{len(body)} segments matched")

    # ── 6. Generate subtitles (segment-accurate timing) ───────────────────────
    srt_content = _generate_srt(body)
    srt_path    = WORKSPACE / "subtitles.srt"
    srt_path.write_text(srt_content, encoding="utf-8")
    logger.info(f"SRT generated ({srt_content.count('-->')} entries)")

    # ── 7. Pass 1: Build b-roll timeline ─────────────────────────────────────
    broll_timeline = WORKSPACE / "broll_timeline.mp4"
    _render_broll_timeline(body, broll_map, background, broll_timeline)

    # ── 8. Pass 2: Final composite ────────────────────────────────────────────
    tmp_output = WORKSPACE / "final.mp4"
    _render_final(
        avatar         = avatar_path,
        broll_timeline = broll_timeline,
        music          = music,
        srt            = srt_path,
        body           = body,
        output         = tmp_output,
        codec          = codec,
        codec_args     = codec_args,
        font           = font,
    )

    if not tmp_output.exists() or tmp_output.stat().st_size < 50_000:
        raise RuntimeError(f"Output missing or too small: {tmp_output.stat().st_size} bytes")

    size_mib = tmp_output.stat().st_size / (1024 * 1024)

    # ── 9. Copy to outputs/ ───────────────────────────────────────────────────
    final_local = OUTPUTS_DIR / f"{RUN_ID}.mp4"
    import shutil
    shutil.copy2(tmp_output, final_local)
    logger.info(f"Saved locally → {final_local} ({size_mib:.1f} MiB)")

    # ── 10. Upload to S3 & update Redis ──────────────────────────────────────
    _upload(tmp_output, S3_FINAL_VIDEO_BUCKET, f"{RUN_ID}/final.mp4")
    _update_redis("video_ready")

    logger.info(f"Elite Video Editor done | run_id={RUN_ID} | {size_mib:.1f} MiB")
