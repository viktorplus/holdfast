# Состав архива: что берётся и как это настроить

<!-- nav -->
**Меню:** [Оглавление](README.md) · [Установка](01-install.md) · [Бэкап](02-backup.md) · **Состав архива** · [Восстановление](04-restore.md) · [Справочник команд](05-commands.md) · [Ошибки](06-errors.md) · [Переход с 0.2](07-upgrade-from-0.2.md)
<!-- /nav -->

**В этом документе:**

- [Как holdfast решает, что брать](#как-holdfast-решает-что-брать)
- [Читать вывод --dry-run](#читать-вывод---dry-run)
- [Исключить лишнее](#исключить-лишнее)
- [Добавить своё](#добавить-своё)
- [Файл доступа к базе (defaults_file)](#файл-доступа-к-базе-defaults_file)
- [Режим manual](#режим-manual)
- [Совет: данные приложения — папкой в проекте](#совет-данные-приложения--папкой-в-проекте)
- [Дальше](#дальше)

Результат: вы знаете, что именно попадёт в копию, и можете это изменить —
исключить лишнее, добавить своё, перейти на ручной список.

Все правки — в `/etc/holdfast/holdfast.toml` на сервере. После **каждой** правки
запускайте:

```sh
holdfast backup --dry-run
```

Он ничего не пишет, зато показывает итоговый состав.

## Как holdfast решает, что брать

Режим записан в `[backup]`:

```toml
[backup]
mode = "auto"
```

| Режим | Кто решает | Когда обновляется |
|---|---|---|
| `auto` (по умолчанию) | holdfast смотрит в Docker и применяет правило | при каждом бэкапе заново |
| `manual` | список пишет `holdfast backup --discover` в `components.toml` | только когда вы запускаете `--discover` |

В обоих режимах к найденному добавляется то, что вы объявили сами блоками
`[[component]]`. Ваш блок всегда главнее правила.

### Правило режима `auto`

Для Docker-хоста holdfast берёт:

1. **Базы данных дампом** (MySQL/MariaDB, PostgreSQL — по образу контейнера),
   все базы, кроме служебных. Файлы базы дампом не заменяются: копия файлов
   работающей базы несогласованна.
2. **Папку compose-проекта целиком** (`docker-compose.yml`, `.env` и всё рядом),
   без папки данных базы.
3. **Тома**, которые подключает хотя бы один контейнер.
4. **Подключённые папки** (bind mount), которых нет в пунктах выше.

Не берётся служебное (`/proc`, `/sys`, `/var/lib/docker` и т. п.) и сами снимки.

## Читать вывод `--dry-run`

```
mode: auto
myapp-db-1 (mysql, from rule, ~210 MB)
  mysql/myapp-db-1-wordpress.sql.zst.age
    docker exec myapp-db-1 sh -c '…' | zstd -T0 -10 -q | age --encrypt -r age1…
project-myapp (path, from rule, ~210 MB)
  project-myapp.tar.zst.age
    ls -d -- /root/myapp >/dev/null && tar … --exclude=root/myapp/db_data -cf - -C / root/myapp | …
volume-myapp-wordpress-1-var-www-html (docker_volume, from rule, ~105 MB)
  docker-volumes/myapp-wordpress-1--var-www-html.tar.zst.age
    tar … -C /var/lib/docker/volumes/b2b2b2…/_data . | …

not taken:
  volume a1a1a1…: no container uses it
  bind mount /root/myapp/db_data: database data, covered by the dump

estimated size before compression: 525 MB
```

Каждый компонент — три строки: имя, тип и источник; файл в снимке (`.age` —
зашифрован); точная команда. По команде видны опечатки в путях и именах.

| Источник в скобках | Значит |
|---|---|
| `from rule` | нашло правило по Docker |
| `from holdfast.toml` | вы объявили блоком `[[component]]` |
| `from components.toml` | только `manual`: список от `--discover` |

Размер (`~210 MB`) — грубая оценка сверху, до сжатия. Если после бэкапа на диске
осталось бы меньше `backup.min_free_gb`, бэкап откажется запускаться.

Имя безымянного тома случайное и на другом сервере будет другим, поэтому его
компонент называется по контейнеру и пути монтирования
(`volume-myapp-wordpress-1-var-www-html`).

### `not taken:` — что не взято и почему

Читайте этот список так же внимательно, как список взятого.

| Причина | Что значит | Что делать |
|---|---|---|
| `no container uses it` | бесхозный том: его не подключает ни один контейнер, даже остановленный | holdfast его не удаляет; решите сами (ниже) |
| `database data, covered by the dump` | папка данных базы, база берётся дампом | ничего |
| `excluded by you` | вы исключили в `backup.exclude` | ничего |
| `declared by hand` | это уже покрывает ваш `[[component]]` | ничего |
| `system` | служебное (`/`, `/proc`, `/sys`, `/dev`, `/run`, `/var/lib/docker`, `*.sock` …) | ничего |
| `the snapshots themselves` | папка внутри `backup.root` | ничего |
| `inside a compose project directory` | лежит внутри папки проекта и попадёт в копию вместе с ней | ничего |
| `inside another bind mount already taken` | лежит внутри другой уже взятой папки | ничего |
| `not on this machine` | папки, подключённой в контейнер, нет на диске | проверить контейнер |
| `the project directory is not on this machine` | метки говорят о папке проекта, которой на диске нет | проверить; проект в копию не попадёт |

**Бесхозный том** (`no container uses it`) — обычный след `docker compose down`
и `up`: контейнеры удаляются, безымянные тома остаются, а `up` создаёт новые.
Посмотреть, что в старом томе (на сервере):

```sh
docker run --rm -v ИМЯ_ТОМА:/v:ro alpine:3.20 ls -la /v
```

Удалить — `docker volume rm ИМЯ_ТОМА`, только когда уверены, что он не нужен.

Ещё внизу бывает раздел `warnings:` — например, что рядом лежит
`components.toml`, не читаемый в режиме `auto`.

## Исключить лишнее

В `[backup]` добавьте `exclude` (второй раздел `[backup]` не создавайте):

```toml
[backup]
mode = "auto"
exclude = [
  "volume:myapp_cache",
  "path:/srv/old-site",
  "database:myapp-db-1/wordpress_test",
  "container:myapp-adminer-1",
]
```

| Вид | Что исключает |
|---|---|
| `volume:<имя тома>` | один том (именованный или безымянный: тогда имя из 64 символов) |
| `path:<абсолютный путь>` | папку проекта или подключённую папку целиком; если путь внутри взятой папки — вырезает его из её архива |
| `database:<контейнер>/<база>` | одну базу; остальные базы контейнера дампятся |
| `container:<имя>` | всё, что правило взяло бы из контейнера: дамп баз, его тома (если их не подключает ещё кто-то), его папки |

Уточнения:

- `path:/root/myapp/logs` — проект берётся, а `logs` вырезается из архива.
  В `--dry-run` это видно: в команде проекта появится `--exclude=root/myapp/logs`,
  в `not taken:` — `path /root/myapp/logs: excluded by you`.
  `path:/root/myapp` исключает проект целиком.
- `container:` не убирает папку compose-проекта: она общая для всех сервисов.
  Для неё — `path:/root/myapp`.
- Лишние слеши в пути (`/root/myapp/logs/`) игнорируются.
- Имя тома, который подключает контейнер:
  `docker inspect -f '{{ range .Mounts }}{{ .Name }} {{ .Destination }}{{ "\n" }}{{ end }}' myapp-wordpress-1`;
  все тома — `docker volume ls`.

Неверный элемент (неизвестный вид, пустое значение, путь не от корня) останавливает
бэкап и `--dry-run` с указанием элемента. Элемент, которому на машине ничего не
соответствует, в режиме `auto` даёт предупреждение
`backup.exclude: '...' matches nothing on this machine` — скорее всего, опечатка.
После правки исключённое должно появиться в `not taken:` с причиной
`excluded by you`.

## Добавить своё

Правило смотрит только в Docker. Всё остальное объявляется блоком `[[component]]`
в конце `holdfast.toml`. Блоков может быть сколько угодно; порядок в файле —
порядок в бэкапе.

Общее для всех: `name` — строчные латинские буквы, цифры, `-` и `_` (из него
получается имя файла в снимке).

### Папка или файл на сервере (`path`)

```toml
[[component]]
type = "path"
name = "letsencrypt"
path = "/etc/letsencrypt"
exclude = []
```

`exclude` — шаблоны `tar`, например `["*/cache/*"]`.

### База MySQL / MariaDB (`mysql`)

В контейнере, пароль из окружения контейнера:

```toml
[[component]]
type = "mysql"
name = "myapp-db-1"
container = "myapp-db-1"
user = "root"
credentials = "container_env"
password_env = "MYSQL_ROOT_PASSWORD"
```

Пароль holdfast не видит: в команде стоит имя переменной, а подставляет её
оболочка внутри контейнера.

Если пароль root меняли после первого запуска контейнера, в нём есть `\`
(образ `mysql`) или в окружении контейнера пароля нет — нужен файл доступа,
см. [«Файл доступа к базе»](#файл-доступа-к-базе-defaults_file) ниже.

База вне контейнера: оставьте `container` пустым — инструменты запустятся на
хосте (нужны клиенты БД на хосте).

Дополнительно: `databases` (по умолчанию `["*"]` — все) и `exclude_databases`
(никогда не дампить). Служебные `information_schema`, `performance_schema`,
`sys` и `mysql` не дампятся в любом случае.

### База PostgreSQL (`postgres`)

```toml
[[component]]
type = "postgres"
name = "pg-main"
container = "myapp-pg-1"
user = "postgres"
databases = ["*"]
globals = true
```

`globals = true` добавляет дамп ролей и табличных пространств: без них
восстановленная база может оказаться без владельца. В контейнере пароль не
нужен: `psql` заходит по локальному сокету. Для случая, когда вход без пароля
отключён, есть `defaults_file` (файл в формате `.pgpass`, нужен `user`).

### Том Docker (`docker_volume`)

Один том по имени:

```toml
[[component]]
type = "docker_volume"
name = "uploads"
volume = "myapp_uploads"
```

Безымянный том — по контейнеру и пути монтирования:

```toml
[[component]]
type = "docker_volume"
name = "site-files"
container = "myapp-wordpress-1"
destination = "/var/www/html"
```

`volume` и `container`/`destination` вместе указывать нельзя.

Не создавайте `docker_volume` без `volume` и `container` в режиме `auto`: он
берёт все тома по имени, в том числе бесхозные дубли (`max_mb` по умолчанию 512).
Правило берёт тома само.

### Собственная команда (`command`)

Для всего остального (MongoDB, Redis и т. п.):

```toml
[[component]]
type = "command"
name = "redis"
produce = "docker exec myapp-redis-1 redis-cli --rdb /dev/stdout"
artifact = "redis.rdb"
check = "cat >/dev/null"
```

- `produce` — команда, пишущая содержимое в stdout; выполняется как написана;
- `artifact` — имя файла в снимке (по умолчанию `<name>.bin`);
- `check` — команда, читающая содержимое со stdin и возвращающая не 0, если оно
  нечитаемо (по умолчанию `cat >/dev/null`, то есть проверки формата нет);
- `restore` — таблица с рецептом восстановления. **Без неё `holdfast restore`
  этот артефакт не вернёт**: он будет назван в `restore list`, в плане и в
  результате как невосстановленный. Формат таблицы — needs verification:
  смотрите `docs/backup.md` и сам `manifest.json`.

Команда выше — только иллюстрация формы; саму команду для вашего сервиса
проверьте вручную.

### Два правила, которые окупаются при восстановлении

1. **Базу берут дампом, а не файлами.** Объявляя что-то сами, не архивируйте
   папку данных работающей базы.
2. **Компонент, который не смог отработать, роняет весь бэкап.** Так сделано
   нарочно.

## Файл доступа к базе (`defaults_file`)

Нужен только в трёх случаях:

- пароль root меняли после первого запуска контейнера (в переменной остался
  старый) — ошибка `Access denied ... the password in the container's
  environment was not accepted`;
- в пароле root есть `\`, а образ — `mysql`: образ подставляет пароль в SQL, где
  `\` — экранирование, и сохранённый пароль не равен значению переменной;
- у контейнера нет переменной с паролем (`MYSQL_RANDOM_ROOT_PASSWORD` или
  собственный образ) — ошибка `its root password is not in its environment`.

**1. Создать файл** на сервере в папке проекта, закрытый для всех, кроме root:

```sh
cd /root/myapp
umask 077
touch holdfast-mysql.cnf
chmod 600 holdfast-mysql.cnf
nano holdfast-mysql.cnf
```

В редакторе (не через `echo`: пароль осел бы в истории оболочки):

```
[client]
user=root
password=ПАРОЛЬ_ROOT
```

`\` в пароле пишется двойным (`\\`); если есть `#` или пробелы по краям —
пароль в двойных кавычках. Проверка: `ls -l holdfast-mysql.cnf` → `-rw-------`.
Файл лежит в папке проекта, поэтому попадёт в архив проекта — зашифрованным.

**2. Подключить в контейнер базы.** Узнайте сервис и compose-файл:

```sh
docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' myapp-db-1
docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' myapp-db-1
```

В compose-файле, в сервис базы, добавьте в `volumes:`:

```yaml
  db:
    volumes:
      - ./db_data:/var/lib/mysql
      - ./holdfast-mysql.cnf:/etc/holdfast/mysql.cnf:ro
```

**3. Пересоздать только контейнер базы** (из папки проекта):

```sh
docker compose up -d db
```

Если файл называется иначе: `docker compose -f docker-compose.prod.yml up -d db`
(несколько файлов — каждый своим `-f`, в том же порядке). **Не используйте
`docker compose down`**: он удалит все контейнеры, а следующий `up` создаст новые
безымянные тома вместо старых.

Проверка (для MariaDB 11 вместо `mysql` — `mariadb`):

```sh
docker exec myapp-db-1 mysql --defaults-file=/etc/holdfast/mysql.cnf -N -e "show databases"
```

**4. Объявить базу в `holdfast.toml`:**

```toml
[[component]]
type = "mysql"
name = "myapp-db-1"
container = "myapp-db-1"
databases = ["*"]
defaults_file = "/etc/holdfast/mysql.cnf"
```

Путь указывается **внутри контейнера**. `credentials` и `defaults_file` вместе
указывать нельзя. После этого `--dry-run` покажет `myapp-db-1 (mysql, from holdfast.toml)`,
а в `not taken:` — `database container myapp-db-1: declared by hand`.

## Режим manual

В `manual` бэкап не смотрит в Docker. Он берёт `[[component]]` из `holdfast.toml`
и `/etc/holdfast/components.toml` — список, который пишет `--discover`. Состав
меняется только когда вы запускаете `--discover`. Нужен, когда состав должен
меняться лишь после того, как человек его прочитал, и на машинах без Docker.

Включить: `holdfast init --fresh --host-label web-1 --backup-mode manual`, либо
строка `mode = "manual"` в `[backup]`.

**На сервере:**

```sh
holdfast backup --discover
```

```
wrote /etc/holdfast/components.toml (3 components)

changes:
  + myapp-db-1 (mysql)
  + project-myapp (path)

not taken:
  volume a1a1a1…: no container uses it
```

- применяется то же правило, что и в `auto`, с учётом `backup.exclude` и ваших
  `[[component]]`;
- файл пишется целиком, права `0600`; прежний сохраняется как
  `components.toml.prev`;
- `changes:` — что добавилось (`+`), ушло (`-`), изменилось (`~`).

**`components.toml` вручную не правьте** — следующий `--discover` перезапишет его
целиком. Свои компоненты и исключения пишите в `holdfast.toml`. Файл без первой
строки, которую пишет `--discover`, бэкап игнорирует и предупреждает.

Когда запускать `--discover` снова: после появления или исчезновения контейнера,
compose-проекта, именованного тома, подключённой папки. После
`docker compose down`/`up` не нужно: безымянный том записан через контейнер и
путь, а не по имени. Новые базы в уже известном контейнере берутся сами.

В режиме `auto` `--discover` ничего не пишет и отвечает, что состав вычисляется
заново при каждом бэкапе.

## Совет: данные приложения — папкой в проекте

Файлы WordPress в безымянном томе (`- /var/www/html` без имени слева) holdfast
сохранит и восстановит, но после каждого `down`/`up` появляется новый том, а
старый остаётся бесхозным; на новом сервере такой том можно восстановить только
после `docker compose up -d`. Папка внутри проекта (`- ./wp_data:/var/www/html`)
этих проблем не имеет: она в архиве проекта и не меняет имя. Для нового проекта
подключайте данные так. Переделка работающего сайта — изменение приложения, а не
holdfast: делайте её отдельно.

## Дальше

- Бэкап и расписание: [02-backup.md](02-backup.md).
- Все ключи конфигурации: `docs/configuration.md`.
