#!/usr/bin/env python3
"""
Retired. The full bundle is now one payload of the single release builder.

This script used to be a second, independent packaging path with its own
manifest (`full_bundle_manifest.json`) and its own receipt
(`full_bundle_receipt.json`). That is precisely what audit A21 objected to: two
manifests describing overlapping file sets, disagreeing with each other, with an
older embedded `artifact_manifest.json` carrying three entries that were stale by
construction. Which artefact state did the earlier receipt certify? The question
had no answer, and that ambiguity is itself the defect.

A21's requirement is "one final immutable manifest/receipt sequence, explicitly
identifying the lite versus full deliverable". So there is now one builder and
one chain, and the payload is a flag:

    python3 build_release.py --payload full --defer-build-b
                                                # pending full candidate
    python3 build_release.py                    # lite: code and reports only

It also had the defect described in the header of build_release.py: it recorded
`f.stat().st_size` without reading the file, and on this volume stat() returns a
stale placeholder size for a cloud-evicted file. Its manifest therefore carried
byte counts that had never been verified against any bytes.

This shim forwards to the mandatory pending-Build-B path, so an existing command
keeps working and produces the single canonical candidate rather than a second
competing artefact or a weaker direct-full PASS receipt.
"""

from __future__ import annotations

import sys


def _forwarded_args(argv: list[str]) -> list[str]:
    return ["--payload", "full", "--defer-build-b", *argv]

if __name__ == "__main__":
    import build_release
    print(__doc__.strip(), "\n")
    print("forwarding to: build_release.py --payload full --defer-build-b\n")
    sys.exit(build_release.main(_forwarded_args(sys.argv[1:])))
