from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from enzo.agents import (
    AgentFailureKind,
    AgentRunnerError,
    AgentRunnerRouter,
    RunnerRoutingPolicy,
)
from enzo.agents.factory import build_agent_runner
from enzo.agents.pi import PiAgentRunner, PiAgentRunnerConfig
from enzo.artifacts.store import ArtifactStore
from enzo.database import Database
from enzo.domain import AgentRequest, AgentRole, AgentWorkload, FeedbackDraft
from enzo.fakes import FakeTaskManagerAdapter
from enzo.orchestrator import Orchestrator

SUCCESS_SERVER = r"""
import json
import os
import sys

request = json.loads(sys.stdin.buffer.readline())
capture = os.environ.get("CAPTURE_PATH")
if capture:
    with open(capture, "w", encoding="utf-8") as stream:
        json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "request": request}, stream)
envelope = {
    "content": (
        "# Intent\n\n## Problem\nP\n## User / Actor\nU\n"
        "## Desired Outcome\nO\n## Scope\nS\n## Out of Scope\nX\n"
        "## Constraints\nC\n## Acceptance Signals\nA\n## Open Questions\nQ"
    ),
    "metadata": {"adapter_value": 42},
}
events = [
    {"id": request["id"], "type": "response", "command": "prompt", "success": True},
    {"type": "agent_start"},
    {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": json.dumps(envelope)}],
            "provider": "test-provider",
            "model": "test-model",
            "usage": {"input": 10, "output": 5, "totalTokens": 15},
            "stopReason": "stop",
        },
    },
    {"type": "agent_settled"},
]
for event in events:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()
sys.stdin.buffer.read()
"""


def write_server(root: Path, source: str) -> Path:
    path = root / "fake_pi.py"
    path.write_text(source, encoding="utf-8")
    return path


def config(
    root: Path,
    server: Path,
    *,
    timeout_seconds: float = 2,
    environment: dict[str, str] | None = None,
) -> PiAgentRunnerConfig:
    return PiAgentRunnerConfig(
        command=(sys.executable, str(server)),
        transcript_root=root / "transcripts",
        scratch_workspace=root / "scratch",
        provider="configured-provider",
        model="configured-model",
        timeout_seconds=timeout_seconds,
        environment=environment or {},
    )


def artifact_request() -> AgentRequest:
    return AgentRequest(
        role=AgentRole.INTENT,
        workload=AgentWorkload.ARTIFACT_GENERATION,
        project_id="project/unsafe",
        feature_id="feature:1",
        artifact_revision_id="revision-1",
        title="Carrier filter",
        unresolved_feedback=(FeedbackDraft("Scope", "Exclude saved searches."),),
        metadata={"required_headings": ["# Intent"]},
    )


def implementation_request(workspace: Path) -> AgentRequest:
    return AgentRequest(
        role=AgentRole.CODER,
        workload=AgentWorkload.IMPLEMENTATION,
        project_id="project-1",
        feature_id="feature-1",
        artifact_revision_id="plan-1",
        title="Carrier filter",
        workspace_path=str(workspace),
        metadata={"approved_spec": "# Specification", "approved_plan": "# Plan"},
    )


def test_pi_rpc_runner_returns_envelope_and_preserves_transcript(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    server = write_server(tmp_path, SUCCESS_SERVER)
    runner = PiAgentRunner(
        config(tmp_path, server, environment={"CAPTURE_PATH": str(capture)})
    )

    result = runner.run(artifact_request())

    assert result.content.startswith("# Intent\n\n## Problem")
    assert result.metadata["adapter_value"] == 42
    assert result.run_info is not None
    assert result.run_info.runner_name == "pi"
    assert result.run_info.runner_kind == "pi-rpc"
    assert result.run_info.provider == "test-provider"
    assert result.run_info.model == "test-model"
    assert result.metadata["pi"]["usage"]["totalTokens"] == 15
    transcript = Path(result.metadata["pi"]["transcript_path"])
    assert transcript.is_file()
    assert transcript.parent.parts[-2:] == ("project-unsafe", "feature-1")
    assert '"type": "agent_settled"' in transcript.read_text(encoding="utf-8")

    invocation = json.loads(capture.read_text(encoding="utf-8"))
    assert invocation["cwd"] == str((tmp_path / "scratch").resolve())
    assert invocation["argv"][:3] == ["--mode", "rpc", "--no-session"]
    assert "--no-tools" in invocation["argv"]
    assert "--no-context-files" in invocation["argv"]
    assert invocation["request"]["type"] == "prompt"
    assert "Exclude saved searches." in invocation["request"]["message"]


def test_pi_implementation_uses_worktree_and_no_shell_tool(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    server = write_server(tmp_path, SUCCESS_SERVER)
    runner = PiAgentRunner(
        config(tmp_path, server, environment={"CAPTURE_PATH": str(capture)})
    )

    runner.run(implementation_request(workspace))

    invocation = json.loads(capture.read_text(encoding="utf-8"))
    assert invocation["cwd"] == str(workspace.resolve())
    tools = invocation["argv"][invocation["argv"].index("--tools") + 1].split(",")
    assert set(tools) == {"read", "write", "edit", "grep", "find", "ls"}
    assert "bash" not in tools


def test_pi_timeout_terminates_process_and_is_retryable(tmp_path: Path) -> None:
    server = write_server(
        tmp_path,
        "import sys, time\nsys.stdin.buffer.readline()\ntime.sleep(30)\n",
    )
    runner = PiAgentRunner(config(tmp_path, server, timeout_seconds=0.05))

    started = time.monotonic()
    with pytest.raises(AgentRunnerError, match="timed out") as raised:
        runner.run(artifact_request())

    assert time.monotonic() - started < 3
    assert raised.value.kind is AgentFailureKind.TIMEOUT
    assert raised.value.retryable is True
    assert list((tmp_path / "transcripts").rglob("*.jsonl"))


def test_pi_rejects_non_jsonl_stdout_as_protocol_failure(tmp_path: Path) -> None:
    server = write_server(
        tmp_path,
        "import sys\nsys.stdin.buffer.readline()\nprint('not-json', flush=True)\n",
    )
    runner = PiAgentRunner(config(tmp_path, server))

    with pytest.raises(AgentRunnerError, match="non-JSONL") as raised:
        runner.run(artifact_request())

    assert raised.value.kind is AgentFailureKind.PROTOCOL
    assert raised.value.retryable is False


def test_pi_classifies_missing_credentials_as_authentication_failure(
    tmp_path: Path,
) -> None:
    server = write_server(
        tmp_path,
        r"""
import json
import sys
request = json.loads(sys.stdin.buffer.readline())
print(json.dumps({
    "id": request["id"],
    "type": "response",
    "command": "prompt",
    "success": False,
    "error": "No API key found; use /login",
}), flush=True)
""",
    )
    runner = PiAgentRunner(config(tmp_path, server))

    with pytest.raises(AgentRunnerError, match="No API key") as raised:
        runner.run(artifact_request())

    assert raised.value.kind is AgentFailureKind.AUTHENTICATION
    assert raised.value.retryable is False


def test_pi_caps_untrusted_rpc_output(tmp_path: Path) -> None:
    server = write_server(
        tmp_path,
        "import sys\nsys.stdin.buffer.readline()\nprint('x' * 1000, flush=True)\n",
    )
    configured = config(tmp_path, server)
    runner = PiAgentRunner(replace(configured, max_transcript_bytes=100))

    with pytest.raises(AgentRunnerError, match="size limit") as raised:
        runner.run(artifact_request())

    assert raised.value.kind is AgentFailureKind.PROTOCOL
    assert raised.value.retryable is False


def test_pi_config_does_not_inherit_secrets_and_forbids_shell_tools(tmp_path: Path) -> None:
    environ = {
        "PATH": "/bin",
        "HOME": "/safe-home",
        "ENZO_PLANE_API_TOKEN": "must-not-leak",
        "OPENAI_API_KEY": "allowed-key",
        "ENZO_PI_ENV_ALLOWLIST": "OPENAI_API_KEY",
    }

    parsed = PiAgentRunnerConfig.from_env(environ, data_dir=tmp_path)

    assert parsed.environment == {
        "HOME": "/safe-home",
        "PATH": "/bin",
        "OPENAI_API_KEY": "allowed-key",
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_TELEMETRY": "0",
    }
    assert "ENZO_PLANE_API_TOKEN" not in parsed.environment

    with pytest.raises(ValueError, match="shell tools are disabled"):
        PiAgentRunnerConfig.from_env(
            {**environ, "ENZO_PI_IMPLEMENTATION_TOOLS": "read,edit,bash"},
            data_dir=tmp_path,
        )


def test_factory_registers_pi_only_when_explicitly_enabled(tmp_path: Path) -> None:
    fake_only = build_agent_runner({}, data_dir=tmp_path)
    with_pi = build_agent_runner(
        {"ENZO_PI_ENABLED": "true", "ENZO_AGENT_DEFAULT_RUNNER": "pi"},
        data_dir=tmp_path,
    )

    assert fake_only.runner_names == ("fake",)
    assert with_pi.runner_names == ("fake", "pi")


def test_mock_pi_rpc_drives_intent_to_human_review_and_is_audited(
    tmp_path: Path,
) -> None:
    server = write_server(tmp_path, SUCCESS_SERVER)
    pi_runner = PiAgentRunner(config(tmp_path, server))
    router = AgentRunnerRouter(
        [pi_runner],
        RunnerRoutingPolicy(default_runner="pi"),
    )
    database = Database(tmp_path / "state.db")
    database.initialize()
    plane = FakeTaskManagerAdapter()
    orchestrator = Orchestrator(
        database=database,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        agent_runner=router,
        task_manager=plane,
        reviewer_ids={"reviewer-1"},
    )
    feature = plane.create_feature("Carrier filter", "PI-INTENT-1")

    orchestrator.ingest_feature(feature, delivery_id="pi-intent:create")
    orchestrator.drain()

    snapshot = orchestrator.feature_snapshot("PI-INTENT-1")
    assert snapshot["stage"] == "INTENT"
    assert snapshot["artifact"]["artifact_status"] == "REVIEW"
    with database.read() as connection:
        run = connection.execute(
            """SELECT runner_name, runner_kind, provider, model, result_json
               FROM agent_runs"""
        ).fetchone()
    assert (run["runner_name"], run["runner_kind"]) == ("pi", "pi-rpc")
    assert (run["provider"], run["model"]) == ("test-provider", "test-model")
    assert Path(json.loads(run["result_json"])["pi"]["transcript_path"]).is_file()
