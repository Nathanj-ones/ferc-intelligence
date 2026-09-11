"""Install the hash-pinned starting data from this repository's private release."""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def sha256(file):
    h = hashlib.sha256()
    with file.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def checked_member(name):
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name:
        raise ValueError('Unsafe bootstrap path')
    if name in {'staging/operating_assets.sqlite', 'source_cache/cache.sqlite', 'source_cache/index.sqlite', 'publication_receipt.json'}:
        return path
    if (len(path.parts) >= 4 and path.parts[:2] == ('.generations', 'consumer_exports')
            and len(path.parts[2]) == 64 and all(c in '0123456789abcdef' for c in path.parts[2])
            and path.suffix in {'.json', '.csv'}):
        return path
    if name == 'source_cache/index.json' or (len(path.parts) == 4 and path.parts[:2] == ('source_cache', 'objects')
                                           and len(path.parts[2]) == 2 and len(path.parts[3]) == 64
                                           and all(c in '0123456789abcdef' for c in path.parts[2] + path.parts[3])):
        return path
    raise ValueError('Unexpected bootstrap member: ' + name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', type=pathlib.Path, help='Use a data archive; the publication archive must be alongside it')
    args = parser.parse_args()
    manifest = json.loads((ROOT / 'backend/bootstrap.json').read_text())
    backend = ROOT / 'backend'
    marker = backend / 'staging/bootstrap-installed.json'
    if marker.exists():
        if json.loads(marker.read_text()).get('sha256') != manifest['sha256']:
            raise SystemExit('A different bootstrap is installed; preserve it and use a fresh checkout')
        for relative in ['staging/operating_assets.sqlite', 'source_cache/index.json']:
            if not (backend / relative).is_file():
                raise SystemExit('Incomplete bootstrap; use a fresh checkout to reinstall safely')
        print('Reviewed backend data is already installed.')
        return
    destinations = ['staging', 'source_cache', '.generations', 'exports', 'publication_receipt.json']
    if any((backend / name).exists() for name in destinations):
        raise SystemExit('Existing backend data is not managed by setup; refusing to overwrite it')
    downloads = ROOT / '.ferc-local/downloads'
    downloads.mkdir(parents=True, exist_ok=True, mode=0o700)
    assets = [manifest, manifest['publicationAsset']]
    archives = []
    for asset in assets:
        archive = ((args.archive.resolve() if asset is manifest else args.archive.resolve().parent / asset['asset'])
                   if args.archive else downloads / asset['asset'])
        if not archive.exists():
            if args.archive:
                raise SystemExit('Both data and publication archives must be downloaded into the same folder')
            if not shutil.which('gh'):
                raise SystemExit('Install GitHub CLI and run gh auth login, or supply --archive /path/to/download')
            print('Downloading ' + asset['asset'] + ' from the private GitHub release…', flush=True)
            subprocess.run(['gh', 'release', 'download', manifest['release'], '--repo', manifest['repository'],
                            '--pattern', asset['asset'], '--dir', str(downloads)], check=True)
        if archive.stat().st_size != asset['bytes'] or sha256(archive) != asset['sha256']:
            raise SystemExit('Bootstrap checksum mismatch; nothing installed. Remove only the invalid download and retry.')
        archives.append((asset, archive))
    if shutil.disk_usage(backend).free < sum(x['unpackedBytes'] for x in assets) + 1024 ** 3:
        raise SystemExit('Not enough disk space to install backend data safely')
    with tempfile.TemporaryDirectory(prefix='.backend-setup-', dir=downloads) as temporary:
        stage = pathlib.Path(temporary)
        for asset, archive in archives:
            with zipfile.ZipFile(archive) as source:
                files = json.loads(source.read('BOOTSTRAP_FILES.json'))
                entries = source.infolist()
                names = [entry.filename for entry in entries]
                if len(names) != len(set(names)) or set(names) != set(files) | {'BOOTSTRAP_FILES.json'}:
                    raise SystemExit('Bootstrap archive does not match its file manifest')
                if len(files) != asset['files'] or sum(x['bytes'] for x in files.values()) != asset['unpackedBytes']:
                    raise SystemExit('Bootstrap archive totals do not match the pinned manifest')
                for entry in entries:
                    if entry.filename == 'BOOTSTRAP_FILES.json':
                        continue
                    relative = checked_member(entry.filename)
                    expected = files[entry.filename]
                    if stat.S_ISLNK(entry.external_attr >> 16) or entry.file_size != expected['bytes']:
                        raise SystemExit('Invalid bootstrap member type or size')
                    output = stage / relative
                    if output.exists():
                        raise SystemExit('Duplicate path across bootstrap assets')
                    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with source.open(entry) as incoming, output.open('wb') as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                    os.chmod(output, 0o600)
                    if sha256(output) != expected['sha256']:
                        raise SystemExit('Bootstrap member checksum mismatch')
        con = sqlite3.connect((stage / 'staging/operating_assets.sqlite').as_uri() + '?mode=ro', uri=True)
        if con.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise SystemExit('Bootstrap database integrity check failed')
        for table, expected in manifest['databaseCounts'].items():
            if table not in {'assets', 'entities', 'observations', 'reviewed_source_annotations'}:
                raise SystemExit('Unexpected bootstrap table')
            if con.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] != expected:
                raise SystemExit('Bootstrap database row count mismatch')
        con.close()
        receipt = json.loads((stage / 'publication_receipt.json').read_text())
        generation = '.generations/consumer_exports/' + manifest['sourceGeneration']
        if receipt['generation_path'] != generation or receipt['generation_id'] != manifest['sourceGeneration']:
            raise SystemExit('Publication does not match the reviewed seed generation')
        for relative, expected in receipt['files'].items():
            if pathlib.PurePosixPath(relative).is_absolute() or '..' in pathlib.PurePosixPath(relative).parts:
                raise SystemExit('Unsafe publication path')
            source = stage / checked_member(generation + '/' + relative)
            if source.stat().st_size != expected['bytes'] or sha256(source) != expected['sha256']:
                raise SystemExit('Publication receipt member mismatch')
            target = stage / 'exports' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        # Publish only after every member and the SQLite database have passed.
        installed = []
        try:
            for name in destinations:
                os.rename(stage / name, backend / name)
                installed.append(name)
            marker.write_text(json.dumps({'sha256': manifest['sha256']}) + '\n')
        except BaseException:
            for name in reversed(installed):
                os.rename(backend / name, stage / name)
            raise
    print('Backend installed and verified. Run npm run backend:check, then npm run dev:live.')


if __name__ == '__main__':
    main()
