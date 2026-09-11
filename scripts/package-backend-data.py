"""Maintainer: package a reviewed, credential-free bootstrap for a private release."""
import argparse
import hashlib
import json
import pathlib
import re
import sqlite3
import tempfile
import zipfile


def digest(file):
    h = hashlib.sha256()
    with file.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=pathlib.Path, required=True)
    parser.add_argument('--archive', type=pathlib.Path, required=True)
    parser.add_argument('--manifest', type=pathlib.Path, required=True)
    args = parser.parse_args()
    if args.archive.exists() or args.manifest.exists():
        raise SystemExit('Refusing to overwrite an existing release artifact')
    args.archive.parent.mkdir(parents=True, exist_ok=True)
    patterns = [rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----',
                rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b',
                rb'\bgithub_pat_[A-Za-z0-9_]{40,}\b',
                rb'\bAKIA[A-Z0-9]{16}\b',
                rb'[?&](?:api_key|apikey|access_token)=[A-Za-z0-9_-]{24,}']
    with tempfile.TemporaryDirectory(prefix='ferc-reviewed-seed-') as temporary:
        db = pathlib.Path(temporary) / 'operating_assets.sqlite'
        source = sqlite3.connect((args.source / 'staging/operating_assets.sqlite').resolve().as_uri() + '?mode=ro', uri=True)
        target = sqlite3.connect(db)
        source.backup(target)
        source.close()
        if target.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise SystemExit('Invalid source database')
        counts = {table: target.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in ['assets', 'entities', 'observations', 'reviewed_source_annotations']}
        target.close()
        members = [('staging/operating_assets.sqlite', db)]
        members += [(p.relative_to(args.source).as_posix(), p)
                    for p in sorted((args.source / 'source_cache').rglob('*'))
                    if p.is_file() and p.name != '.DS_Store']
        file_manifest = {}
        with zipfile.ZipFile(args.archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, file in members:
                if file.is_symlink():
                    raise SystemExit('Refusing a symlink in bootstrap: ' + name)
                # Chunk overlap catches a credential spanning a read boundary.
                tail = b''
                with file.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        scan = tail + block
                        if any(re.search(pattern, scan) for pattern in patterns):
                            raise SystemExit('Potential secret in ' + name + '; package not approved')
                        tail = scan[-512:]
                file_manifest[name] = {'bytes': file.stat().st_size, 'sha256': digest(file)}
                archive.write(file, name)
            archive.writestr('BOOTSTRAP_FILES.json', json.dumps(file_manifest, sort_keys=True))
        manifest = {'schema': 'ferc_backend_bootstrap_v1', 'repository': 'Nathanj-ones/ferc-intelligence',
                    'release': 'backend-data-2026-09-10-v1', 'asset': args.archive.name,
                    'sha256': digest(args.archive), 'bytes': args.archive.stat().st_size,
                    'unpackedBytes': sum(x['bytes'] for x in file_manifest.values()),
                    'files': len(file_manifest), 'databaseCounts': counts,
                    'sourceGeneration': json.loads((args.source / 'publication_receipt.json').read_text())['generation_id']}
        args.manifest.write_text(json.dumps(manifest, indent=2) + '\n')
        print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
