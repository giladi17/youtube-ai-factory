"""
Agent 2: Hebrew Voiceover Generator
=====================================
Converts script.json → voice.mp3 using OpenAI TTS or ElevenLabs.

Providers:
  openai      — tts-1-hd model, supports Hebrew text natively
                API key: OPENAI_API_KEY env var
                Voices:  nova (default) | alloy | echo | fable | onyx | shimmer

  elevenlabs  — Superior Hebrew quality, dedicated multilingual voices
                API key: ELEVENLABS_API_KEY env var
                Voice:   ELEVENLABS_VOICE_ID env var (default: Rachel)

Usage:
  # OpenAI TTS (default)
  $env:OPENAI_API_KEY = "sk-..."
  python agent2_voice.py --script outputs/run001_script.json

  # ElevenLabs
  $env:ELEVENLABS_API_KEY = "..."
  python agent2_voice.py --script outputs/run001_script.json --provider elevenlabs

  # Custom voice / model
  python agent2_voice.py --script outputs/run001_script.json --voice onyx --model tts-1

Output:
  outputs/<run_id>_voice.mp3
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent2-voice")

ROOT    = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
OUTPUTS.mkdir(exist_ok=True)

# ElevenLabs multilingual v2 voice — good Hebrew quality
# Browse voices: https://elevenlabs.io/voice-library
DEFAULT_EL_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # Rachel


# ── Text extraction ────────────────────────────────────────────────────────────

def _extract_text(script: dict) -> str:
    """
    Build the full voiceover text from script.json.
    Uses `voiceover_text` if present (clean continuous narration).
    Falls back to concatenating hook[0] + body[].text + cta.
    """
    vt = script.get("voiceover_text", "").strip()
    if vt:
        return vt

    parts = []
    hooks = script.get("hooks", [])
    if hooks:
        parts.append(hooks[0]["text"])
    for seg in script.get("body", []):
        parts.append(seg.get("text", ""))
    cta = script.get("cta", "")
    if cta:
        parts.append(cta)
    return "  ".join(p for p in parts if p)


# ── OpenAI TTS ─────────────────────────────────────────────────────────────────

def _tts_openai(text: str, voice: str, model: str, output: Path) -> None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        log.error("OPENAI_API_KEY not set. Run: $env:OPENAI_API_KEY = 'sk-...'")
        sys.exit(1)

    try:
        from openai import OpenAI
    except ImportError:
        log.error("Missing: pip install openai")
        sys.exit(1)

    client = OpenAI(api_key=api_key)
    log.info(f"OpenAI TTS | model={model} | voice={voice} | chars={len(text)}")

    # OpenAI TTS has a 4096-character limit per request — chunk if needed
    CHUNK_SIZE = 4000
    if len(text) <= CHUNK_SIZE:
        response = client.audio.speech.create(model=model, voice=voice, input=text,
                                               response_format="mp3")
        output.write_bytes(response.content)
    else:
        # Split on sentence boundaries and concatenate chunks
        import tempfile, subprocess, shutil
        chunks = _split_text(text, CHUNK_SIZE)
        chunk_files = []
        with tempfile.TemporaryDirectory(prefix="tts_chunks_") as tmpdir:
            for i, chunk in enumerate(chunks):
                chunk_path = Path(tmpdir) / f"chunk_{i:03d}.mp3"
                log.info(f"  chunk {i+1}/{len(chunks)} ({len(chunk)} chars)")
                resp = client.audio.speech.create(model=model, voice=voice, input=chunk,
                                                   response_format="mp3")
                chunk_path.write_bytes(resp.content)
                chunk_files.append(chunk_path)

            if not shutil.which("ffmpeg"):
                log.error("FFmpeg required to concatenate TTS chunks. Add ffmpeg to PATH.")
                sys.exit(1)

            concat_list = Path(tmpdir) / "concat.txt"
            concat_list.write_text(
                "\n".join(f"file '{p.as_posix()}'" for p in chunk_files), encoding="utf-8"
            )
            subprocess.run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-c", "copy", str(output),
            ], check=True)

    size_kb = output.stat().st_size // 1024
    log.info(f"Voiceover saved → {output.name}  ({size_kb} KB)")


def _split_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks <= max_chars, breaking on sentence ends."""
    sentences = []
    for sent in text.replace(".\n", ". ").split(". "):
        sentences.append(sent.strip() + ". ")

    chunks, buf = [], ""
    for sent in sentences:
        if len(buf) + len(sent) > max_chars:
            if buf:
                chunks.append(buf.strip())
            buf = sent
        else:
            buf += sent
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


# ── ElevenLabs TTS ─────────────────────────────────────────────────────────────

def _tts_elevenlabs(text: str, voice_id: str, output: Path) -> None:
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        log.error("ELEVENLABS_API_KEY not set. Run: $env:ELEVENLABS_API_KEY = '...'")
        sys.exit(1)

    try:
        import requests
    except ImportError:
        log.error("Missing: pip install requests")
        sys.exit(1)

    log.info(f"ElevenLabs TTS | voice_id={voice_id} | chars={len(text)}")

    url     = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",   # best Hebrew quality
        "voice_settings": {
            "stability":        0.50,
            "similarity_boost": 0.75,
            "style":            0.35,
            "use_speaker_boost": True,
        },
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=120, stream=True)
    if not resp.ok:
        log.error(f"ElevenLabs error: {resp.status_code}  {resp.text[:500]}")
        resp.raise_for_status()

    output.write_bytes(resp.content)
    size_kb = output.stat().st_size // 1024
    log.info(f"Voiceover saved → {output.name}  ({size_kb} KB)")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Agent 2 — Hebrew Voiceover Generator")
    parser.add_argument("--script",   required=True, help="Path to script.json")
    parser.add_argument("--provider", default="openai", choices=["openai", "elevenlabs"],
                        help="TTS provider (default: openai)")
    parser.add_argument("--voice",    default="nova",
                        help="OpenAI voice name (default: nova). "
                             "Ignored for ElevenLabs — set ELEVENLABS_VOICE_ID env var.")
    parser.add_argument("--model",    default="tts-1-hd",
                        help="OpenAI TTS model (default: tts-1-hd). tts-1 is faster/cheaper.")
    parser.add_argument("--run-id",   default=None, help="Run ID for output filename")
    parser.add_argument("--out",      default=None, help="Override full output path")
    args = parser.parse_args()

    script_path = Path(args.script)
    if not script_path.exists():
        log.error(f"Script not found: {script_path}")
        sys.exit(1)

    script  = json.loads(script_path.read_text(encoding="utf-8"))
    run_id  = args.run_id or script_path.stem.replace("_script", "")
    output  = Path(args.out) if args.out else OUTPUTS / f"{run_id}_voice.mp3"
    text    = _extract_text(script)

    if not text:
        log.error("No narration text found in script.json")
        sys.exit(1)

    word_count = len(text.split())
    log.info(f"Script: {word_count} words (~{word_count * 60 // 150}s at 150 WPM)")

    if args.provider == "elevenlabs":
        _tts_elevenlabs(text, DEFAULT_EL_VOICE_ID, output)
    else:
        _tts_openai(text, args.voice, args.model, output)

    log.info("")
    log.info("=" * 55)
    log.info("  AGENT 2 COMPLETE")
    log.info(f"  Voiceover : {output}")
    log.info(f"  Next step : python agent3_assembler.py \\")
    log.info(f"                --script {script_path} \\")
    log.info(f"                --voice  {output}")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
