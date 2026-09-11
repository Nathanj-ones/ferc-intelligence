"""Attach the reviewed publication/evidence boundary to the bootstrap manifest."""
import argparse
import hashlib
import json
import pathlib
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=pathlib.Path, required=True)
parser.add_argument('--archive', type=pathlib.Path, required=True)
parser.add_argument('--manifest', type=pathlib.Path, required=True)
args = parser.parse_args()
if args.archive.exists():
    raise SystemExit('Refusing to overwrite an existing publication artifact')
receipt = json.loads((args.source / 'publication_receipt.json').read_text())
prefix = '.generations/consumer_exports/' + receipt['generation_id']
assert receipt['generation_path'] == prefix
members = {'publication_receipt.json': args.source / 'publication_receipt.json',
           prefix + '/GENERATION_MANIFEST.json': args.source / prefix / 'GENERATION_MANIFEST.json'}
for relative, expected in receipt['files'].items():
    assert '..' not in pathlib.PurePosixPath(relative).parts and not relative.startswith('/')
    file = args.source / prefix / relative
    data = file.read_bytes()
    assert len(data) == expected['bytes'] and hashlib.sha256(data).hexdigest() == expected['sha256']
    members[prefix + '/' + relative] = file
files = {}
with zipfile.ZipFile(args.archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
    for relative, file in members.items():
        data = file.read_bytes()
        files[relative] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        output.writestr(relative, data)
    output.writestr('BOOTSTRAP_FILES.json', json.dumps(files, sort_keys=True))
manifest = json.loads(args.manifest.read_text())
manifest['publicationAsset'] = {'asset': args.archive.name, 'bytes': args.archive.stat().st_size,
                                'sha256': hashlib.sha256(args.archive.read_bytes()).hexdigest(),
                                'unpackedBytes': sum(x['bytes'] for x in files.values()), 'files': len(files)}
args.manifest.write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps(manifest['publicationAsset'], indent=2))
