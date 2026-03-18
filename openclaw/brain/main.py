"""
YouTube AI Factory — Pipeline Orchestrator
==========================================
Replaces the OpenClaw Telegram/Groq brain.

Responsibilities:
  1. POST /run      → start a new pipeline run (spawns Stage 1: Scriptwriter)
  2. GET  /health   → liveness / readiness probe
  3. GET  /status/<run_id> → return current pipeline stage from Redis
  4. Background thread: watch K8s Job completions and chain next stage

Pipeline stages:
  1-scriptwriter  → 2-avatar-director → 3-video-editor → 4-seo-publisher → completed

Job naming convention: <stage-name>-<run_id>
  e.g. scriptwriter-abc12345, video-editor-abc12345
"""
import logging
import os
import threading
import time
import uuid

import redis as redis_lib
from flask import Flask, jsonify, request
from kubernetes import client, config, watch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Kubernetes setup ──────────────────────────────────────────────────────
config.load_incluster_config()
k8s_batch = client.BatchV1Api()
V1Job = client.V1Job
V1JobSpec = client.V1JobSpec
V1ObjectMeta = client.V1ObjectMeta
V1PodTemplateSpec = client.V1PodTemplateSpec
V1PodSpec = client.V1PodSpec
V1Container = client.V1Container
V1EnvVar = client.V1EnvVar
V1EnvFromSource = client.V1EnvFromSource
V1ConfigMapEnvSource = client.V1ConfigMapEnvSource
V1ResourceRequirements = client.V1ResourceRequirements
V1LocalObjectReference = client.V1LocalObjectReference
V1Toleration = client.V1Toleration
V1EmptyDirVolumeSource = client.V1EmptyDirVolumeSource
V1Volume = client.V1Volume
V1VolumeMount = client.V1VolumeMount

# ── Redis setup ───────────────────────────────────────────────────────────
REDIS_HOST = os.environ.get("REDIS_HOST", "redis-service")
redis_client = redis_lib.Redis(host=REDIS_HOST, port=6379, decode_responses=True)

# ── Image configuration ───────────────────────────────────────────────────
AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "giladi17/yt-factory-agent:latest")
VIDEO_EDITOR_IMAGE = os.environ.get("VIDEO_EDITOR_IMAGE", "giladi17/yt-factory-video-editor:latest")

# ── Stage → next stage mapping ────────────────────────────────────────────
PIPELINE_NEXT = {
    "1-scriptwriter":   "2-avatar-director",
    "2-avatar-director":"3-video-editor",
    "3-video-editor":   "4-seo-publisher",
    "4-seo-publisher":  None,  # terminal stage
}

# Namespace per stage (video-editor runs in its own namespace)
STAGE_NAMESPACE = {
    "1-scriptwriter":   "default",
    "2-avatar-director":"default",
    "3-video-editor":   "video-editor",
    "4-seo-publisher":  "default",
}

# ── Common pod fields ─────────────────────────────────────────────────────
COMMON_ENV_FROM = [V1EnvFromSource(config_map_ref=V1ConfigMapEnvSource(name="pipeline-config"))]
PULL_SECRETS    = [V1LocalObjectReference(name="dockerhub-secret")]


# ─────────────────────────────────────────────────────────────────────────
# Job factory functions — one per stage
# ─────────────────────────────────────────────────────────────────────────

def _light_agent_job(stage: str, role: str, task: str, run_id: str) -> V1Job:
    """Returns a K8s Job spec for a light-agent stage (stages 1, 2, 4)."""
    return V1Job(
        metadata=V1ObjectMeta(
            name=f"{role.replace('_', '-')}-{run_id}",
            namespace=STAGE_NAMESPACE[stage],
            labels={
                "run-id":           run_id,
                "pipeline-stage":   stage,
                "app":              f"yt-factory-{role.replace('_', '-')}",
                "app.kubernetes.io/part-of": "yt-factory",
            },
        ),
        spec=V1JobSpec(
            ttl_seconds_after_finished=300,
            backoff_limit=1,
            active_deadline_seconds=2700,  # generous — avatar render can be slow
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels={
                    "run-id":         run_id,
                    "pipeline-stage": stage,
                    "app":            f"yt-factory-{role.replace('_', '-')}",
                    "app.kubernetes.io/part-of": "yt-factory",
                }),
                spec=V1PodSpec(
                    restart_policy="Never",
                    service_account_name=f"{role.replace('_', '-')}-sa",
                    image_pull_secrets=PULL_SECRETS,
                    node_selector={"node.kubernetes.io/pool": "light-agents"},
                    containers=[V1Container(
                        name=role.replace("_", "-"),
                        image=AGENT_IMAGE,
                        image_pull_policy="Always",
                        env=[
                            V1EnvVar(name="RUN_ID", value=run_id),
                            V1EnvVar(name="ROLE",   value=role),
                            V1EnvVar(name="TASK",   value=task),
                        ],
                        env_from=COMMON_ENV_FROM,
                        resources=V1ResourceRequirements(
                            requests={"memory": "256Mi", "cpu": "250m"},
                            limits={"memory": "512Mi", "cpu": "500m"},
                        ),
                    )],
                ),
            ),
        ),
    )


def _video_editor_job(run_id: str) -> V1Job:
    """Returns the heavy K8s Job spec for the video editor (stage 3).

    Targets Karpenter video-editor NodePool (on-demand c5.2xlarge).
    Requires the workload=video-editor:NoSchedule toleration.
    Uses a 20 GiB emptyDir for FFmpeg workspace.
    ttlSecondsAfterFinished=60 ensures fast node reclaim.
    """
    return V1Job(
        metadata=V1ObjectMeta(
            name=f"video-editor-{run_id}",
            namespace="video-editor",
            labels={
                "run-id":           run_id,
                "pipeline-stage":   "3-video-editor",
                "app":              "yt-factory-video-editor",
                "app.kubernetes.io/part-of": "yt-factory",
            },
        ),
        spec=V1JobSpec(
            ttl_seconds_after_finished=60,   # fast teardown → Karpenter reclaims node
            backoff_limit=1,
            active_deadline_seconds=3600,    # 1 hr hard ceiling for FFmpeg
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels={
                    "run-id":         run_id,
                    "pipeline-stage": "3-video-editor",
                    "app":            "yt-factory-video-editor",
                    "app.kubernetes.io/part-of": "yt-factory",
                }),
                spec=V1PodSpec(
                    restart_policy="Never",
                    service_account_name="video-editor-sa",
                    image_pull_secrets=PULL_SECRETS,
                    node_selector={"node.kubernetes.io/pool": "video-editor"},
                    tolerations=[V1Toleration(
                        key="workload",
                        operator="Equal",
                        value="video-editor",
                        effect="NoSchedule",
                    )],
                    containers=[V1Container(
                        name="video-editor",
                        image=VIDEO_EDITOR_IMAGE,
                        image_pull_policy="Always",
                        env=[
                            V1EnvVar(name="RUN_ID", value=run_id),
                            V1EnvVar(name="ROLE",   value="video_editor"),
                            V1EnvVar(name="TASK",   value="render_final_video"),
                        ],
                        env_from=COMMON_ENV_FROM,
                        resources=V1ResourceRequirements(
                            requests={"memory": "4Gi", "cpu": "2"},
                            limits={"memory": "8Gi", "cpu": "4"},
                        ),
                        volume_mounts=[V1VolumeMount(
                            name="render-workspace",
                            mount_path="/tmp/render",
                        )],
                    )],
                    volumes=[V1Volume(
                        name="render-workspace",
                        empty_dir=V1EmptyDirVolumeSource(size_limit="20Gi"),
                    )],
                ),
            ),
        ),
    )


# ── Stage dispatch table ──────────────────────────────────────────────────
def _make_scriptwriter_job(run_id: str) -> V1Job:
    return _light_agent_job("1-scriptwriter", "scriptwriter", "produce_script", run_id)

def _make_avatar_job(run_id: str) -> V1Job:
    return _light_agent_job("2-avatar-director", "avatar_director", "generate_avatar_video", run_id)

def _make_publisher_job(run_id: str) -> V1Job:
    return _light_agent_job("4-seo-publisher", "seo_publisher", "publish_to_youtube", run_id)

STAGE_JOB_FACTORY = {
    "2-avatar-director": _make_avatar_job,
    "3-video-editor":    _video_editor_job,
    "4-seo-publisher":   _make_publisher_job,
}


# ─────────────────────────────────────────────────────────────────────────
# Pipeline state helpers
# ─────────────────────────────────────────────────────────────────────────

def _set_stage(run_id: str, stage: str) -> None:
    redis_client.setex(f"run:{run_id}:stage", 86400, stage)

def _get_stage(run_id: str) -> str:
    return redis_client.get(f"run:{run_id}:stage") or "unknown"

def _mark_failed(run_id: str, stage: str) -> None:
    redis_client.setex(f"run:{run_id}:stage", 86400, f"FAILED:{stage}")
    logger.error(f"Run {run_id} FAILED at stage {stage}")


# ─────────────────────────────────────────────────────────────────────────
# Job watcher — background thread
# ─────────────────────────────────────────────────────────────────────────

def _on_job_event(job: client.V1Job) -> None:
    """Called for every MODIFIED Job event. Chains pipeline stages."""
    labels = job.metadata.labels or {}
    run_id = labels.get("run-id")
    stage  = labels.get("pipeline-stage")

    if not run_id or not stage:
        return  # not a pipeline job

    status = job.status
    if status.succeeded and status.succeeded >= 1:
        next_stage = PIPELINE_NEXT.get(stage)
        if next_stage is None:
            _set_stage(run_id, "completed")
            logger.info(f"Pipeline COMPLETE | run_id={run_id}")
            return

        logger.info(f"Stage complete: {stage} → spawning {next_stage} | run_id={run_id}")
        factory   = STAGE_JOB_FACTORY[next_stage]
        next_job  = factory(run_id)
        namespace = STAGE_NAMESPACE[next_stage]
        k8s_batch.create_namespaced_job(namespace=namespace, body=next_job)
        _set_stage(run_id, next_stage)

    elif status.failed and status.failed >= 2:
        _mark_failed(run_id, stage)


def _watch_loop() -> None:
    """Continuous watch loop with reconnect on error."""
    logger.info("Job watcher thread started")
    while True:
        try:
            w = watch.Watch()
            logger.info("Watching K8s Jobs across all namespaces...")
            for event in w.stream(
                k8s_batch.list_job_for_all_namespaces,
                timeout_seconds=0,
            ):
                if event["type"] == "MODIFIED":
                    _on_job_event(event["object"])
        except Exception as exc:
            logger.warning(f"Job watcher disconnected ({exc}), reconnecting in 5s...")
            time.sleep(5)


# ─────────────────────────────────────────────────────────────────────────
# Flask application
# ─────────────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.get("/health")
def health():
    try:
        redis_client.ping()
        return jsonify({"status": "ok", "redis": "connected"}), 200
    except Exception as exc:
        return jsonify({"status": "degraded", "error": str(exc)}), 503


@app.post("/run")
def trigger_run():
    """Create a new pipeline run.

    Optional JSON body:
      { "source": "cronjob", "schedule": "Monday" }

    Returns:
      { "run_id": "...", "stage": "1-scriptwriter" }
    """
    run_id = uuid.uuid4().hex[:8]
    body   = request.get_json(silent=True) or {}
    source = body.get("source", "manual")

    logger.info(f"New pipeline run | run_id={run_id} | source={source}")

    job = _make_scriptwriter_job(run_id)
    k8s_batch.create_namespaced_job(namespace="default", body=job)
    _set_stage(run_id, "1-scriptwriter")
    redis_client.setex(f"run:{run_id}:source", 86400, source)

    return jsonify({"run_id": run_id, "stage": "1-scriptwriter"}), 202


@app.get("/status/<run_id>")
def get_status(run_id: str):
    stage = _get_stage(run_id)
    return jsonify({"run_id": run_id, "stage": stage}), 200


# ─────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    watcher = threading.Thread(target=_watch_loop, daemon=True, name="job-watcher")
    watcher.start()

    logger.info("Orchestrator HTTP server starting on :8080")
    app.run(host="0.0.0.0", port=8080, threaded=True)


if __name__ == "__main__":
    main()
