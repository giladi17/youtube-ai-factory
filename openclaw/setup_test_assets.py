"""
Local Video Lab Setup
=====================
Creates all test assets needed to run video_editor.py locally.

Generated files:
  avatar_input.mp4                   5s green-screen silent video
  assets/background_music/lo-fi.mp3  10s silent MP3
  assets/b-roll/openai.png           OpenAI placeholder image
  assets/fonts/                      ready for Hebrew font

Strategy (no hard dependency on FFmpeg):
  - Video  : FFmpeg if available, otherwise imageio+numpy (pip install imageio[ffmpeg] numpy)
  - Audio  : FFmpeg if available, otherwise Python wave module (stdlib)
  - Image  : requests download, fallback to Pillow solid-colour
"""
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

# ── Dependency check ───────────────────────────────────────────────────────────
try:
    import requests
except ImportError:
    print("Missing: pip install requests")
    sys.exit(1)

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT      = Path(__file__).resolve().parent
ASSETS    = ROOT / "assets"
MUSIC_DIR = ASSETS / "background_music"
BROLL_DIR = ASSETS / "b-roll"
FONTS_DIR = ASSETS / "fonts"
OUTPUTS   = ROOT / "outputs"

for d in (MUSIC_DIR, BROLL_DIR, FONTS_DIR, OUTPUTS):
    d.mkdir(parents=True, exist_ok=True)

# ── Detect FFmpeg ──────────────────────────────────────────────────────────────
FFMPEG = shutil.which("ffmpeg")
if FFMPEG:
    print(f"[ok] FFmpeg found: {FFMPEG}")
else:
    print("[warn] FFmpeg not found in PATH - using Python fallbacks")
    print("       Install FFmpeg for best results:")
    print("       https://www.gyan.dev/ffmpeg/builds/  (Windows - add bin/ to PATH)")


def ffmpeg(args: list[str], label: str) -> bool:
    """Run an FFmpeg command. Returns True on success, False on failure."""
    if not FFMPEG:
        return False
    print(f"  + {label} ...", end=" ", flush=True)
    r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error"] + args,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"FAILED\n{r.stderr[-500:]}")
        return False
    print("OK")
    return True


# ── 1. avatar_input.mp4 ────────────────────────────────────────────────────────
avatar_out = ROOT / "avatar_input.mp4"
if avatar_out.exists():
    print(f"[skip] {avatar_out.name} already exists")
else:
    done = ffmpeg(
        [
            "-f", "lavfi", "-i", "color=c=0x00B140:size=1920x1080:rate=30:duration=5",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", "5",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            str(avatar_out),
        ],
        f"Creating {avatar_out.name} (5s green-screen video)",
    )
    if not done:
        # Fallback: write a minimal valid MP4 stub (1 green frame, no audio)
        # Uses imageio if available, else writes an empty placeholder
        print("  + Python fallback for avatar_input.mp4 ...", end=" ", flush=True)
        try:
            import numpy as np
            import imageio
            green = np.full((1080, 1920, 3), (0, 177, 64), dtype="uint8")  # #00B140
            writer = imageio.get_writer(str(avatar_out), fps=30, codec="libx264",
                                        output_params=["-pix_fmt", "yuv420p"])
            for _ in range(5 * 30):   # 5 seconds * 30 fps
                writer.append_data(green)
            writer.close()
            print("OK (imageio)")
        except ImportError:
            # Last resort: write a 1-byte placeholder so the path exists
            avatar_out.write_bytes(b"")
            print("STUB (pip install imageio[ffmpeg] numpy for a real video)")

# ── 2. lo-fi.mp3 ───────────────────────────────────────────────────────────────
music_out = MUSIC_DIR / "lo-fi.mp3"
if music_out.exists():
    print(f"[skip] {music_out.name} already exists")
else:
    done = ffmpeg(
        [
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", "10", "-c:a", "libmp3lame", "-b:a", "128k",
            str(music_out),
        ],
        f"Creating {music_out.name} (10s silent MP3)",
    )
    if not done:
        # Fallback: write a silent 10s WAV (stdlib wave module), save as .mp3 name
        # video_editor.py accepts .wav too
        wav_out = music_out.with_suffix(".wav")
        print(f"  + Python fallback: {wav_out.name} (10s silent WAV) ...", end=" ", flush=True)
        sample_rate, channels, duration_s = 44100, 2, 10
        n_frames = sample_rate * duration_s
        with wave.open(str(wav_out), "w") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)           # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(b"\x00" * n_frames * channels * 2)
        # Rename lo-fi.mp3 → lo-fi.wav so video_editor finds it (accepts .wav)
        wav_out.rename(MUSIC_DIR / "lo-fi.wav")
        print("OK")
        music_out = MUSIC_DIR / "lo-fi.wav"

# ── 3. openai.png ─────────────────────────────────────────────────────────────
broll_out = BROLL_DIR / "openai.png"
if broll_out.exists():
    print(f"[skip] {broll_out.name} already exists")
else:
    print("  + Downloading placeholder for openai.png ...", end=" ", flush=True)
    try:
        resp = requests.get("https://picsum.photos/1920/1080", timeout=15, stream=True)
        resp.raise_for_status()
        broll_out.write_bytes(resp.content)
        print(f"OK ({len(resp.content) // 1024} KB)")
    except requests.RequestException as exc:
        print(f"download failed ({exc})")
        # Pillow fallback: solid dark-blue image with "OpenAI B-Roll" label
        print("  + Pillow fallback for openai.png ...", end=" ", flush=True)
        try:
            from PIL import Image, ImageDraw, ImageFont
            img  = Image.new("RGB", (1920, 1080), color=(26, 26, 46))
            draw = ImageDraw.Draw(img)
            draw.text((880, 520), "OpenAI B-Roll", fill=(255, 255, 255))
            img.save(str(broll_out))
            print("OK (Pillow solid colour)")
        except ImportError:
            # Minimal 1x1 white PNG (valid file, FFmpeg can read it)
            MINIMAL_PNG = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
                b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
                b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
                b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            broll_out.write_bytes(MINIMAL_PNG)
            print("OK (1x1 PNG stub)")

# ── 4. assets/fonts/ ──────────────────────────────────────────────────────────
font_hint = FONTS_DIR / "README.txt"
fonts = [p.name for p in FONTS_DIR.iterdir() if p.suffix in {".ttf", ".otf"}]
if fonts:
    print(f"[ok] Font present: {', '.join(fonts)}")
elif not font_hint.exists():
    font_hint.write_text(
        "Place a Hebrew font here, e.g. NotoSansHebrew-Regular.ttf\n"
        "Download free from: https://fonts.google.com/noto/specimen/Noto+Sans+Hebrew\n"
        "video_editor.py will auto-detect any .ttf/.otf in this folder.\n",
        encoding="utf-8",
    )
    print("[info] assets/fonts/README.txt created - add Hebrew .ttf for RTL subtitles")
else:
    print("[info] assets/fonts/ ready - add NotoSansHebrew-Regular.ttf for RTL subtitles")

# ── Summary ────────────────────────────────────────────────────────────────────
print("\nDone! Lab assets ready:")
print(f"   {ROOT / 'avatar_input.mp4'}")
print(f"   {music_out}")
print(f"   {broll_out}")
print(f"   {FONTS_DIR}  (drop NotoSansHebrew-Regular.ttf here for Hebrew subs)")
print(f"   {OUTPUTS}    (rendered videos will appear here)")
if not FFMPEG:
    print()
    print("NOTE: Install FFmpeg to generate a proper test video:")
    print("  https://www.gyan.dev/ffmpeg/builds/  -> ffmpeg-release-essentials.zip")
    print("  Extract, add the bin/ folder to your system PATH, restart terminal.")
