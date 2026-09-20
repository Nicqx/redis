"""Shared, dependency-free operations helpers (nicqx-ops v1)."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import uuid
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = 'default'
PROTECTED = {'availability-calendar', 'availability-calendar-service',
             'connectivity-check', 'munkaido', 'munkaido-nyilvantarto',
             'rsvp1984', 'rsvp1985'}


class Failure(RuntimeError):
    pass


def run(args, data=None, *, sensitive=False, timeout=180):
    result = subprocess.run([str(a) for a in args], input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    if result.returncode:
        detail = 'A muvelet hibazott; erzekeny adatot nem naplozunk.' if sensitive else result.stderr.decode(errors='replace')[-1600:]
        raise Failure(f'Parancs sikertelen ({result.returncode}): {detail}')
    return result.stdout


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def clean_resource(obj):
    result = copy.deepcopy(obj)
    result.pop('status', None)
    meta = result.setdefault('metadata', {})
    for field in ['uid', 'resourceVersion', 'generation', 'creationTimestamp',
                  'managedFields', 'selfLink']:
        meta.pop(field, None)
    meta.get('annotations', {}).pop('kubectl.kubernetes.io/last-applied-configuration', None)
    return result


def private_write(path, content):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def state_dir(target):
    base = Path(os.environ.get('NICQX_STATE_DIR', '~/.local/state/nicqx-infra')).expanduser()
    dest = base / target / ROOT.name
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    return dest


@contextmanager
def operation_lock(target):
    # Shared by the four repos for one local operator and target.
    path = state_dir(target).parent / 'operation.lock'
    with path.open('a') as handle:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Failure('Ezen a gepen mar fut egy infrastruktura-muvelet.') from exc
        yield


class Kube:
    def __init__(self, target, context=None):
        self.target = target
        self.prefix = shlex.split(os.environ.get('KUBECTL', 'kubectl'))
        if context:
            self.prefix += ['--context', context]
        self.prefix += ['--request-timeout=30s']
        self.identity = None

    def call(self, *args, data=None, sensitive=False, timeout=180):
        return run(self.prefix + list(args), data, sensitive=sensitive, timeout=timeout)

    def get(self, kind, name=None, namespace=NAMESPACE):
        args = ['get', kind]
        if name:
            args += [name, '--ignore-not-found=true']
        if namespace:
            args += ['-n', namespace]
        args += ['-o', 'json']
        raw = self.call(*args)
        return json.loads(raw) if raw.strip() else None

    def verify(self, require_ready=True):
        nodes = self.get('nodes', namespace=None)['items']
        if len(nodes) != 1 or nodes[0]['metadata']['name'] != self.target:
            raise Failure(f'A kube-context nem az egy node-os {self.target} clusterre mutat.')
        node = nodes[0]
        ready = any(x.get('type') == 'Ready' and x.get('status') == 'True'
                    for x in node.get('status', {}).get('conditions', []))
        if require_ready and not ready:
            raise Failure('A cel node nem Ready; elobb diagnose szukseges.')
        expected_arch = {'pi5': 'arm64', 'nuc': 'amd64'}[self.target]
        if node.get('status', {}).get('nodeInfo', {}).get('architecture') != expected_arch:
            raise Failure('A celgep architekturaja elter a profiltol.')
        self.identity = self.get('namespace', 'kube-system', namespace=None)['metadata']['uid']
        print(f'Cel ellenorizve: {self.target} ({expected_arch}); namespace: default')

    def apply(self, items, allowed, *, dry_run=False, sensitive=False):
        for item in items:
            meta = item.get('metadata', {})
            ident = (item['kind'], meta.get('name'))
            if ident not in allowed or meta.get('name') in PROTECTED:
                raise Failure(f'Nem engedelyezett eroforras: {ident}')
            if meta.get('namespace', NAMESPACE) != NAMESPACE:
                raise Failure('Varatlan namespace az alkalmazasmanifestben.')
        payload = canonical({'apiVersion': 'v1', 'kind': 'List', 'items': items})
        # Validate the entire batch before the first write. No prune/delete/force.
        self.call('apply', '--dry-run=server', '-f', '-', data=payload, sensitive=sensitive)
        if dry_run:
            print('Szerveroldali dry-run sikeres; cluster-modositas nem tortent.')
            return
        output = self.call('apply', '-f', '-', data=payload, sensitive=sensitive)
        print(output.decode().strip())


def snapshot(kube, items):
    previous = []
    for item in items:
        kind = item['kind']
        if kind == 'Middleware': kind = 'middlewares.' + item['apiVersion'].split('/')[0]
        old = kube.get(kind, item['metadata']['name'], namespace=None if item['kind'] == 'ClusterIssuer' else NAMESPACE)
        if old:
            previous.append(clean_resource(old))
    dest = state_dir(kube.target) / (time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.json')
    private_write(dest, canonical({'cluster_uid': kube.identity, 'previous': previous,
                                   'planned': items}))
    print(f'Korabbi manifestek mentese: {dest}')
    return dest


def checksum(data):
    return hashlib.sha256(canonical(data)).hexdigest()


def restore_snapshot(kube, file, allowed, dry_run=False):
    data = json.loads(Path(file).read_text())
    if data.get('cluster_uid') != kube.identity:
        raise Failure('A manifestmentes masik clusterhez tartozik.')
    previous = data.get('previous')
    if not isinstance(previous, list) or not previous:
        raise Failure('Nincs korabbi manifest a mentesben; uj eroforrast nem torlunk automatikusan.')
    # The normal allowlist is enforced again, including protected-resource checks.
    kube.apply(previous, allowed, dry_run=dry_run, sensitive=True)
    if not dry_run: rollout(kube, previous)
    print('Visszaallitas dry-run kesz.' if dry_run else 'Korabbi manifestek visszaallitva. Uj eroforrast, PVC-t es adatot nem toroltunk.')


def label(items, component):
    for item in items:
        item.setdefault('metadata', {}).setdefault('labels', {})['app.kubernetes.io/managed-by'] = 'nicqx-infra'
        item['metadata']['labels']['nicqx.dev/component'] = component
        if item['kind'] not in {'ClusterIssuer'}:
            item['metadata']['namespace'] = NAMESPACE
    return items


def rollout(kube, items):
    for item in items:
        if item['kind'] in {'Deployment', 'StatefulSet'} and item['spec'].get('replicas', 1):
            kube.call('rollout', 'status', item['kind'].lower() + '/' + item['metadata']['name'],
                      '-n', NAMESPACE, '--timeout=180s', timeout=210)
            print(f"Ready: {item['kind']}/{item['metadata']['name']}")


def diagnose(kube):
    for args in [('get', 'pods', '-A', '-o', 'wide'),
                 ('get', 'events', '-A', '--sort-by=.lastTimestamp'),
                 ('get', 'daemonsets,services', '-n', 'kube-system', '-o', 'wide'),
                 ('get', 'pvc', '-n', NAMESPACE, '-o', 'wide')]:
        print('\n$ kubectl ' + ' '.join(args))
        print(kube.call(*args).decode())
    print('A Docker/Compose alkalmazasok nem jelennek meg ebben a listaban.')
    print('Helyi, olvasasi ellenorzes: docker ps -a --format "table {{.Names}}\\t{{.Status}}\\t{{.Ports}}"')


def parser(description, commands):
    result = argparse.ArgumentParser(description=description)
    result.add_argument('command', choices=commands)
    result.add_argument('--target', required=True, choices=['pi5', 'nuc'])
    result.add_argument('--context')
    result.add_argument('--dry-run', action='store_true')
    return result


def entrypoint(main):
    try:
        main()
    except (Failure, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f'HIBA: {exc}', file=__import__('sys').stderr)
        raise SystemExit(1)
