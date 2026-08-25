# max-transport-module

Модуль транспорта [MAX](https://max.ru) для экосистемы
[CryptoLayer](https://github.com/igmunv/cryptolayer): обмен текстовыми сообщениями
между двумя людьми через **личные аккаунты MAX**, со сквозным шифрованием,
которое выполняет само ядро CryptoLayer.

Канал связи — **неофициальный пользовательский API** (WebSocket-протокол веб-клиента
web.max.ru) через библиотеку [vkmax](https://github.com/nsdkinx/vkmax).
Боты и Bot API не используются вообще (см. ADR-2). Официального юзерского API
у MAX нет, поэтому использование неофициального API **нарушает условия MAX**
и несёт риск блокировки аккаунта; используйте на свой риск (полная ремарка —
в [Max/README.md](Max/README.md)).

## Схема работы

```
приложение на CryptoLayer ──► ядро (шифрование, чанки, ACK, ретраи)
                                   │ текст
                                   ▼
                    модуль Max (этот репозиторий)
                      Sender  : очередь → паузы 2–6 c → vkmax send_message (op 64)
                      Listener: push op=128 → фильтр чата/отправителя → ingester
                                   │ WebSocket wss://ws-api.oneme.ru (vkmax)
                                   ▼
                            MAX (аккаунт web.max.ru)
```

Ядро CryptoLayer шифрует и режет данные на чанки ~100 байт; модуль лишь доставляет
текст в диалог собеседника и принимает входящие сообщения. Отправка искусственно
замедлена «человеческими» случайными паузами (по умолчанию 2–6 с) — цена незаметности:
сообщение ~1 КБ идёт минуты. Подробности: [Max/README.md](Max/README.md),
решения: [docs/DECISIONS.md](docs/DECISIONS.md).

## Подключение

Три способа, от простого к гибкому:

1. **Скопировать папку модуля** `Max/` в `src/modules/` вашего приложения
   на [cryptolayer-cli](https://github.com/igmunv/cryptolayer-cli). CLI сканирует
   каталог и подхватит модуль автоматически. Важно: после копии НЕ запускайте
   `./run.sh` / `git submodule update --init --recursive` — они сотрут скопированный
   модуль; зависимости доставляйте через `python3 src/modules/generate_reqs.py`
   и `pip install -r src/modules/common_requirements.txt`.
2. **PR в официальный сборник** [igmunv/cryptolayer-modules](https://github.com/igmunv/cryptolayer-modules):
   модуль станет доступен всем пользователям CLI как git-сабмодуль. Папка `Max/`
   уже соответствует конвенции сборника (`__init__.py` пустой, `main.py`,
   `requirements.txt`, `README.md`).
3. **Для своих оболочек**: `pip install git+https://github.com/<ваш-fork>/max-transport-module`
   и прямое использование:

   ```python
    from Max.main import Max

    module = Max()
    module.init(["<Token>", "<Device ID>", "", ""], user_id="<Chat ID>")
    # CryptoLayer(ui_provider, data_dir, module_class=module, password, wordcoder_dict)
    ```

Значения credentials (Token/ Device ID из LocalStorage web.max.ru, Chat ID
через `scripts/discover_chats.py` как Peer ID) —
пошаговая инструкция в [Max/README.md](Max/README.md).

## Почему vkmax

Из ~11 проверенных библиотек неофициального MAX API только vkmax проходит все
критерии одновременно:

| Критерий | vkmax | WebMax | PyMax | MadMax / mochensky / max-user-api | unosmm mobile-api | pyromax |
| --- | --- | --- | --- | --- | --- | --- |
| Живой проект (пуш недавно) | ✅ 2026-08-18 | ❌ тишина ~8 мес | ❌ архив 02.2026 | ❌ низкая активность | ❌ 2 коммита | ❌ alpha |
| Есть на PyPI (`pip install`) | ✅* | ❌ только git | — | — | — | — |
| Вход по токену из web.max.ru | ✅ | ✅ | ✅ | частично | ❌ мобильный реверс | ❌ alpha |
| Документация протокола (opcodes) | ✅ v11 | частично | — | — | — | — |

\* фактически ставится git-коммит (см. ADR-1): PyPI-wheel 1.0.2 расходится
с документированным API репозитория.

Полное обоснование, критерии реверса зависимости («что делать, если vkmax умрёт»)
и все остальные решения — в [docs/DECISIONS.md](docs/DECISIONS.md).

## Структура репозитория

- `Max/` — самодостаточная папка модуля (то, что копируется/поставляется);
- `tests/` — оффлайновые pytest-тесты на моках;
- `scripts/` — `smoke.py` (чеклист ручной проверки + живая отправка) и
  `discover_chats.py` (узнать Chat ID);
- `docs/` — [DECISIONS.md](docs/DECISIONS.md) (ADR) и
  [vkmax-contract.md](docs/vkmax-contract.md) (зафиксированный контракт библиотеки).

Разработка: см. [AGENTS.md](AGENTS.md) (окружение, команды, правила).
