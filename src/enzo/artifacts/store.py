from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class StoredArtifact:
    content_path: Path
    manifest_path: Path
    content_sha256: str
    extra_paths: dict[str, Path] = field(default_factory=dict)
    extra_sha256: dict[str, str] = field(default_factory=dict)


class ArtifactStore:
    """Filesystem storage whose finalized revision directories are never overwritten."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.staging_root = root / ".staging"
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def store_markdown(
        self,
        *,
        artifact_type: str,
        display_name: str,
        feature_id: str,
        revision_id: str,
        revision_no: int,
        parent_revision_id: str | None,
        created_by: str,
        content: str,
        extra_files: dict[str, str] | None = None,
    ) -> StoredArtifact:
        content_bytes = content.encode("utf-8")
        digest = hashlib.sha256(content_bytes).hexdigest()
        artifact_slug = artifact_type.lower()
        final_dir = self.root / "features" / feature_id / artifact_slug / f"r{revision_no:04d}"
        if final_dir.exists():
            raise FileExistsError(f"artifact revision directory already exists: {final_dir}")

        staging_dir = Path(tempfile.mkdtemp(prefix=f"{revision_id}-", dir=self.staging_root))
        extra_paths: dict[str, Path] = {}
        extra_sha256: dict[str, str] = {}
        try:
            content_path = staging_dir / display_name
            content_path.write_bytes(content_bytes)
            files = {display_name: {"sha256": digest, "bytes": len(content_bytes)}}
            for filename, extra_content in (extra_files or {}).items():
                if Path(filename).name != filename or filename == display_name:
                    raise ValueError(f"invalid artifact filename: {filename}")
                extra_bytes = extra_content.encode("utf-8")
                extra_digest = hashlib.sha256(extra_bytes).hexdigest()
                (staging_dir / filename).write_bytes(extra_bytes)
                files[filename] = {"sha256": extra_digest, "bytes": len(extra_bytes)}
                extra_sha256[filename] = extra_digest
            manifest = {
                "schema_version": 1,
                "feature_id": feature_id,
                "artifact_type": artifact_type,
                "revision_id": revision_id,
                "revision": revision_no,
                "parent_revision_id": parent_revision_id,
                "created_by": created_by,
                "created_at": datetime.now(UTC).isoformat(),
                "files": files,
            }
            manifest_path = staging_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging_dir, final_dir)
            extra_paths = {
                filename: final_dir / filename for filename in (extra_files or {})
            }
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

        return StoredArtifact(
            content_path=final_dir / display_name,
            manifest_path=final_dir / "manifest.json",
            content_sha256=digest,
            extra_paths=extra_paths,
            extra_sha256=extra_sha256,
        )

    @staticmethod
    def read_content(path: str | Path) -> str:
        return Path(path).read_text(encoding="utf-8")
