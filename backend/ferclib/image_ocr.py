"""Bounded, offline page-image OCR for image-only official FERC PDFs.

This module has no retrieval path.  It renders bytes already supplied by the
caller with Poppler and invokes the local macOS Vision recognizer through the
shipped Swift source.  Both child processes receive a minimal environment with
no FERC credential.  The returned rows retain coordinates, confidence, raw OCR
text and every deterministic correction.

Only two narrow OCR-confusion corrections are applied: ``Bef`` is normalised to
``Bcf`` when the same OCR row explicitly says ``storage capacity``; and
``assumptior`` is normalised to ``assumptions`` only inside the complete phrase
``reasonably representative operating ...``.  These fix observed glyph
confusions without guessing a value, unit, or missing source sentence from an
expected answer.  Raw text remains recorded alongside the normalised row.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple


OCR_SCHEMA = "ferc-macos-vision-ocr-v1"
DEFAULT_DPI = 180
_BCF_CONFUSION = re.compile(r"\bBef\b", re.IGNORECASE)
_OPERATING_ASSUMPTIONS_CONFUSION = re.compile(
    r"(?P<prefix>\breasonably\s+representative\s+operating\s+)assumptior\b",
    re.IGNORECASE,
)


class ImageOCRFailure(RuntimeError):
    """A local rendering/OCR prerequisite or output failed validation."""


def _resolve_executable(requested: Optional[str], name: str) -> Path:
    value = requested or shutil.which(name)
    if not value:
        raise ImageOCRFailure(f"required local executable {name!r} was not found")
    path = Path(value).resolve()
    if not path.is_file() or not os.access(str(path), os.X_OK):
        raise ImageOCRFailure(f"required local executable is not executable: {path}")
    return path


def _child_environment(temp: Path) -> Dict[str, str]:
    # Deliberately do not copy os.environ: FERC_API_KEY and unrelated user
    # configuration must not reach a local OCR subprocess as hidden input.
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "TMPDIR": str(temp),
        "CLANG_MODULE_CACHE_PATH": str(temp / "clang-module-cache"),
        "SWIFT_MODULE_CACHE_PATH": str(temp / "swift-module-cache"),
    }


def _run(argv: List[str], *, env: Dict[str, str], timeout: int,
         label: str) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            env=env,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImageOCRFailure(f"{label} could not execute: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[:1000]
        raise ImageOCRFailure(
            f"{label} returned {completed.returncode}: {detail}")
    return completed


def _normalise_row(text: str) -> Tuple[str, List[dict]]:
    normalised = " ".join(text.split())
    corrections: List[dict] = []
    corrected, count = _OPERATING_ASSUMPTIONS_CONFUSION.subn(
        r"\g<prefix>assumptions", normalised)
    if count:
        corrections.append({
            "kind": "bounded_ocr_caveat_glyph_correction",
            "from": normalised,
            "to": corrected,
            "rule": ("assumptior -> assumptions only after "
                     "reasonably representative operating"),
        })
        normalised = corrected
    if re.search(r"storage\s+capacity", normalised, re.IGNORECASE):
        corrected, count = _BCF_CONFUSION.subn("Bcf", normalised)
        if count:
            corrections.append({
                "kind": "bounded_ocr_unit_glyph_correction",
                "from": normalised,
                "to": corrected,
                "rule": "Bef -> Bcf only on a row that states storage capacity",
            })
            normalised = corrected
    return normalised, corrections


def extract_image_pdf(raw: bytes, *, vision_script: Path,
                      pdftoppm: Optional[str] = None,
                      swift: Optional[str] = None,
                      dpi: int = DEFAULT_DPI,
                      expected_pages: Optional[int] = None) -> Tuple[List[dict], dict]:
    """Return ``capacity.extract_pdf``-shaped pages plus an OCR evidence record."""
    if not raw.startswith(b"%PDF-"):
        raise ImageOCRFailure("image OCR input is not a PDF")
    if not isinstance(dpi, int) or dpi < 120 or dpi > 600:
        raise ImageOCRFailure(f"unsupported render DPI: {dpi!r}")
    script = Path(vision_script).resolve()
    if not script.is_file() or script.is_symlink():
        raise ImageOCRFailure(f"shipped Vision OCR source is missing: {script}")
    pdftoppm_path = _resolve_executable(pdftoppm, "pdftoppm")
    swift_path = _resolve_executable(swift, "swift")

    with tempfile.TemporaryDirectory(prefix="ferc-image-ocr-") as name:
        temp = Path(name)
        env = _child_environment(temp)
        pdf_path = temp / "source.pdf"
        pdf_path.write_bytes(raw)
        prefix = temp / "page"
        render_argv = [
            str(pdftoppm_path), "-r", str(dpi), "-png",
            str(pdf_path), str(prefix),
        ]
        rendered = _run(render_argv, env=env, timeout=120, label="pdftoppm")
        image_paths = sorted(temp.glob("page-*.png"))
        if not image_paths:
            single = prefix.with_suffix(".png")
            image_paths = [single] if single.is_file() else []
        if not image_paths:
            raise ImageOCRFailure("pdftoppm returned success without page images")
        if expected_pages is not None and len(image_paths) != expected_pages:
            raise ImageOCRFailure(
                f"rendered page count {len(image_paths)} != expected {expected_pages}")

        pages: List[dict] = []
        page_evidence: List[dict] = []
        all_corrections: List[dict] = []
        for page_number, image_path in enumerate(image_paths, start=1):
            image_bytes = image_path.read_bytes()
            ocr_argv = [str(swift_path), str(script), str(image_path)]
            recognised = _run(ocr_argv, env=env, timeout=120,
                              label=f"macOS Vision OCR page {page_number}")
            try:
                payload = json.loads(recognised.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ImageOCRFailure(f"Vision OCR returned invalid JSON: {exc}") from exc
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if payload.get("schema") != OCR_SCHEMA or not isinstance(rows, list) or not rows:
                raise ImageOCRFailure("Vision OCR returned the wrong schema or no rows")

            page_rows: List[dict] = []
            raw_rows: List[dict] = []
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or not isinstance(row.get("text"), str):
                    raise ImageOCRFailure(f"Vision OCR row {index} is invalid")
                raw_text = row["text"]
                text, corrections = _normalise_row(raw_text)
                if not text:
                    continue
                try:
                    x = float(row["x"])
                    y = float(row["y"])
                    width = float(row["width"])
                    confidence = float(row["confidence"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ImageOCRFailure(f"Vision OCR row {index} lacks geometry") from exc
                if not (0 <= x <= 1 and 0 <= y <= 1 and 0 <= width <= 1
                        and 0 <= confidence <= 1):
                    raise ImageOCRFailure(f"Vision OCR row {index} has invalid geometry")
                for correction in corrections:
                    correction.update({"page": page_number, "row": index})
                    all_corrections.append(correction)
                # Capacity's parser uses PDF-point-like coordinates only for
                # ordering/evidence.  Vision coordinates are normalised with a
                # lower-left origin, so scaling preserves those relationships.
                x_points = round(x * 612.0, 3)
                x_end = round((x + width) * 612.0, 3)
                y_points = round(y * 792.0, 3)
                page_rows.append({
                    "y": y_points,
                    "cells": [{"x": x_points, "x_end": x_end,
                               "text": text, "size": 11.0}],
                    "reordered": False,
                    "text": text,
                    "ocr_raw_text": raw_text,
                    "ocr_confidence": confidence,
                })
                raw_rows.append({
                    "text": raw_text,
                    "normalised_text": text,
                    "confidence": confidence,
                    "x": x,
                    "y": y,
                    "width": width,
                })
            if not page_rows:
                raise ImageOCRFailure(f"Vision OCR page {page_number} has no usable rows")
            page_rows.sort(key=lambda row: -row["y"])
            pages.append({
                "page": page_number,
                "rows": page_rows,
                "height": 792.0,
                "shifted_fonts": [],
                "image_ocr": True,
            })
            page_evidence.append({
                "page": page_number,
                "image_bytes": len(image_bytes),
                "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                "ocr_rows": raw_rows,
                "stderr": recognised.stderr.decode("utf-8", errors="replace")[:1000],
            })

        evidence = {
            "schema": "ferc-image-pdf-ocr-evidence-v1",
            "source_bytes": len(raw),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "dpi": dpi,
            "pdftoppm": str(pdftoppm_path),
            "swift": str(swift_path),
            "vision_script": str(script),
            "vision_script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            "render_argv": render_argv,
            "render_stderr": rendered.stderr.decode("utf-8", errors="replace")[:1000],
            "pages": page_evidence,
            "corrections": all_corrections,
            "network_used": False,
            "credential_environment_forwarded": False,
        }
        return pages, evidence
