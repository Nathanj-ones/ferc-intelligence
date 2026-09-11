#!/usr/bin/env python3
"""Capture the official Federal Register sources behind ``oil_index``.

This is a deliberately narrow, one-shot evidence capture tool.  It obtains the
FederalRegister.gov document metadata, the official electronic Federal
Register PDF linked by that metadata from GPO's govinfo.gov, and the government
text rendition used for deterministic value checks.  It writes a new versioned
directory atomically and refuses to overwrite an existing bundle.

The runtime adapter never calls this tool and never accesses the network.  Its
only consumer is the declared, captured bundle written here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import oil_index  # noqa: E402


USER_AGENT = "FERC-operating-assets-source-capture/1.0"
ALLOWED_HOSTS = {
    "www.federalregister.gov",
    "federalregister.gov",
    "www.govinfo.gov",
    "govinfo.gov",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _fetch(url: str, *, timeout: int,
           allowed_statuses=(200,)) -> tuple[bytes, dict]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise RuntimeError(f"refusing non-official or non-HTTPS source URL: {url}")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        method="GET",
    )
    requested_at = _utc_now()
    try:
        with urllib.request.urlopen(
                request, timeout=timeout,
                context=ssl.create_default_context()) as response:
            data = response.read()
            status = int(response.status)
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        data = exc.read()
        status = int(exc.code)
        final_url = exc.geturl()
        content_type = exc.headers.get("Content-Type", "")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"retrieval failed for {url}: {exc.reason}") from exc
    if status not in allowed_statuses:
        raise RuntimeError(f"unexpected HTTP {status} retrieving {url}")
    final = urllib.parse.urlsplit(final_url)
    if final.scheme != "https" or final.hostname not in ALLOWED_HOSTS:
        raise RuntimeError(f"source redirected outside official hosts: {final_url}")
    if not data:
        raise RuntimeError(f"empty HTTP 200 body from {final_url}")
    return data, {
        "request_url": url,
        "requested_at": requested_at,
        "http_status": status,
        "final_url": final_url,
        "content_type": content_type,
        "bytes": len(data),
        "sha256": _sha256(data),
    }


def _write_artifact(root: pathlib.Path, relative: str, data: bytes,
                    provenance: dict) -> dict:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": relative, **provenance}


def _metadata_checks(metadata: dict, expected: dict) -> None:
    doc = expected["fr_document"]
    published = expected["published"]
    if metadata.get("document_number") != doc:
        raise RuntimeError(
            f"metadata identity mismatch for {doc}: "
            f"{metadata.get('document_number')!r}")
    if metadata.get("publication_date") != published:
        raise RuntimeError(
            f"metadata date mismatch for {doc}: "
            f"{metadata.get('publication_date')!r} != {published!r}")
    agencies = {row.get("name") for row in metadata.get("agencies", [])}
    if "Federal Energy Regulatory Commission" not in agencies:
        raise RuntimeError(f"{doc}: metadata does not identify FERC")
    if not any(oil_index.DOCKET in str(value)
               for value in metadata.get("docket_ids", [])):
        raise RuntimeError(f"{doc}: metadata does not identify {oil_index.DOCKET}")
    identity_text = " ".join(str(metadata.get(field) or "")
                             for field in ("title", "action", "abstract")).lower()
    if ("oil pipeline" not in identity_text
            or ("producer price index" not in identity_text
                and "oil pipeline index figure" not in identity_text)):
        raise RuntimeError(f"{doc}: unexpected Federal Register subject")
    for field in ("pdf_url", "raw_text_url"):
        url = str(metadata.get(field) or "")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise RuntimeError(f"{doc}: invalid official {field}: {url!r}")


def _text_checks(raw: bytes, expected: dict) -> None:
    doc = expected["fr_document"]
    text = raw.decode("utf-8", errors="strict")
    normalised = " ".join(text.split())
    if oil_index.DOCKET not in normalised:
        raise RuntimeError(f"{doc}: captured text omits docket {oil_index.DOCKET}")
    if doc.lower() not in normalised.lower():
        raise RuntimeError(f"{doc}: captured text omits its document number")
    if expected["factor"] not in normalised:
        raise RuntimeError(
            f"{doc}: captured text does not contain factor {expected['factor']}")
    change = expected["index_change"]
    if change:
        negative = change.startswith("-")
        digits = re.escape(change.lstrip("+-0"))
        sign = r"(?:negative\s+|-\s*)" if negative else r"(?:positive\s+|\+\s*)?"
        pattern = rf"(?<![0-9]){sign}0?{digits}(?![0-9])"
        if re.search(pattern, normalised, flags=re.IGNORECASE) is None:
            raise RuntimeError(
                f"{doc}: captured text does not contain published change "
                f"{change}")
    if not oil_index._text_supports_interval(text, expected):
        raise RuntimeError(
            f"{doc}: captured text has no operative factor/target-interval passage")


def capture(destination: pathlib.Path, *, timeout: int) -> pathlib.Path:
    destination = destination.resolve()
    if destination.exists():
        raise RuntimeError(f"refusing to overwrite existing bundle: {destination}")
    partial = destination.with_name(
        f".{destination.name}.partial.{os.getpid()}")
    if partial.exists():
        raise RuntimeError(f"partial path already exists: {partial}")
    partial.mkdir(parents=True)
    captured_at = _utc_now()
    documents = []
    try:
        for expected in oil_index.INDEX_TABLE:
            doc = expected["fr_document"]
            api_url = (
                "https://www.federalregister.gov/api/v1/documents/"
                f"{urllib.parse.quote(doc, safe='')}.json")
            metadata_raw, metadata_provenance = _fetch(api_url, timeout=timeout)
            metadata = json.loads(metadata_raw.decode("utf-8"))
            _metadata_checks(metadata, expected)

            pdf_raw, pdf_provenance = _fetch(
                metadata["pdf_url"], timeout=timeout)
            if not pdf_raw.startswith(b"%PDF-"):
                raise RuntimeError(f"{doc}: govinfo response is not a PDF")

            text_raw, text_provenance = _fetch(
                metadata["raw_text_url"], timeout=timeout)
            _text_checks(text_raw, expected)

            base = f"documents/{doc}"
            metadata_entry = _write_artifact(
                partial, f"{base}/federalregister_metadata.json",
                metadata_raw, metadata_provenance)
            pdf_entry = _write_artifact(
                partial, f"{base}/official_govinfo.pdf",
                pdf_raw, pdf_provenance)
            text_entry = _write_artifact(
                partial, f"{base}/federalregister_text.txt",
                text_raw, text_provenance)
            metrics = [oil_index.METRIC]
            if expected["index_change"]:
                metrics.append(oil_index.METRIC_CHANGE)
            documents.append({
                "fr_document": doc,
                "publication_date": expected["published"],
                "federal_register_citation": metadata.get("citation"),
                "docket": oil_index.DOCKET,
                "consumer": {
                    "adapter": oil_index.ADAPTER,
                    "source_table_version": oil_index.SOURCE_TABLE_VERSION,
                    "metrics": metrics,
                },
                "asserted_values": {
                    "factor": expected["factor"],
                    "index_change": expected["index_change"],
                    "interval_start": expected["interval_start"],
                    "interval_end": expected["interval_end"],
                    "effective_from": expected["effective_from"],
                },
                "metadata": metadata_entry,
                "official_pdf": pdf_entry,
                "extraction_text": text_entry,
            })

        summary_url = (
            "https://www.ferc.gov/general-information-1/oil-pipeline-index")
        summary_raw, summary_provenance = _fetch(
            summary_url, timeout=timeout, allowed_statuses=(403,))
        summary_relative = "supplemental/ferc_summary_http_403.html"
        summary_entry = _write_artifact(
            partial, summary_relative, summary_raw, summary_provenance)

        manifest = {
            "manifest_version": 1,
            "binding_revision": oil_index.SOURCE_BINDING_REVISION,
            "bundle_version": oil_index.SOURCE_BUNDLE_VERSION,
            "captured_at": captured_at,
            "capture_tool": "tools/capture_oil_index_sources.py",
            "source_scope": (
                "Federal Register publication occurrences used by the FERC "
                "Oil Pipeline Index adapter; official electronic PDFs are "
                "from GPO govinfo and text renditions are from the Office of "
                "the Federal Register/GPO service."),
            "authority": (
                "The govinfo PDF is the authoritative electronic Federal "
                "Register artifact. The FederalRegister.gov metadata and text "
                "are retained for identity and deterministic extraction checks; "
                "they do not replace the official PDF."),
            "runtime_network_required": False,
            "documents": documents,
            "supplemental_attempts": [{
                "attempted": True,
                "classification": oil_index.CLASS_ACCESS_BLOCKED,
                "purpose": (
                    "Bounded diagnostic probe of FERC's convenience summary "
                    "page; response body retained, transient response headers "
                    "and Set-Cookie excluded."),
                "explicitly_not": (
                    "This is not a FERC outage and is not evidence that the "
                    "official notices are unavailable. The summary page is a "
                    "non-authoritative convenience source and no value depends "
                    "on it."),
                **summary_entry,
            }],
        }
        encoded = (json.dumps(manifest, indent=2, sort_keys=True)
                   + "\n").encode("utf-8")
        (partial / "manifest.json").write_bytes(encoded)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial.rename(destination)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return destination


def rebind_existing(destination: pathlib.Path) -> pathlib.Path:
    """Atomically bind already-captured bytes to the current source table.

    No response body is changed or fetched.  The prior manifest is retained by
    full hash as ``manifest.capture.v1.json`` before the consumer/value binding
    is updated.  This path exists for the concrete repair discovered while
    inspecting the captured notices: every notice publishes an index change,
    while the first table binding retained only seven of them.
    """
    destination = destination.resolve()
    manifest_path = destination / "manifest.json"
    raw = manifest_path.read_bytes()
    prior_hash = _sha256(raw)
    manifest = json.loads(raw.decode("utf-8"))
    if manifest.get("bundle_version") != oil_index.SOURCE_BUNDLE_VERSION:
        raise RuntimeError("refusing to rebind a different source-bundle version")
    entries = manifest.get("documents")
    if not isinstance(entries, list):
        raise RuntimeError("captured manifest has no document list")
    by_document = {entry.get("fr_document"): entry for entry in entries}
    expected_ids = {row["fr_document"] for row in oil_index.INDEX_TABLE}
    if set(by_document) != expected_ids or len(entries) != len(expected_ids):
        raise RuntimeError("captured manifest occurrence set does not match INDEX_TABLE")

    for expected in oil_index.INDEX_TABLE:
        entry = by_document[expected["fr_document"]]
        for kind in ("metadata", "official_pdf", "extraction_text"):
            artifact = entry[kind]
            path = destination / artifact["path"]
            body = path.read_bytes()
            if (len(body) != artifact["bytes"]
                    or _sha256(body) != artifact["sha256"]):
                raise RuntimeError(
                    f"{expected['fr_document']}: refusing to rebind altered {kind}")
        text = (destination / entry["extraction_text"]["path"]).read_bytes()
        _text_checks(text, expected)
        metrics = [oil_index.METRIC, oil_index.METRIC_CHANGE]
        entry["consumer"] = {
            "adapter": oil_index.ADAPTER,
            "source_table_version": oil_index.SOURCE_TABLE_VERSION,
            "metrics": metrics,
        }
        entry["asserted_values"] = {
            "factor": expected["factor"],
            "index_change": expected["index_change"],
            "interval_start": expected["interval_start"],
            "interval_end": expected["interval_end"],
            "effective_from": expected["effective_from"],
        }

    prior_revision = int(manifest.get("binding_revision") or 1)
    if prior_revision >= oil_index.SOURCE_BINDING_REVISION:
        raise RuntimeError(
            f"manifest binding revision {prior_revision} is not older than "
            f"target {oil_index.SOURCE_BINDING_REVISION}")
    receipt_name = ("manifest.capture.v1.json" if prior_revision == 1
                    else f"manifest.binding.v{prior_revision}.json")
    receipt = destination / receipt_name
    if receipt.exists():
        if _sha256(receipt.read_bytes()) != prior_hash:
            raise RuntimeError("existing binding receipt does not match prior manifest")
    else:
        receipt.write_bytes(raw)
    history = list(manifest.get("binding_history") or [])
    history.append({
        "binding_revision": prior_revision,
        "manifest_sha256": prior_hash,
        "receipt": receipt_name,
        "reason": manifest.get("binding_reason", "initial capture binding"),
    })
    manifest["binding_history"] = history
    manifest["binding_revision"] = oil_index.SOURCE_BINDING_REVISION
    manifest["binding_updated_at"] = _utc_now()
    manifest["binding_reason"] = (
        "Source-backed binding v3: retain both quantities from all 31 notices "
        "and preserve newly identified internal source anomalies in FR "
        "00-13115 and FR E7-12192 without changing the published values.")
    manifest["previous_manifest_sha256"] = prior_hash
    encoded = (json.dumps(manifest, indent=2, sort_keys=True)
               + "\n").encode("utf-8")
    temporary = destination / f".manifest.json.partial.{os.getpid()}"
    temporary.write_bytes(encoded)
    temporary.replace(manifest_path)
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--destination", type=pathlib.Path,
        default=ROOT / "inputs" / "official_ferc" / "oil_pipeline_index" / "v1")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--rebind-existing", action="store_true")
    args = parser.parse_args()
    if args.rebind_existing:
        path = rebind_existing(args.destination)
        print(json.dumps({"status": "rebound", "path": str(path)}, sort_keys=True))
        return 0
    path = capture(args.destination, timeout=args.timeout)
    print(json.dumps({"status": "captured", "path": str(path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
