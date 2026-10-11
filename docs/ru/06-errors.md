# Сообщения об ошибках

<!-- nav -->
**Меню:** [Оглавление](README.md) · [Установка](01-install.md) · [Бэкап](02-backup.md) · [Состав архива](03-contents.md) · [Восстановление](04-restore.md) · [Справочник команд](05-commands.md) · **Ошибки** · [Переход с 0.2](07-upgrade-from-0.2.md)
<!-- /nav -->

**В этом документе:**

- [Бэкап и конфигурация](#бэкап-и-конфигурация)
- [Базы данных](#базы-данных)
- [Восстановление](#восстановление)
- [Если всё «зелёное», а бэкап падает](#если-всё-зелёное-а-бэкап-падает)
- [Чего в holdfast пока нет](#чего-в-holdfast-пока-нет)

Ошибки начинаются с имени команды, например `holdfast backup: ...`.
Строки, помеченные «предупреждение», — не ошибки: они печатаются в разделе
`warnings:` вывода `--dry-run` и без этого префикса.

## Бэкап и конфигурация

| Сообщение | Причина | Что делать |
|---|---|---|
| `encryption is on and no recipient is configured` | пуст `encryption.recipients` | [02-backup.md](02-backup.md#2-публичный-ключ-в-конфигурации) |
| `backup.mode is '...'; it is either "auto" or "manual"` | опечатка в `mode` | поправить строку в `[backup]` |
| `backup.exclude: '...' is not one of volume:<...>, path:<...>, database:<...>, container:<...>` | неверный элемент `exclude` | [03-contents.md](03-contents.md#исключить-лишнее) |
| `backup.mode is "auto"; on a machine without Docker set backup.mode = "manual"` | `auto`, а Docker нет или он не отвечает | без Docker — `mode = "manual"`; иначе запустить Docker |
| `backup.exclude: '...' matches nothing on this machine` | **предупреждение, не ошибка:** исключение ни с чем не совпало, скорее всего опечатка | сверить с `not taken:`, `docker ps -a`, `docker volume ls` |
| `a docker_volume component without volume or container takes every volume by name, ...` | **предупреждение, не ошибка:** в `auto` остался блок `docker_volume` из 0.2 | удалить блок: тома возьмёт правило ([07-upgrade-from-0.2.md](07-upgrade-from-0.2.md)) |
| `this snapshot is estimated at up to N MB before compression, /opt/backups has X GB free, and backup.min_free_gb asks for 8 to be left` | после бэкапа места осталось бы меньше порога | освободить место, исключить лишнее или уменьшить `backup.min_free_gb` |
| `/etc/holdfast/components.toml is not read in auto mode; ...` | **предупреждение, не ошибка:** в `auto` остался файл от `manual` или 0.2 | удалить файл, если не нужен |
| `/etc/holdfast/components.toml was not written by holdfast backup --discover, so it is ignored; ...` | **предупреждение, не ошибка:** в `manual` лежит черновик 0.2 | перенести нужное в `holdfast.toml` и удалить файл, либо `--discover` |
| `nothing to back up: no [[component]] produced an artifact` | ни правило, ни вы не дали ни одного компонента | `holdfast backup --dry-run`: что в `not taken:`; в `manual` — `--discover` |
| `component 'app' failed with exit 2`, выше `ls: cannot access ...` | в `path` указана несуществующая папка | поправить путь |
| `there is no bash on PATH` | нет `bash` | holdfast рассчитан на Linux с systemd |

## Базы данных

| Сообщение | Причина | Что делать |
|---|---|---|
| `container 'myapp-db-1': its root password is not in its environment, so holdfast cannot reach the database; ...` | у контейнера нет переменной с паролем root | [defaults_file](03-contents.md#файл-доступа-к-базе-defaults_file) или исключить: `"container:myapp-db-1"` |
| `... Access denied ... - the password in the container's environment was not accepted; ...` | пароль root меняли после первого запуска, или в нём `\` (образ `mysql`) | [defaults_file](03-contents.md#файл-доступа-к-базе-defaults_file) |
| `... Access denied ... - this component has no defaults_file, so mysql ran without a password; ...` | вы объявили `mysql` сами, без `defaults_file` | добавить `defaults_file` или удалить блок — тогда базу возьмёт правило |
| `component 'myapp-db-1': the container 'myapp-db-1' is not running, so there is nothing to dump` | контейнер базы остановлен | запустить его или исключить `"container:myapp-db-1"` |

## Восстановление

| Сообщение | Причина | Что делать |
|---|---|---|
| `this snapshot was taken on 'web-1', and this machine is 'web-2'` | `host-label` на машине другой | если это осознанно — `--from-other-host`; иначе инициализировать с тем же именем ([04-restore.md](04-restore.md#шаг-2-инициализация-с-тем-же-именем-машины)) |
| `this snapshot was taken on '...', and this machine has no host_label, so there is nothing to check it against` | у машины нет `host_label` | задать при `init --host-label`, либо `--from-other-host` |
| `... has no volume at /var/www/html; bring the application up first (docker compose up -d) and try again` | безымянного тома ещё нет: контейнер не создан | поднять приложение (`docker compose up -d` в папке проекта), повторить. Ничего не изменено |
| `--root moves only files, and ... would go into the live volumes` | репетиция захватила том или базу | `--component <файловый компонент>` |
| `this is not an interactive session, so there is nobody to confirm with; pass --yes if that is what you meant` | нет терминала | запускать в интерактивной сессии или добавить `--yes` |
| `that did not match, and nothing was restored` | неверно набрано имя машины при подтверждении | запустить снова и набрать имя из строки подтверждения |
| `PARTIALLY RESTORED` | восстановление оборвалось на середине (или сбой случился до любой записи, например контейнер не остановился: причина в тексте перед этими словами) | устранить причину и запустить ту же команду снова ([04-restore.md](04-restore.md#если-восстановление-оборвалось)) |

## Если всё «зелёное», а бэкап падает

`config check` печатает `configuration is complete`, но бэкап падает: это
нормально, `config check` не проверяет готовность бэкапа. Смотрите
`holdfast backup --dry-run`.

## Чего в holdfast пока нет

- оповещений: `alerts.chat_id` в конфигурации есть, но его никто не читает;
- своего планировщика — только cron или systemd-таймер;
- веб-интерфейса и HTTP API: `api.token` создаётся, но не используется;
- баз вне Docker в режиме `auto`: их объявляют сами ([03-contents.md](03-contents.md#добавить-своё));
- MongoDB, Redis и других СУБД — только через компонент `command`.
