# Справочник команд

<!-- nav -->
**Меню:** [Оглавление](README.md) · [Установка](01-install.md) · [Бэкап](02-backup.md) · [Состав архива](03-contents.md) · [Восстановление](04-restore.md) · **Справочник команд** · [Ошибки](06-errors.md) · [Переход с 0.2](07-upgrade-from-0.2.md)
<!-- /nav -->

**В этом документе:**

- [Общее](#общее)
- [holdfast version](#holdfast-version)
- [holdfast init](#holdfast-init)
- [holdfast config check](#holdfast-config-check)
- [holdfast backup](#holdfast-backup)
- [holdfast restore](#holdfast-restore)
- [holdfast audit](#holdfast-audit)
- [Коды возврата](#коды-возврата)
- [Ключи holdfast.toml](#ключи-holdfasttoml)
- [Команды вокруг holdfast](#команды-вокруг-holdfast)

Список составлен по `holdfast/cli.py` версии 0.3.0; актуальный перечень на вашей
машине даёт `holdfast --help` и `holdfast КОМАНДА --help`.

## Общее

```sh
holdfast [--config-dir ПАПКА] КОМАНДА ...
```

| Флаг | Смысл |
|---|---|
| `--config-dir ПАПКА` | где лежит `holdfast.toml` (по умолчанию `/etc/holdfast`). Ставится **перед** командой: `holdfast --config-dir /srv/holdfast config check` |

## holdfast version

Печатает номер версии (`0.3.0`). Удобно, чтобы проверить установку и обновление.

```sh
holdfast version
```

## holdfast init

Создаёт `/etc/holdfast/holdfast.toml` (права `0600`). Существующий файл не
перезаписывает.

```sh
holdfast init --fresh --host-label web-1 --backup-mode auto
```

| Флаг | Смысл |
|---|---|
| `--fresh` | отдельная машина. Обязателен `--fresh` **или** `--join` |
| `--join ПРОФИЛЬ` | присоединить машину к коллекции по файлу профиля (допускаются только `collection`, `alerts.chat_id`, `offsite.remote`, `encryption.recipients`) |
| `--host-label ИМЯ` | имя машины (по умолчанию имя хоста). Должно быть уникальным среди ваших серверов; при восстановлении сверяется со снимком |
| `--backup-mode auto\|manual` | `auto` — состав вычисляется при каждом бэкапе (по умолчанию); `manual` — по списку от `--discover`. Без Docker нужен `manual` |

Подробнее: [01-install.md](01-install.md).

## holdfast config check

Сообщает, чего не хватает в настройках установки. Печатает
`configuration is complete`, когда всё на месте. Ключа шифрования и состава
копии не проверяет — для этого `holdfast backup --dry-run`.

```sh
holdfast config check
holdfast config check --json
```

`--json` печатает один объект: `{"complete": true|false, "complaints": [{"key", "why", "fix"}]}`. Код возврата тот же (1, если чего-то не хватает).
Если файл настроек повреждён, команда печатает ошибку в stderr, stdout пуст, код 1:
разбирайте stdout, только если он не пуст.

## holdfast backup

Делает снимок машины в `backup.root` (по умолчанию `/opt/backups`).

```sh
holdfast backup                 # сделать снимок
holdfast backup --dry-run       # показать, что будет взято; ничего не писать
holdfast backup --discover      # режим manual: записать список в components.toml
holdfast backup --json          # то же, что backup, но результат — JSON
```

| Флаг | Смысл |
|---|---|
| (без флагов) | снимок: зашифрованные артефакты, `manifest.json`, `SHA256SUMS`; потом ротация и отправка второй копии, если настроена |
| `--dry-run` | печатает выбор и точные команды, код возврата 0; не пишет и не меняет. Проверяет конфигурацию, Docker и пароли баз; дампы не снимает |
| `--discover` | в `manual` пишет `/etc/holdfast/components.toml` (прежний — в `.prev`); в `auto` ничего не пишет и объясняет почему |
| `--json` | результат запуска одним JSON-объектом: `{"status": "done", "snapshot", "directory", "total_bytes", "artifacts", "warnings", "skipped"}`; если бэкап уже идёт — `{"status": "busy", "message"}` (код 0). Предупреждения попадают в `warnings`, а не в stderr. Ошибки по-прежнему идут в stderr с кодом 1 и пустым stdout |

`--dry-run` и `--discover` взаимоисключающие; `--json` не сочетается ни с одним из них (код 2). Если бэкап уже идёт, команда
сообщает об этом и возвращает 0. Подробнее: [02-backup.md](02-backup.md),
[03-contents.md](03-contents.md).

## holdfast restore

```sh
holdfast restore list   --snapshot ПАПКА [--json]
holdfast restore verify --snapshot ПАПКА --identity КЛЮЧ [--json]
holdfast restore        --snapshot ПАПКА --identity КЛЮЧ [флаги]
```

Первое слово — действие: `list` (что внутри; ключ не нужен), `verify` (читается
ли копия; пишет только журнал) или без действия — восстановить.

| Флаг | Смысл |
|---|---|
| `--snapshot ПАПКА` | **обязателен.** Папка снимка, например `/opt/backups/20260923-021500` |
| `--identity ФАЙЛ` | приватный ключ (`age` или секретный ключ `gpg`). Читается на время запуска, нигде не копируется |
| `--identity-passphrase-file ФАЙЛ` | файл с паролем защищённого ключа `gpg` (на аварийной машине нет pinentry) |
| `--component ИМЯ` | восстановить только этот компонент (имена — из `restore list`). Все рецепты снимка всё равно проверяются |
| `--root ПАПКА` | куда класть `path`-компоненты (по умолчанию `/`). Для репетиции: `--root /tmp/drill`. Только файлы; снимок с томом или базой в области отклоняется |
| `--json` | только для `list` и `verify`: вывод JSON. `list` — `{snapshot, host_label, created_at, encryption, records[], unrestorable[]}`; `verify` — `{snapshot, checked, ok, failures[], warnings[]}` (код 1, если `ok` ложно). С настоящим восстановлением `--json` не принимается (код 2) |
| `--dry-run` | напечатать план и ничего не менять, включая контейнеры. Все проверки до записи он всё равно делает (суммы, ключ, вопрос к Docker про безымянные тома) |
| `--yes` | не спрашивать подтверждение (для скриптов). Без него нужно набрать `host-label` из снимка (первая строка `restore list`), а без терминала команда остановится |
| `--no-stop` | не останавливать и не запускать контейнеры. Для компонента-папки не обязателен (контейнеры останавливаются только при восстановлении томов и баз); нужен, если в область попали тома или базы, а контейнеров ещё нет |
| `--from-other-host` | разрешить снимок, снятый на машине с другим `host-label` |
| `--pg-container ИМЯ` | заменить контейнер PostgreSQL из рецепта |
| `--pg-user ИМЯ` | заменить пользователя PostgreSQL из рецепта |

Что `restore` проверяет до записи: машину (`host-label`), все рецепты, все
контрольные суммы, ключ, и только потом спрашивает вас. Сценарии:
[04-restore.md](04-restore.md).

## holdfast audit

Сторож: осматривает машину и печатает отчёт. Сам ничего не меняет.

```sh
holdfast audit
holdfast audit --baseline
holdfast audit --json
holdfast audit --coverage
```

| Флаг | Смысл |
|---|---|
| `--json` | отчёт в JSON |
| `--baseline` | после отчёта печатает блок TOML с тем, что на машине сейчас считается нормой |
| `--coverage` | таблица проверок без запуска |
| `--markdown` | вместе с `--coverage` — таблица в формате `docs/checks.md` |
| `--no-docker` | не обращаться к Docker |
| `--no-offsite` | не обращаться к удалённому хранилищу |
| `--host КОРЕНЬ` | где смонтирована проверяемая файловая система (по умолчанию `/`) |

Список проверок: `docs/checks.md`.

## Коды возврата

| Команда | 0 | 1 |
|---|---|---|
| `backup` | снимок снят, либо другой бэкап уже идёт | бэкап не удался |
| `audit` | ни одна проверка не провалена | есть FAIL |
| `restore verify` | все артефакты читаются | хоть один не прочитался |
| `backup --dry-run` | выбор напечатан (даже с `warnings:`) | ошибка конфигурации |
| `init` | файл создан | ошибка (в том числе файл уже есть) |
| `config check` | всё на месте | чего-то не хватает, или файл не читается |
| `restore`, `restore list` | восстановлено / список напечатан | `ConfigError`, `RestoreError`, `BackupError` |

Код 2 — неверные аргументы командной строки (например, неизвестный флаг или
`--json` вместе с `--dry-run`).

С `--json` код 1 бывает и «с ответом» (`config check`, `restore verify`:
в stdout JSON), и «без ответа» (ошибка в stderr, stdout пуст).

Ошибки выводятся с именем команды: `holdfast backup: ...`. Разбор — в
[06-errors.md](06-errors.md).

## Ключи holdfast.toml

Файл `/etc/holdfast/holdfast.toml`. Основное; полный список —
`docs/configuration.md`.

| Ключ | По умолчанию | Смысл |
|---|---|---|
| `host_label` | имя хоста | имя машины |
| `encryption.enabled` | `true` | шифровать снимки |
| `encryption.tool` | `"age"` | `age` или `gpg` (только публичные ключи) |
| `encryption.recipients` | `[]` | публичные ключи; без них бэкап не запустится |
| `encryption.recipients_file` | `""` | файл с публичными ключами по одному в строке |
| `backup.mode` | пусто (= `manual`; `init` пишет `auto`) | `auto` или `manual` |
| `backup.exclude` | `[]` | `"volume:…"`, `"path:…"`, `"database:контейнер/база"`, `"container:…"` |
| `backup.root` | `/opt/backups` | где лежат снимки |
| `backup.retention_days` | `7` | сколько дней хранить; `0` — не удалять |
| `backup.min_free_gb` | `8` | минимум свободного места: проверяется до запуска, а в режиме `auto` ещё и по оценке размера снимка (после бэкапа должно остаться не меньше) |
| `backup.zstd_level` | `10` | степень сжатия |
| `backup.lock_file` | `/run/holdfast-backup.lock` | замок от двух одновременных бэкапов |
| `offsite.remote` | `""` | назначение `rclone`, например `shared:backups`; пусто — второй копии нет |
| `offsite.timeout_minutes` | `120` | сколько ждать отправку |
| `jobs.dir` | `/var/lib/holdfast/jobs` | журнал запусков |
| `[[component]]` | нет | свои компоненты: `path`, `mysql`, `postgres`, `docker_volume`, `command` — [03-contents.md](03-contents.md) |

## Команды вокруг holdfast

Не часть holdfast, но нужны в инструкциях.

| Команда | Зачем |
|---|---|
| `age-keygen -o ФАЙЛ` | создать ключ шифрования (у себя) |
| `age-keygen -y ФАЙЛ` | напечатать публичный ключ из файла ключа |
| `docker ps -a` | контейнеры, включая остановленные |
| `docker volume ls` | все тома |
| `docker compose up -d` | поднять проект (из его папки) |
| `docker compose up -d db` | пересоздать только сервис `db` (не `down`!) |
| `rclone config` / `rclone copy ОТКУДА КУДА` | настроить удалённое хранилище / скачать снимок |
| `shred -u ФАЙЛ` | стереть файл с ключом |
| `readlink /opt/backups/latest` | какая папка — последний снимок |
| `systemctl is-active cron` | работает ли cron (`active`) |
| `netstat -ano \| findstr :ПОРТ` | (Windows) кто слушает порт |
