#!/usr/bin/env python3
"""Fail-closed A17 release gate for the two reviewed image-only FERC PDFs.

This verifier reads the actual page pixels, all offline. It proves that:

* the release contains the exact FERC PDF bytes independently reviewed for A17;
* the pipeline's own PDF extractor still reports that those bytes have no text
  layer, so it cannot silently relabel the manual review as automatic parsing;
* Poppler can render page 1 and reproduces the independently pinned render;
* the shipped local macOS Vision path independently OCRs the rendered page;
* ``adapters.capacity.read_figures`` recovers values, units, subjects and
  qualifiers from the OCR rows rather than from a typed answer; and
* that OCR result agrees with the hash-bound non-OCR manual review.

The rendered PNGs live only in ``TemporaryDirectory`` and are removed before
the result is returned.  Missing inputs or ``pdftoppm`` are failures, never
skips.  No network module or retrieval path is used.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zlib
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


SCHEMA = "ferc-a17-reviewed-image-gate-v1"
FIXTURE_SCHEMA = "ferc-reviewed-image-sources-v1"
FIXTURE_SHA256 = "278c32b6a1ad5590d9b13dc26cfbe0ac4c217d8588fb0861a405d3a884edc4e9"
SOURCE_EVIDENCE_SHA256 = "12e283d0b3eda344b6199e7b8298463a7f0cb5e8993c8509450a748566c6e548"
RENDER_DPI = 180
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# These are independent acceptance constants, not values regenerated from the
# implementation under test.  The source and render identities come from the
# final A17 visual-verification evidence; the text values/caveats are the
# independently reviewed transcription contract.
SOURCE_CONTRACTS: Tuple[Mapping[str, Any], ...] = (
    {
        "accession": "20240223-5073",
        "entity": "Cadeville Gas Storage LLC",
        "file_name": "2024.02.23 - CGS Section 284.13 Capacity Report.pdf",
        "source_sha256": "31f9219d3da5bc260392b6e2351cde0313ab38978ade4ee714f70308d7036d94",
        "source_bytes": 337391,
        "pages": 1,
        "render_sha256": "90764d4355daf325787e3270e2c82237e861a440f381522050c9b86cf5388fc8",
        "render_width": 1530,
        "render_height": 1980,
        "expected": (
            ("estimated_peak_day_delivery_capacity", "420", "MMcf/day", "approximately"),
            ("estimated_total_storage_capacity", "23.7", "Bcf", "estimated"),
        ),
        "caveats": ("BASE GAS", "OFF-SYSTEM", "reasonably representative operating assumptions"),
    },
    {
        "accession": "20240223-5075",
        "entity": "Monroe Gas Storage Company, LLC",
        "file_name": "2024.02.23 - MGS Section 284.13 Capacity Report.pdf",
        "source_sha256": "8e2069eeb07fdfdca7167b323b25cf91f0c34bbf91fd82a8bd82caf9826c0040",
        "source_bytes": 339731,
        "pages": 1,
        "render_sha256": "e4021bf93407f7bd906fa8a70db51a228937668834b203ef8de81db9bc1f8688",
        "render_width": 1530,
        "render_height": 1980,
        "expected": (
            ("estimated_peak_day_delivery_capacity", "465", "MMcf/day", "approximately"),
            ("estimated_total_storage_capacity", "11.96", "Bcf", "estimated"),
        ),
        "caveats": ("BASE GAS", "OFF-SYSTEM", "reasonably representative operating assumptions"),
    },
)


class GateFailure(RuntimeError):
    """One fail-closed A17 gate assertion."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_identity(path: pathlib.Path) -> Dict[str, Any]:
    raw = path.read_bytes()
    return {"path": str(path), "bytes": len(raw), "sha256": _sha256(raw)}


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise GateFailure(code, message)


def _normalise_words(value: str) -> str:
    return " ".join(value.lower().replace("-", " ").split())


def _load_json_file(path: pathlib.Path, code: str) -> Tuple[bytes, Any]:
    _require(path.is_file(), code, "required file is absent: %s" % path)
    _require(not path.is_symlink(), code, "required file may not be a symlink: %s" % path)
    try:
        raw = path.read_bytes()
        return raw, json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateFailure(code, "cannot read valid JSON from %s: %s" % (path, exc)) from exc


def _load_capacity(root: pathlib.Path):
    expected = (root / "adapters" / "capacity.py").resolve()
    _require(expected.is_file(), "capacity_adapter_missing",
             "capacity adapter is absent: %s" % expected)
    root_s = str(root.resolve())
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    module = importlib.import_module("adapters.capacity")
    actual = pathlib.Path(module.__file__).resolve()
    _require(actual == expected, "capacity_adapter_wrong_tree",
             "imported %s instead of release adapter %s" % (actual, expected))
    return module


def _validate_fixture(path: pathlib.Path) -> Tuple[Dict[str, Mapping[str, Any]], Dict[str, Any]]:
    raw, fixture = _load_json_file(path, "review_fixture_invalid")
    _require(_sha256(raw) == FIXTURE_SHA256, "review_fixture_digest_mismatch",
             "review fixture SHA-256 does not match the independent acceptance identity")
    _require(isinstance(fixture, dict) and fixture.get("schema") == FIXTURE_SCHEMA,
             "review_fixture_schema_mismatch", "review fixture schema is not %s" % FIXTURE_SCHEMA)
    provenance = fixture.get("provenance") or {}
    _require(provenance.get("source_evidence_sha256") == SOURCE_EVIDENCE_SHA256,
             "review_evidence_identity_mismatch", "review fixture names the wrong A17 evidence hash")
    method = str(provenance.get("method") or "")
    limitation = str(provenance.get("limitation") or "")
    _require("independently inspected" in method.lower(), "review_method_not_independent",
             "review fixture does not state independent page inspection")
    _require(("does not" in limitation.lower() or "must not" in limitation.lower())
             and "fresh visual review" in limitation.lower(),
             "review_limitation_missing", "review fixture does not preserve its no-fresh-review limitation")

    sources = fixture.get("sources")
    _require(isinstance(sources, list) and len(sources) == len(SOURCE_CONTRACTS),
             "review_source_count_mismatch", "review fixture must contain exactly two sources")
    by_hash: Dict[str, Mapping[str, Any]] = {}
    for source in sources:
        _require(isinstance(source, dict), "review_source_invalid", "review source must be an object")
        source_hash = source.get("source_object_sha256")
        _require(isinstance(source_hash, str) and source_hash not in by_hash,
                 "review_source_duplicate", "review source hashes must be present and unique")
        by_hash[source_hash] = source

    expected_hashes = {str(c["source_sha256"]) for c in SOURCE_CONTRACTS}
    _require(set(by_hash) == expected_hashes, "review_source_identity_mismatch",
             "review fixture does not name exactly the two independent source hashes")
    for contract in SOURCE_CONTRACTS:
        source = by_hash[str(contract["source_sha256"])]
        _require(source.get("accession") == contract["accession"],
                 "review_accession_mismatch", "review fixture accession does not match source hash")
        _require(source.get("entity") == contract["entity"],
                 "review_entity_mismatch", "review fixture entity does not match source hash")
        _require(source.get("source_bytes") == contract["source_bytes"],
                 "review_size_mismatch", "review fixture byte size does not match source hash")
        _require(source.get("pages") == contract["pages"] and source.get("text_layer") is False,
                 "review_page_contract_mismatch", "review fixture must identify one image-only page")
        _require(source.get("render_sha256") == contract["render_sha256"],
                 "review_render_identity_mismatch", "review fixture names the wrong page render")
        _require(source.get("reviewed_by") == "independent visual review",
                 "review_attribution_mismatch", "review fixture must retain manual visual-review attribution")
        _require(source.get("ocr_used") is False, "review_ocr_mismatch",
                 "review fixture must state that OCR was not used")
        expected = tuple((item.get("kind"), item.get("value_text"), item.get("unit"),
                          item.get("qualifier")) for item in source.get("expected") or [])
        _require(expected == tuple(contract["expected"]), "review_values_mismatch",
                 "review fixture values, units, qualifiers, or order do not match acceptance evidence")
        _require(tuple(source.get("required_qualifiers") or ()) == tuple(contract["caveats"]),
                 "review_caveats_mismatch", "review fixture caveat contract changed")
    return by_hash, {"path": str(path), "bytes": len(raw), "sha256": _sha256(raw)}


def _resolve_cached_object(cache_dir: pathlib.Path, index: Mapping[str, Any],
                           contract: Mapping[str, Any]) -> Tuple[pathlib.Path, List[str]]:
    source_hash = str(contract["source_sha256"])
    matches = [(key, value) for key, value in index.items()
               if isinstance(value, dict) and value.get("content_hash") == source_hash]
    _require(bool(matches), "source_not_indexed",
             "%s is not resolved by source_cache/index.json" % source_hash)
    canonical_rel = "objects/%s/%s" % (source_hash[:2], source_hash)
    index_keys: List[str] = []
    for key, record in matches:
        _require(record.get("cache_path") == canonical_rel, "source_cache_path_mismatch",
                 "%s resolves outside its canonical content-addressed path" % source_hash)
        _require(record.get("byte_size") == contract["source_bytes"],
                 "source_index_size_mismatch", "%s has the wrong indexed byte size" % source_hash)
        _require(record.get("source_system") == "eLibrary", "source_system_mismatch",
                 "%s is not labelled as eLibrary evidence" % source_hash)
        index_keys.append(str(key))
    path = cache_dir / canonical_rel
    _require(path.is_file(), "source_object_missing", "cached source object is absent: %s" % path)
    _require(not path.is_symlink(), "source_object_symlink", "cached source object may not be a symlink: %s" % path)
    resolved_cache = cache_dir.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_cache)
    except ValueError as exc:
        raise GateFailure("source_object_escape", "cached source resolves outside source_cache") from exc
    return path, sorted(index_keys)


def _png_identity(raw: bytes) -> Dict[str, Any]:
    _require(raw.startswith(PNG_SIGNATURE), "render_not_png", "pdftoppm output lacks the PNG signature")
    _require(len(raw) >= 33, "render_truncated", "pdftoppm output is too short for a PNG IHDR")
    pos = len(PNG_SIGNATURE)
    width = height = 0
    idat_bytes = 0
    saw_iend = False
    while pos + 12 <= len(raw):
        length = int.from_bytes(raw[pos:pos + 4], "big")
        kind = raw[pos + 4:pos + 8]
        end = pos + 12 + length
        _require(end <= len(raw), "render_truncated", "PNG chunk extends beyond rendered output")
        payload = raw[pos + 8:pos + 8 + length]
        expected_crc = int.from_bytes(raw[pos + 8 + length:end], "big")
        actual_crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        _require(actual_crc == expected_crc, "render_crc_mismatch", "PNG chunk CRC mismatch")
        if kind == b"IHDR":
            _require(length == 13, "render_invalid_ihdr", "PNG IHDR has the wrong length")
            width = int.from_bytes(payload[0:4], "big")
            height = int.from_bytes(payload[4:8], "big")
        elif kind == b"IDAT":
            idat_bytes += length
        elif kind == b"IEND":
            saw_iend = True
            _require(end == len(raw), "render_trailing_bytes", "PNG has bytes after IEND")
            break
        pos = end
    _require(width > 0 and height > 0, "render_invalid_dimensions", "PNG dimensions are empty")
    _require(idat_bytes > 0 and saw_iend, "render_empty", "PNG has no image payload or IEND")
    return {"bytes": len(raw), "sha256": _sha256(raw), "width": width, "height": height,
            "idat_bytes": idat_bytes, "signature_valid": True, "crc_valid": True}


def _resolve_pdftoppm(requested: Optional[str]) -> pathlib.Path:
    candidate = requested or shutil.which("pdftoppm")
    _require(bool(candidate), "pdftoppm_missing", "required A17 gate tool pdftoppm was not found")
    if os.path.sep not in str(candidate):
        candidate = shutil.which(str(candidate))
    _require(bool(candidate), "pdftoppm_missing", "required A17 gate tool pdftoppm was not found")
    path = pathlib.Path(str(candidate)).resolve()
    _require(path.is_file() and os.access(str(path), os.X_OK), "pdftoppm_not_executable",
             "pdftoppm is not an executable file: %s" % path)
    return path


def _tool_version(executable: pathlib.Path) -> Dict[str, Any]:
    argv = [str(executable), "-v"]
    try:
        completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, timeout=15, check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateFailure("pdftoppm_version_failed", "cannot execute pdftoppm -v: %s" % exc) from exc
    text = (completed.stdout + "\n" + completed.stderr).strip()
    _require(completed.returncode == 0, "pdftoppm_version_failed",
             "pdftoppm -v returned %s: %s" % (completed.returncode, text[:500]))
    _require("pdftoppm version" in text.lower(), "pdftoppm_version_unrecognised",
             "pdftoppm -v did not identify the tool")
    return {"path": str(executable), "argv": argv, "returncode": completed.returncode,
            "version_output": text}


def _render_page_one(executable: pathlib.Path, source: pathlib.Path) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ferc-a17-render-") as temp_name:
        prefix = pathlib.Path(temp_name) / "page"
        argv = [str(executable), "-f", "1", "-l", "1", "-singlefile", "-r",
                str(RENDER_DPI), "-png", str(source), str(prefix)]
        try:
            completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, timeout=45, check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GateFailure("render_execution_failed", "pdftoppm render failed: %s" % exc) from exc
        png_path = prefix.with_suffix(".png")
        _require(completed.returncode == 0, "render_execution_failed",
                 "pdftoppm returned %s: %s" % (completed.returncode, completed.stderr[:500]))
        _require(png_path.is_file() and not png_path.is_symlink(), "render_missing",
                 "pdftoppm returned success without the requested page PNG")
        rendered = _png_identity(png_path.read_bytes())
        rendered.update({"argv": argv, "returncode": completed.returncode,
                         "stdout": completed.stdout, "stderr": completed.stderr,
                         "dpi": RENDER_DPI, "render_retained": False})
        return rendered


def _validate_reviewed_transcription(capacity, contract: Mapping[str, Any],
                                     fixture_source: Mapping[str, Any],
                                     ocr_pages: List[dict],
                                     ocr_evidence: Mapping[str, Any]) -> Dict[str, Any]:
    source_hash = str(contract["source_sha256"])
    record = capacity.REVIEWED_PAGE_IMAGES.get(source_hash)
    _require(isinstance(record, dict), "adapter_review_missing",
             "%s has no hash-bound reviewed transcription" % contract["accession"])
    _require(record.get("accession") == contract["accession"], "adapter_review_accession_mismatch",
             "adapter review accession is not bound to the expected source")
    _require(record.get("file_name") == contract["file_name"], "adapter_review_filename_mismatch",
             "adapter review names the wrong FERC attachment")
    _require(record.get("byte_size") == contract["source_bytes"], "adapter_review_size_mismatch",
             "adapter review byte size is not bound to the expected source")
    _require(record.get("page_count") == contract["pages"], "adapter_review_page_mismatch",
             "adapter review page count is not bound to the expected source")
    _require(record.get("ocr_used") is False, "adapter_review_ocr_mismatch",
             "adapter review must explicitly state OCR was not used")
    _require("review" in str(record.get("reviewed_by") or "").lower(),
             "adapter_review_attribution_missing", "adapter review lacks manual review attribution")
    _require("pdftoppm" in str(record.get("render") or "").lower(),
             "adapter_review_render_missing", "adapter review lacks its historical render method")
    _require(capacity.REVIEWED_IMAGE_METHOD == "reviewed_page_image_transcription",
             "adapter_review_method_mismatch", "adapter manual extraction method changed")

    manual_pages = capacity.reviewed_pages(source_hash)
    _require(isinstance(manual_pages, list) and len(manual_pages) == contract["pages"],
             "adapter_review_pages_missing", "adapter did not return the reviewed page")
    manual_text = "\n".join(row.get("text", "") for page in manual_pages
                            for row in page.get("rows", []))
    _require(_normalise_words(str(contract["entity"])) in _normalise_words(manual_text),
             "adapter_review_entity_missing",
             "reviewed transcription does not identify the expected filing entity")
    for caveat in contract["caveats"]:
        _require(_normalise_words(str(caveat)) in _normalise_words(manual_text),
                 "adapter_review_caveat_missing", "reviewed transcript lost caveat %r" % caveat)

    ocr_text = "\n".join(row.get("text", "") for page in ocr_pages
                         for row in page.get("rows", []))
    _require(_normalise_words(str(contract["entity"])) in _normalise_words(ocr_text),
             "automatic_ocr_entity_missing",
             "automatic OCR does not identify the expected filing entity")
    for caveat in contract["caveats"]:
        _require(_normalise_words(str(caveat)) in _normalise_words(ocr_text),
                 "automatic_ocr_caveat_missing",
                 "automatic OCR lost source caveat %r" % caveat)

    manual_figures = capacity.read_figures(manual_pages, 2023)
    figures = capacity.read_figures(ocr_pages, 2023)
    _require(capacity._review_signature(figures) ==
             capacity._review_signature(manual_figures),
             "automatic_ocr_review_disagreement",
             "automatic OCR figure/caveat signature disagrees with independent review")
    expected = tuple(contract["expected"])
    by_value = {str(item.get("value_text")): item for item in figures}
    _require(len(figures) == len(expected) and len(by_value) == len(expected),
             "adapter_review_figure_count_mismatch",
             "reviewed transcript must yield exactly the two reviewed figures")
    checked_figures: List[Dict[str, Any]] = []
    for kind, value_text, unit, qualifier in expected:
        figure = by_value.get(value_text)
        _require(figure is not None, "adapter_review_value_missing",
                 "%s did not yield reviewed value %s" % (contract["accession"], value_text))
        _require(figure.get("unit") == unit and figure.get("qualifier") == qualifier,
                 "adapter_review_semantics_mismatch",
                 "reviewed value %s has wrong unit or qualifier" % value_text)
        _require(figure.get("value_num") == float(value_text), "adapter_review_numeric_mismatch",
                 "reviewed value %s has wrong numeric representation" % value_text)
        sentence = _normalise_words(str(figure.get("sentence") or ""))
        if kind == "estimated_peak_day_delivery_capacity":
            _require("estimated peak day delivery capacity" in sentence and "facilit" in sentence,
                     "adapter_review_subject_mismatch", "peak-day value lost its facility subject")
        else:
            _require("estimated total storage capacity" in sentence,
                     "adapter_review_subject_mismatch", "storage value lost its storage subject")
        caveat_text = _normalise_words("\n".join(figure.get("caveats") or []))
        for caveat in contract["caveats"]:
            _require(_normalise_words(str(caveat)) in caveat_text,
                     "adapter_review_output_caveat_missing",
                     "reviewed figure %s lost output caveat %r" % (value_text, caveat))
        _require(not (figure.get("problems") or []), "adapter_review_output_ambiguous",
                 "reviewed figure %s is marked ambiguous" % value_text)
        checked_figures.append({"kind": kind, "value_text": value_text,
                                "value_num": figure["value_num"], "unit": unit,
                                "qualifier": qualifier,
                                "caveat_markers": list(contract["caveats"])})

    combined_review = dict(record)
    combined_review.update({
        "automatic_ocr_used": True,
        "ocr_engine": "macOS Vision VNRecognizeTextRequest accurate",
        "ocr_script_sha256": ocr_evidence.get("vision_script_sha256"),
        "ocr_checked_against_manual_review": True,
        "ocr_corrections": ocr_evidence.get("corrections") or [],
    })
    filing = {
        "filing_id": contract["accession"], "reporting_year": 2023,
        "filed_date": "2024-02-23", "_as_of": "2024-02-23",
        "_as_of_basis": "date on the independently reviewed filing", "_figures": figures,
        "_shifted": [], "_text_layer": "no", "_review": combined_review,
        "_extraction": capacity.IMAGE_OCR_METHOD, "_ocr_evidence": dict(ocr_evidence),
        "_document_id": "eLibrary|%s|a17-release-gate" % contract["accession"],
        "content_hash": source_hash, "version_status": "original",
    }
    observations = capacity._capacity_observations(
        None, {"entity_key": "A17_GATE_%s" % contract["accession"]}, filing,
        {"cap_reported_capacity": capacity.BY_ID["cap_reported_capacity"]})
    fact_observations = [item for item in observations if item.get("_document_fact")]
    facts_by_value = {str(item["value_text"]): item for item in fact_observations}
    _require(set(facts_by_value) == {item[1] for item in expected},
             "adapter_review_canonicalisation_mismatch",
             "manual transcription did not canonicalise to exactly the reviewed values")
    for _, value_text, _, _ in expected:
        observation = facts_by_value[value_text]
        fact = observation["_document_fact"]
        _require(observation.get("method") == "document_extracted",
                 "automatic_ocr_method_wrong", "OCR value is not labelled document extracted")
        _require(fact.get("extraction_method") == capacity.IMAGE_OCR_METHOD
                 and fact.get("review_state") == "reviewed",
                 "automatic_ocr_lineage_mismatch", "OCR value lost its reviewed image lineage")
        _require("ocr_reviewed_page_image" in str(fact.get("confidence") or ""),
                 "automatic_ocr_confidence_mismatch", "OCR value lost review confidence")
        _require("OCR used: True" in str(fact.get("reviewer_note") or ""),
                 "automatic_ocr_lineage_missing", "OCR value does not preserve OCR lineage")

    _require(fixture_source.get("ocr_used") is False, "review_fixture_ocr_mismatch",
             "independent fixture no longer records manual non-OCR review")
    return {"method": capacity.IMAGE_OCR_METHOD, "manual_review": True, "ocr_used": True,
            "reviewed_by": record["reviewed_by"], "reviewed_at": record.get("reviewed_at"),
            "historical_render_note": record.get("render"), "figures": checked_figures,
            "canonical_observations": len(fact_observations),
            "ocr_rows": sum(len(page.get("rows") or []) for page in ocr_pages),
            "ocr_corrections": list(ocr_evidence.get("corrections") or []),
            "vision_script_sha256": ocr_evidence.get("vision_script_sha256")}


def run_gate(root: pathlib.Path, cache_dir: pathlib.Path, fixture_path: pathlib.Path,
             pdftoppm: Optional[str] = None, swift: Optional[str] = None) -> Dict[str, Any]:
    """Run the offline gate and return a machine-readable result.

    The function reports failures instead of raising them so the CLI can always
    emit JSON.  ``status == 'verified'`` is the only successful outcome.
    """
    root = pathlib.Path(root).resolve()
    cache_dir = pathlib.Path(cache_dir).resolve()
    fixture_path = pathlib.Path(fixture_path).resolve()
    result: Dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "status": "failed",
        "purpose": "fail-closed pixel-to-OCR verification of two reviewed image-only FERC sources",
        "claim_boundary": {
            "value_source": "actual page pixels read by local OCR, checked against independent manual review",
            "render_use": "source/render integrity and OCR input",
            "ocr_used": True,
            "automatic_image_value_extraction_claimed": True,
            "network_used": False,
            "renders_retained": False,
        },
        "runtime": {"python_executable": sys.executable, "python_version": sys.version,
                    "platform": sys.platform},
        "inputs": {}, "tool": {}, "sources": [], "errors": [],
    }
    try:
        _require(cache_dir.is_dir() and not cache_dir.is_symlink(), "source_cache_invalid",
                 "source cache is absent or aliased: %s" % cache_dir)
        index_path = cache_dir / "index.json"
        index_raw, index = _load_json_file(index_path, "source_cache_index_invalid")
        _require(isinstance(index, dict), "source_cache_index_invalid",
                 "source_cache/index.json must contain an object")
        fixture_by_hash, fixture_identity = _validate_fixture(fixture_path)
        capacity = _load_capacity(root)
        executable = _resolve_pdftoppm(pdftoppm)
        tool = _tool_version(executable)
        result["tool"] = tool
        result["inputs"] = {
            "root": str(root),
            "source_cache_index": {"path": str(index_path), "bytes": len(index_raw),
                                   "sha256": _sha256(index_raw)},
            "review_fixture": fixture_identity,
            "capacity_adapter": _file_identity(root / "adapters" / "capacity.py"),
            "image_ocr_module": _file_identity(root / "ferclib" / "image_ocr.py"),
            "vision_ocr_source": _file_identity(root / "tools" / "vision_ocr.swift"),
        }

        for contract in SOURCE_CONTRACTS:
            try:
                path, index_keys = _resolve_cached_object(cache_dir, index, contract)
                raw = path.read_bytes()
                source_hash = _sha256(raw)
                _require(len(raw) == contract["source_bytes"], "source_object_size_mismatch",
                         "%s source byte size changed" % contract["accession"])
                _require(source_hash == contract["source_sha256"], "source_object_hash_mismatch",
                         "%s source SHA-256 changed" % contract["accession"])
                _require(raw.startswith(b"%PDF-"), "source_object_not_pdf",
                         "%s does not have PDF magic" % contract["accession"])

                extracted = capacity.extract_pdf(raw)
                extracted_pages = len(extracted)
                extracted_rows = sum(len(page.get("rows") or []) for page in extracted)
                _require(extracted_pages == contract["pages"], "pipeline_page_count_mismatch",
                         "%s pipeline extractor reports the wrong page count" % contract["accession"])
                _require(extracted_rows == 0, "pipeline_text_layer_detected",
                         "%s pipeline extractor no longer reports an image-only source" % contract["accession"])

                try:
                    ocr_pages, ocr_evidence = capacity.extract_image_pdf(
                        raw, vision_script=capacity.VISION_OCR_SCRIPT,
                        pdftoppm=str(executable), swift=swift,
                        expected_pages=contract["pages"])
                except capacity.ImageOCRFailure as exc:
                    raise GateFailure("automatic_ocr_failed", str(exc)) from exc
                reviewed = _validate_reviewed_transcription(
                    capacity, contract, fixture_by_hash[str(contract["source_sha256"])],
                    ocr_pages, ocr_evidence)
                render = _render_page_one(executable, path)
                _require(render["sha256"] == contract["render_sha256"],
                         "render_hash_mismatch", "%s page-1 render identity changed" % contract["accession"])
                _require((render["width"], render["height"]) ==
                         (contract["render_width"], contract["render_height"]),
                         "render_dimensions_mismatch",
                         "%s page-1 render dimensions changed" % contract["accession"])
                result["sources"].append({
                    "status": "verified", "accession": contract["accession"],
                    "entity": contract["entity"], "source_object": {
                        "path": str(path), "bytes": len(raw), "sha256": source_hash,
                        "pdf_magic_valid": True, "cache_index_keys": index_keys,
                    },
                    "pipeline_extractor": {"pages": extracted_pages, "rows": extracted_rows,
                                           "text_layer": False},
                    "automatic_ocr": {
                        "pages": len(ocr_pages),
                        "rows": sum(len(page.get("rows") or []) for page in ocr_pages),
                        "engine": "macOS Vision VNRecognizeTextRequest accurate",
                        "vision_script_sha256": ocr_evidence["vision_script_sha256"],
                        "page_images": [{"page": item["page"],
                                         "image_bytes": item["image_bytes"],
                                         "image_sha256": item["image_sha256"],
                                         "ocr_row_count": len(item["ocr_rows"])}
                                        for item in ocr_evidence["pages"]],
                        "corrections": ocr_evidence["corrections"],
                        "credential_environment_forwarded":
                            ocr_evidence["credential_environment_forwarded"],
                    },
                    "reviewed_transcription": reviewed,
                    "page_1_render": render,
                })
            except GateFailure as exc:
                result["sources"].append({"status": "failed",
                                          "accession": contract["accession"],
                                          "entity": contract["entity"],
                                          "error": {"code": exc.code, "message": str(exc)}})
                result["errors"].append({"code": exc.code, "accession": contract["accession"],
                                         "message": str(exc)})
    except GateFailure as exc:
        result["errors"].append({"code": exc.code, "message": str(exc)})
    except Exception as exc:  # fail closed even for an unanticipated runtime defect
        result["errors"].append({"code": "unexpected_gate_error",
                                 "message": "%s: %s" % (type(exc).__name__, exc)})

    if not result["errors"] and len(result["sources"]) == len(SOURCE_CONTRACTS) \
            and all(item.get("status") == "verified" for item in result["sources"]):
        result["status"] = "verified"
    result["summary"] = {
        "required_sources": len(SOURCE_CONTRACTS),
        "verified_sources": sum(item.get("status") == "verified" for item in result["sources"]),
        "failed_sources": sum(item.get("status") == "failed" for item in result["sources"]),
        "error_count": len(result["errors"]),
    }
    return result


def _write_result(result: Mapping[str, Any], output: Optional[pathlib.Path]) -> None:
    payload = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if output is None:
        sys.stdout.buffer.write(payload)
        return
    output = pathlib.Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=output.name + ".", suffix=".tmp",
                                     dir=str(output.parent), delete=False) as handle:
        temporary = pathlib.Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(str(temporary), str(output))
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = pathlib.Path(__file__).resolve().parent.parent
    parser.add_argument("--root", type=pathlib.Path, default=default_root,
                        help="release root (default: parent of this tool)")
    parser.add_argument("--cache-dir", type=pathlib.Path,
                        help="source cache (default: ROOT/source_cache)")
    parser.add_argument("--review-fixture", type=pathlib.Path,
                        help="review contract (default: ROOT/evidence/test_fixtures/reviewed_image_sources.json)")
    parser.add_argument("--pdftoppm", help="explicit pdftoppm executable; absence is a failure")
    parser.add_argument("--swift", help="explicit Swift executable for local Vision OCR")
    parser.add_argument("--output", type=pathlib.Path,
                        help="write JSON atomically here instead of stdout")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    cache_dir = (args.cache_dir or root / "source_cache").resolve()
    fixture = (args.review_fixture or root / "evidence" / "test_fixtures" /
               "reviewed_image_sources.json").resolve()
    result = run_gate(root, cache_dir, fixture, args.pdftoppm, args.swift)
    _write_result(result, args.output)
    return 0 if result["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
