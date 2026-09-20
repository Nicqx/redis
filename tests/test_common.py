import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from common import Failure, Kube, private_write, restore_snapshot


class OperationsBoundaryTests(unittest.TestCase):
    def test_wrong_machine_or_architecture_is_rejected(self):
        for node_name, arch in [('pi5', 'arm64'), ('nuc', 'arm64')]:
            kube = Kube('nuc')
            kube.get = Mock(return_value={'items': [{'metadata': {'name': node_name},
                'status': {'conditions': [{'type': 'Ready', 'status': 'True'}], 'nodeInfo': {'architecture': arch}}}]})
            kube.call = Mock()
            with self.assertRaises(Failure): kube.verify()
            kube.call.assert_not_called()

    def test_protected_service_cannot_be_applied_even_if_allowlisted(self):
        kube = Kube('nuc'); kube.call = Mock()
        obj = {'kind': 'Service', 'metadata': {'name': 'availability-calendar-service'}}
        with self.assertRaises(Failure):
            kube.apply([obj], {('Service', 'availability-calendar-service')})
        kube.call.assert_not_called()

    def test_wrong_namespace_is_rejected_before_write(self):
        kube = Kube('nuc'); kube.call = Mock()
        obj = {'kind': 'Service', 'metadata': {'name': 'test', 'namespace': 'kube-system'}}
        with self.assertRaises(Failure): kube.apply([obj], {('Service', 'test')})
        kube.call.assert_not_called()

    def test_server_dry_run_has_no_real_apply(self):
        kube = Kube('nuc'); kube.call = Mock(return_value=b'validated')
        obj = {'kind': 'Service', 'metadata': {'name': 'test'}}
        kube.apply([obj], {('Service', 'test')}, dry_run=True)
        self.assertEqual(kube.call.call_count, 1)
        self.assertIn('--dry-run=server', kube.call.call_args.args)

    def test_failed_validation_prevents_real_apply(self):
        kube = Kube('nuc'); kube.call = Mock(side_effect=Failure('server rejects'))
        with self.assertRaises(Failure): kube.apply([{'kind': 'Service', 'metadata': {'name': 'test'}}], {('Service', 'test')})
        self.assertEqual(kube.call.call_count, 1)

    def test_private_export_permissions_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'export.json'
            private_write(p, b'private-test-value')
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError): private_write(p, b'new')
            self.assertEqual(p.read_bytes(), b'private-test-value')

    def test_rollback_cannot_cross_cluster(self):
        kube = Kube('nuc'); kube.identity = 'nuc-uid'; kube.apply = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'snapshot.json'
            p.write_text(json.dumps({'cluster_uid': 'pi-uid', 'previous': []}))
            with self.assertRaises(Failure): restore_snapshot(kube, p, set())
        kube.apply.assert_not_called()
