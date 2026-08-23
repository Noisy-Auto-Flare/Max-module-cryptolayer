# vkmax contract (task 2 spike) — фактический контракт библиотеки

> Источник истины для задач 3–7. Все утверждения подтверждены чтением исходников
> УСТАНОВЛЕННОЙ версии в `.venv\Lib\site-packages\vkmax\` (не README).
> Каждая сигнатура воспроизведена скриптом: `.omo/evidence/task-2-max-transport-module.log`.
> Пути вида `client.py:NNN` = `.venv\Lib\site-packages\vkmax\client.py`, строка NNN,
> коммит указан ниже. Функции `functions/*.py:N` аналогично.

## 0. Версия и происхождение

| Поле | Значение |
| --- | --- |
| Пакет | `vkmax` (pip label версии: **1.0.2** — совпадает с `pyproject` репозитория) |
| Фактическая установка | **git-коммит `c67a5097ac3e565dfb5d2448f9b17aff7f92a596`** ветки main |
| Подтверждение | `.venv\Lib\site-packages\vkmax-1.0.2.dist-info\direct_url.json`: `"commit_id": "c67a5097ac3e565dfb5d2448f9b17aff7f92a596"` |
| Зависимости | `websockets==17.0.1` + `aiohttp==3.14.3` (транзитивно, нужен git-версии для upload/download) |
| Python | 3.14.3 — импорт OK, все сигнатуры воспроизводятся |
| Почему git, а не PyPI | PyPI-wheel 1.0.2 расходится с документированным API (нет `device_id` в `login_by_token`, нет Origin-заголовка, `_recv_loop` не переживает обрыв). RISK-блок и решение — §11; журнал решения — `.omo/evidence/task-2-failure-max-transport-module.log` |

Константы протокола: `WS_HOST = "wss://ws-api.oneme.ru/websocket"` (`client.py:14`),
`RPC_VERSION = 11` (`client.py:15`), `APP_VERSION = "26.2.2"` (`client.py:16`).

## 1. Класс MaxClient

Конструктор **без аргументов**: `MaxClient()` (`client.py:32-45`). Нет ни `token`,
ни `device_id` в конструкторе. Атрибута **`me` НЕ существует** (runtime-проверка:
`hasattr(c, "me") == False`) — свой id берётся из ответа логина, см. §5.

### 1.1 Сигнатуры (все воспроизведены inspect.signature в evidence-логе)

| Сигнатура | Источник |
| --- | --- |
| `async connect(self)` | `client.py:49-62` |
| `async disconnect(self)` | `client.py:64-72` |
| `async invoke_method(self, opcode: int, payload: dict[str, Any], retries: int = 2)` | `client.py:74-107` |
| `async login_by_token(self, token: str, device_id: str \| None = None)` | `client.py:283-313` |
| `async send_code(self, phone: str) -> str` | `client.py:239-251` |
| `async sign_in(self, sms_token: str, sms_code: int)` | `client.py:253-281` |
| `set_packet_callback(self, function)` — **СИНХРОННЫЙ** метод (без await) | `client.py:114-117` |
| `set_reconnect_callback(self, function)` — синхронный | `client.py:119-122` |
| `set_callback(self, function)` — async, DEPRECATED (DeprecationWarning), делегирует `set_packet_callback`; НЕ использовать | `client.py:109-112` |
| `@property device_id(self) -> Optional[str]` — id, использованный в hello-пакете | `client.py:315-317` |

⚠️ Особенность: почти все методы завёрнуты в декоратор `@ensure_connected`
(`client.py:22-29`) — sync-обёртка, поэтому `inspect.iscoroutinefunction` на них
возвращает False, хотя вызов по-прежнему требует `await client.method(...)`
(обёртка синхронно возвращает корутину внутреннего async-метода).

⚠️ `ensure_connected` бросает `RuntimeError("WebSocket not connected. Call .connect() first.")`
(`client.py:25-26`) при любом вызове метода до `connect()`. После успешного
`disconnect()` `_connection = None` (`client.py:69`) → те же RuntimeError.
Исключения «already connected»: `Exception("Already connected")` если `connect()`
при живом соединении (`client.py:50-51`); `Exception('Keepalive task already started')`
(`client.py:202-203`); `Exception('Keepalive task is not running')` (`client.py:208-210`).

## 2. Жизненный цикл подключения

1. `await client.connect()` — открывает WSS c заголовками `Origin: https://web.max.ru`
   и браузерным User-Agent (`client.py:54-58`), запускает фоновую задачу `_recv_loop`
   (`client.py:60`).
2. `await client.login_by_token(token, device_id=None)` — hello-пакет opcode 6 с
   userAgent WEB/Chrome и `deviceId` = переданный `device_id` ИЛИ свежий
   `uuid.uuid4()` если None (`client.py:218-237`), затем opcode 19 c payload
   `{interactive: true, token, chatsCount: 40, chatsSync: 0, contactsSync: 0,
   presenceSync: -1, draftsSync: 0}` (`client.py:287-298`). При успехе ставит
   `_is_logged_in = True`, запускает keepalive и **возвра­щает ПОЛНЫЙ ответ сервера**
   (`return login_response`, `client.py:313`).
3. Keepalive: каждые 30 сек opcode 1 `{interactive: false}` c таймаутом 15 c
   (`client.py:175-198`); таймаут пинга триггерит reconnect-callback если задан
   (`client.py:183-187`).
4. Остановка — `disconnect()`, детали и идемпотентность: раздел 6.

**Порядок важен**: `set_packet_callback(cb)` вызывать ДО того как ожидаются входящие;
callback диспетчеризуется только когда установлен (`client.py:170-171`).

## 3. Callback входящих пакетов

```python
async def packet_callback(client: MaxClient, packet: dict) -> None: ...
client.set_packet_callback(packet_callback)   # синхронный вызов, БЕЗ await
```

- Требование асинхронности проверяется: sync-fn → `TypeError('callback must be async')`
  (`client.py:114-117`; runtime-проверено в evidence-логе).
- Диспетчеризация: `asyncio.create_task(self._incoming_event_callback(self, packet))`
  — callback получает **ДВА позиционных аргумента** `(client, packet)`
  (`client.py:170-171`), выполняется отдельной задачей цикла событий (исключение в
  callback НЕ роняет recv-loop, но молча гибнет в задаче — адаптер обязан ловить сам).
- Диспетчеризация идёт на ЛЮБОЙ пакет, чей `seq` не найден среди ожидающих запросов
  (`client.py:152-156`): это push-события сервера (сообщения = opcode 128, медиа =
  opcode 136 и пр.).

## 4. Схема входящего push-пакета op=128 (DISPATCH)

Форма конверта — единая для всех пакетов протокола v11 (`packet.py:5-12`,
`docs/protocol.md` репозитория):

```
packet = {"ver": 11, "cmd": <int>, "seq": <int>, "opcode": 128, "payload": {...}}
```

Ключевые пути внутри `payload` (подтверждены эталонным клиентом той же версии
протокола Aist/max2tg `app/max_client.py::_parse_message` + embedded README vkmax):

| Что | Путь | Тип |
| --- | --- | --- |
| id диалога | `packet["payload"]["chatId"]` | int |
| тело сообщения | `packet["payload"]["message"]` | dict |
| текст | `packet["payload"]["message"]["text"]` | str |
| id сообщения | `packet["payload"]["message"]["id"]` | str/int |
| **отправитель** | **`packet["payload"]["message"]["sender"]`** | int user_id |
| время правки (только у edited) | `packet["payload"]["message"]["updateTime"]` | int/null |

- Поле отправителя: **`sender`**, лежит ВНУТРИ `message`, путь
  `payload.message.sender`. Свои сообщения отличаем сравнением с собственным id (§5);
  echo-петля исключается только этой фильтрацией.
- ⚠️ Статус подтверждения: схема `chatId`/`message.text` подтверждена исходниками
  самого vkmax (embedded README в METADATA wheel'а, пример callback). Поле `sender`
  в исходниках vkmax нигде не разбирается (grep по пакету — 0 упоминаний), поэтому
  путь взят из независимой реализации того же протокола (max2tg) и подлежит
  контролю при раннем живом чекпойнте (задача 12).

## 5. Свой id (`my_id`) — ЖИВО ПОДТВЕРЖДЕНО 2026-08-23

`client.me` НЕ существует. Источник собственного id — ответ логина:

```
login_response = await client.login_by_token(token, device_id)
my_id = login_response["payload"]["profile"]["contact"]["id"]   # ЖИВОЙ путь
```

- **Живое подтверждение** (`scripts/dump_profile.py`, токен владельца):
  `payload.profile.contact.id = 393356389`; путь сходится с тремя
  независимыми местами той же выгрузки: `chat.owner`,
  `chat.lastMessage.sender` и ключами `chat.participants`.
- **Фолбэк** `payload.profile.id` сохранён в адаптере как защитный
  (легаси-путь; на живом сервере этого ключа НЕТ).
- Сам vkmax читает из profile только телефон
  `["payload"]["profile"]["contact"]["phone"]` в try/except (`client.py:303-307`)
  — наличие `profile.contact` подтверждено и кодом библиотеки, и живой выгрузкой.
- Если id не читается ни по одному из путей — адаптер обязан падать
  RuntimeError (эхо-фильтрация задачи 5 без него невозможна).

### Живое подтверждение остальных полей (чекпойнт задачи 12, выполнен)

| Поле контракта | Статус |
| --- | --- |
| вход по токену `login_by_token(token, device_id)` | ✅ живой логин успешен |
| `payload.chats` (список диалогов) | ✅ 7 диалогов извлечены |
| идентификатор диалога: `chat.chatId`, фолбэк `chat.id` | ✅ таблица discovery корректна (типы DIALOG) |
| отправитель сообщения: `message.sender` (int user_id) | ✅ `lastMessage.sender == contact.id владельца` |
| свой id: `payload.profile.contact.id` | ✅ см. выше (спайковый `profile.id` опровергнут) |

## 6. SHUTDOWN (специальный раздел)

Точная сигнатура закрытия: **`async def disconnect(self)`** (`client.py:64-72`).
Порядок действий внутри: `_stop_keepalive_task()` → `self._recv_task.cancel()` →
`await self._connection.close()` → `_connection = None` → закрытие aiohttp-pool.

**Идемпотентность двойного вызова: НЕТ.**
- Вызов 2 подряд при живой сессии: первый освобождает keepalive-task и ставит
  `_keepalive_task = None`; второй упадёт `Exception('Keepalive task is not running')`
  (`client.py:208-210`), не доходя до закрытия сокета.
- Вызов ДО login (после одного `connect()`): тот же `Exception('Keepalive task is not running')`
  — keepalive стартует только при успехе логина (`client.py:279, 311`).
- Вызов без соединения вообще: `RuntimeError` от `ensure_connected` (`client.py:25-26`).

→ Адаптер обязан оборачивать: свой идемпотентный `shutdown()` с
try/except вокруг каждого шага и флагом «уже остановлены».

**Поведение при активном приёме:** `disconnect()` канцеллит `_recv_task`
(`client.py:67`) — CancelledError ловится внутри `_recv_loop` (`client.py:130-132`),
цикл завершается штатно; незавершённые future ожидающих `invoke_method` НЕ
резолвятся и НЕ канцелятся → висящий `invoke_method` остаётся навсегда
(у него нет таймаута, §7). Отправку во время shutdown нужно предотвращать
на уровне адаптера (остановить worker до disconnect).

**Повторное подключение тем же объектом возможно** (в отличие от 1.0.2):
после `disconnect()` `_connection is None`, повторные `connect()`+`login_by_token()`
работают. Для реконнект-цикла задачи 5 допустимы оба пути; рекомендуемый —
новый `MaxClient()` на каждый цикл (чистые `_pending`/`_seq`).

## 7. invoke_method: таймауты и ошибки

- `invoke_method(opcode, payload, retries=2)` ждёт ответ **БЕЗ ТАЙМАУТА**
  (`await future`, `client.py:104`). Молчание сервера = вечное зависание.
  → Адаптер оборачивает все вызовы в `asyncio.wait_for(..., timeout=N)`.
- Если `send` падает `websockets.exceptions.ConnectionClosed`: при заданном
  reconnect-callback — реконнект + до 2 автоповторов; иначе тихий `return None`
  (`client.py:94-102`). `None` в ответе = «не отправлено» — трактовать как
  транзиентную ошибку.
- Ответ матчится ТОЛЬКО по `seq` без проверки `cmd` (`client.py:152-156`);
  ошибка протокола приходит обычным пакетом с `"error"` в payload (см. §8),
  отдельного класса ошибок нет.
- Теоретический риск коллизии seq push-пакета с seq запроса существует
  (нумерации независимые) — принятый риск, не блокирующий v1.

## 8. Исключения при невалидном токене

`login_by_token` проверяет `"error" in login_response["payload"]` и бросает
**generic `Exception(str_ошибки)`** (`client.py:300-301`); то же в `sign_in`
(`client.py:268-269`). Специальных классов исключений нет. Возможен также
`KeyError` при неожиданной форме ответа (`login_response["payload"]`).

→ Классификация задачей 6: любые исключения от `login_by_token` = терминальная
ошибка входа (обёртка `RuntimeError("MAX login failed: ...")` в задаче 4);
строковые маркеры флуда/лимитов при отправке классифицировать по тексту
(точные тексты сервера зафиксировать при живом чекпойнте, задача 12).

## 9. Отправка сообщения

```python
from vkmax.functions.messages import send_message
await asyncio.wait_for(send_message(client, chat_id, text), timeout=...)
```

| Сигнатура | Источник |
| --- | --- |
| `async send_message(client, chat_id: int, text: str, notify: bool = True, reply_to: str\|int\|None = None, attaches: list = [])` | `functions/messages.py:14-47` |
| `async reply_message(client, chat_id, text, reply_to_message_id, notify=True)` — обёртка над send_message(reply_to=...) | `functions/messages.py:107-120` |

opcode **64** (`messages.py:45`), payload: `chatId`, `message{text, cid, elements[],
link?, attaches[]}`, `notify`. `cid` генерируется случайно
`randint(1750000000000, 2000000000000)` (`messages.py:28`). Совпадает с
docs/opcodes.md (v11) и планом задачи 6.

Прочее (для справки, в v1 не используется): edit=67, delete=66, pin=55,
реакции 178/181 (`functions/groups.py:283-…`), uploads 80/82/83/87/88/65
(`functions/uploads.py`).

## 10. GET-CHATS (специальный раздел) — РЕШЕНО

**Готовой функции «получить список чатов» в пакете НЕТ** (модулей chats.py не
существует; grep `async def.*chats` пуст).

**Опровергнуто:** номер 49 из черновика — это НЕ список чатов. Opcode 49 =
GET_MESSAGES (история сообщений конкретного чата): payload
`{chatId, from(cid), forward, backward, getMessages}` —
`functions/groups.py:272-280` (join_group_by_link) и docs/opcodes.md
репозитория («Получение текущего чата»). НЕ использовать для discovery.

**РЕШЁННЫЙ способ (принят для scripts/discover_chats.py, задача 12):**

1. **Первичный**: список чатов уже приходит в ОТВЕТЕ на логин. Запрос op=19
   содержит `chatsCount: 40` (`client.py:292`), и `login_by_token` ВОЗВРАЩАЕТ
   полный ответ (`client.py:313`):

   ```python
   resp = await client.login_by_token(token, device_id)
   chats = resp["payload"].get("chats", [])      # список dict'ов-чатов
   # каждый chat: type ("DIALOG"|"CHAT"|"CHANNEL"), id диалога = chat["chatId"],
   # название = chat["title"]/..., последнее сообщение — по ключам message*/lastMessage
   ```

   Обоснование ключа `chats`: обработка того же ответа в Ladvix/WebMax
   (`src/webmax/api.py:96-101`: `response_payload.get('chats', [])`) и
   MaxTeamAPI/PyMax (`src/pymax/interfaces.py:512-520`: перебор
   `raw_payload.get("chats", [])` с разбором `type` DIALOG/CHAT). Точные имена
   внутренних полей chat-объекта (`chatId`, `title`) сверить при живом чекпойнте.

2. **Резерв/уточнение по известным id**: готовая функция
   `async resolve_channel_id(client, channel_id: int)` — opcode **48** CHAT_GET,
   payload `{"chatIds": [id]}` (`functions/channels.py:19-30`; в enum протокола
   max2tg — CHAT_GET=48). Возвращает информацию по указанным чатам.

3. **Участники группы** (если понадобится): `async get_group_members(client,
   group_id: int, marker=0, count=500)` — opcode 59, максимум 500 за вызов
   (`functions/groups.py:206-228`).

Отдельного «list all chats» opcode в задокументированном наборе v11
(docs/opcodes.md) не существует — snapshot логина является штатным источником
списка для всех эталонных клиентов протокола.

## 11. RISK-блок: версия зафиксирована git-коммитом

PyPI-wheel 1.0.2 расходится с документированным API репозитория и потребностями
плана: (1) `login_by_token(token)` без device_id; (2) connect без Origin/UA
заголовков; (3) `_recv_loop` не переживает обрыв соединения; (4) `disconnect()`
не сбрасывает состояние для повторного подключения. По критерию плана
«signatures diverge» принято решение: **git-установка, pinned commit
`c67a5097ac3e565dfb5d2448f9b17aff7f92a596`** (полный журнал сравнения и команд —
`.omo/evidence/task-2-failure-max-transport-module.log`).

Следствия: обновление vkmax = повторный прогон спайка; в `Max/requirements.txt`
— git-строка с хэшем (НЕ `vkmax==1.0.2`, которая резолвится в PyPI-wheel!).

Каноничная строка зависимости (единственный источник для Max/requirements.txt):

```
vkmax @ git+https://github.com/nsdkinx/vkmax@c67a5097ac3e565dfb5d2448f9b17aff7f92a596
```

## 12. Вердикты (YES/NO)

| Вопрос | Вердикт | Основание |
| --- | --- | --- |
| Эмуляция «печатает…» поддерживается? | **NO** | Ни одной функции/opcode статуса набора в пакете нет: grep `typing|set_status` по всем *.py — только импорты модуля `typing`; docs/opcodes.md такого метода не содержит. ADR-3a (задача 6): typing не имплементировать |
| Внутренняя реконнект-логика? | **NO** (условное) | Автопереподключения нет. Есть ТОЛЬКО hook `set_reconnect_callback(fn)` (`client.py:119-122`), который библиотека вызывает при обрыве recv-loop (`client.py:134-141`), таймауте keepalive (`client.py:183-187`) или обрыве в момент send (`client.py:94-102`) — но сама НЕ переподключается. Без callback: обрыв при залогине = тихая смерть recv-loop (`client.py:138-141`). Реконнект-цикл реализует адаптер (задача 5) — либо через callback, либо внешним циклом; дублировать не нужно только hook, не логику |
| Windows / ProactorEventLoop | **YES — работает на дефолтном ProactorEventLoop** | Runtime-доказательство (evidence-лог): policy `_WindowsProactorEventLoopPolicy`, loop `ProactorEventLoop`; `websockets.connect()` к закрытому порту даёт `ConnectionRefusedError` (симптом несовместимости — NotImplementedError — отсутствует). websockets 17.x использует современный asyncio-API без selector-требований; в Python 3.14 Proactor даже получил add_reader/add_writer. SelectorEventLoop НЕ требуется; `asyncio.new_event_loop()` в потоке адаптера (задача 4) оставляем дефолтным |

## 13. Выбранные имена для адаптера (фиксация для задач 3–7)

| Понятие адаптера | Контрактное имя |
| --- | --- |
| фабрика клиента (monkeypatch-точка) | `Max.main._build_client(token, device_id)` → `vkmax.client.MaxClient()` |
| логин | `await client.login_by_token(token, device_id)` (все вызовы под `asyncio.wait_for`) |
| свой id | `login_response["payload"]["profile"]["id"]` → holder.my_id |
| регистрация приёма | `client.set_packet_callback(async cb(client, packet))` — синхронно |
| фильтр входящих | `packet["opcode"] == 128 and packet["payload"]["chatId"] == chat_id and packet["payload"]["message"]["sender"] != my_id` → `packet["payload"]["message"]["text"]` |
| отправка | `vkmax.functions.messages.send_message(client, chat_id, text)` (op 64) |
| реконнект | собственный цикл адаптера: новый `MaxClient()` → `connect()` → `login_by_token`; hook `set_reconnect_callback` НЕ использовать (упрощение отладки) |
| остановка | идемпотентная обёртка вокруг `await client.disconnect()` (см. §6) |
| discovery чатов | `resp["payload"]["chats"]` из логина; резерв `resolve_channel_id` (op 48) — см. §10 |
