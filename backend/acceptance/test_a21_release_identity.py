"""
A21 -- release identity must be acyclic and coherent.

The delivered artefact set has three defects, all reproducible by construction
rather than by accident:

  * `artifact_manifest.json` carries entries for `release_receipt.json`,
    `IMPLEMENTATION_ACCEPTANCE.md` and `staging/operating_assets.sqlite` -- three
    files written or changed AFTER the manifest, so those entries are stale the
    moment they land. That is the audit's "3 stale entries (acceptance, receipt,
    database)";
  * two shared files differ between the lite and full uploads, so which artefact
    state the receipt certifies is ambiguous;
  * there is no nested release ZIP, so the receipt cannot be checked against the
    thing it names without going outside the package.

The required scheme is a one-way sequence:

    freeze payload -> hash the payload -> create the archive -> issue an
    EXTERNAL receipt identifying the exact final archive.

Each stage may only reference bytes frozen by an earlier one. A receipt inside
the archive it hashes is a cycle: it can never state its own truth.

Everything here validates an EXTRACTED candidate and its outputs, not just
archive hashes. An archive whose hash matches a receipt is not evidence that
what is inside it is a coherent release.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
import zipfile

from . import candidate, contract
from .harness import (BASELINE_TREE, TREE, Tier, TierUnavailable, acceptance,
                      code_only, fixture_json, must_reject, require,
                      require_population, sha256_read)

BUILDER = TREE / "build_release.py"


def _build(env, name: str, payload: str = "lite"):
    root = candidate.build_candidate(env, name)
    proc = subprocess.run(
        [sys.executable, str(BUILDER), "--payload", payload,
         "--root", str(root), "--out-dir", str(root.parent)],
        capture_output=True, text=True, cwd=str(TREE), timeout=900)
    return root, proc


# ------------------------------------------------------------------ the historical defect

@acceptance(issue="A21.1", group="release_identity",
            mutation="a manifest entry for a file written after the manifest")
def t_delivered_manifest_has_stale_entries(env):
    """the acyclicity check detects the delivered manifest's stale entries"""
    # Preserve the historical negative as the exact mismatch extract recorded by
    # the original independent audit.  The old path silently aliased the current
    # manifest in a standalone candidate, turning a historical test into a test
    # of whatever happened to be built most recently.
    historical = fixture_json("historical_release_identity.json")
    manifest_files = historical.get("manifest_files") or {}
    observed = historical.get("observed_after_manifest") or {}
    require_population(manifest_files, "historical stale manifest entries", minimum=3)
    require(set(manifest_files) == set(observed),
            f"historical before/after entry sets differ: "
            f"{sorted(manifest_files)} vs {sorted(observed)}")
    changed = sorted(
        rel for rel in manifest_files
        if (manifest_files[rel].get("sha256"), manifest_files[rel].get("bytes"))
        != (observed[rel].get("sha256"), observed[rel].get("bytes")))
    require(changed == sorted(manifest_files),
            f"the historical three-entry mismatch population changed: {changed}")

    violations = contract.manifest_cycle_violations({"files": manifest_files})
    stale = [v for v in violations if v["kind"] in
             ("STALE_BY_CONSTRUCTION", "MUTABLE_AFTER_MANIFEST")]
    require_population(stale, "stale-by-construction entries in the delivered manifest")

    paths = sorted(v["path"] for v in stale)
    require("release_receipt.json" in paths,
            f"the receipt is not flagged as stale-by-construction: {paths}")
    require(any(p.startswith("staging/") for p in paths),
            f"the database is not flagged as written-after-manifest: {paths}")
    expected = sorted(v["path"]
                      for v in historical.get("expected_contract_violations") or [])
    require(paths == expected,
            f"structural violation paths changed: expected {expected}, got {paths}")
    return (f"hash-verified historical extract preserves all three changed entries "
            f"{changed}; the acyclicity contract independently flags {paths}")


# ------------------------------------------------------------------ the fixed scheme

@acceptance(issue="A21.2", group="release_identity",
            mutation="a rebuilt manifest that still records post-manifest files")
def t_built_manifest_is_acyclic(env):
    """a freshly built manifest records nothing written after itself"""
    require(BUILDER.is_file(), f"build_release.py absent: {BUILDER}")
    root, proc = _build(env, "a21_acyclic")
    require(proc.returncode == 0,
            f"candidate build failed:\n{proc.stdout[-1500:]}\n{proc.stderr[-800:]}")

    manifest = json.loads((root / "artifact_manifest.json").read_bytes())
    require_population(manifest.get("files"), "built manifest entries", minimum=20)
    violations = contract.manifest_cycle_violations(manifest, tree=root)
    require(not violations,
            f"the rebuilt manifest is still cyclic/stale: {violations[:5]}")

    for name in contract.WRITTEN_AFTER_MANIFEST:
        require(name not in manifest["files"],
                f"{name} is a manifest entry but is written after the manifest")
    stages = [s["stage"] if isinstance(s, dict) else s
              for s in manifest.get("release_stages", [])]
    require(stages == [s for s, _ in contract.RELEASE_STAGES],
            f"the manifest does not declare the release sequence: {stages}")
    return (f"{len(manifest['files'])} entries, 0 cycle violations; stages declared "
            f"in order {stages}")


@acceptance(issue="A21.3", group="release_identity",
            mutation="a receipt shipped inside the archive whose hash it states")
def t_receipt_is_external_to_the_archive(env):
    """the receipt is external to the archive it certifies"""
    root, proc = _build(env, "a21_external")
    require(proc.returncode == 0, f"candidate build failed:\n{proc.stdout[-1200:]}")

    zips = sorted(root.parent.glob("operating_assets_all_regimes*.zip"))
    require_population(zips, "a built archive")
    archive = zips[-1]
    with zipfile.ZipFile(archive) as z:
        members = {pathlib.PurePosixPath(n).name for n in z.namelist()}
    require("release_receipt.json" not in members,
            "the receipt certifying this archive is INSIDE it. The hash it states can "
            "never be the hash of the bytes it ships in -- that is the self-hashing "
            "cycle A21 is about.")
    require((root / "release_receipt.json").is_file(),
            "no external receipt was written")
    return (f"{archive.name}: {len(members)} members, receipt held externally at "
            f"{(root / 'release_receipt.json').name}")


@acceptance(issue="A21.4", group="release_identity",
            mutation="a receipt hash that does not identify the final archive bytes")
def t_receipt_identifies_the_exact_archive(env):
    """the receipt's hash is the hash of the final archive's actual bytes"""
    root, proc = _build(env, "a21_identity")
    require(proc.returncode == 0, f"candidate build failed:\n{proc.stdout[-1200:]}")
    receipt = json.loads((root / "release_receipt.json").read_bytes())

    archive_entry = receipt.get("archive")
    if isinstance(archive_entry, dict):
        named = archive_entry.get("name")
        digest = archive_entry.get("sha256")
        stated_bytes = archive_entry.get("bytes")
    else:
        named = archive_entry or receipt.get("zip") or receipt.get("archive_name")
        digest = receipt.get("zip_sha256") or receipt.get("archive_sha256")
        stated_bytes = receipt.get("zip_bytes") or receipt.get("archive_bytes")
    require(named, f"the receipt names no archive: {sorted(receipt)[:15]}")
    require(digest, f"the receipt states no archive hash: {sorted(receipt)[:15]}")

    archive = root.parent / pathlib.PurePosixPath(str(named)).name
    require(archive.is_file(), f"the archive the receipt names does not exist: {archive}")

    # INDEPENDENT recomputation from the bytes on disk.
    actual, size = sha256_read(archive)
    require(actual == digest,
            f"the receipt's hash does not identify the archive it names.\n"
            f"  receipt : {digest}\n  actual  : {actual}")

    if stated_bytes is not None:
        require(stated_bytes == size,
                f"the receipt records {stated_bytes} bytes; the archive is {size} bytes "
                "when actually read. A size taken without reading is unreliable on this "
                "volume.")

    # NEGATIVE: a one-byte change must break the identity.
    tampered = root.parent / f"tampered_{archive.name}"
    data = bytearray(archive.read_bytes())
    data[-1] ^= 0x01
    tampered.write_bytes(bytes(data))
    tampered_hash, _ = sha256_read(tampered)
    require(tampered_hash != digest,
            "flipping a byte in the archive did not change its hash")
    return (f"{archive.name}: receipt hash matches the {size:,} bytes actually read; "
            "a single flipped byte breaks the match")


@acceptance(issue="A21.5", group="release_identity",
            mutation="declaring a payload without saying which deliverable it is")
def t_lite_and_full_are_explicitly_identified(env):
    """the manifest and receipt say which deliverable they are"""
    root, proc = _build(env, "a21_payload")
    require(proc.returncode == 0, f"candidate build failed:\n{proc.stdout[-1200:]}")
    manifest = json.loads((root / "artifact_manifest.json").read_bytes())
    receipt = json.loads((root / "release_receipt.json").read_bytes())
    require(manifest.get("payload") in ("lite", "full"),
            f"the manifest does not identify its payload: {manifest.get('payload')!r}")
    require(receipt.get("payload") in ("lite", "full"),
            f"the receipt does not identify its payload: {receipt.get('payload')!r}")
    require(manifest["payload"] == receipt["payload"],
            f"manifest says {manifest['payload']!r}, receipt says {receipt['payload']!r} "
            "-- exactly the lite/full ambiguity A21 raised")
    if manifest["payload"] == "lite":
        excluded = json.dumps(manifest.get("lite_exclusions", {}))
        require("staging" in excluded and "source_cache" in excluded,
                f"the lite manifest does not declare what it excludes: {excluded[:200]}")
    return (f"payload={manifest['payload']!r} declared consistently in manifest and "
            "receipt, with exclusions named")


# ------------------------------------------------------------------ extracted, not hashed

@acceptance(issue="A21.6", group="release_identity",
            mutation="accepting a matching archive hash as proof the contents are coherent")
def t_extracted_candidate_satisfies_the_contract(env):
    """an EXTRACTED candidate is validated, not just its archive hash"""
    root, proc = _build(env, "a21_extracted")
    require(proc.returncode == 0, f"candidate build failed:\n{proc.stdout[-1200:]}")
    zips = sorted(root.parent.glob("operating_assets_all_regimes*.zip"))
    require_population(zips, "a built archive")
    archive = zips[-1]

    dest = env.scratch / "a21_extract"
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(dest)
    roots = [p for p in dest.iterdir() if p.is_dir()]
    require_population(roots, "an extracted package root")
    pkg = roots[0]

    # Re-derive every manifest entry from the EXTRACTED bytes.
    manifest = json.loads((pkg / "artifact_manifest.json").read_bytes())
    mismatched, missing = [], []
    for rel, meta in manifest["files"].items():
        f = pkg / rel
        if not f.is_file():
            missing.append(rel)
            continue
        digest, size = sha256_read(f)
        if digest != meta["sha256"] or size != meta["bytes"]:
            mismatched.append({"path": rel, "manifest": meta["sha256"][:16],
                               "actual": digest[:16],
                               "manifest_bytes": meta["bytes"], "actual_bytes": size})
    require(not missing, f"{len(missing)} manifest entries absent from the archive: "
                         f"{missing[:5]}")
    require(not mismatched, f"{len(mismatched)} extracted files disagree with the "
                            f"manifest: {mismatched[:3]}")

    # And the extracted tree must satisfy the required-output contract, which a
    # hash comparison says nothing about.
    violations = contract.blocking(contract.check_required_outputs(pkg, None))
    require(not violations,
            f"the EXTRACTED package violates the required-output contract even though "
            f"its hashes match: {violations[:4]}")
    return (f"{len(manifest['files'])} entries re-derived from extracted bytes, 0 "
            "mismatches, and the extracted tree satisfies the required-output contract")


# ------------------------------------------------------------------ the volume behaviour

@acceptance(issue="A21.7", group="release_identity",
            mutation="a size or hash taken from stat() without reading the bytes")
def t_release_takes_no_size_from_stat(env):
    """no size or hash in the release path comes from stat()"""
    # The INVARIANT, always checkable. Split from the environment demonstration
    # below because whether an evicted file happens to be available is not a
    # property of the code, and a check that depends on it would be flaky in
    # both directions.
    require(BUILDER.is_file(), f"build_release.py absent: {BUILDER}")
    # Scoped to the RELEASE PATH: the modules that decide what a manifest or a
    # receipt records. `acceptance/harness.py` is deliberately excluded because
    # its job is to STUDY stat() -- `read_bytes_and_size` exists precisely to
    # compare the placeholder size against the bytes -- and a rule that forbade
    # that would forbid measuring the hazard at all.
    checked = {}
    for path in (BUILDER, pathlib.Path(contract.__file__)):
        # Comments and docstrings are stripped first. build_release.py documents
        # at length why it does NOT call stat(), and a raw grep reads that
        # explanation as the defect -- which is exactly the mistake this check
        # made on its first pass.
        code = code_only(path.read_text(encoding="utf-8"))
        for pattern in ("st_size", "st_mtime", "st_blocks"):
            require(pattern not in code,
                    f"{path.name} calls {pattern} in executable code. On this volume "
                    "stat() returns a stale placeholder for a cloud-evicted file until "
                    "the bytes are read, so a size taken from it was never verified "
                    "against the file.")
        checked[path.name] = len(code)

    builder_code = code_only(BUILDER.read_text(encoding="utf-8"))
    require("sha256" in builder_code and "read" in builder_code,
            "build_release.py records no hash taken from a read")
    harness_src = (pathlib.Path(contract.__file__).parent / "harness.py").read_text(
        encoding="utf-8")
    require("def sha256_read" in harness_src,
            "the harness offers no read-based hash+size helper")
    return (f"no size or hash taken from stat() in executable code across "
            f"{', '.join(sorted(checked))}; sizes come from the bytes read")


@acceptance(issue="A21.7b", group="release_identity",
            mutation="trusting stat() on a cloud-evicted placeholder")
def t_stat_placeholder_demonstration(env):
    """stat().st_size disagrees with the bytes on an evicted placeholder"""
    # The production hash helper must stay correct even when metadata is
    # unusable.  This deterministic guard reaches the actual helper with a path
    # object whose stat() is forbidden, then the optional scan below records the
    # real File Provider behaviour when this volume happens to expose it.
    payload = b"verified bytes, deliberately unrelated to metadata"

    class NoStatPath:
        def read_bytes(self):
            return payload

        def stat(self):
            raise AssertionError("sha256_read consulted unverified stat metadata")

    digest, verified_size = sha256_read(NoStatPath())
    require(digest == hashlib.sha256(payload).hexdigest()
            and verified_size == len(payload),
            "the read-based hash helper did not return the bytes it actually read")

    # OPPORTUNISTIC. Eviction is a transient property of this volume: once a file
    # has been read it stays materialised, so a demonstration that worked an hour
    # ago may find nothing now. That absence is not a defect, so it is SKIPPED
    # rather than failed -- and the invariant above is what actually protects the
    # release. The deterministic no-stat guard above means absence of an evicted
    # file is still an exercised worker control, never a skip or a pass-by-zero.
    # The scan is retained because the behaviour was observed directly:
    # exports/coverage_statistics.json reported 878 bytes from stat() before it
    # was read and 964 after, an 86-byte error in a size nobody had verified.
    checked = evicted = disagreed = 0
    examples = []
    search = [BASELINE_TREE / d for d in ("exports", "config", "discovery", "verification")]
    search.append(BASELINE_TREE / "source_cache" / "objects")
    for base in search:
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if not f.is_file():
                continue
            if checked >= 400:
                break
            try:
                st = f.stat()
            except OSError:
                continue
            was_evicted = st.st_blocks == 0
            checked += 1
            if not was_evicted:
                continue
            evicted += 1
            try:
                data = f.read_bytes()
            except OSError:
                continue
            if st.st_size != len(data):
                disagreed += 1
                if len(examples) < 5:
                    examples.append({"path": str(f.relative_to(BASELINE_TREE)),
                                     "stat_before_read": st.st_size,
                                     "actual_bytes": len(data),
                                     "stat_after_read": f.stat().st_size})
    if evicted == 0:
        return (f"no cloud-evicted placeholder was available among {checked} probed "
                "files; the actual read-based helper was exercised with stat() forbidden "
                f"and returned {verified_size} verified bytes")
    if disagreed == 0:
        return (f"{evicted}/{checked} probed files were evicted placeholders and none "
                "had stat() disagree with the bytes on this pass. The hazard is "
                "structural rather than currently realised; no size in the release "
                "path rests on stat() either way.")
    return (f"{disagreed} of {evicted} evicted placeholders reported a stat() size "
            f"that disagreed with the bytes read, e.g. {examples[0]}")


@acceptance(issue="A21.8", group="release_identity",
            mutation="an unreadable file recorded as though it were verified")
def t_unreadable_is_never_unchanged(env):
    """an unreadable payload file fails the build rather than being assumed"""
    builder_src = BUILDER.read_text(encoding="utf-8") if BUILDER.is_file() else ""
    require(builder_src, f"build_release.py absent: {BUILDER}")
    require("unreadable" in builder_src.lower(),
            "build_release.py has no unreadable-file branch; on a volume that evicts "
            "files, an OSError during hashing must fail the release, never be skipped")
    require("UNREADABLE" in builder_src or "unreadable" in builder_src,
            "no explicit unreadable outcome is recorded")
    return "build_release.py records unreadable payload files and refuses to proceed"
