#!/usr/bin/env python3
"""Exercise the real Kilo V2 loader against an immutable public Git package."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', required=True, type=Path)
    parser.add_argument('--reference', required=True, type=Path)
    parser.add_argument('--ref', required=True)
    parser.add_argument('--work', required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.ref):
        raise ValueError('An immutable public commit is required')
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    workspace = work / 'workspace'
    workspace.mkdir()
    (workspace / '.git').mkdir()
    (workspace / '.env').touch()
    env = {key: os.environ[key] for key in ['PATH', 'HOME', 'SYSTEMROOT', 'TMPDIR',
                                            'TEMP', 'TMP', 'SSL_CERT_FILE'] if key in os.environ}
    for key in ['config', 'data', 'cache', 'state']:
        directory = work / key
        directory.mkdir()
        env['XDG_' + key.upper() + '_HOME'] = str(directory)
    empty_npm = work / 'empty.npmrc'
    empty_npm.touch()
    env.update({'npm_config_userconfig': str(empty_npm), 'npm_config_cache': str(work / 'npm-cache'),
                'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'credential.helper',
                'GIT_CONFIG_VALUE_0': '', 'GIT_TERMINAL_PROMPT': '0', 'DO_NOT_TRACK': '1'})
    cli = str(args.cli.resolve())
    version = subprocess.check_output([cli, '--version'], env=env, cwd=workspace, text=True).strip()
    if version != '7.8.15':
        raise ValueError('Unexpected native host version: ' + version)
    spec = 'solaris-dev-shop-kilo@github:Shaisolaris/solaris-dev-shop-kilo#' + args.ref
    with (work / 'install.log').open('w') as output:
        subprocess.run([cli, 'plugin', spec], env=env, cwd=workspace, stdout=output,
                       stderr=subprocess.STDOUT, check=True, timeout=180)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    password = secrets.token_urlsafe(24)
    env['KILO_SERVER_USERNAME'] = 'solaris-native-qa'
    env['KILO_SERVER_PASSWORD'] = password
    headers = {'Authorization': 'Basic ' + base64.b64encode(
        ('solaris-native-qa:' + password).encode()).decode()}
    query = urllib.parse.urlencode({'location[directory]': str(workspace)})
    index = json.loads((args.reference / 'native/skills.json').read_text())
    names = {item['name'] for item in index}
    assert len(index) == len(names) == 62
    with (work / 'server.log').open('w') as output:
        server = subprocess.Popen([cli, 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
                                  env=env, cwd=workspace, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 90
            rows = []
            while time.monotonic() < deadline:
                assert server.poll() is None, 'Native server exited before discovery'
                try:
                    request = urllib.request.Request(
                        f'http://127.0.0.1:{port}/api/skill?' + query, headers=headers)
                    with urllib.request.urlopen(request, timeout=3) as response:
                        body = json.load(response)
                    catalog = body.get('data', body) if isinstance(body, dict) else body
                    rows = [item for item in catalog if item.get('name') in names]
                    if len(rows) == 62:
                        break
                except (OSError, ValueError, TypeError):
                    pass
                time.sleep(0.3)
            assert len(rows) == len({item['name'] for item in rows}) == 62, 'Incomplete native catalog'
            installed = list((work / 'cache/kilo/packages').rglob('solaris-dev-shop-kilo/native/skills.json'))
            assert len(installed) == 1, installed
            root = installed[0].parent.parent
            expected_names = {p.relative_to(args.reference).as_posix()
                              for p in args.reference.rglob('*') if p.is_file()}
            assert not any(p.is_symlink() for p in root.rglob('*')), 'Symlink in native installed package'
            actual_names = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
            assert actual_names == expected_names, 'Missing or unexpected installed product files'
            count = 0
            for source in args.reference.rglob('*'):
                if not source.is_file():
                    continue
                target = root / source.relative_to(args.reference)
                assert target.is_file() and not target.is_symlink() and target.read_bytes() == source.read_bytes(), source
                assert bool(target.stat().st_mode & 0o111) == bool(source.stat().st_mode & 0o111), source
                count += 1
            assert count == 1977, count
            by_name = {item['name']: item for item in rows}
            for item in index:
                actual = by_name[item['name']]
                canonical = root / item['canonical']
                assert Path(actual['location']).resolve() == canonical.resolve(), item['name']
                assert actual['description'] == item['description'], item['name']
                expected = re.sub(r'^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)', '', canonical.read_text(), count=1)
                assert actual['content'] == expected, item['name']
                result = subprocess.check_output([sys.executable, '-B', str(root / 'skills' / item['name'] / 'scripts/run.py'),
                                                  '--context'], cwd=workspace, env=env, text=True, timeout=15)
                assert json.loads(result)['source_commit'] == '0391e15cc52ddfa88a6c02b5997fd9955e214491'
            provenance = json.loads((root / 'SOLARIS-BUILD-SOURCE.json').read_text())
            assert len(provenance['canonical_files']) == 1200
            for path, digest in provenance['canonical_files'].items():
                mapped = provenance.get('source_path_map', {}).get(path, path)
                assert hashlib.sha256((root / mapped).read_bytes()).hexdigest() == digest, path
            result = subprocess.check_output([sys.executable, '-B', str(root / 'skills/chief-of-staff/scripts/run.py'),
                                              'intake', 'review this code', '--json'], cwd=workspace, env=env,
                                             text=True, timeout=20)
            assignment = json.loads(result)
            assert assignment['assignment']['primary_capability'] == 'solaris.code-reviewer'
            assert assignment['performed_work'] is False
            result = subprocess.check_output([sys.executable, '-B', str(root / 'employees/quality-security/security-auditor/provenance_check.py'),
                                              'check', str(root), '--exclude', 'examples/capabilities/G38/fixtures', '--json'],
                                             cwd=workspace, env=env, text=True, timeout=30)
            check = json.loads(result)
            assert check['result'] == 'consistent' and not check['findings']
            print(json.dumps({'host': 'Kilo ' + version, 'source': args.ref, 'transport': 'named npm-compatible Git',
                              'nativeWorkflows': len(rows), 'matchingProfileFilesAndModes': count, 'canonicalFiles': 1200,
                              'nativeCallerContexts': 62, 'coordinatorAssignment': 'solaris.code-reviewer',
                              'compositeProvenance': 'consistent', 'modelCalls': 0, 'pass': True}, indent=2))
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == '__main__':
    main()
