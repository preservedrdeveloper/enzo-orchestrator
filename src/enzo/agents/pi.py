from __future__ import annotations

import json
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from ..domain import (
    AgentCapability,
    AgentRequest,
    AgentResult,
    AgentRunInfo,
    AgentRunnerSpec,
    AgentWorkload,
)
from .errors import AgentFailureKind, AgentRunnerError
from .prompts import build_agent_prompt

_BASE_ENVIRONMENT = (
    "HOME",
    "LANG",
    "LC_ALL",
    "NODE_EXTRA_CA_CERTS",
    "PATH",
    "SSL_CERT_FILE",
    "TMPDIR",
    "USER",
)
_IMPLEMENTATION_TOOLS = ("read", "write", "edit", "grep", "find", "ls")


@dataclass(frozen=True)
class PiAgentRunnerConfig:
    command: tuple[str, ...]
    transcript_root: Path
    scratch_workspace: Path
    provider: str | None = None
    model: str | None = None
    timeout_seconds: float = 900
    max_transcript_bytes: int = 10 * 1024 * 1024
    environment: Mapping[str, str] | None = None
    implementation_tools: tuple[str, ...] = _IMPLEMENTATION_TOOLS
    name: str = "pi"

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        *,
        data_dir: Path,
    ) -> PiAgentRunnerConfig:
        raw_command = environ.get("ENZO_PI_COMMAND", "pi").strip()
        command = tuple(shlex.split(raw_command))
        if not command:
            raise ValueError("ENZO_PI_COMMAND cannot be empty")
        try:
            timeout_seconds = float(environ.get("ENZO_PI_TIMEOUT_SECONDS", "900"))
        except ValueError as exc:
            raise ValueError("ENZO_PI_TIMEOUT_SECONDS must be numeric") from exc
        if timeout_seconds <= 0:
            raise ValueError("ENZO_PI_TIMEOUT_SECONDS must be positive")
        try:
            max_transcript_bytes = int(
                environ.get("ENZO_PI_MAX_TRANSCRIPT_BYTES", str(10 * 1024 * 1024))
            )
        except ValueError as exc:
            raise ValueError("ENZO_PI_MAX_TRANSCRIPT_BYTES must be an integer") from exc
        if max_transcript_bytes <= 0:
            raise ValueError("ENZO_PI_MAX_TRANSCRIPT_BYTES must be positive")
        transcript_root = _configured_path(
            environ.get("ENZO_PI_TRANSCRIPT_DIR"),
            default=data_dir / "agent-runs" / "pi",
            relative_to=data_dir,
        )
        scratch_workspace = _configured_path(
            environ.get("ENZO_PI_SCRATCH_DIR"),
            default=data_dir / "agent-workspaces" / "pi",
            relative_to=data_dir,
        )
        allowlist = tuple(
            name.strip()
            for name in environ.get("ENZO_PI_ENV_ALLOWLIST", "").split(",")
            if name.strip()
        )
        child_environment = {
            name: value
            for name in (*_BASE_ENVIRONMENT, *allowlist)
            if (value := environ.get(name)) is not None
        }
        child_environment.update({"PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"})
        tools = tuple(
            tool.strip()
            for tool in environ.get(
                "ENZO_PI_IMPLEMENTATION_TOOLS",
                ",".join(_IMPLEMENTATION_TOOLS),
            ).split(",")
            if tool.strip()
        )
        if "bash" in tools or "powershell" in tools:
            raise ValueError(
                "Pi shell tools are disabled: Enzo owns commands and Git lifecycle"
            )
        return cls(
            command=command,
            transcript_root=transcript_root,
            scratch_workspace=scratch_workspace,
            provider=environ.get("ENZO_PI_PROVIDER") or None,
            model=environ.get("ENZO_PI_MODEL") or None,
            timeout_seconds=timeout_seconds,
            max_transcript_bytes=max_transcript_bytes,
            environment=child_environment,
            implementation_tools=tools,
            name=environ.get("ENZO_PI_RUNNER_NAME", "pi").strip() or "pi",
        )


class PiAgentRunner:
    """Pi RPC adapter. Pi performs work; Enzo retains state and Git authority."""

    def __init__(self, config: PiAgentRunnerConfig) -> None:
        self.config = config

    @property
    def spec(self) -> AgentRunnerSpec:
        return AgentRunnerSpec(
            name=self.config.name,
            kind="pi-rpc",
            capabilities=frozenset(
                {AgentCapability.GENERATE_TEXT, AgentCapability.EDIT_WORKSPACE}
            ),
        )

    def run(self, request: AgentRequest) -> AgentResult:
        workspace = self._workspace(request)
        transcript_path = self._transcript_path(request)
        command = self._command(request)
        environment = dict(self.config.environment or {})
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise AgentRunnerError(
                f"Pi executable was not found: {command[0]}",
                kind=AgentFailureKind.CONFIGURATION,
                runner_name=self.spec.name,
                retryable=False,
            ) from exc
        except OSError as exc:
            raise AgentRunnerError(
                f"failed to start Pi: {exc}",
                kind=AgentFailureKind.PROCESS,
                runner_name=self.spec.name,
                retryable=True,
            ) from exc

        events: list[dict[str, Any]] = []
        raw_lines: list[bytes] = []
        stderr_chunks: list[bytes] = []
        output_queue: queue.Queue[bytes | None] = queue.Queue()
        stdout_thread = threading.Thread(
            target=_pump_lines,
            args=(process.stdout, output_queue, self.config.max_transcript_bytes),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_pump_bytes,
            args=(process.stderr, stderr_chunks, self.config.max_transcript_bytes),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        prompt_id = str(uuid.uuid4())
        try:
            assert process.stdin is not None
            try:
                process.stdin.write(
                    json.dumps(
                        {
                            "id": prompt_id,
                            "type": "prompt",
                            "message": build_agent_prompt(request),
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    + b"\n"
                )
                process.stdin.flush()
            except OSError as exc:
                raise AgentRunnerError(
                    "Pi exited before accepting the prompt",
                    kind=AgentFailureKind.PROCESS,
                    runner_name=self.spec.name,
                    retryable=True,
                ) from exc
            final_message = self._read_until_settled(
                process,
                output_queue,
                events,
                raw_lines,
                prompt_id=prompt_id,
            )
        finally:
            _stop_process(process)
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            _write_transcript(transcript_path, raw_lines)
            _write_stderr(transcript_path, stderr_chunks)

        text = _assistant_text(final_message, runner_name=self.spec.name)
        content, metadata = _parse_result_envelope(text, runner_name=self.spec.name)
        usage = final_message.get("usage")
        metadata = {
            **metadata,
            "pi": {
                "transcript_path": str(transcript_path),
                "event_count": len(events),
                "usage": usage if isinstance(usage, dict) else None,
                "tools_used": [
                    event.get("toolName")
                    for event in events
                    if event.get("type") == "tool_execution_start"
                ],
            },
        }
        return AgentResult(
            content=content,
            metadata=metadata,
            run_info=AgentRunInfo(
                runner_name=self.spec.name,
                runner_kind=self.spec.kind,
                provider=_optional_string(final_message.get("provider")),
                model=_optional_string(final_message.get("model")),
            ),
        )

    def _read_until_settled(
        self,
        process: subprocess.Popen[bytes],
        output_queue: queue.Queue[bytes | None],
        events: list[dict[str, Any]],
        raw_lines: list[bytes],
        *,
        prompt_id: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.timeout_seconds
        prompt_accepted = False
        final_message: dict[str, Any] | None = None
        final_retry_error: str | None = None
        transcript_bytes = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentRunnerError(
                    f"Pi timed out after {self.config.timeout_seconds:g} seconds",
                    kind=AgentFailureKind.TIMEOUT,
                    runner_name=self.spec.name,
                    retryable=True,
                )
            try:
                raw = output_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise AgentRunnerError(
                    f"Pi timed out after {self.config.timeout_seconds:g} seconds",
                    kind=AgentFailureKind.TIMEOUT,
                    runner_name=self.spec.name,
                    retryable=True,
                ) from exc
            if raw is None:
                return_code = process.poll()
                raise AgentRunnerError(
                    f"Pi exited before agent_settled (exit code {return_code})",
                    kind=AgentFailureKind.PROCESS,
                    runner_name=self.spec.name,
                    retryable=return_code not in {0, 2},
                )
            transcript_bytes += len(raw)
            if transcript_bytes > self.config.max_transcript_bytes:
                raise AgentRunnerError(
                    "Pi RPC transcript exceeded the configured size limit",
                    kind=AgentFailureKind.PROTOCOL,
                    runner_name=self.spec.name,
                    retryable=False,
                )
            raw_lines.append(raw)
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AgentRunnerError(
                    "Pi emitted a non-JSONL stdout record",
                    kind=AgentFailureKind.PROTOCOL,
                    runner_name=self.spec.name,
                    retryable=False,
                ) from exc
            if not isinstance(event, dict):
                raise AgentRunnerError(
                    "Pi emitted a JSONL record that is not an object",
                    kind=AgentFailureKind.PROTOCOL,
                    runner_name=self.spec.name,
                    retryable=False,
                )
            events.append(event)
            if event.get("type") == "response" and event.get("id") == prompt_id:
                if not event.get("success"):
                    error_message = str(event.get("error") or "unknown error")
                    raise AgentRunnerError(
                        f"Pi rejected the prompt: {error_message}",
                        kind=_rejection_kind(error_message),
                        runner_name=self.spec.name,
                        retryable=False,
                    )
                prompt_accepted = True
            if event.get("type") == "message_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    final_message = message
            if event.get("type") == "auto_retry_end" and event.get("success") is False:
                final_retry_error = str(event.get("finalError") or "provider retry failed")
            if event.get("type") == "agent_settled":
                break
        if not prompt_accepted:
            raise AgentRunnerError(
                "Pi settled without accepting the prompt",
                kind=AgentFailureKind.PROTOCOL,
                runner_name=self.spec.name,
                retryable=False,
            )
        if final_retry_error:
            raise AgentRunnerError(
                f"Pi provider retries were exhausted: {final_retry_error}",
                kind=AgentFailureKind.PROCESS,
                runner_name=self.spec.name,
                retryable=True,
            )
        if final_message is None:
            raise AgentRunnerError(
                "Pi settled without a final assistant message",
                kind=AgentFailureKind.PROTOCOL,
                runner_name=self.spec.name,
                retryable=False,
            )
        stop_reason = final_message.get("stopReason")
        if stop_reason in {"error", "aborted", "length"}:
            raise AgentRunnerError(
                f"Pi assistant stopped with reason {stop_reason}",
                kind=(
                    AgentFailureKind.CANCELLED
                    if stop_reason == "aborted"
                    else AgentFailureKind.PROCESS
                ),
                runner_name=self.spec.name,
                retryable=stop_reason == "error",
            )
        return final_message

    def _workspace(self, request: AgentRequest) -> Path:
        if request.workload in {
            AgentWorkload.IMPLEMENTATION,
            AgentWorkload.IMPLEMENTATION_REVISION,
        }:
            if not request.workspace_path:
                raise AgentRunnerError(
                    "Pi implementation request has no workspace_path",
                    kind=AgentFailureKind.CONFIGURATION,
                    runner_name=self.spec.name,
                    retryable=False,
                )
            workspace = Path(request.workspace_path).resolve()
            if not workspace.is_dir():
                raise AgentRunnerError(
                    f"Pi workspace does not exist: {workspace}",
                    kind=AgentFailureKind.CONFIGURATION,
                    runner_name=self.spec.name,
                    retryable=False,
                )
            return workspace
        self.config.scratch_workspace.mkdir(parents=True, exist_ok=True)
        return self.config.scratch_workspace.resolve()

    def _transcript_path(self, request: AgentRequest) -> Path:
        directory = (
            self.config.transcript_root
            / _safe_segment(request.project_id)
            / _safe_segment(request.feature_id)
        )
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{request.workload.value.lower()}-{uuid.uuid4().hex}.jsonl"

    def _command(self, request: AgentRequest) -> tuple[str, ...]:
        command = [
            *self.config.command,
            "--mode",
            "rpc",
            "--no-session",
            "--no-approve",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
        if self.config.provider:
            command.extend(("--provider", self.config.provider))
        if self.config.model:
            command.extend(("--model", self.config.model))
        if request.workload in {
            AgentWorkload.IMPLEMENTATION,
            AgentWorkload.IMPLEMENTATION_REVISION,
        }:
            command.extend(("--tools", ",".join(self.config.implementation_tools)))
        else:
            command.append("--no-tools")
        return tuple(command)


def _assistant_text(message: Mapping[str, Any], *, runner_name: str) -> str:
    blocks = message.get("content")
    if not isinstance(blocks, list):
        raise AgentRunnerError(
            "Pi assistant message content is not a list",
            kind=AgentFailureKind.PROTOCOL,
            runner_name=runner_name,
            retryable=False,
        )
    text = "".join(
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()
    if not text:
        raise AgentRunnerError(
            "Pi assistant returned no text content",
            kind=AgentFailureKind.PROTOCOL,
            runner_name=runner_name,
            retryable=False,
        )
    return text


def _parse_result_envelope(text: str, *, runner_name: str) -> tuple[str, dict[str, Any]]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentRunnerError(
            "Pi final response is not the required JSON object",
            kind=AgentFailureKind.PROTOCOL,
            runner_name=runner_name,
            retryable=False,
        ) from exc
    if not isinstance(value, dict):
        raise AgentRunnerError(
            "Pi result envelope must be an object",
            kind=AgentFailureKind.PROTOCOL,
            runner_name=runner_name,
            retryable=False,
        )
    content = value.get("content")
    metadata = value.get("metadata", {})
    if not isinstance(content, str) or not content.strip():
        raise AgentRunnerError(
            "Pi result envelope must contain non-empty string content",
            kind=AgentFailureKind.PROTOCOL,
            runner_name=runner_name,
            retryable=False,
        )
    if not isinstance(metadata, dict):
        raise AgentRunnerError(
            "Pi result envelope metadata must be an object",
            kind=AgentFailureKind.PROTOCOL,
            runner_name=runner_name,
            retryable=False,
        )
    return content, metadata


def _pump_lines(
    stream: BinaryIO | None,
    output: queue.Queue[bytes | None],
    max_record_bytes: int,
) -> None:
    if stream is None:
        output.put(None)
        return
    try:
        while line := stream.readline(max_record_bytes + 1):
            output.put(line)
    finally:
        output.put(None)


def _pump_bytes(
    stream: BinaryIO | None,
    output: list[bytes],
    max_bytes: int,
) -> None:
    if stream is None:
        return
    captured = 0
    while chunk := stream.read(65536):
        if captured < max_bytes:
            kept = chunk[: max_bytes - captured]
            output.append(kept)
            captured += len(kept)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.stdin:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _write_transcript(path: Path, raw_lines: list[bytes]) -> None:
    path.write_bytes(b"".join(raw_lines))


def _write_stderr(transcript_path: Path, chunks: list[bytes]) -> None:
    if chunks:
        transcript_path.with_suffix(".stderr.log").write_bytes(b"".join(chunks))


def _configured_path(value: str | None, *, default: Path, relative_to: Path) -> Path:
    if not value:
        return default
    path = Path(value)
    return path if path.is_absolute() else relative_to / path


def _safe_segment(value: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value
    )
    return safe[:100] or "unknown"


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _rejection_kind(message: str) -> AgentFailureKind:
    normalized = message.lower()
    if any(
        marker in normalized
        for marker in ("api key", "credential", "authentication", "log in", "login")
    ):
        return AgentFailureKind.AUTHENTICATION
    return AgentFailureKind.PROTOCOL
