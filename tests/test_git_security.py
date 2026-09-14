from __future__ import annotations

from types import SimpleNamespace

import pytest

from enzo.execution.git import GitOperationError, GitWorkspaceManager


def test_git_errors_redact_credentials_embedded_in_https_remotes(monkeypatch) -> None:
    secret = "super-secret-token"

    def fail(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=128,
            stdout="",
            stderr=f"fatal: unable to access https://user:{secret}@git.example/repo.git",
        )

    monkeypatch.setattr("enzo.execution.git.subprocess.run", fail)

    with pytest.raises(GitOperationError) as raised:
        GitWorkspaceManager._run(
            "clone",
            "--",
            f"https://user:{secret}@git.example/repo.git",
            "/tmp/repo",
        )

    assert secret not in str(raised.value)
    assert "https://***@git.example/repo.git" in str(raised.value)
