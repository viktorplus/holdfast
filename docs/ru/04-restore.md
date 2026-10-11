# Восстановление из бэкапа

<!-- nav -->
**Меню:** [Оглавление](README.md) · [Установка](01-install.md) · [Бэкап](02-backup.md) · [Состав архива](03-contents.md) · **Восстановление** · [Справочник команд](05-commands.md) · [Ошибки](06-errors.md) · [Переход с 0.2](07-upgrade-from-0.2.md)
<!-- /nav -->

**В этом документе:**

- [Что нужно для любого восстановления](#что-нужно-для-любого-восстановления)
- [Три команды и чем они отличаются](#три-команды-и-чем-они-отличаются)
- [А. Данные повреждены (тот же сервер)](#а-данные-повреждены-тот-же-сервер)
- [Б. Новый сервер с нуля](#б-новый-сервер-с-нуля)
- [Проверка копии без риска](#проверка-копии-без-риска)
- [Если восстановление оборвалось](#если-восстановление-оборвалось)
- [Чего восстановление не умеет](#чего-восстановление-не-умеет)

## Что нужно для любого восстановления

1. **Снимок** — папка вида `/opt/backups/20260923-021500` (внутри
   `manifest.json`, `SHA256SUMS` и артефакты `*.age`).
2. **Приватный ключ** `age` (файл, который вы создавали в
   [02-backup.md](02-backup.md#1-ключ-шифрования)).
3. **holdfast той же версии**, что делала копию (снимки 0.3 восстанавливает
   0.3). Установка — [01-install.md](01-install.md).
4. **Имя машины (`host-label`)**. Оно записано в снимке; holdfast на целевой
   машине должен быть инициализирован с тем же именем. Узнать его:
   `holdfast restore list --snapshot ПАПКА` — первая строка
   `snapshot ДАТА from web-1`.

Ключ читается из того места, где лежит, на время одного запуска и никуда не
копируется. На рабочем сервере держите его только на время восстановления.

## Три команды и чем они отличаются

| Команда | Нужен ключ | Что пишет | Отвечает на вопрос |
|---|---|---|---|
| `holdfast restore list` | нет | ничего | что в этой копии |
| `holdfast restore verify` | да | только журнал | читается ли копия |
| `holdfast restore` | да | машину | возвращает данные |

```sh
holdfast restore list   --snapshot ПАПКА
holdfast restore verify --snapshot ПАПКА --identity КЛЮЧ
holdfast restore        --snapshot ПАПКА --identity КЛЮЧ
```

Перед записью `restore` по порядку проверяет: имя машины, все рецепты, все
контрольные суммы нужных артефактов, ключ. Потом печатает план (что и куда
ляжет, какие контейнеры будут остановлены) и просит ввести имя машины. Без
терминала и без `--yes` он останавливается, а не ждёт.

Порядок восстановления: сначала файлы и тома (`path`, `docker_volume`), потом
контейнеры поднимаются, потом базы (PostgreSQL: роли, затем базы; MySQL). Базу
можно загрузить только в работающий сервер.

## А. Данные повреждены (тот же сервер)

Сервер жив, holdfast установлен, нужно вернуть данные из снимка.

**1. Выбрать снимок и посмотреть состав.** На сервере:

```sh
ls /opt/backups
holdfast restore list --snapshot /opt/backups/20260923-021500
```

Вывод выглядит так:

```
snapshot 20260923-021500 from web-1
taken 2026-09-23T02:15:00.412345+00:00, encrypted with age

       1234567  myapp-db-1       mysql_database   mysql/myapp-db-1-wordpress.sql.zst.age
      25000000  project-myapp    path             project-myapp.tar.zst.age
       6000000  volume-myapp-wordpress-1-var-www-html docker_volume    docker-volumes/myapp-wordpress-1--var-www-html.tar.zst.age
```

Колонки: размер в байтах, компонент, способ восстановления, файл. Имена
компонентов (`project-myapp`, `myapp-db-1`) понадобятся для `--component`.
Первая строка (`from web-1`) — `host-label`, который нужно набрать при
подтверждении и под которым инициализируют новый сервер.

**2. Положить ключ на сервер** на время восстановления, например `~/key.txt`
(`scp` с вашего компьютера). После восстановления удалите: `shred -u ~/key.txt`.

**3. Проверить, что снимок читается:**

```sh
holdfast restore verify --snapshot /opt/backups/20260923-021500 --identity ~/key.txt
```

Ожидаемо: `checked N artifacts in 20260923-021500`, код возврата 0.

**4. Посмотреть план, ничего не меняя:**

```sh
holdfast restore --snapshot /opt/backups/20260923-021500 --identity ~/key.txt --dry-run
```

**5. Восстановить.** Всё:

```sh
holdfast restore --snapshot /opt/backups/20260923-021500 --identity ~/key.txt
```

или только то, что повреждено:

```sh
holdfast restore --snapshot /opt/backups/20260923-021500 --identity ~/key.txt --component myapp-db-1
```

Появится план и просьба набрать имя машины **из снимка**: его `host-label`
(первая строка `restore list`). При `--from-other-host` это имя старой
машины, а не этой. Контейнеры, которые пишут
в заменяемые данные, будут остановлены и после восстановления запущены снова.
Если контейнер не останавливается, восстановление прекращается — оно не
пропускает его молча.

Что происходит с данными: папка распаковывается поверх текущей, том
очищается и заполняется заново. В базе пересоздаются таблицы и объекты,
которые есть в дампе (PostgreSQL — `pg_restore --clean`); то, чего в дампе
нет, остаётся.
**Файлы, которых нет в снимке, из папки не удаляются.**

**6. Проверить результат** руками: сайт открывается, в базе нужные данные.

**7. Удалить ключ с сервера:** `shred -u ~/key.txt`.

Флаги `--no-stop` (не трогать контейнеры) и `--yes` (не спрашивать) — в
[справочнике](05-commands.md#holdfast-restore).

## Б. Новый сервер с нуля

Старый сервер потерян. Есть снимок (на удалённом хранилище или у вас) и ключ.

Предусловия, **которые обязательно выполнить до начала**:

- снимок скачан или доступен на новом сервере;
- ключ у вас под рукой;
- вы знаете `host-label` из снимка.

### Шаг 1. Голый сервер: пакеты и Docker

Выполните [01-install.md](01-install.md): шаг 1 (вход на сервер), шаг 2
(пакеты и Docker) и шаг 3 (holdfast). Шаг 4 оттуда (`init`) — это шаг 2
ниже.
Docker нужен, если в копии есть базы или тома.

### Шаг 2. Инициализация с тем же именем машины

**На сервере** — `host-label` **тот же, что в снимке**, режим тот же:

```sh
holdfast init --fresh --host-label web-1 --backup-mode auto
holdfast config check
```

Если имя будет другим, holdfast откажется восстанавливать, пока вы не добавите
`--from-other-host` (см. ниже). Тогда это осознанное «я знаю, что это другая
машина».

Для будущих бэкапов на этом сервере впишите публичный ключ в
`[encryption]` ([02-backup.md](02-backup.md#2-публичный-ключ-в-конфигурации)).
Для самого восстановления он не нужен.

### Шаг 3. Доставить снимок и ключ на сервер

**С удалённого хранилища** (если настроена вторая копия), на новом сервере:

```sh
apt-get install -y rclone
rclone config
rclone copy shared:backups/web-1/20260923-021500 /opt/backups/20260923-021500 --progress
```

(Имя хранилища и путь — ваши; структура описана в
[02-backup.md](02-backup.md#8-вторая-копия-рекомендуется).)

**Со своего компьютера:**

```sh
scp -r ./snap root@НОВЫЙ_СЕРВЕР:/opt/backups/20260923-021500
scp ~/holdfast-age-key.txt root@НОВЫЙ_СЕРВЕР:/root/key.txt
```

Папка `/opt/backups` может не существовать — создайте: `mkdir -p /opt/backups`.

Проверьте, что снимок цел и ключ подходит:

```sh
holdfast restore list   --snapshot /opt/backups/20260923-021500
holdfast restore verify --snapshot /opt/backups/20260923-021500 --identity /root/key.txt
```

### Шаг 4. Сначала папка проекта

Нужно вернуть `docker-compose.yml`, `.env` и остальное, чтобы поднять
приложение. Имя компонента проекта возьмите из `restore list` (в примере
`project-myapp`):

```sh
holdfast restore --snapshot /opt/backups/20260923-021500 --identity /root/key.txt \
  --component project-myapp --no-stop
```

- `--component` — только этот компонент;
- `--no-stop` — не останавливать и не запускать контейнеры. Для компонента-
  папки он не обязателен (holdfast останавливает контейнеры только тогда,
  когда восстанавливает тома или базы), но безопасен. Нужен, если в область
  попали тома или базы, а контейнеров ещё нет;
- если `host-label` на этом сервере **не совпадает** со снимком — добавьте
  `--from-other-host`.

holdfast напечатает план и попросит набрать имя машины. Результат —
`restored 1 artifacts from ...`. Проверьте: `ls /root/myapp`.

### Шаг 5. Поднять приложение

```sh
cd /root/myapp
docker compose up -d
```

Флаг `-p` не нужен и вреден: compose называет проект по имени папки, а
рецепты снимка ссылаются на контейнеры и тома с именами, которые из этого
имени получились (`myapp-db-1`, `myapp_data`). Другое имя проекта даст
другие имена контейнеров и новые пустые тома рядом с теми, куда holdfast
восстанавливает, а восстановление всё равно сообщит об успехе. Поэтому
папка проекта должна называться так же, как на старом сервере; имена
контейнеров из `restore list` и `docker ps` должны совпасть. Если проект
когда-то запускали с другим именем (`-p`, `name:` в файле compose или
`COMPOSE_PROJECT_NAME` в `.env`), используйте то же имя здесь.

Контейнеры создаются пустыми: база новая, тома новые. Это нужно, чтобы
было **куда** восстанавливать безымянные тома и дампы баз.

Подождите, пока база стартует: `docker compose ps`.

### Шаг 6. Восстановить всё остальное

```sh
holdfast restore --snapshot /opt/backups/20260923-021500 --identity /root/key.txt
```

Без `--component` восстанавливается всё: тома, дампы баз (и ещё раз папка
проекта — поверх самой себя, это безопасно). Контейнеры на время
остановятся и снова запустятся. Если имя машины не совпадало, и здесь нужен
`--from-other-host`.

Безымянный том holdfast кладёт в том, который контейнер подключает **сейчас**:
поэтому приложение должно быть поднято на шаге 5. Если контейнера нет, будет
ошибка `the container ... has no volume at ...; bring the application up first` —
поднимите приложение и повторите.

`--dry-run` полного восстановления на этом этапе не помогает: он тоже
спрашивает Docker про безымянные тома и до шага 5 закончится той же
ошибкой. Проверяйте им только компонент-папку.

База загружается с теми же учётными данными, с которыми снималась: контейнер
базы должен быть запущен с той же переменной пароля (она вернулась вместе с
`.env`) или тем же `defaults_file` по тому же пути.

### Шаг 7. Проверить и убрать за собой

1. Сайт открывается, данные на месте, база отвечает.
2. Удалить ключ: `shred -u /root/key.txt`.
3. Настроить бэкап заново: [02-backup.md](02-backup.md) — публичный ключ,
   `holdfast backup --dry-run`, `holdfast backup`, расписание в
   `/etc/cron.d/holdfast`.
4. Сменить пароль, который хранился в рабочей заметке вместе с адресом
   сервера, если такой есть.

Если при восстановлении база была в `/etc/holdfast/components.toml`
(режим `manual`), создайте его заново: `holdfast backup --discover`.

## Проверка копии без риска

Бэкап, из которого никто не восстанавливался, — это надежда. Проверять надо
**у себя** (или на любой машине, где есть ключ), не на рабочем сервере.

**1. Скачать снимок. У себя:** имя свежего снимка — папка, на которую
указывает `latest`: `ssh root@ВАШ_СЕРВЕР readlink /opt/backups/latest`.

```sh
mkdir holdfast-check
cd holdfast-check
scp -r root@ВАШ_СЕРВЕР:/opt/backups/20260923-021500 ./snap
```

Папку `./snap` заранее не создавайте: если она есть, `scp` положит снимок
внутрь неё.

**2. Linux или WSL.** Нужны `zstd`, `age`, Python 3.11+ и holdfast той же
версии:

```sh
sudo apt-get install -y python3 python3-venv zstd age
git clone https://github.com/viktorplus/holdfast.git
python3 -m venv ./venv
./venv/bin/pip install ./holdfast
./venv/bin/holdfast restore list   --snapshot ./snap
./venv/bin/holdfast restore verify --snapshot ./snap --identity ~/holdfast-age-key.txt
```

`verify` расшифровывает каждый артефакт и проверяет формат: должно быть
`checked N artifacts in ...` и код 0. Без `sudo` он всё равно проверяет, но
добавляет строку `could not record this verify in /var/lib/holdfast/jobs` —
на вашей машине журнал не нужен.

Репетиция восстановления **папки** в пустое место (нужен `sudo` — запись в журнал):

```sh
sudo ./venv/bin/holdfast restore --snapshot ./snap --identity ~/holdfast-age-key.txt \
  --root /tmp/drill --component project-myapp --yes --no-stop --from-other-host
ls -la /tmp/drill/root/myapp
```

`--root` переносит файлы в `/tmp/drill`. `--from-other-host` нужен, потому что
проверка идёт не на той машине, где снимался снимок.

**3. Windows: одноразовый контейнер.** Всё делает контейнер `ubuntu:24.04` в
Docker Desktop и удаляется (`--rm`). Ключ подключается только для чтения. В папке
`holdfast-check` уже лежит `snap`; скачайте исходники
(`git clone https://github.com/viktorplus/holdfast.git`, а если клон есть —
`git -C holdfast pull`).

Git Bash (в `KEY` — путь к ключу с прямыми слешами):

```sh
KEY='C:/путь/к/holdfast-age-key.txt'
MSYS_NO_PATHCONV=1 docker run --rm -e DEBIAN_FRONTEND=noninteractive \
  -v "$(pwd -W)/snap:/snap:ro" \
  -v "$(pwd -W)/holdfast:/src:ro" \
  -v "$KEY:/key.txt:ro" \
  ubuntu:24.04 bash -c 'apt-get update -qq && apt-get install -y -qq python3 python3-venv zstd age >/dev/null && cp -r /src /tmp/holdfast && python3 -m venv /opt/hf && /opt/hf/bin/pip install -q /tmp/holdfast && /opt/hf/bin/holdfast version && /opt/hf/bin/holdfast restore list --snapshot /snap && /opt/hf/bin/holdfast restore verify --snapshot /snap --identity /key.txt && /opt/hf/bin/holdfast restore --snapshot /snap --identity /key.txt --root /tmp/drill --component project-myapp --yes --no-stop --from-other-host && ls -la /tmp/drill/root/myapp'
```

PowerShell:

```powershell
$key = "C:\путь\к\holdfast-age-key.txt"
docker run --rm -e DEBIAN_FRONTEND=noninteractive `
  -v "${PWD}\snap:/snap:ro" `
  -v "${PWD}\holdfast:/src:ro" `
  -v "${key}:/key.txt:ro" `
  ubuntu:24.04 bash -c 'apt-get update -qq && apt-get install -y -qq python3 python3-venv zstd age >/dev/null && cp -r /src /tmp/holdfast && python3 -m venv /opt/hf && /opt/hf/bin/pip install -q /tmp/holdfast && /opt/hf/bin/holdfast version && /opt/hf/bin/holdfast restore list --snapshot /snap && /opt/hf/bin/holdfast restore verify --snapshot /snap --identity /key.txt && /opt/hf/bin/holdfast restore --snapshot /snap --identity /key.txt --root /tmp/drill --component project-myapp --yes --no-stop --from-other-host && ls -la /tmp/drill/root/myapp'
```

Что должно напечататься по порядку: версия (`0.3.0`), состав снимка,
`checked N artifacts in ...`, план и `restored 1 artifacts from ...`, список
файлов проекта. Цепочка идёт через `&&` и остановится на первой ошибке.

**Что проверка не покрывает.** `--root` переносит только файлы. Том и базу так
репетировать нельзя — holdfast откажется, если в область попал том или база
(сузьте `--component` до файлового компонента). Их формат проверяет `verify`.
Полная репетиция «с нуля» — это сценарий Б на тестовом сервере.

> Проверка сторожа `restore_tested` читает журнал той машины, где её запускают.
> Если репетиция была у вас, на сервере она останется FAIL: нужное доказательство
> получается только с ключом на сервере, а класть его туда постоянно нельзя.

## Если восстановление оборвалось

Сообщение скажет **PARTIALLY RESTORED**: часть рецептов выполнена, часть нет, а
остановленные контейнеры запущены обратно. Устраните причину и **запустите ту же
команду снова**: рецепты повторяемы (папка распаковывается поверх, том очищается
и заполняется, в базе пересоздаются таблицы из дампа).

Это же сообщение появится, если сбой случился раньше любой записи, например
когда контейнер не удалось остановить: тогда в нём сказано, что именно
произошло, а данные не менялись. Совет тот же: устранить причину и повторить.

## Чего восстановление не умеет

- том Docker восстанавливается записью в каталог, который Docker называет
  путём тома; holdfast внутри контейнера без этого каталога, под rootless Docker
  или с нелокальным драйвером тома этого сделать не может и скажет об этом;
- артефакт компонента `command` без таблицы `restore` не возвращается: он назван
  в `list`, в плане и в результате;
- повторяю: восстановить снимок можно только если есть ключ, которым он
  зашифрован.

Команды и флаги — в [справочнике](05-commands.md#holdfast-restore). Сообщения об
ошибках — в [06-errors.md](06-errors.md).
