"""
Local Video Lab Setup
=====================
Creates all test assets needed to run video_editor.py locally:

  avatar_input.mp4              — 5s silent 1920x1080 test video (green-screen colour)
  assets/background_music/lo-fi.mp3  — 10s silent MP3
  assets/b-roll/openai.png      — placeholder image for 'OpenAI' visual cue keyword
  assets/fonts/                 — directory ready for a Hebrew font (e.g. NotoSansHebrew-Regular.ttf)

Requirements: FFmpeg in PATH, requests (pip install requests)
"""
import os
import subprocess
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing: pip install requests")
    sys.exit(1)

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).resolve().parent          # openclaw/
ASSETS     = ROOT / "assets"
MUSIC_DIR  = ASSETS / "background_music"
BROLL_DIR  = ASSETS / "b-roll"
FONTS_DIR  = ASSETS / "fonts"
OUTPUTS    = ROOT / "outputs"

for d in (MUSIC_DIR, BROLL_DIR, FONTS_DIR, OUTPUTS):
    d.mkdir(parents=True, exist_ok=True)


# ── Helper ─────────────────────────────────────────────────────────────────────
def run(cmd: list[str], label: str) -> None:
    print(f"  → {label} ...", end=" ", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FAILED")
        print(result.stderr[-1000:])
        sys.exit(1)
    print("OK")


# ── 1. avatar_input.mp4  (5s, 1920x1080, solid #00B140 green-screen + silent audio) ──
avatar_out = ROOT / "avatar_input.mp4"
if avatar_out.exists():
    print(f"[skip] {avatar_out.name} already exists")
else:
    run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            # Green-screen video source
            "-f", "lavfi", "-i", "color=c=0x00B140:size=1920x1080:rate=30:duration=5",
            # Silent audio source
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", "5",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            str(avatar_out),
        ],
        f"Creating {avatar_out.name} (5s green-screen)",
    )

# ── 2. lo-fi.mp3  (10s silent track in assets/background_music/) ──────────────
music_out = MUSIC_DIR / "lo-fi.mp3"
if music_out.exists():
    print(f"[skip] {music_out.name} already exists")
else:
    run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", "10",
            "-c:a", "libmp3lame", "-b:a", "128k",
            str(music_out),
        ],
        f"Creating {music_out.name} (10s silent MP3)",
    )

# ── 3. openai.png  (placeholder image via requests → assets/b-roll/) ──────────
broll_out = BROLL_DIR / "openai.png"
if broll_out.exists():
    print(f"[skip] {broll_out.name} already exists")
else:
    print("  → Downloading placeholder image for openai.png ...", end=" ", flush=True)
    try:
        # picsum.photos serves random 1920x1080 CC0 photos
        resp = requests.get("https://picsum.photos/1920/1080", timeout=15, stream=True)
        resp.raise_for_status()
        broll_out.write_bytes(resp.content)
        print(f"OK  ({len(resp.content) // 1024} KB)")
    except requests.RequestException as exc:
        print(f"FAILED ({exc})")
        print("  ↳ Generating solid-colour fallback with FFmpeg instead ...")
        run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi",
                "-i", "color=c=0x1a1a2e:size=1920x1080:rate=1:duration=1",
                "-frames:v", "1",
                str(broll_out),
            ],
            f"Fallback {broll_out.name}",
        )

# ── 4. assets/fonts/  (ready for Hebrew font) ─────────────────────────────────
font_hint = FONTS_DIR / "README.txt"
if not any(FONTS_DIR.iterdir()) and not font_hint.exists():
    font_hint.write_text(
        "Place your Hebrew font here, e.g.:\n"
        "  NotoSansHebrew-Regular.ttf\n\n"
        "Free download:\n"
        "  https://fonts.google.com/noto/specimen/Noto+Sans+Hebrew\n"
        "\n"
        "video_editor.py will auto-detect any .ttf/.otf in this folder.\n",
        encoding="utf-8",
    )
    print("  → Created assets/fonts/README.txt with download instructions")
else:
    fonts = [p.name for p in FONTS_DIR.iterdir() if p.suffix in {".ttf", ".otf"}]
    if fonts:
        print(f"  ✓ Font already present: {', '.join(fonts)}")
    else:
        print("  [info] assets/fonts/ is ready — add a Hebrew .ttf to enable RTL subtitles")

# ── Summary ────────────────────────────────────────────────────────────────────
print("\n✅  Lab assets ready:")
print(f"   {ROOT / 'avatar_input.mp4'}")
print(f"   {music_out}")
print(f"   {broll_out}")
print(f"   {FONTS_DIR}/  ← drop NotoSansHebrew-Regular.ttf here")
print(f"   {OUTPUTS}/    ← rendered videos land here")
print()
print("Next step — set env vars and run a local test:")
print()
print('  export RUN_ID=test-001')
print('  export S3_SCRIPTS_BUCKET=mock')
print('  export S3_RAW_VIDEO_BUCKET=mock')
print('  export S3_FINAL_VIDEO_BUCKET=mock')
print('  python -c "from agent.video_editor import run; run()"')
