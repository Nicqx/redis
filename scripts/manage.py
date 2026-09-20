#!/usr/bin/env python3
"""Redis adoption, RDB backup and collision-safe game-key migration."""
from __future__ import annotations
import copy
import json
from common import restore_snapshot
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urlsplit
from common import (ROOT, NAMESPACE, Failure, Kube, canonical, clean_resource,
                    diagnose, entrypoint, label, operation_lock, parser, private_write,
                    rollout, snapshot, state_dir)

GAMES = {
    'sumplete': ('sum-local', ['sumplete:session:']),
    'chess': ('chess-game-deployment', ['chess:session:']),
    'ttt': ('ultimate-tic-tac-toe', ['ttt:session:']),
    'sudoku': ('sudoku-app', ['sudoku:']),
    'bakos': ('bakos-game', ['bakos:session:']),
    'maffia': ('maffia-game', ['maffia:session:']),
}
MAX_BYTES = 8 * 1024 * 1024
MAX_KEYS = 5000
POD = 'redis-master-0'
ALLOWED = {('ConfigMap', 'redis-config'), ('StatefulSet', 'redis-master'),
           ('Deployment', 'redis-replica'), ('Service', 'redis-master'),
           ('Service', 'redis-service'), ('Service', 'redis-replica-service')}


def lua_string(value):
    marker = '='
    while ']' + marker + ']' in value:
        marker += '='
    return '[' + marker + '[' + value + ']' + marker + ']'


def prefixes(apps):
    return [p for name in apps for p in GAMES[name][1]]


def selected_key(key, apps):
    return (any(key.startswith(p) for p in prefixes(apps))
            and not key.endswith((':lock', ':move_lock', ':ai_lock')))


def render(kube=None):
    items = json.loads((ROOT / 'k8s/resources.json').read_text())
    if kube:
        # Adoption intentionally preserves live storage, clients, image, selectors,
        # authentication, replicas and all existing configuration. No shared-Redis restart.
        for index, item in enumerate(items):
            old = kube.get(item['kind'], item['metadata']['name'])
            if old:
                if item['kind'] == 'StatefulSet':
                    claims = old['spec'].get('volumeClaimTemplates', [])
                    if not any(c['metadata']['name'] == 'redis-data' for c in claims):
                        raise Failure('Varatlan Redis PVC-szerkezet; nincs automatikus atalakitas.')
                items[index] = clean_resource(old)
        service = next(i for i in items if i['metadata']['name'] == 'redis-service')
        if service['spec'].get('type', 'ClusterIP') != 'ClusterIP':
            raise Failure('A Redis service nem belso ClusterIP; kezi felmeres szukseges.')
    return label(items, 'redis')


def require_redis(kube):
    pod = kube.get('pod', POD)
    ready = pod and any(c.get('type') == 'Ready' and c.get('status') == 'True'
                        for c in pod.get('status', {}).get('conditions', []))
    if not ready:
        raise Failure('Redis nem Ready. Futtasd a diagnose parancsot; adatot/PVC-t nem torlunk.')
    pong = kube.call('exec', '-n', NAMESPACE, POD, '-c', 'redis', '--',
                     'redis-cli', '--raw', '-n', '0', 'PING')
    if pong.strip() != b'PONG':
        raise Failure('A helyi Redis PING sikertelen (az ACL-es telepites kulon beallitast igenyel).')


def eval_lua(kube, script):
    raw = kube.call('exec', '-i', '-n', NAMESPACE, POD, '-c', 'redis', '--',
                    'redis-cli', '--raw', '-n', '0', '--eval', '/dev/stdin',
                    data=script.encode(), sensitive=True)
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise Failure('A Redis muvelet nem sikerult; erzekeny adatot nem naplozunk.') from exc
    if not isinstance(value, dict):
        raise Failure('Varatlan Redis valasz.')
    return value


def backup_rdb(kube, output=None):
    require_redis(kube)
    name = 'nicqx-' + uuid.uuid4().hex + '.rdb'
    remote = '/tmp/' + name
    dest = Path(output) if output else state_dir(kube.target) / name
    try:
        kube.call('exec', '-n', NAMESPACE, POD, '-c', 'redis', '--',
                  'redis-cli', '--rdb', remote, sensitive=True)
        data = kube.call('exec', '-n', NAMESPACE, POD, '-c', 'redis', '--',
                         'cat', remote, sensitive=True)
        if len(data) < 20 or not data.startswith(b'REDIS'):
            raise Failure('Ervenytelen RDB-mentes; nem folytatjuk a frissitest.')
        path = private_write(dest, data)
        private_write(str(path) + '.sha256', (checksum_bytes(data) + '\n').encode())
        print(f'Teljes Redis RDB-mentes (erzekeny adat): {path}')
        return path
    finally:
        kube.call('exec', '-n', NAMESPACE, POD, '-c', 'redis', '--', 'rm', '-f', remote,
                  sensitive=True)


def checksum_bytes(data):
    return __import__('hashlib').sha256(data).hexdigest()


def require_paused(kube, apps):
    for name in apps:
        deployment = kube.get('deployment', GAMES[name][0])
        if deployment and (deployment['spec'].get('replicas', 1) != 0
                           or deployment.get('status', {}).get('replicas', 0) != 0):
            raise Failure(f'{name}: az export/import elott pause-games szukseges.')


def require_known_storage(kube, apps):
    """Refuse custom Redis databases/prefixes instead of silently exporting too little."""
    hosts = {'redis-service', 'redis-service.default', 'redis-service.default.svc',
             'redis-service.default.svc.cluster.local'}
    for app in apps:
        obj = kube.get('deployment', GAMES[app][0])
        if not obj: continue
        containers = obj['spec']['template']['spec']['containers']
        if len(containers) != 1 or containers[0].get('envFrom'):
            raise Failure(f'{app}: egyedi kornyezeti konfiguracio; elobb az adatforrast kell azonositanunk.')
        values = {}
        relevant = {'REDIS_URL', 'REDIS_HOST', 'REDIS_PORT', 'REDIS_DB', 'REDIS_KEY_PREFIX',
                    'SESSION_BACKEND', 'SESSION_KEY_PREFIX', 'LEGACY_SESSION_KEY_PREFIX'}
        for item in containers[0].get('env', []):
            if item['name'] not in relevant: continue
            if 'value' not in item:
                raise Failure(f'{app}: kozvetett Redis beallitas; nincs automatikus adatimport/export.')
            values[item['name']] = item['value']
        url = urlsplit(values.get('REDIS_URL', 'redis://redis-service:6379'))
        if (url.scheme != 'redis' or url.hostname not in hosts or url.port not in (None, 6379)
                or url.path not in ('', '/', '/0') or url.username or url.password or url.query or url.fragment
                or values.get('REDIS_HOST', 'redis-service') not in hosts
                or values.get('REDIS_PORT', '6379') != '6379' or values.get('REDIS_DB', '0') != '0'):
            raise Failure(f'{app}: eltero Redis adatforras; nincs hianyos migracio.')
        expected = GAMES[app][1][0].rstrip(':')
        if app != 'sudoku' and values.get('REDIS_KEY_PREFIX', expected) != expected:
            raise Failure(f'{app}: eltero Redis kulcsprefix; elobb felmeres szukseges.')
        if app == 'sudoku' and (values.get('SESSION_BACKEND') != 'redis'
                or values.get('SESSION_KEY_PREFIX', 'sudoku:session') != 'sudoku:session'
                or values.get('LEGACY_SESSION_KEY_PREFIX', 'sudoku:') != 'sudoku:'):
            raise Failure('Sudoku: nem a vart Redis tarolot/prefixet hasznalja; nincs hianyos migracio.')


def pause_games(kube, apps, output=None):
    saved = []
    for app in apps:
        obj = kube.get('deployment', GAMES[app][0])
        if obj:
            saved.append({'app': app, 'replicas': obj['spec'].get('replicas', 1)})
    path = Path(output) if output else state_dir(kube.target) / ('pause-' + uuid.uuid4().hex + '.json')
    private_write(path, canonical({'format': 'nicqx-paused-games-v1', 'cluster_uid': kube.identity, 'games': saved}))
    print(f'Visszainditasi allapot: {path}')
    for item in saved:
        kube.call('scale', 'deployment/' + GAMES[item['app']][0], '-n', NAMESPACE, '--replicas=0')
    deadline = time.monotonic() + 180
    while True:
        try:
            require_paused(kube, apps)
            break
        except Failure:
            if time.monotonic() >= deadline:
                raise Failure(f'A podok meg nem alltak le. Allapot megorizve: {path}')
            time.sleep(1)
    print('Csak a kivalasztott jatekok alltak le; a vedett alkalmazasok valtozatlanok.')


def resume_games(kube, file):
    data = json.loads(Path(file).read_text())
    if data.get('format') != 'nicqx-paused-games-v1' or data.get('cluster_uid') != kube.identity:
        raise Failure('A visszainditasi fajl masik clusterhez tartozik.')
    for item in data['games']:
        if item['app'] not in GAMES or type(item['replicas']) is not int or not 0 <= item['replicas'] <= 20:
            raise Failure('Ervenytelen visszainditasi allapot.')
    for item in data['games']:
        kube.call('scale', 'deployment/' + GAMES[item['app']][0], '-n', NAMESPACE,
                  '--replicas=' + str(item['replicas']))
    print('A mentett replikaertekek visszaallitva; az alkalmazasok Ready allapotat ellenorizd.')


EXPORT_LUA = r'''
local prefixes = cjson.decode(PREFIXES)
local function selected(k)
  if string.match(k, ':lock$') or string.match(k, ':move_lock$') or string.match(k, ':ai_lock$') then return false end
  for _, p in ipairs(prefixes) do if string.sub(k, 1, #p) == p then return true end end
  return false
end
local tm = redis.call('TIME')
local now = tonumber(tm[1]) * 1000 + math.floor(tonumber(tm[2]) / 1000)
local cursor, seen, entries, legacy, scanned, bytes = '0', {}, {}, 0, 0, 0
repeat
  local result = redis.call('SCAN', cursor, 'COUNT', 1000)
  cursor = result[1]
  for _, k in ipairs(result[2]) do
    if not seen[k] then
      seen[k] = true
      scanned = scanned + 1
      if scanned > 200000 then return redis.error_reply('SCAN_LIMIT') end
      if string.match(k, '^session:') or string.match(k, '^%d%d%d%d%d$') then legacy = legacy + 1 end
      if selected(k) then
        local dump, ttl = redis.call('DUMP', k), redis.call('PTTL', k)
        if dump and ttl ~= -2 then
          local hex = (dump:gsub('.', function(c) return string.format('%02x', string.byte(c)) end))
          bytes = bytes + #hex
          if #entries >= 5000 or bytes > 8388608 then return redis.error_reply('EXPORT_LIMIT') end
          table.insert(entries, {key=k, dump_hex=hex, expires_at_ms=ttl >= 0 and now+ttl or -1})
        end
      end
    end
  end
until cursor == '0'
return cjson.encode({keys=entries, legacy_count=legacy, captured_at_ms=now})
'''


def export_games(kube, apps, file):
    require_paused(kube, apps)
    require_known_storage(kube, apps)
    require_redis(kube)
    data = eval_lua(kube, EXPORT_LUA.replace('PREFIXES', lua_string(json.dumps(prefixes(apps)))))
    if data['legacy_count']:
        raise Failure('Regi, nem egyertelmu session:* vagy szamkulcsok vannak. Ezeket elobb azonositsuk; nincs hianyos export.')
    data['keys'] = data['keys'] or []
    data.update(format='nicqx-game-keys-v1', apps=apps, source_cluster_uid=kube.identity)
    encoded = canonical(data)
    if len(encoded) > MAX_BYTES:
        raise Failure('Az export tul nagy ehhez az egyszeru migracios eszkozhoz.')
    path = private_write(file, encoded)
    private_write(str(path) + '.sha256', (checksum_bytes(encoded) + '\n').encode())
    print(f"Jatekexport: {path}; {len(data['keys'])} kulcs. A TTL nem indul ujra.")


def load_export(file):
    raw = Path(file).read_bytes()
    if len(raw) > MAX_BYTES:
        raise Failure('Tul nagy exportfajl.')
    expected = Path(str(file) + '.sha256').read_text().strip()
    if checksum_bytes(raw) != expected:
        raise Failure('Az export ellenorzoosszege elter.')
    data = json.loads(raw)
    if data.get('format') != 'nicqx-game-keys-v1' or not data.get('source_cluster_uid'):
        raise Failure('Ismeretlen exportformatum.')
    apps = data.get('apps', [])
    if not isinstance(apps, list) or not apps or any(not isinstance(a, str) or a not in GAMES for a in apps):
        raise Failure('Ismeretlen alkalmazas az exportban.')
    keys = data.get('keys')
    if not isinstance(keys, list) or len(keys) > MAX_KEYS:
        raise Failure('Ervenytelen kulcslista.')
    seen = set()
    for entry in keys:
        if not isinstance(entry, dict):
            raise Failure('Ervenytelen kulcsadat.')
        k = entry.get('key', '')
        if not isinstance(k, str) or not selected_key(k, apps) or k in seen:
            raise Failure('Nem engedelyezett vagy duplikalt kulcs az exportban.')
        seen.add(k)
        if not isinstance(entry.get('dump_hex'), str) or not re.fullmatch(r'[0-9a-f]{20,}', entry['dump_hex']) or len(entry['dump_hex']) % 2:
            raise Failure('Ervenytelen Redis dump.')
        expires = entry.get('expires_at_ms')
        if type(expires) is not int or (expires != -1 and not 0 <= expires < 2**53):
            raise Failure('Ervenytelen lejarat.')
    return data


IMPORT_LUA = r'''
local entries = cjson.decode(@@ENTRIES@@)
local tm = redis.call('TIME')
local now = tonumber(tm[1])*1000 + math.floor(tonumber(tm[2])/1000)
local prefix, staged, inserted, skipped = @@STAGING@@, {}, 0, 0
local function decode(s) return (s:gsub('..', function(cc) return string.char(tonumber(cc,16)) end)) end
local function clean() for _, item in ipairs(staged) do redis.call('DEL', item.tmp) end end
-- Check ALL collisions before any staging or destination write.
for _, e in ipairs(entries) do
  e.active = e.expires_at_ms == -1 or e.expires_at_ms > now
  if e.active then
    e.dump = decode(e.dump_hex)
    local old = redis.call('DUMP', e.key)
    if old and old ~= e.dump then return redis.error_reply('DESTINATION_CONFLICT') end
    e.skip = old and true or false
  end
end
-- Let Redis validate every serialized value. A bad dump never partially replaces game keys.
for index, e in ipairs(entries) do
  if e.active and not e.skip then
    local tmp = prefix .. index
    if redis.call('EXISTS', tmp) ~= 0 then clean(); return redis.error_reply('STAGING_CONFLICT') end
    local result = redis.pcall('RESTORE', tmp, 300000, e.dump)
    if type(result) == 'table' and result.err then clean(); return redis.error_reply('INVALID_DUMP') end
    table.insert(staged, {tmp=tmp, key=e.key, expires=e.expires_at_ms})
  else skipped = skipped + 1 end
end
for _, e in ipairs(staged) do
  redis.call('RENAME', e.tmp, e.key)
  if e.expires == -1 then redis.call('PERSIST', e.key) else redis.call('PEXPIREAT', e.key, e.expires) end
  inserted = inserted + 1
end
return cjson.encode({inserted=inserted, skipped=skipped})
'''


def import_script(data):
    return IMPORT_LUA.replace('@@ENTRIES@@', lua_string(json.dumps(data['keys']))).replace(
        '@@STAGING@@', lua_string('__nicqx_import_' + uuid.uuid4().hex + ':'))


def import_games(kube, file):
    data = load_export(file)
    if data['source_cluster_uid'] == kube.identity:
        raise Failure('Az export forrasa es celja azonos cluster.')
    require_paused(kube, data['apps'])
    require_known_storage(kube, data['apps'])
    require_redis(kube)
    backup_rdb(kube)
    result = eval_lua(kube, import_script(data))
    # Persist the imported dataset using the already configured Redis AOF/RDB policy.
    saved = kube.call('exec', '-n', NAMESPACE, POD, '-c', 'redis', '--', 'redis-cli', 'SAVE', sensitive=True)
    if saved.strip() != b'OK':
        raise Failure('A kulcsimport lefutott, de az RDB SAVE nem sikerult. Ne inditsd a jatekokat; ellenorizd a Redis perzisztenciat.')
    print(f"Import kesz: {result['inserted']} uj kulcs, {result['skipped']} lejart/azonos kulcs kihagyva.")
    print('Meglevo, eltero erteket nem irtunk felul; mas alkalmazas kulcsait nem modosítottuk.')


def main():
    p = parser('Redis es jatekadatok biztonsagos kezelese',
               ['update', 'rollback', 'render', 'diagnose', 'backup', 'pause-games', 'resume-games', 'export-games', 'import-games'])
    p.add_argument('--apps', nargs='+', choices=list(GAMES), default=list(GAMES))
    p.add_argument('--file')
    args = p.parse_args()
    if args.command == 'render':
        print(json.dumps(render(), indent=2)); return
    if args.command in {'resume-games', 'export-games', 'import-games'} and not args.file:
        p.error('--file szukseges')
    if args.dry_run and args.command not in {'update', 'rollback'}:
        p.error('--dry-run csak update mellett ervenyes')
    kube = Kube(args.target, args.context)
    kube.verify(require_ready=args.command != 'diagnose')
    if args.command == 'diagnose':
        diagnose(kube); return
    with operation_lock(args.target):
        if args.command == 'rollback':
            if not args.file: p.error('--file szukseges')
            if not args.dry_run: backup_rdb(kube)
            restore_snapshot(kube, args.file, ALLOWED, args.dry_run)
            return
        if args.command == 'update':
            items = render(kube)
            if not args.dry_run:
                existing = kube.get('statefulset', 'redis-master')
                pvc = kube.get('pvc', 'redis-data-redis-master-0')
                if existing or pvc:
                    backup_rdb(kube)
                snapshot(kube, items)
            kube.apply(items, ALLOWED, dry_run=args.dry_run)
            if not args.dry_run: rollout(kube, items)
        elif args.command == 'backup': backup_rdb(kube, args.file)
        elif args.command == 'pause-games': pause_games(kube, args.apps, args.file)
        elif args.command == 'resume-games': resume_games(kube, args.file)
        elif args.command == 'export-games': export_games(kube, args.apps, args.file)
        elif args.command == 'import-games': import_games(kube, args.file)


if __name__ == '__main__':
    entrypoint(main)
