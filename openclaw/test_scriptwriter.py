"""
Local test runner for Elite Scriptwriter
=========================================
Mocks: S3, Secrets Manager, Redis
Real:  RSS feeds + GPT-4o call

Usage:
    set OPENAI_API_KEY=sk-...
    python test_scriptwriter.py
"""
import json
import logging
import os
import sys
from unittest.mock import MagicMock, patch

# ── Require API key ───────────────────────────────────────────────────────────
if not os.environ.get("OPENAI_API_KEY"):
    print("\n❌  Set your key first:\n    set OPENAI_API_KEY=sk-...\n")
    sys.exit(1)

# ── Inject fake env vars so scriptwriter.py imports without crashing ──────────
os.environ.setdefault("RUN_ID",               "test-run-001")
os.environ.setdefault("S3_SCRIPTS_BUCKET",    "mock-scripts-bucket")
os.environ.setdefault("SECRETS_MANAGER_NAME", "mock-secret")
os.environ.setdefault("AWS_REGION",           "eu-north-1")
os.environ.setdefault("REDIS_HOST",           "localhost")

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

# ── Patches ───────────────────────────────────────────────────────────────────
mock_s3_client    = MagicMock()
mock_sm_client    = MagicMock()
mock_redis_client = MagicMock()

mock_sm_client.get_secret_value.return_value = {
    "SecretString": json.dumps({"OPENAI_API_KEY": os.environ["OPENAI_API_KEY"]})
}

def mock_boto3_client(service, **kwargs):
    if service == "secretsmanager":
        return mock_sm_client
    if service == "s3":
        return mock_s3_client
    return MagicMock()

captured_script: dict = {}

original_upload = None

def capturing_upload(script: dict) -> None:
    """Intercept the S3 upload and save locally instead."""
    captured_script.update(script)
    out = "script_output.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)
    print(f"\n✅  Script saved locally → {out}\n")

# ── Run ───────────────────────────────────────────────────────────────────────
with patch("boto3.client", side_effect=mock_boto3_client), \
     patch("redis.Redis", return_value=mock_redis_client):

    import agent.scriptwriter as sw
    sw._upload_script = capturing_script = capturing_upload
    sw._update_redis  = lambda stage: print(f"✅  Redis stage → {stage}  (mocked)")

    sw.run()

# ── Pretty-print results ──────────────────────────────────────────────────────
if not captured_script:
    print("❌  No script captured.")
    sys.exit(1)

s = captured_script
SEP = "─" * 70

print(f"\n{'═'*70}")
print(f"  TITLE        : {s.get('title')}")
print(f"  THUMBNAIL    : {s.get('thumbnail_idea')}")
print(f"  EST. DURATION: {s.get('duration_est_sec')}s")
print(f"{'═'*70}\n")

print("📌  HOOKS")
print(SEP)
for h in s.get("hooks", []):
    print(f"  [{h.get('type').upper()}]")
    print(f"  {h.get('text')}\n")

print("🎬  BODY SEGMENTS")
print(SEP)
for seg in s.get("body", []):
    print(f"  [{seg.get('segment')}] {seg.get('title')}  {seg.get('tone')}  ({seg.get('duration_sec')}s)")
    print(f"  VISUAL : {seg.get('visual_cue')}")
    print(f"  SCRIPT : {seg.get('text', '')[:300]}{'...' if len(seg.get('text',''))>300 else ''}")
    print()

print("📣  CTA")
print(SEP)
print(f"  {s.get('cta')}\n")

print("🔑  KEYWORDS:", ", ".join(s.get("keywords", [])))
print(f"\n{'═'*70}\n")
