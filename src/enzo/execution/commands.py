from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .models import CommandResult, CommandSpec


class CommandRunner:
    """Runs configured deterministic checks without a shell."""

    def __init__(self, *, timeout_seconds: float = 900) -> None:
        self.timeout_seconds = timeout_seconds

    def run(self, command: CommandSpec, *, cwd: Path) -> CommandResult:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(command.argv),
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            exit_code = completed.returncode
            output = completed.stdout
            if completed.stderr:
                if output and not output.endswith("\n"):
                    output += "\n"
                output += completed.stderr
        except FileNotFoundError as error:
            exit_code = 127
            output = f"executable not found: {error.filename}\n"
        except subprocess.TimeoutExpired as error:
            exit_code = 124
            output = _timeout_output(error)
        return CommandResult(
            name=command.name,
            argv=command.argv,
            exit_code=exit_code,
            output=output,
            duration_seconds=time.monotonic() - started,
        )


def _timeout_output(error: subprocess.TimeoutExpired) -> str:
    output = error.stdout or ""
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    stderr = error.stderr or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    if stderr:
        if output and not output.endswith("\n"):
            output += "\n"
        output += stderr
    return f"command timed out\n{output}"
