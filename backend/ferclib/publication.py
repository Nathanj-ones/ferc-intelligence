"""Recoverable publication helpers for generated file collections.

SQLite supplies one atomic boundary for database rows; a directory of CSV/JSON
files does not.  A generation is therefore written once under a content-derived
directory, validated there, and named by a small receipt written last. Legacy
paths may be refreshed for compatibility, but consumers can prove a coherent
set by following and verifying the receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import tempfile
import uuid
from collections.abc import Mapping


class PublicationError(RuntimeError):
    """A generation could not be staged or published coherently."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_fsync(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _atomic_json(path: pathlib.Path, payload: dict) -> None:
    data = (json.dumps(payload, indent=1, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        _write_fsync(tmp, data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def stage_generation(base: pathlib.Path, kind: str,
                     files: Mapping[str, bytes], *, metadata: dict | None = None) -> dict:
    """Write an immutable, content-addressed generation and return its manifest.

    `files` keys are logical relative paths. All bytes are written and rehashed
    before one directory rename makes the generation eligible for publication.
    No current timestamp participates in the identity.
    """
    base = pathlib.Path(base)
    if not files:
        raise PublicationError("a generation must contain at least one file")
    bad = [name for name in files if pathlib.PurePosixPath(name).is_absolute()
           or ".." in pathlib.PurePosixPath(name).parts]
    if bad:
        raise PublicationError(f"unsafe generation path(s): {bad}")
    identities = {name: {"bytes": len(body), "sha256": _sha(body)}
                  for name, body in sorted(files.items())}
    identity_payload = {"kind": kind, "files": identities,
                        "metadata": metadata or {}}
    generation_id = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    root = base / ".generations" / kind
    final = root / generation_id
    manifest = {"generation_id": generation_id, "kind": kind,
                "generation_path": str(final.relative_to(base)),
                "files": identities, "metadata": metadata or {}}
    if final.is_dir():
        verify_generation(base, manifest)
        return manifest

    root.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=f".{generation_id}.tmp-", dir=root))
    try:
        for name, body in files.items():
            _write_fsync(tmp / name, body)
        _write_fsync(tmp / "GENERATION_MANIFEST.json",
                     (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode())
        for name, expected in identities.items():
            body = (tmp / name).read_bytes()
            if len(body) != expected["bytes"] or _sha(body) != expected["sha256"]:
                raise PublicationError(f"staged identity mismatch: {name}")
        os.replace(tmp, final)
    finally:
        # Only an unpublished, uniquely named staging directory is removed.
        if tmp.exists():
            for p in sorted(tmp.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    p.rmdir()
            tmp.rmdir()
    return manifest


def verify_generation(base: pathlib.Path, manifest: dict) -> None:
    if not isinstance(manifest, dict):
        raise PublicationError("generation manifest is not an object")
    kind = manifest.get("kind")
    generation_id = manifest.get("generation_id")
    files = manifest.get("files")
    metadata = manifest.get("metadata", {})
    if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", kind):
        raise PublicationError(f"unsafe generation kind: {kind!r}")
    if not isinstance(generation_id, str) or not re.fullmatch(r"[0-9a-f]{64}", generation_id):
        raise PublicationError(f"invalid generation id: {generation_id!r}")
    if not isinstance(files, dict) or not files:
        raise PublicationError("generation manifest has no file identities")
    if not isinstance(metadata, dict):
        raise PublicationError("generation metadata is not an object")
    unsafe = [name for name in files
              if not isinstance(name, str)
              or pathlib.PurePosixPath(name).is_absolute()
              or ".." in pathlib.PurePosixPath(name).parts
              or "\\" in name]
    if unsafe:
        raise PublicationError(f"unsafe manifest file path(s): {unsafe}")
    expected_id = hashlib.sha256(json.dumps(
        {"kind": kind, "files": files, "metadata": metadata},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if generation_id != expected_id:
        raise PublicationError(
            f"generation id does not bind manifest content: expected {expected_id}, "
            f"got {generation_id}")
    expected_relative = pathlib.PurePosixPath(".generations") / kind / generation_id
    if pathlib.PurePosixPath(str(manifest.get("generation_path") or "")) != expected_relative:
        raise PublicationError("generation path does not match kind and generation id")
    base = pathlib.Path(base)
    root = base / pathlib.Path(*expected_relative.parts)
    if root.is_symlink() or not root.is_dir():
        raise PublicationError(f"generation directory absent: {root}")
    embedded_path = root / "GENERATION_MANIFEST.json"
    if embedded_path.is_symlink() or not embedded_path.is_file():
        raise PublicationError("embedded generation manifest is absent or a symlink")
    try:
        embedded = json.loads(embedded_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"embedded generation manifest is invalid: {exc}") from None
    if embedded != manifest:
        raise PublicationError("receipt and embedded generation manifest disagree")
    for name, expected in files.items():
        if (not isinstance(expected, dict)
                or not isinstance(expected.get("bytes"), int)
                or expected["bytes"] < 0
                or not isinstance(expected.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"])):
            raise PublicationError(f"invalid file identity for {name}")
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise PublicationError(f"generated file absent: {name}")
        body = path.read_bytes()
        if len(body) != expected["bytes"] or _sha(body) != expected["sha256"]:
            raise PublicationError(f"generated file identity mismatch: {name}")


def publish_generation(base: pathlib.Path, receipt: pathlib.Path, kind: str,
                       files: Mapping[str, bytes], *, metadata: dict | None = None,
                       compatibility_targets: Mapping[str, pathlib.Path] | None = None) -> dict:
    """Stage, validate and publish a generation; write the receipt last.

    Compatibility files are replaced from fully staged bytes. If an ordinary
    exception occurs during that replacement, their previous bytes are restored.
    An abrupt process death is detected by the unchanged receipt, whose hashes
    will not match a mixed legacy directory; the immutable generation remains a
    recoverable source of truth.
    """
    base = pathlib.Path(base)
    manifest = stage_generation(base, kind, files, metadata=metadata)
    verify_generation(base, manifest)
    targets = compatibility_targets or {}
    previous: dict[pathlib.Path, bytes | None] = {}
    staged: dict[pathlib.Path, pathlib.Path] = {}
    try:
        for logical, target in targets.items():
            if logical not in files:
                raise PublicationError(f"compatibility target has no generated file: {logical}")
            target = pathlib.Path(target)
            previous[target] = target.read_bytes() if target.is_file() else None
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            _write_fsync(tmp, files[logical])
            staged[target] = tmp
        for target, tmp in staged.items():
            os.replace(tmp, target)
    except BaseException:
        for target, prior in previous.items():
            try:
                if prior is None:
                    if target.exists():
                        target.unlink()
                else:
                    restore = target.with_name(
                        f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.restore")
                    _write_fsync(restore, prior)
                    os.replace(restore, target)
            except OSError:
                pass
        raise
    finally:
        for tmp in staged.values():
            if tmp.exists():
                tmp.unlink()
    verify_generation(base, manifest)
    _atomic_json(pathlib.Path(receipt), manifest)
    return manifest


def verify_receipt(base: pathlib.Path, receipt: pathlib.Path) -> dict:
    receipt = pathlib.Path(receipt)
    if receipt.is_symlink() or not receipt.is_file():
        raise PublicationError(f"publication receipt is absent or a symlink: {receipt}")
    try:
        manifest = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"publication receipt is invalid: {exc}") from None
    verify_generation(pathlib.Path(base), manifest)
    return manifest
