from __future__ import annotations

from pathlib import Path
from typing import Mapping

from ..fakes import FakeAgentRunner
from .pi import PiAgentRunner, PiAgentRunnerConfig
from .routing import AgentRunnerRouter, RunnerRoutingPolicy


def build_agent_runner(
    environ: Mapping[str, str],
    *,
    data_dir: Path,
) -> AgentRunnerRouter:
    """Application composition root for installed runner adapters."""

    runners = [FakeAgentRunner()]
    if _enabled(environ.get("ENZO_PI_ENABLED", "false")):
        runners.append(
            PiAgentRunner(PiAgentRunnerConfig.from_env(environ, data_dir=data_dir))
        )
    return AgentRunnerRouter(runners, RunnerRoutingPolicy.from_env(environ))


def _enabled(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("ENZO_PI_ENABLED must be a boolean")
