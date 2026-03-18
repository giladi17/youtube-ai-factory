"""
Agent 3: Elite Video Editor  ★ HEAVY ★
========================================
Runs on a Karpenter-provisioned c5.2xlarge node (tainted: workload=video-editor:NoSchedule).

Pipeline:
  1. Download inputs from S3
       - avatar.mp4       (green-screen from HeyGen)
       - script.json      (body[] with visual_cue, tone, duration_sec per segment)
       - background.*     (main backdrop image)
       - music.mp3        (background track)
       - broll assets     (per-segment, matched by visual_cue keywords against S3 filenames)

  2. Asset mapping
       - Extract keywords from each segment's visual_cue
       - Score-match against s3://yt-assets/broll/* filenames
       - Download best match; fall back to looped background

  3. Pass 1 — B-roll timeline (FFmpeg concat)
       - Trim/loop each b-roll clip to segment's duration_sec
       - Apply pacing effect based on tone cue:
           [FAST]            → setpts=0.85*PTS  (slight speed-up)
           [SLOW]            → setpts=1.20*PTS  (slow-down)
           [DRAMATIC PAUSE]  → 1.5s freeze prepended
           [ENERGETIC]       → subtle zoom-in via zoompan
           [CALM]            → no effect
       - Output: broll_timeline.mp4

  4. Pass 2 — Final composite (FFmpeg complex filtergraph)
       a. Chromakey avatar  (#00B140 green screen removal)
       b. Overlay keyed avatar over b-roll timeline
       c. Auto-duck music under voice via sidechaincompress
       d. Burn SRT subtitles with libass (timed from segment durations)
       e. Segment title cards via drawtext (first 2.5s of each segment)
       f. Hardware-accelerated encode: NVENC → AMF → libx264 fallback
       Output: final.mp4

  5. Upload final.mp4 → s3://yt-final-video/{RUN_ID}/final.mp4
  6. Update Redis: run:{RUN_ID}:stage = "video_ready"
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
S3_ASSETS_BUCKET      = os.environ["S3_ASSETS_BUCKET"]
S3_FINAL_VIDEO_BUCKET = os.environ["S3_FINAL_VIDEO_BUCKET"]
AWS_REGION            = os.environ.get("AWS_REGION", "eu-north-1")
REDIS_HOST            = os.environ.get("REDIS_HOST", "redis-service")
RESOLUTION            = os.environ.get("VIDEO_EDITOR_TARGET_RESOLUTION", "1920x1080")
FPS                   = os.environ.get("VIDEO_EDITOR_TARGET_FPS", "30")

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
    keys = _list_prefix(S3_ASSETS_BUCKET, "backgrounds/")
    image_keys = [k for k in keys if k.lower().endswith((".png", ".jpg", ".jpeg"))]
    if not image_keys:
        logger.warning("No backgrounds found — using colour fallback")
        return None
    dest = WORKSPACE / "background" / Path(image_keys[0]).name
    return _download(S3_ASSETS_BUCKET, image_keys[0], dest)


def _get_music() -> Optional[Path]:
    keys = _list_prefix(S3_ASSETS_BUCKET, "music/")
    mp3_keys = [k for k in keys if k.lower().endswith(".mp3")]
    if not mp3_keys:
        logger.warning("No music found — output will be voice-only")
        return None
    key = random.choice(mp3_keys)
    dest = WORKSPACE / "music" / Path(key).name
    return _download(S3_ASSETS_BUCKET, key, dest)


# ─────────────────────────────────────────────────────────────────────────────
# Visual cue → B-roll asset mapping
# ─────────────────────────────────────────────────────────────────────────────

def _build_asset_index(bucket: str, prefix: str = "broll/") -> dict[str, str]:
    """
    Returns {normalised_filename: s3_key} for all video assets under prefix.
    Supports .mp4, .mov, .webm, .jpg, .png (stills become short loops).
    """
    keys = _list_prefix(bucket, prefix)
    video_exts = {".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png"}
    index = {}
    for k in keys:
        ext = Path(k).suffix.lower()
        if ext in video_exts:
            stem = Path(k).stem.lower().replace("-", " ").replace("_", " ")
            index[stem] = k
    return index


def _asset_mapper(visual_cue: str, asset_index: dict[str, str]) -> Optional[str]:
    """
    Score-based keyword match between visual_cue text and asset filenames.
    Returns the S3 key of the best match, or None if no meaningful overlap.
    """
    # Extract meaningful words (≥4 chars) from the cue
    cue_words = set(re.findall(r"\b\w{4,}\b", visual_cue.lower()))
    best_key, best_score = None, 0

    for filename_stem, s3_key in asset_index.items():
        asset_words = set(re.findall(r"\b\w{4,}\b", filename_stem))
        score = len(cue_words & asset_words)
        if score > best_score:
            best_score, best_key = score, s3_key

    if best_score > 0:
        logger.info(f"Asset match (score={best_score}): '{visual_cue[:60]}...' → {best_key}")
    return best_key if best_score > 0 else None


def _download_broll_assets(
    body: list[dict],
    asset_index: dict[str, str],
) -> dict[int, Optional[Path]]:
    """Download the best-matching b-roll for each segment. Returns {seg_idx: Path|None}."""
    result: dict[int, Optional[Path]] = {}
    broll_dir = WORKSPACE / "broll"
    broll_dir.mkdir(parents=True, exist_ok=True)

    for i, seg in enumerate(body):
        visual_cue = seg.get("visual_cue", "")
        s3_key = _asset_mapper(visual_cue, asset_index)
        if s3_key:
            dest = broll_dir / f"seg_{i:02d}_{Path(s3_key).name}"
            try:
                _download(S3_ASSETS_BUCKET, s3_key, dest)
                result[i] = dest
            except Exception as exc:
                logger.warning(f"Segment {i} b-roll download failed: {exc}")
                result[i] = None
        else:
            logger.info(f"Segment {i}: no b-roll match for cue '{visual_cue[:50]}...'")
            result[i] = None

    return result


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
) -> None:
    """
    Full composite:
      - avatar chromakey overlay on b-roll timeline
      - auto-ducking music via sidechaincompress
      - subtitle burn-in (libass)
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

        # Subtitle burn-in
        f"[composed] subtitles='{srt_escaped}'"
        f":force_style='FontSize={max(20, WIDTH // 80)},"
        f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        f"Outline=2,Shadow=1,MarginV=40' [subbed];"

        # Title cards per segment
        f"[subbed] {drawtext} [video_out];"
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

    # ── 1. Detect hardware encoder ────────────────────────────────────────────
    codec, codec_args = _detect_hw_encoder()

    # ── 2. Download primary inputs ────────────────────────────────────────────
    avatar_path = _download(S3_RAW_VIDEO_BUCKET, f"{RUN_ID}/avatar.mp4",  WORKSPACE / "avatar.mp4")
    script_path = _download(S3_SCRIPTS_BUCKET,   f"{RUN_ID}/script.json", WORKSPACE / "script.json")
    background  = _get_background()
    music       = _get_music()

    # ── 3. Parse script ───────────────────────────────────────────────────────
    with open(script_path, encoding="utf-8") as f:
        script = json.load(f)

    body: list[dict] = script.get("body", [])
    if not body:
        raise ValueError("script.json missing 'body' segments")

    voiceover_text = script.get("voiceover_text", "")
    if not voiceover_text:
        # Build from body segments as fallback
        voiceover_text = " ".join(seg.get("text", "") for seg in body)

    logger.info(f"Script loaded | segments={len(body)} | "
                f"total_duration={sum(s.get('duration_sec', 60) for s in body):.0f}s")

    # ── 4. Asset mapping — download b-roll per segment ────────────────────────
    asset_index = _build_asset_index(S3_ASSETS_BUCKET, prefix="broll/")
    logger.info(f"Asset index: {len(asset_index)} b-roll items in S3")
    broll_map = _download_broll_assets(body, asset_index)
    matched = sum(1 for v in broll_map.values() if v is not None)
    logger.info(f"B-roll coverage: {matched}/{len(body)} segments matched")

    # ── 5. Generate subtitles (segment-accurate timing) ───────────────────────
    srt_content = _generate_srt(body)
    srt_path    = WORKSPACE / "subtitles.srt"
    srt_path.write_text(srt_content, encoding="utf-8")
    logger.info(f"SRT generated ({srt_content.count('-->') } entries)")

    # ── 6. Pass 1: Build b-roll timeline ──────────────────────────────────────
    broll_timeline = WORKSPACE / "broll_timeline.mp4"
    _render_broll_timeline(body, broll_map, background, broll_timeline)

    # ── 7. Pass 2: Final composite ────────────────────────────────────────────
    output_path = WORKSPACE / "final.mp4"
    _render_final(
        avatar         = avatar_path,
        broll_timeline = broll_timeline,
        music          = music,
        srt            = srt_path,
        body           = body,
        output         = output_path,
        codec          = codec,
        codec_args     = codec_args,
    )

    if not output_path.exists() or output_path.stat().st_size < 50_000:
        raise RuntimeError(f"Output missing or too small: {output_path.stat().st_size} bytes")

    size_mib = output_path.stat().st_size / (1024 * 1024)
    logger.info(f"Final video: {output_path} ({size_mib:.1f} MiB)")

    # ── 8. Upload & update Redis ──────────────────────────────────────────────
    _upload(output_path, S3_FINAL_VIDEO_BUCKET, f"{RUN_ID}/final.mp4")
    _update_redis("video_ready")

    logger.info(f"Elite Video Editor done | run_id={RUN_ID} | {size_mib:.1f} MiB")
