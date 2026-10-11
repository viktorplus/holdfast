# Переход с 0.2 на 0.3

<!-- nav -->
**Меню:** [Оглавление](README.md) · [Установка](01-install.md) · [Бэкап](02-backup.md) · [Состав архива](03-contents.md) · [Восстановление](04-restore.md) · [Справочник команд](05-commands.md) · [Ошибки](06-errors.md) · **Переход с 0.2**
<!-- /nav -->

**В этом документе:**

- [1. Обновить программу](#1-обновить-программу)
- [2. Сохранить конфигурацию](#2-сохранить-конфигурацию)
- [3. Включить auto](#3-включить-auto)
- [4. Убрать лишние компоненты](#4-убрать-лишние-компоненты)
- [5. Проверить](#5-проверить)
- [6. Старый components.toml](#6-старый-componentstoml)
- [7. Файл с паролем для базы](#7-файл-с-паролем-для-базы-необязательно)
- [8. Первый бэкап на новой версии](#8-первый-бэкап-на-новой-версии)

Всё — **на сервере**.

## 1. Обновить программу

```sh
cd /root
git -C holdfast pull
/opt/holdfast/bin/pip install ./holdfast
holdfast version
```

Должно быть `0.3.0`. Сразу после обновления поведение прежнее: в конфигурации
0.2 нет `backup.mode`, и это значит `manual`. Но две вещи работают иначе сразу:
служебная база `mysql` (пользователи и права) больше не дампится, а
восстановление MySQL использует тот же `defaults_file`, что и дамп.

## 2. Сохранить конфигурацию

```sh
cp -p /etc/holdfast/holdfast.toml /etc/holdfast/holdfast.toml.bak
```

## 3. Включить auto

```sh
grep -n '^\[backup\]' /etc/holdfast/holdfast.toml
```

Строка нашлась — допишите под ней `mode = "auto"`. Нет — добавьте в конец файла:

```toml
[backup]
mode = "auto"
```

## 4. Убрать лишние компоненты

Блоки `[[component]]` из инструкции 0.2, которые теперь покрывает правило:

- `type = "mysql"` с `defaults_file` — правило снимает дамп с паролем из
  окружения контейнера. Оставьте блок, только если ваш случай — исключение
  ([defaults_file](03-contents.md#файл-доступа-к-базе-defaults_file));
- `type = "docker_volume"` с `exclude` и `max_mb` — брал все тома подряд,
  включая бесхозные; правило берёт только подключённые;
- `type = "path"` для папки проекта (`/root/myapp`) — правило берёт её само.

Блоки `path` для папок **вне** Docker (например, `/etc/letsencrypt`) оставьте.
Блоки для `/opt/holdfast` и `/opt/containerd`, если черновик 0.2 их предложил,
удалите.

## 5. Проверить

```sh
holdfast config check
holdfast backup --dry-run
```

`configuration is complete`, затем `mode: auto` и разбор по
[03-contents.md](03-contents.md). Всё, что вы хранили раньше, должно быть взято
либо стоять в `not taken:` с понятной причиной. Откат:

```sh
cp -p /etc/holdfast/holdfast.toml.bak /etc/holdfast/holdfast.toml
```

## 6. Старый components.toml

Если вы сохраняли вывод `--discover` в `/etc/holdfast/components.toml`,
`--dry-run` предупредит: `components.toml is not read in auto mode`. Убедитесь,
что нужное уже в `holdfast.toml` или найдено правилом, и удалите:

```sh
rm /etc/holdfast/components.toml
```

## 7. Файл с паролем для базы (необязательно)

В режиме `auto` `holdfast-mysql.cnf` не нужен; `--dry-run` показывает его в
`not taken:` как `inside a compose project directory`, то есть он попадает в
архив проекта зашифрованным. Можно оставить. Убирать — только когда блока `mysql`
с `defaults_file` в `holdfast.toml` уже нет (пункт 4). По порядку:

1. в compose-файле, в сервисе базы, удалить строку
   `- ./holdfast-mysql.cnf:/etc/holdfast/mysql.cnf:ro`;
2. пересоздать **только** контейнер базы: `cd /root/myapp && docker compose up -d db`
   (с `-f <файл>`, если compose-файл назван иначе). Не `down`/`up`;
3. `holdfast backup --dry-run` — дамп по-прежнему `from rule`, строки про
   `holdfast-mysql.cnf` в `not taken:` нет;
4. удалить файл: `rm /root/myapp/holdfast-mysql.cnf`.

## 8. Первый бэкап на новой версии

Как в [02-backup.md](02-backup.md#4-первый-бэкап), проверка копии — как в
[04-restore.md](04-restore.md#проверка-копии-без-риска). Снимки 0.3
восстанавливаются только holdfast 0.3.
