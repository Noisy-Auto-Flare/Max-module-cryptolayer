# AGENTS.md — max-transport-module

## Назначение

Репозиторий модуля транспорта **Max** для [CryptoLayer](https://github.com/igmunv/cryptolayer):
обмен текстовыми сообщениями через личные аккаунты MAX по неофициальному
пользовательскому API (библиотека vkmax), шифрование выполняет ядро CryptoLayer.
Ботов и Bot API нет вообще (ADR-2). План работ: `.omo/plans/max-transport-module.md`
(не модифицировать).

## Карта файлов

- `Max/` — самодостаточная папка модуля (то, что поставляется):
  `__init__.py` (**обязан оставаться пустым**), `main.py` (весь модуль),
  `requirements.txt` (pinned-зависимость vkmax git-коммитом), `README.md`
  (инструкция пользователя, ToS-ремарка, troubleshooting).
- `tests/` — оффлайновые pytest-тесты на моках (сеть не используется).
- `scripts/` — dev-скрипты: `smoke.py` (чеклист ручной проверки + живая отправка,
  НЕ часть папки модуля), `discover_chats.py` (узнать Chat ID по токену).
- `docs/` — `DECISIONS.md` (ADR-1…4 + ADR-3a) и `vkmax-contract.md`
  (зафиксированный контракт библиотеки vkmax — источник истины для адаптера).
- `ruff.toml`, `requirements-dev.txt` — дев-инструменты.

## Окружение и установка

- Windows-хост; venv в `.venv`, запускать через `.venv\Scripts\python.exe`;
  Python 3.14.3 (требование >= 3.10).
- Дев-зависимости: `.venv\Scripts\pip install -r requirements-dev.txt`
  (pytest>=8, ruff).
- Интерфейс ядра: `.venv\Scripts\pip install git+https://github.com/igmunv/cryptolayer-module-interface.git`
  (даёт модуль `base_module`: классы `BaseModule`, `Credential`). Без него не
  импортируется ни код модуля, ни тесты.
- Рантайм-зависимость модуля ставится из `Max/requirements.txt`:
  `vkmax @ git+https://github.com/nsdkinx/vkmax@c67a5097ac3e565dfb5d2448f9b17aff7f92a596`
  (НЕ PyPI-wheel — см. contract.md §11).

## Команды

- Тесты: `.venv\Scripts\python.exe -m pytest -q`
- Линт: `.venv\Scripts\python.exe -m ruff check .`
- Smoke (ручной, владельцу): `python scripts/smoke.py --help`; живой discovery:
  `python scripts/discover_chats.py --token <TOKEN> --device-id <DEVICE_ID>`

## Interface pin

- cryptolayer-module-interface @ git+https://github.com/igmunv/cryptolayer-module-interface.git, commit `34cb4a4c079ae1326c3d65f89147f95898e1aa6c` (provides top-level module `base_module`: classes `BaseModule`, `Credential`)

## Правила

- **Секреты в git — никогда.** Токены (`__oneme_auth`), Device ID, сессии не
  логировать полностью и не коммитить (см. `.gitignore`); в скриптах токены
  маскируются до последних 4 символов.
- **vkmax-адаптер менять только по `docs/vkmax-contract.md`.** Все сигнатуры,
  схемы пакетов и вердикты (typing=NO, автопереподключение=NO, disconnect не
  идемпотентен, invoke_method без таймаута) зафиксированы там спайком задачи 2;
  расхождения с реальностью фиксируются в контракте, а не правятся вслепую в коде.
- **Конвенция папки модуля неизменна:** `Max/__init__.py` пустой, `main.py`,
  `requirements.txt`, `README.md` — так ожидает loader cryptolayer-cli
  (`importlib.import_module("Max.main")`) и сборник cryptolayer-modules.
- Чужие репозитории `cryptolayer*` из этого проекта не трогать (PR туда —
  отдельное решение владельца).
- Документация на русском, идентификаторы кода на английском; факты в docs —
  только из draft/contract/кода, ничего не выдумывать (в т.ч. не утверждать,
  что живая проверка проводилась, пока владелец не прогнал smoke/discovery).

## Ссылки

- Решения и их обоснования: [docs/DECISIONS.md](docs/DECISIONS.md)
- Контракт vkmax: [docs/vkmax-contract.md](docs/vkmax-contract.md)
- Инструкция пользователя: [Max/README.md](Max/README.md)
