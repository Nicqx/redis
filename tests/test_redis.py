import copy
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import manage
from common import Failure


class AdoptionTests(unittest.TestCase):
    def test_live_image_storage_auth_and_replicas_are_preserved(self):
        original = manage.render()
        old = next(x for x in original if x['kind'] == 'StatefulSet')
        old['spec']['template']['spec']['containers'][0]['image'] = 'existing:tag'
        old['spec']['template']['spec']['containers'][0]['env'] = [{'name': 'SHARED_CLIENT_SETTING', 'value': 'keep'}]
        old['spec']['volumeClaimTemplates'][0]['spec']['resources']['requests']['storage'] = '9Gi'
        old['spec']['replicas'] = 0
        old['metadata']['uid'] = 'live-uid'
        kube = Mock(); kube.get.side_effect = lambda kind, name: copy.deepcopy(old) if kind == 'StatefulSet' else None
        after = next(x for x in manage.render(kube) if x['kind'] == 'StatefulSet')
        self.assertEqual(old['spec'], after['spec'])
        self.assertNotIn('uid', after['metadata'])

    def test_unavailable_redis_blocks_update_before_apply(self):
        kube = Mock(); kube.get.return_value = {'spec': {}}
        kube.target = 'pi5'
        with patch.object(manage, 'Kube', return_value=kube), patch.object(manage, 'render', return_value=[]), \
             patch.object(manage, 'operation_lock'), patch.object(manage, 'snapshot') as snap, \
             patch.object(sys, 'argv', ['manage.py', 'update', '--target', 'pi5']):
            with self.assertRaises(Failure): manage.main()
        kube.apply.assert_not_called(); snap.assert_not_called()

    def test_protected_app_cannot_be_in_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'export.json'
            raw = json.dumps({'format': 'nicqx-game-keys-v1', 'source_cluster_uid': 'pi', 'apps': ['chess'],
                             'keys': [{'key': 'availability:calendar', 'dump_hex': '00'*20, 'expires_at_ms': -1}]}).encode()
            p.write_bytes(raw); Path(str(p)+'.sha256').write_text(manage.checksum_bytes(raw))
            with self.assertRaises(Failure): manage.load_export(p)

    def test_pause_and_resume_only_known_game_deployments(self):
        kube = Mock(); kube.identity = 'nuc'; kube.get.return_value = {'spec': {'replicas': 0}, 'status': {'replicas': 0}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(manage, 'state_dir', return_value=Path(tmp)):
            manage.pause_games(kube, ['chess', 'ttt'])
            names = [c.args[1] for c in kube.call.call_args_list]
            self.assertEqual(names, ['deployment/chess-game-deployment', 'deployment/ultimate-tic-tac-toe'])
            saved = next(Path(tmp).glob('pause-*.json'))
            kube.identity = 'different-cluster'; kube.call.reset_mock()
            with self.assertRaises(Failure): manage.resume_games(kube, saved)
            kube.call.assert_not_called()


SERVER = os.environ.get('REDIS_SERVER') or shutil.which('redis-server')
CLI = os.environ.get('REDIS_CLI') or shutil.which('redis-cli')
if os.environ.get('REQUIRE_REDIS_TESTS') and not (SERVER and CLI):
    raise RuntimeError('Redis integration tests are required, but binaries are unavailable')


@unittest.skipUnless(SERVER and CLI, 'Set REDIS_SERVER/REDIS_CLI or install Redis for integration tests')
class RedisIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = str(probe.getsockname()[1])
        self.process = subprocess.Popen([SERVER, '--bind', '127.0.0.1', '--port', self.port,
                                        '--save', '', '--appendonly', 'no', '--dir', self.tmp.name],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                if self.cli('PING').strip() == b'PONG': break
            except subprocess.CalledProcessError:
                pass
            time.sleep(.02)
        else:
            self.process.terminate(); self.process.wait(timeout=5); self.tmp.cleanup()
            self.fail('Test Redis did not start')

    def tearDown(self):
        self.process.terminate(); self.process.wait(timeout=5); self.tmp.cleanup()

    def cli(self, *args, data=None):
        return subprocess.run([CLI, '-h', '127.0.0.1', '-p', self.port, '--raw', *args], input=data,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout

    def lua(self, script):
        return self.cli('--eval', '/dev/stdin', data=script.encode())

    def exported(self):
        return json.loads(self.lua(manage.EXPORT_LUA.replace('PREFIXES', manage.lua_string(json.dumps(manage.prefixes(['chess']))))))

    def test_binary_round_trip_ttl_and_unrelated_keys(self):
        value = b'one\x00two\nthree\xff'
        self.cli('-x', 'SET', 'chess:session:12345', data=value)
        self.cli('PEXPIRE', 'chess:session:12345', '60000')
        self.cli('SET', 'chess:session:12345:ai_lock', 'transient')
        self.cli('SET', 'calendar:do-not-touch', 'existing')
        data = self.exported()
        self.assertEqual(len(data['keys']), 1)
        deadline = data['keys'][0]['expires_at_ms']
        self.cli('DEL', 'chess:session:12345')
        result = json.loads(self.lua(manage.import_script(data)))
        self.assertEqual(result, {'inserted': 1, 'skipped': 0})
        self.assertEqual(self.cli('GET', 'chess:session:12345'), value + b'\n')
        self.assertEqual(int(self.cli('PEXPIRETIME', 'chess:session:12345')), deadline)
        self.assertEqual(self.cli('GET', 'calendar:do-not-touch').strip(), b'existing')
        self.assertEqual(self.cli('GET', 'chess:session:12345:ai_lock').strip(), b'transient')

    def test_conflict_never_partially_imports(self):
        self.cli('SET', 'chess:session:one', 'original')
        self.cli('SET', 'chess:session:two', 'original')
        data = self.exported()
        data['keys'].sort(key=lambda e: e['key'])
        self.cli('DEL', 'chess:session:one')
        self.cli('SET', 'chess:session:two', 'target-data')
        self.assertIn(b'DESTINATION_CONFLICT', self.lua(manage.import_script(data)))
        self.assertEqual(self.cli('EXISTS', 'chess:session:one').strip(), b'0')
        self.assertEqual(self.cli('GET', 'chess:session:two').strip(), b'target-data')
        self.assertEqual(self.cli('DBSIZE').strip(), b'1')

    def test_invalid_dump_cleans_staging_without_partial_import(self):
        self.cli('SET', 'chess:session:one', 'first')
        self.cli('SET', 'chess:session:two', 'second')
        data = self.exported()
        data['keys'][1]['dump_hex'] = 'ff' * 20
        self.cli('DEL', 'chess:session:one', 'chess:session:two')
        self.assertIn(b'INVALID_DUMP', self.lua(manage.import_script(data)))
        self.assertEqual(self.cli('DBSIZE').strip(), b'0')

    def test_retry_is_idempotent_and_expired_data_is_skipped(self):
        self.cli('SET', 'chess:session:one', 'first')
        data = self.exported()
        self.assertEqual(json.loads(self.lua(manage.import_script(data)))['skipped'], 1)
        self.cli('DEL', 'chess:session:one')
        data['keys'][0]['expires_at_ms'] = 1
        self.assertEqual(json.loads(self.lua(manage.import_script(data)))['inserted'], 0)
        self.assertEqual(self.cli('DBSIZE').strip(), b'0')

    def test_ambiguous_legacy_keys_are_reported(self):
        self.cli('SET', 'session:12345', 'legacy')
        self.cli('SET', '67890', 'legacy')
        self.assertEqual(self.exported()['legacy_count'], 2)
