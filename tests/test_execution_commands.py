from __future__ import annotations

from enzo.execution.commands import CommandRunner
from enzo.execution.models import CommandSpec


def test_missing_executable_is_a_deterministic_failed_result(tmp_path) -> None:
    result = CommandRunner().run(
        CommandSpec("lint", ("/definitely/not/an/enzo-command",)),
        cwd=tmp_path,
    )

    assert result.exit_code == 127
    assert "executable not found" in result.output
