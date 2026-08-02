# vk-archive

Личный архив данных ВКонтакте → поисковая база по всей переписке.

> *EN: Turns your VK (VKontakte) GDPR export into a locally searchable
> PostgreSQL database — hybrid full-text + vector search with cross-encoder
> reranking, local Whisper transcription of voice messages, photo OCR via
> macOS Vision. Everything runs on your machine. Docs are in Russian, like
> the archives this tool is built for.*

Набор скриптов, который превращает GDPR-экспорт VK (голый HTML) в
PostgreSQL с гибридным поиском: полнотекстовым, векторным и с
пересортировкой кросс-энкодером. Голосовые расшифровываются локально
через Whisper, текст с фотографий распознаётся встроенным в macOS Vision.
Всё считается на своей машине: наружу не уходит ничего, кроме запросов
к серверам VK за собственными вложениями.

> **Это инструмент для собственного архива.** Он работает с вашим
> экспортом и ничего не скачивает за вас. В репозитории нет ни данных, ни
> имён, ни идентификаторов: `peer_id` в примерах — выдуманные, всё
> содержимое лежит в `data/`, а этот каталог целиком в `.gitignore`.
> Помните, что в переписке есть и вторая сторона: её сообщения — такие же
> персональные данные, и распоряжаться ими стоит соответственно. И действуйте
> в рамках [правил VK](https://vk.com/terms): инструмент рассчитан только на
> ваш собственный экспорт и ваши собственные вложения.

Нужны Python 3.12, Docker (под базу) и [Ollama](https://ollama.com)
для эмбеддингов. Зависимости разнесены по трём файлам, чтобы не тянуть
лишнее: [requirements.txt](requirements.txt) — база,
[requirements-macos.txt](requirements-macos.txt) — анализ фотографий,
[requirements-rerank.txt](requirements-rerank.txt) — пересортировка выдачи.

**Две фазы работают не везде и требуют внешнего.** Импорт, чанки,
эмбеддинги и поиск запускаются на любой системе; а вот:

- **анализ фотографий** — только macOS: OCR, сцена и лица считаются
  системным фреймворком Vision;
- **транскрипция голосовых** — только Apple Silicon, и вдобавок нужен
  сервис whisper на MLX, которого в этом репозитории нет. Скрипты ждут
  обёртку, принимающую `.ogg` и кладущую рядом `.txt`; путь к ней
  задаётся переменной `VK_TRANSCRIBE_SH` (по умолчанию
  `~/.claude/scripts/transcribe.sh`), ярлык launchd-сервиса —
  `VK_WHISPER_LABEL`. Без своей обёртки транскрипция упадёт на первом
  же файле; всё остальное при этом работает.

Секреты в репозитории ищет [gitleaks](https://github.com/gitleaks/gitleaks) —
конфигурация в [.gitleaks.toml](.gitleaks.toml), в CI прогон идёт по всей
истории коммитов:

```bash
brew install gitleaks
gitleaks dir . --config .gitleaks.toml     # текущие файлы
gitleaks git . --config .gitleaks.toml     # вся история
```

## North Star

Собрать сетап, который позволяет быстро находить любую информацию из переписок,
сообществ, стены и т.д. и восстанавливать данные биографии: хронологию жизни,
прошлые переживания, разговоры. Пример целевого запроса: *«восстанови хронологию
моей жизни за такие-то годы»* — и ответ собирается из всех имеющихся данных.

## Что имеем

Порядки величин ниже — с того архива, на котором всё это писалось и мерилось.
У вашего они будут другими; здесь они только затем, чтобы было видно, на какой
масштаб рассчитаны скрипты. GDPR-экспорт VK — это один zip на сотню-другую
мегабайт, внутри голый HTML:

- порядка миллиона сообщений в тысяче с лишним диалогов, полтора десятка лет
- сотня тысяч фото-вложений и десяток тысяч видео — в архиве не файлы,
  а только ссылки на серверы VK
- стена, фото-альбомы, лайки, профиль, комментарии

Подробности: [docs/data-inventory.md](docs/data-inventory.md), формат: [docs/archive-format.md](docs/archive-format.md).

## Уже сделано

- ✅ **Голосовые спасены**: качалка вытянула их почти все — доли процента
  отдают 504/404 и возвращаются только новым экспортом. Тянуть надо не
  откладывая: ссылки в архиве живут не вечно
- ✅ **Всё в PostgreSQL + pgvector** (docker, порт 5433): сообщения, вложения,
  транскрипты, словари, промпты. Русский полнотекстовый поиск работает
- ✅ **Голосовые расшифрованы**: несколько тысяч записей, десятки часов аудио —
  личные переписки и те беседы, где хозяин архива реально участвовал.
  Настоящих срывов и галлюцинаций 0
- ✅ **Удалённые аккаунты опознаны**: VK стирает имена везде («DELETED»), но
  собеседник восстанавливается по тексту переписки — с цитатами-основаниями
  в `dialogs.meta`
- ✅ **Экспорт в md/txt** с разбивкой по размеру файла

## Как пользоваться

Сам архив запрашивается в настройках VK ([инструкция](https://vk.com/faq18145));
ссылка на скачивание приходит, когда VK его соберёт. Распакуйте экспорт в
`data/raw/` и задайте своё имя — им подписаны собственные реплики внутри
чанка, то есть оно уходит в текст и в эмбеддинг. Задавать **до** первой
сборки чанков: смена имени потом означает пересборку и пересчёт векторов.

```bash
export VK_OWN_NAME="Ваше имя"                      # по умолчанию «Я»
export VK_OWN_NICKNAMES="прозвище,ник"             # identify_deleted.py:
                                                   # обращения к себе — не улика

docker compose -f db/docker-compose.yml up -d      # поднять базу
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python scripts/db_import.py all           # архив → БД (идемпотентно)
.venv/bin/python scripts/export_chat.py --list      # какие диалоги есть
.venv/bin/python scripts/export_chat.py --peer 123456789 --max-size 2

# голосовые: собрать ссылки вложений в манифест и скачать файлы (resume, retry)
python3 scripts/build_media_manifest.py
python3 scripts/download_media.py --kind voice

# транскрипция: собрать очередь диалогов и прогнать её (resume-safe)
.venv/bin/python scripts/build_queue.py             # посмотреть, кого возьмём
.venv/bin/python scripts/build_queue.py --write
nohup .venv/bin/python scripts/run_queue.py &       # прогон с контролем качества
python3 scripts/find_bad_transcripts.py             # разбор подозрительных

# один диалог мимо очереди
nohup python3 scripts/watchdog_transcribe.py --peer <peer_id> &

# опознать собеседников из удалённых аккаунтов
.venv/bin/python scripts/identify_deleted.py --min-msgs 500
.venv/bin/python scripts/identify_deleted.py --all --write

# векторный поиск: нарезать чанки → посчитать эмбеддинги → искать
.venv/bin/python scripts/build_chunks.py --peer 123456789        # прикинуть
.venv/bin/python scripts/build_chunks.py --all --write
ollama pull bge-m3
nohup .venv/bin/python scripts/embed_chunks.py --all --batch 32 &
.venv/bin/python scripts/embed_chunks.py --stats

.venv/bin/python scripts/search.py "переезд в другой город"
.venv/bin/python scripts/search.py "поиск работы" --from 2014 --to 2016
# границы включающие на любой гранулярности: --to 2016 берёт весь 2016 год,
# --to 2016-05 — весь май, --to 2016-05-10 — вместе с десятым числом.
# То же в export_chat.py
# анализ фотографий: скачать → OCR + сцена + лица (macOS Vision) → БД
.venv/bin/pip install -r requirements-macos.txt
.venv/bin/python scripts/analyze_photos.py --limit 500           # пилот
nohup caffeinate -i .venv/bin/python -u scripts/analyze_photos.py \
      --dialog-kind user --workers 12 &                          # личные, ~1 ч
.venv/bin/python scripts/analyze_photos.py --stats
# --discard удаляет картинку сразу после анализа: данные в БД остаются,
# место не растёт. Пауза — touch data/processed/photos/PAUSE

# распознанный на фото текст — в поиск: чанк на каждую осмысленную картинку
.venv/bin/python scripts/build_photo_chunks.py --all --sample 5   # посмотреть
.venv/bin/python scripts/build_photo_chunks.py --all --write      # чанк на фото
.venv/bin/python scripts/embed_chunks.py --all                    # ~14 чанков/с
.venv/bin/python scripts/search.py "скриншот переписки" --source photo
# замер: --source messages = корпус без фото, --photo-cases = ответ только на фото
.venv/bin/python scripts/eval_search.py --photo-cases

.venv/bin/python scripts/search.py "билеты" --mode fts         # сравнить режимы
.venv/bin/python scripts/eval_search.py --verbose              # замер качества

# пересортировка кросс-энкодером: точнее (MRR 0.402 → 0.592), но +4 с на запрос
.venv/bin/pip install -r requirements-rerank.txt               # torch + transformers
.venv/bin/python scripts/rerank.py                             # самопроверка, качает 2,2 ГБ
.venv/bin/python scripts/search.py "как мы познакомились" --rerank
.venv/bin/python scripts/eval_search.py --rerank               # замер с реранком
```

Пауза прогона — `touch data/processed/transcription/PAUSE`, снять — удалить файл.
Мягкая остановка — `touch .../STOP`.

## Структура проекта

```
vk-archive/
├── README.md
├── PLAN.md                  # план работ по фазам
├── requirements.txt         # база; -macos и -rerank ставятся сверх неё
├── db/
│   ├── schema.sql           # PostgreSQL + pgvector, 10 таблиц
│   └── docker-compose.yml   # база на 127.0.0.1:5433
├── docs/
│   ├── archive-format.md    # формат экспорта VK (проверен на живом экспорте)
│   ├── data-inventory.md    # что вообще лежит в экспорте и в каком объёме
│   ├── db-schema.md         # схема БД и гибридный поиск
│   └── transcription-settings.md  # версии настроек whisper, что перепрогонять
├── scripts/
│   ├── common.py                  # общее: имя, прозвища, DSN — всё через env
│   ├── build_media_manifest.py    # URL всех вложений → manifest JSONL
│   ├── download_media.py          # качалка медиа (resume, retry)
│   ├── analyze_participation.py   # разметка диалогов по участию (useless)
│   ├── db_import.py               # архив → PostgreSQL (идемпотентно)
│   ├── transcribe_voice.py        # транскрипция ГС: промпты Sonnet + whisper
│   ├── watchdog_transcribe.py     # сторож прогона: рестарт, память, синк БД
│   ├── build_queue.py             # отбор диалогов в очередь транскрипции
│   ├── run_queue.py               # прогон очереди + контроль качества
│   ├── identify_deleted.py        # опознание собеседников из «DELETED»
│   ├── build_chunks.py            # нарезка переписки на чанки для вектора
│   ├── embed_chunks.py            # эмбеддинги чанков (ollama bge-m3) → pgvector
│   ├── search.py                  # поиск: fts / vector / гибрид по RRF
│   ├── rerank.py                  # пересортировка выдачи кросс-энкодером
│   ├── analyze_photos.py          # фото → OCR + сцена + лица (macOS Vision) → БД
│   ├── build_photo_chunks.py      # текст с фотографий → чанки для поиска
│   ├── eval_search.py             # замер качества поиска на контрольных запросах
│   ├── find_bad_transcripts.py    # контроль качества расшифровок
│   ├── label_settings.py          # версии настроек транскрипции
│   ├── export_chat.py             # экспорт диалога в md/txt с разбивкой
│   └── export_dialog_text.py      # простой текстовый дамп периода
└── data/                    # ЛИЧНЫЕ ДАННЫЕ — не коммитить (в .gitignore)
    ├── raw/                 # распакованный архив VK
    ├── media/voice/         # скачанные голосовые (.ogg)
    ├── db/                  # том PostgreSQL
    └── processed/           # manifest, транскрипции, экспорты
```

## Ближайший шаг

Фаза 3 из [PLAN.md](PLAN.md). Корпус нарезан и целиком уложен в вектора:
переписка плюс распознанный на фотографиях текст, модель **bge-m3** локально
через ollama, гибридный поиск FTS + косинус со слиянием по RRF. На контрольных
запросах MRR гибрида 0.402, с пересортировкой кросс-энкодером 0.592 — против
0.028 у чистого полнотекста. Осталось построить второй ярус — помесячные
дайджесты, без которых запрос вида «восстанови хронологию за такие-то годы»
не собирается никаким top-k.

## Правила

- Всё в `data/` — персональные данные: никогда не коммитить, не выкладывать, не
  копировать в другие репозитории.
- HTML архива в кодировке **windows-1251** — читать через `decode('cp1251')`.
- macOS BSD `grep` считает эти файлы бинарными и молча пропускает кириллицу —
  для поиска по архиву использовать Python, а не grep.

## Лицензия и статус

Код — под лицензией [MIT](LICENSE). Модели, которые скрипты скачивают сами
(bge-m3, bge-reranker-v2-m3, whisper), в репозиторий не входят и живут под
собственными пермиссивными лицензиями (MIT / Apache-2.0).

Это личный инструмент, опубликованный «как есть»: issues и идеи
приветствуются, но обещаний по срокам и поддержке нет.
