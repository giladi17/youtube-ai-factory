"""
YouTube AI Factory — Agent Dispatcher
======================================
Entry point for all agent K8s Jobs.
Routes to the correct skill module based on the ROLE environment variable.

Environment variables (injected by orchestrator at Job creation time):
  ROLE    — scriptwriter | avatar_director | video_editor | seo_publisher
  TASK    — human-readable task name (for logging)
  RUN_ID  — unique pipeline run identifier (hex string)

All other config (S3 bucket names, AWS region, Secrets Manager name)
comes from the pipeline-config ConfigMap via envFrom.
"""
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

ROLE   = os.environ.get("ROLE", "")
TASK   = os.environ.get("TASK", "")
RUN_ID = os.environ.get("RUN_ID", "")


def main() -> None:
    if not ROLE:
        logger.error("ROLE environment variable is not set — cannot dispatch")
        sys.exit(1)

    if not RUN_ID:
        logger.error("RUN_ID environment variable is not set — cannot dispatch")
        sys.exit(1)

    logger.info(f"Agent dispatcher | role={ROLE} | task={TASK} | run_id={RUN_ID}")

    if ROLE == "scriptwriter":
        from scriptwriter import run
        run()

    elif ROLE == "avatar_director":
        from avatar_director import run
        run()

    elif ROLE == "video_editor":
        from video_editor import run
        run()

    elif ROLE == "seo_publisher":
        from seo_publisher import run
        run()

    else:
        logger.error(f"Unknown ROLE: '{ROLE}'. Valid values: scriptwriter, avatar_director, video_editor, seo_publisher")
        sys.exit(1)

    logger.info(f"Agent {ROLE} completed successfully | run_id={RUN_ID}")


if __name__ == "__main__":
    main()
