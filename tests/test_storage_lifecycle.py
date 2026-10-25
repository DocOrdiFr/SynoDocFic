"""Exercise real lifecycle scripts with isolated storage and DSM FHS paths."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash')
if os.name == 'nt':
    BASH = r'C:\Program Files\Git\bin\bash.exe'

def posix(path):
    value = Path(path).resolve().as_posix()
    return '/' + value[0].lower() + value[2:] if os.name == 'nt' else value

with tempfile.TemporaryDirectory(prefix='synodocfic-test-') as temporary:
    sandbox = Path(temporary)
    scripts = sandbox / 'scripts'
    shutil.copytree(ROOT / 'scripts', scripts)
    target = sandbox / 'target'
    (target / 'var').mkdir(parents=True)
    profiles = '[{"id":"saved","source_pass":"test-secret"}]'
    (target / 'var/sync_profiles.json').write_text(profiles)
    (target / 'var/history.json').write_text('[{"id":"rename"}]')
    data = sandbox / 'pkgvar/settings'
    environment = dict(os.environ, TEST_ROOT=posix(sandbox),
        SYNOPKG_PKGDEST=posix(target), SYNOPKG_PKGVAR=posix(data.parent))

    def run(action, status, delete='false', pkgvar=None, success=True):
        env = dict(environment, SYNOPKG_PKG_STATUS=status,
            synodocfic_delete_settings=delete, SYNOPKG_PKGVAR=pkgvar if pkgvar is not None else posix(data.parent))
        command = f'sh "{posix(scripts / action)}"'
        result = subprocess.run([BASH, '-c', command], env=env, capture_output=True, text=True)
        assert (result.returncode == 0) == success, (action, result.stdout, result.stderr)

    run('preupgrade', 'UPGRADE')
    assert (data / 'sync_profiles.json').read_text() == profiles
    assert (data / 'history.json').is_file()
    shutil.rmtree(target)
    target.mkdir()
    run('postinst', 'UPGRADE')
    assert (target / 'var/data-dir').read_text().strip() == posix(data)
    run('postuninst', 'UPGRADE', 'true')
    assert (data / 'sync_profiles.json').read_text() == profiles
    run('postuninst', 'UNINSTALL')
    assert data.is_dir()
    (target / 'var/sync_profiles.json').write_text('[]')
    run('postinst', 'INSTALL')
    assert (data / 'sync_profiles.json').read_text() == profiles
    (data / 'logs/operations.log').write_text('log')
    unrelated = sandbox / 'synchronized-file.txt'
    unrelated.write_text('keep me')
    run('postuninst', 'UNINSTALL', 'true')
    assert not data.exists()
    assert unrelated.read_text() == 'keep me'
    run('postinst', 'INSTALL', pkgvar='relative/path', success=False)
    print('PASS: migration, reinstall, upgrade deletion guard, explicit erase, FHS path guard')

with tarfile.open(ROOT / 'SynoDocFic-1.3.4-0001.spk') as archive:
    assert b'version="1.3.4-0001"' in archive.extractfile('INFO').read()
    privilege = json.load(archive.extractfile('conf/privilege'))
    assert privilege['defaults']['run-as'] == 'package'
    assert not privilege.get('ctrl-script'), 'No privileged lifecycle scripts allowed'
    wizard = json.load(archive.extractfile('WIZARD_UIFILES/uninstall_uifile'))
    assert wizard[0]['items'][0]['subitems'][0]['defaultValue'] is False
    for member in archive.getmembers():
        if member.name.startswith('scripts/'):
            assert member.mode == 0o755
            assert b'\r\n' not in archive.extractfile(member).read()
    with tarfile.open(fileobj=io.BytesIO(archive.extractfile('package.tgz').read())) as payload:
        assert not any('__pycache__' in name for name in payload.getnames())
        assert b'--data-dir "${DATA_DIR}"' in archive.extractfile('scripts/start-stop-status').read()
print('PASS: SPK version, wizard default, permissions, LF scripts, clean payload')
