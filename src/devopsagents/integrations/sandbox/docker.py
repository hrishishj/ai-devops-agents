"""Docker sandbox: run untrusted PR code in a locked-down, throw-away container (free).

Isolation, per run:
  * no network (`--network none`), read-only root filesystem, a small tmpfs for /tmp only
  * all Linux capabilities dropped, no-new-privileges, non-root user
  * hard memory, CPU and process limits; the container is killed on timeout
  * NO host mounts and NO Docker socket: the files are sent over stdin and the result comes back on stdout

This is container-grade isolation, not VM-grade. It is appropriate for your own repositories and for
ephemeral CI runners; for code from strangers prefer a VM-backed sandbox. The process that calls this
(the worker) must be able to run `docker`; do NOT mount the Docker socket into a long-lived service
container to achieve that - run the agents on the host or on an ephemeral runner instead.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import uuid
from collections.abc import Callable
from typing import Any

from devopsagents.integrations.sandbox.base import (
    STDIN_RUNNER,
    SandboxResult,
    SandboxUnavailable,
    parse_runner_output,
)

Run = Callable[..., "subprocess.CompletedProcess[str]"]


class DockerSandbox:
    def __init__(
        self,
        image: str = "devopsagents-sandbox",
        *,
        docker_bin: str = "docker",
        memory: str = "512m",
        cpus: str = "1",
        pids_limit: int = 128,
        run: Run = subprocess.run,  # injectable for tests
    ) -> None:
        self._image = image
        self._docker = docker_bin
        self._memory = memory
        self._cpus = cpus
        self._pids = pids_limit
        self._run = run

    def _argv(self, name: str) -> list[str]:
        return [
            self._docker,
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,size=64m",  # noqa: S108 - container-internal scratch space
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--memory",
            self._memory,
            "--memory-swap",
            self._memory,  # no swap
            "--cpus",
            self._cpus,
            "--pids-limit",
            str(self._pids),
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "PYTHONUNBUFFERED=1",
            "--workdir",
            "/tmp",  # noqa: S108
            self._image,
            "python",
            "-c",
            STDIN_RUNNER,
        ]

    def run_pytest(
        self,
        files: dict[str, str],
        target: str,
        timeout_seconds: int = 60,
        cover: list[str] | None = None,
    ) -> SandboxResult:
        name = f"dasbx-{uuid.uuid4().hex[:12]}"
        payload = json.dumps(
            {"files": files, "target": target, "timeout": timeout_seconds, "cover": cover or []}
        )
        try:
            proc: Any = self._run(
                self._argv(name),
                input=payload,
                capture_output=True,
                text=True,
                timeout=timeout_seconds + 30,  # headroom for container start-up
                check=False,
            )
        except FileNotFoundError as exc:
            raise SandboxUnavailable("docker is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired:
            self._kill(name)  # the CLI timing out does not stop the container
            return SandboxResult(-1, "", "", timed_out=True)

        stderr = str(proc.stderr or "")
        if proc.returncode != 0 and "@@RESULT@@" not in str(proc.stdout or ""):
            lowered = stderr.lower()
            if "unable to find image" in lowered or "pull access denied" in lowered:
                raise SandboxUnavailable(
                    f"sandbox image {self._image!r} not found; build it: "
                    "docker build -t devopsagents-sandbox infra/sandbox"
                )
            if "cannot connect to the docker daemon" in lowered or "is the docker daemon running" in lowered:
                raise SandboxUnavailable("the Docker daemon is not running")
            raise SandboxUnavailable(f"docker run failed ({proc.returncode}): {stderr[-300:]}")
        return parse_runner_output(str(proc.stdout or ""), stderr)

    def _kill(self, name: str) -> None:
        # best effort; --rm and the memory/pids limits still bound the damage if this fails
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            self._run([self._docker, "kill", name], capture_output=True, text=True, timeout=20, check=False)
