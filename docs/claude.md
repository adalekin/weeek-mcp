# Как подключить weeek-mcp к Claude

Для Claude Desktop есть готовый бандл, с него файл и начинается. Claude Code и ручная настройка через pip описаны [в конце](#claude-code-и-ручная-настройка).

## Claude Desktop

1. Скачайте [`weeek-mcp.mcpb`](https://github.com/adalekin/weeek-mcp/releases/latest/download/weeek-mcp.mcpb) из последнего релиза.
2. Откройте файл двойным кликом (или перетащите его в **Settings → Extensions**) и введите API-токен Weeek. Токен берётся в Weeek: **Настройки → API** → создать токен.

Задачи подключены. Python ставить не нужно: Desktop запускает сервер своим встроенным uv, и при первом запуске тот сам скачает `weeek-mcp` с PyPI.

### База знаний

Публичного API у базы знаний нет, поэтому сервер работает через сохранённую сессию браузера. Войдите один раз из терминала. Для этого поставьте [uv](https://docs.astral.sh/uv/) и выполните:

```bash
uvx --from "weeek-mcp[kb]" playwright install chromium
uvx --from "weeek-mcp[kb]" weeek-mcp-login
```

Откроется браузер. Войдите в Weeek (2FA, SSO и капча тоже подойдут), и сессия сохранится. Потом выключите и включите расширение в **Settings → Extensions**, чтобы сервер её подхватил.

Email и пароль в настройках расширения необязательны. С ними сервер сам войдёт заново, когда сессия истечёт. При 2FA и SSO это не сработает, тогда повторите `weeek-mcp-login`.

## Проверка

Попросите Claude:

> Покажи мои проекты в Weeek

Под капотом вызовется `weeek_list_projects` (или сначала `weeek_whoami`). Вернулись проекты — задачи подключены.

Для базы знаний:

> Найди в базе знаний Weeek документ про onboarding и покажи его содержимое

В Claude Desktop документы KB также доступны как вложения: меню **+** у подключённого сервера → выбираете документ → в контекст попадает его **содержимое**, а не ссылка.

## Если что-то не так

- **Инструментов базы знаний не видно.** Сервер показывает их только при сохранённой сессии или заданных email и пароле. Прогоните `weeek-mcp-login` и перезапустите расширение.
- **KB отвечает ошибкой входа.** Сессия истекла. Прогоните `weeek-mcp-login` заново. Для аккаунтов с 2FA/SSO автологин по email и паролю не сработает, только `weeek-mcp-login`.
- **Непонятно, что происходит.** Лог сервера лежит в `~/Library/Logs/Claude/mcp-server-Weeek.log` на macOS и в `%APPDATA%\Claude\logs\mcp-server-Weeek.log` на Windows.

## Claude Code и ручная настройка

Этот вариант нужен для Claude Code или если не хочется ставить бандл. Понадобится **Python 3.10+**.

### Установка

```bash
# только задачи
pip install weeek-mcp

# задачи + база знаний
pip install "weeek-mcp[kb]"
playwright install chromium
weeek-mcp-login
```

Через [uv](https://docs.astral.sh/uv/) как отдельную утилиту (удобно, не мусорит в системном Python):

```bash
uv tool install "weeek-mcp[kb]"
uv tool run --from weeek-mcp playwright install chromium
weeek-mcp-login
```

Если команда `weeek-mcp` не находится, допишите каталог из вывода `pip show -f weeek-mcp` (или `uv tool dir`) в `PATH`, либо укажите в конфиге абсолютный путь до `weeek-mcp`.

### Claude Code

```bash
claude mcp add weeek -s user -e WEEEK_API_TOKEN=ваш_токен -- weeek-mcp
```

- `-s user` — сервер будет доступен во всех проектах. Замените на `-s project` (запишется в `.mcp.json` репозитория, шарится с командой) или `-s local` (только этот проект, по умолчанию).
- Проверка: `claude mcp list`, внутри сессии `/mcp`.

### Claude Desktop через конфиг

Откройте **Settings → Developer → Edit Config** (файл `claude_desktop_config.json`) и добавьте сервер в `mcpServers`:

```json
{
  "mcpServers": {
    "weeek": {
      "command": "weeek-mcp",
      "env": { "WEEEK_API_TOKEN": "ваш_токен" }
    }
  }
}
```

Потом полностью перезапустите Claude Desktop (выйти из приложения, а не просто закрыть окно).

### Переменные окружения

Сессия базы знаний по умолчанию лежит в `~/.local/state/weeek-mcp/`, и сервер находит её сам. Остальные переменные нужны редко.

| Переменная | Зачем |
| --- | --- |
| `WEEEK_API_TOKEN` | Токен API задач. Обязателен для инструментов задач. |
| `WEEEK_EMAIL` / `WEEEK_PASSWORD` | Автоматический вход в KB. Не нужны, если засеяли сессию через `weeek-mcp-login`. |
| `WEEEK_STORAGE_STATE` | Куда кешируется сессия браузера, если не по умолчанию. |
| `WEEEK_WORKSPACE_ID` | ID воркспейса KB. Необязательно — определяется автоматически. |
| `WEEEK_HEADLESS` | `false`, чтобы видеть браузер во время входа. |
| `WEEEK_KB_CACHE_TTL` | Сколько секунд кешировать список документов KB (по умолчанию `300`). |
| `WEEEK_DEBUG_LOG` | `1`/`true` — писать диагностические логи в `~/.local/state/weeek-mcp/debug.log` (некоторые клиенты глотают stderr). По умолчанию выключено. |

---

Ограничения и детали по каждому инструменту — в [README.ru.md](../README.ru.md).
