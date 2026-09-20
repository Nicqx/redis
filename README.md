# Redis – mentés és biztonságos előkészítés a költözéshez

A `redis-service:6379`, a master/replica nevei és a `redis-data-redis-master-0` PVC változatlanok. A meglévő Redis közös infrastruktúra lehet: az update átveszi és megőrzi az élő konfigurációt, image-et, replikaszámot, tárolást és hitelesítést. Emiatt az első egységesítő update **nem Redis-verziófrissítés és nem ACL-átállítás**. Így a külön kezelt alkalmazások Redis-klienseit sem kényszerítjük változtatásra.

Üres clusterben a `k8s/resources.json` települ: digesttel rögzített Redis 7.4.11 Alpine, ARM64/AMD64 támogatás, egy 2 GiB-os `local-path` master PVC és egy ideiglenes adattárolójú replica. Egygépes clusteren ez nem magas rendelkezésre állás. Az új alapkonfiguráció a jelenlegi kliensekkel kompatibilis, hitelesítés nélküli, belső ClusterIP elérésű. A Redis-portot ne publikáld. ACL/NetworkPolicy csak a teljes kliensleltár és az alkalmazások felkészítése után vezethető be.

A régi `redis.yaml` helyét az update és a strukturált manifest vette át. Meglévő telepítés/PVC esetén **sikeres RDB-mentés nélkül nincs update vagy rollback**. Nem Ready Redis esetén előbb diagnosztika kell; nem készítünk üres adatbázist a hibás helyére.

## Közös használat

A parancsokat a repo könyvtárából futtasd. Előfeltétel: Bash, Git, Python 3.9+ és a helyi clusterhez hozzáférő `kubectl` (az ingresshez OpenSSL is kell). Python-csomag telepítése nem szükséges.

```bash
git pull --ff-only
./update.sh --target pi5 --dry-run
./update.sh --target pi5
```

A NUC-on `--target nuc` kell. A parancs ellenőrzi a node nevét és architektúráját; többnode-os clusterhez szándékosan nincs automatikus telepítés. Más kube-context: `--context NEV`. Ha a kubeconfig csak sudo-val olvasható, a teljes script helyett csak a kubectl kapjon jogosultságot:

```bash
KUBECTL='sudo k3s kubectl' ./update.sh --target nuc
```

Az update tiszta munkakönyvtárban `git pull --ff-only` után dolgozik. A `--dry-run` a jelenlegi helyi kódot ellenőrzi a Kubernetes API-val, nem pullol és nem ír a clusterbe. A tudatosan helyi kódhoz `--no-pull` használható. Nincs `reset --hard`, force push, prune vagy tömeges erőforrás-törlés.

Diagnosztika és kapcsolat nélküli manifest-előnézet:

```bash
./scripts/diagnose.sh --target pi5
python3 scripts/manage.py render --target nuc
```

A módosítás előtt a korábbi manifestek `0600` jogosultságú mentése készül a `~/.local/state/nicqx-infra/<target>/<repo>/` könyvtárba. A parancs kiírja a pontos fájlnevet. Más mentési gyökér: `NICQX_STATE_DIR`. A négy repo ugyanazon felhasználó/gép/cél műveleteit helyi zárral sorosítja; több operátor között külön egyeztetés szükséges.

Manifest-visszaállítás a **kiírt mentési fájl** teljes elérési útjával:

```bash
python3 scripts/manage.py rollback --target pi5 --file /teljes/ut/manifest-mentes.json --dry-run
python3 scripts/manage.py rollback --target pi5 --file /teljes/ut/manifest-mentes.json
```

Ez csak ugyanabban a clusterben, a repo engedélyezett erőforrásaira működik. Nem állít vissza adatbázist, és nem törli az időközben létrehozott erőforrásokat. Sikertelen rolloutnál az update hibával áll le; a visszaállítás külön, látható művelet.

Az `availability-calendar`, `availability-calendar-service`, `connectivity-check`, `munkaido`, `munkaido-nyilvantarto`, `rsvp1984`, `rsvp1985` neveket a közös eszköz védi. Más, ismeretlen erőforrásokat sem alkalmaz a repo saját engedélylistáján kívül. Dockerhez, K3s szolgáltatáshoz vagy routerhez egyik update sem nyúl.

## Teljes mentés

```bash
python3 scripts/manage.py backup --target pi5 --file "$HOME/redis-pi5-mentes.rdb"
```

A Redis maga készít konzisztens RDB-t a replikációs mentési protokollal. A fájl mellett SHA-256 ellenőrzőösszeg keletkezik. A teljes RDB minden adatbázist tartalmazhat, ezért érzékeny adatként kezeld. Meglévő fájlt nem írunk felül. A jelenlegi eszköz a helyi, hitelesítés nélküli Redishez készült; ACL-es környezetben leáll, amíg a hozzáférést külön be nem állítjuk.

A teljes RDB **katasztrófa utáni mentés**, nem a már használt NUC Redisére másolandó fájl. AOF mellett önmagában egy bemásolt RDB nem biztosít helyes visszaállítást. Az élő NUC PVC-jét és AOF-könyvtárát ne cseréld le.

## Játékadatok szelektív költöztetése

A tényleges költözéskor, az új alkalmazások előkészítése után:

```bash
python3 scripts/manage.py pause-games --target pi5 --file "$HOME/pi5-pause.json"
python3 scripts/manage.py export-games --target pi5 --file "$HOME/games-export.json"
```

A pause menti a korábbi replikaszámokat, csak a hat ismert játék Deploymentjét állítja nullára, és megvárja a leállásukat. Egy részhalmaz például `--apps chess ttt`. Nem állítja le a Redis-t vagy a védett szolgáltatásokat. Az export megtagadja a műveletet, ha játékpod még fut vagy ismerttől eltérő Redis-címet, adatbázist/prefixet talál.

| Játék | Engedélyezett kulcsprefix | Deployment |
|---|---|---|
| Sumplete | `sumplete:session:` | `sum-local` |
| Sakk | `chess:session:` | `chess-game-deployment` |
| Ultimate amőba | `ttt:session:` | `ultimate-tic-tac-toe` |
| Sudoku | `sudoku:` | `sudoku-app` |
| Bakos | `bakos:session:` | `bakos-game` |
| Maffia | `maffia:session:` | `maffia-game` |

A rövid életű zárolókulcsokat nem visszük át. Régi `session:*` vagy ötjegyű, prefix nélküli kulcsoknál az export megáll, mert a tulajdonosuk nem dönthető el biztonságosan. Nincs csendes, hiányos migráció. Az eszköz Redis DB 0-ra, legfeljebb 5000 kiválasztott kulcsra / 8 MiB exportra készült; az export rövid Lua-művelet alatt blokkolhatja a Redis-t. Nagyobb adatállományhoz külön migráció kell.

Az export JSON-t **és a `.sha256` fájlját** vidd át titkosított kapcsolaton a NUC-ra. Ott saját pause-fájl kell:

```bash
python3 scripts/manage.py pause-games --target nuc --file "$HOME/nuc-pause.json"
python3 scripts/manage.py import-games --target nuc --file "$HOME/games-export.json"
python3 scripts/manage.py resume-games --target nuc --file "$HOME/nuc-pause.json"
```

Az import előbb teljes mentést készít a célról. Más alkalmazás kulcsait nem módosítja. Eltérő értékű, már létező célkulcsnál leáll, és semmilyen játékadatot nem ír be. Az összes dumpot előbb ideiglenes kulcsokkal ellenőrzi, majd egyetlen Lua-műveletben helyezi el az új kulcsokat. Az ismételt import azonos értékeket kihagy; ezek meglévő céloldali TTL-jét sem írja át. Az új kulcsok az eredeti abszolút lejáratot kapják, a már lejárt kulcsok kimaradnak. `FLUSHDB`, `FLUSHALL`, `RESTORE REPLACE` nincs.

Sikeres import után RDB `SAVE` is történik. A célon továbbra is a konfigurált AOF/RDB tartósság érvényes. Ütközésnél ne töröld a céladatokat találomra; előbb azonosítani kell, melyik adatsor maradjon.

A `resume-games` a mentett replikaszámokat állítja vissza. Nem kapcsol be olyan alkalmazást, amely eleve nullára volt állítva. A Pi mentett replikaszámai csak a Pi-n állíthatók vissza. Visszalépéskor az eredeti pause-fájlt használd; az ismételt pause már nullás állapotot mentene.

Részletes sorrend: [Pi5 → NUC útmutató](https://github.com/Nicqx/ingress/blob/main/MIGRATION.md).

## Ellenőrzés

`python3 -m unittest discover -s tests -v` futtatja a teszteket. A valódi Redis-tesztekhez `redis-server` és `redis-cli` kell; más elérési úthoz `REDIS_SERVER` / `REDIS_CLI` adható meg. `REQUIRE_REDIS_TESTS=1` esetén hiányzó binárissal a teszt hibázik. A CI ezt kötelezővé teszi. A teszt Redis külön, ideiglenes könyvtárban, csak loopback címen indul.

## Leállítás és eltávolítás

Adatművelet előtt mindig készíts és ellenőrizz RDB-mentést a `scripts/manage.py backup` paranccsal.

```bash
# biztonságos ideiglenes leállítás; a PVC megmarad
sudo k3s kubectl scale deployment/redis-replica -n default --replicas=0
sudo k3s kubectl scale statefulset/redis-master -n default --replicas=0

# visszaindítás
sudo k3s kubectl scale statefulset/redis-master -n default --replicas=1
sudo k3s kubectl scale deployment/redis-replica -n default --replicas=1

# workloadok és service-ek eltávolítása, a PVC megtartásával
sudo k3s kubectl delete deployment/redis-replica statefulset/redis-master -n default
sudo k3s kubectl delete service/redis-master service/redis-service service/redis-replica-service -n default
sudo k3s kubectl delete configmap/redis-config -n default
```

Teljes, visszaállíthatatlan adattörléshez külön kell törölni a `redis-data-redis-master-0` PVC-t. Ezt csak ellenőrzött, másik gépre másolt mentés után tedd; az update és a fenti alap uninstall szándékosan nem törli.
