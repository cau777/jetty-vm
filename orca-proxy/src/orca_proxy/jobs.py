"""In-memory progress tracking for long Jetty operations."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("jetty.jobs")


class JobManager:
    """Run long operations in the aiohttp loop and expose serializable state."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tasks: set[asyncio.Task] = set()

    def start(
        self,
        kind: str,
        name: str | None,
        steps: list[tuple[str, str]],
        operation: Callable[[Callable[[str], None]], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "kind": kind,
            "name": name,
            "state": "queued",
            "steps": [{"key": key, "label": label, "state": "pending"} for key, label in steps],
            "current_step": None,
            "elapsed_seconds": 0.0,
            "result": None,
            "error": None,
            "_started": time.monotonic(),
            "_current_index": -1,
        }
        self._jobs[job_id] = job
        task = asyncio.create_task(self._run(job, operation), name=f"jetty-job-{kind}-{job_id[:8]}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return self.get(job_id)  # type: ignore[return-value]

    async def _run(
        self,
        job: dict[str, Any],
        operation: Callable[[Callable[[str], None]], Awaitable[dict[str, Any]]],
    ) -> None:
        job["state"] = "running"

        def progress(key: str) -> None:
            index = next((i for i, step in enumerate(job["steps"]) if step["key"] == key), None)
            if index is None:
                raise ValueError(f"Unknown progress step: {key}")
            for i, step in enumerate(job["steps"]):
                if i < index:
                    step["state"] = "done"
                elif i == index:
                    step["state"] = "running"
                elif step["state"] != "failed":
                    step["state"] = "pending"
            job["_current_index"] = index
            job["current_step"] = key

        try:
            job["result"] = await operation(progress)
            for step in job["steps"]:
                step["state"] = "done"
            job["state"] = "done"
        except asyncio.CancelledError:
            job["state"] = "cancelled"
            raise
        except Exception as exc:  # surfaced as a job failure for the UI and CLI
            index = job["_current_index"]
            if index >= 0:
                job["steps"][index]["state"] = "failed"
            elif job["steps"]:
                job["steps"][0]["state"] = "failed"
                job["current_step"] = job["steps"][0]["key"]
            job["error"] = {"message": str(exc)}
            fields = getattr(exc, "fields", None)
            if fields:
                job["error"]["fields"] = fields
            job["state"] = "failed"
            log.exception("Jetty job %s (%s) failed", job["job_id"], job["kind"])
        finally:
            job["elapsed_seconds"] = round(time.monotonic() - job["_started"], 2)

    def get(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        result = copy.deepcopy({key: value for key, value in job.items() if not key.startswith("_")})
        if job["state"] in {"queued", "running"}:
            result["elapsed_seconds"] = round(time.monotonic() - job["_started"], 2)
        return result

    def has_active(self, *kinds: str) -> bool:
        """Return whether any named operation is queued or still running."""
        return any(
            job["kind"] in kinds and job["state"] in {"queued", "running"}
            for job in self._jobs.values()
        )

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
