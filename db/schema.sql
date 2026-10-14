-- vk-archive: PostgreSQL + pgvector
-- Всё содержимое БД — персональные данные; инстанс только на 127.0.0.1.

CREATE EXTENSION IF NOT EXISTS vector;

-- ============ диалоги ============
-- Источники: index-messages.html + participation.json
CREATE TABLE dialogs (
  peer_id     bigint PRIMARY KEY,          -- >0 user, <0 community, 2e9+ chat
  name        text NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('user', 'chat', 'community')),
  useless     boolean NOT NULL DEFAULT false,  -- своих сообщений нет — пропускаем
  total_msgs  integer,
  own_msgs    integer,                     -- своих сообщений
  own_share   real,
  voice_total integer,
  voice_own   integer,
  years       int[],                       -- годы активности
  meta        jsonb NOT NULL DEFAULT '{}'
);

-- ============ сообщения ============
-- Источник: messages/<peer>/messages*.html (cp1251)
CREATE TABLE messages (
  id        bigserial PRIMARY KEY,
  peer_id   bigint NOT NULL REFERENCES dialogs,
  vk_msg_id bigint NOT NULL,               -- data-id; уникален внутри диалога
  sent_at   timestamptz,                   -- из «27 дек 2018 в 0:58:19», МСК
  sender    text,                          -- имя из экспорта; для своих — 'Вы'
  is_own    boolean NOT NULL DEFAULT false,
  text      text NOT NULL DEFAULT '',
  fwd_json  jsonb,                         -- прикреплённые/пересланные (свёрнуто)
  page_file text,                          -- messages1400.html — трассировка к HTML
  UNIQUE (peer_id, vk_msg_id)
);
CREATE INDEX messages_peer_time_idx ON messages (peer_id, sent_at);
CREATE INDEX messages_time_idx ON messages (sent_at);

-- полнотекст: русская конфигурация; ищет и по тексту сообщений
ALTER TABLE messages ADD COLUMN tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED;
CREATE INDEX messages_tsv_idx ON messages USING gin (tsv);

-- ============ вложения ============
-- Источники: kludges в HTML + media_manifest.jsonl + downloads.log.jsonl
CREATE TABLE attachments (
  id          bigserial PRIMARY KEY,
  message_id  bigint NOT NULL REFERENCES messages ON DELETE CASCADE,
  att_type    text NOT NULL,               -- «Фотография», «Файл», ...
  kind        text,                        -- voice | photo | other (классификация)
  url         text,
  local_path  text,                        -- data/media/voice/<sha1-16>.ogg и т.п.
  download_ok boolean                      -- NULL = не качали
);
CREATE INDEX attachments_kind_idx ON attachments (kind);

-- ============ батчи промптов (Sonnet/период) ============
-- Источник: data/processed/transcription/batches/*.json
CREATE TABLE prompt_batches (
  key           text PRIMARY KEY,          -- <peer>-<first_msg>-<last_msg>
  peer_id       bigint REFERENCES dialogs,
  batch_date    text,                      -- дата первого ГС батча (как в экспорте)
  context       text,                      -- фрагмент переписки, ушедший в Sonnet
  prompt        text NOT NULL,
  prompt_source text,                      -- sonnet/slim | haiku/... | period | fallback
  new_terms     jsonb NOT NULL DEFAULT '[]',
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- ============ транскрипты голосовых ============
-- Источник: transcription/transcripts/*.txt + *.meta.json
CREATE TABLE voice_transcripts (
  attachment_id  bigint PRIMARY KEY REFERENCES attachments,
  message_id     bigint NOT NULL REFERENCES messages,
  duration_s     real,
  transcript     text,
  empty_audio    boolean NOT NULL DEFAULT false, -- шум/музыка: whisper дал пусто
  whisper_prompt text,
  batch_key      text REFERENCES prompt_batches,
  whisper_model  text,
  wall_s         real,
  settings       text,      -- версия настроек (v1-baseline … v5-…), см.
                            -- docs/transcription-settings.md — чтобы знать,
                            -- что перепрогнать при улучшении качества
  max_repeat     int,       -- макс. одинаковых слов подряд: маркер срыва
  suspect        boolean NOT NULL DEFAULT false,
  boilerplate    boolean NOT NULL DEFAULT false, -- транскрипт целиком —
                            -- «водяной знак» Whisper («Субтитры делал…»),
                            -- см. BOILERPLATE в db_import.py
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX voice_transcripts_settings_idx ON voice_transcripts (settings);
ALTER TABLE voice_transcripts ADD COLUMN tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('russian', coalesce(transcript, ''))) STORED;
CREATE INDEX voice_transcripts_tsv_idx ON voice_transcripts USING gin (tsv);

-- ============ анализ фотографий ============
-- Источник: scripts/analyze_photos.py — macOS Vision (OCR + классификатор
-- сцены + детектор лиц), всё на устройстве. Строка пишется и при неудаче:
-- мёртвая ссылка — это тоже факт, который надо запомнить, а не потерять.
-- Ссылки VK смертны (несколько десятков голосовых уже не вернуть), поэтому здесь дублируются
-- url и всё, что помогает опознать и восстановить файл потом.
CREATE TABLE photo_analysis (
  attachment_id bigint PRIMARY KEY REFERENCES attachments,
  message_id    bigint NOT NULL REFERENCES messages,
  status        text NOT NULL,           -- ok | http_error | net_error |
                                         --   html_stub | unreadable
  error         text,
  url           text,                    -- копия на момент анализа
  url_size      text,                    -- «898x1035» из параметра size= в URL
  bytes         int,
  sha256        text,                    -- опознание файла и поиск дублей
  content_type  text,
  headers       jsonb NOT NULL DEFAULT '{}',  -- Last-Modified, ETag: для восстановления
  width         int,
  height        int,
  ocr_text      text,
  ocr_conf      real,
  ocr_level     text,                    -- accurate | fast
  labels        jsonb NOT NULL DEFAULT '[]',  -- [{"id":"screenshot","conf":0.96}, ...]
  n_faces       smallint,
  kept_file     boolean NOT NULL DEFAULT false,
  analyzed_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX photo_analysis_status_idx ON photo_analysis (status);
CREATE INDEX photo_analysis_labels_idx ON photo_analysis USING gin (labels);
CREATE INDEX photo_analysis_sha_idx ON photo_analysis (sha256);
ALTER TABLE photo_analysis ADD COLUMN tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('russian', coalesce(ocr_text, ''))) STORED;
CREATE INDEX photo_analysis_tsv_idx ON photo_analysis USING gin (tsv);

-- ============ словарь терминов ============
-- Источник: transcription/vocab/<peer>.md (ручной посев + автосбор Sonnet)
CREATE TABLE vocab_terms (
  id          bigserial PRIMARY KEY,
  peer_id     bigint REFERENCES dialogs,   -- NULL = общий для всех диалогов
  term        text NOT NULL,
  gloss       text,                        -- пояснение
  source      text NOT NULL CHECK (source IN ('manual', 'sonnet')),
  first_batch text,                        -- где впервые встретился
  first_date  text,
  UNIQUE (peer_id, term)
);

-- ============ периоды диалога ============
-- Источник: vocab/<peer>-periods.md (описание тем + готовый whisper-промпт)
CREATE TABLE dialog_periods (
  peer_id        bigint NOT NULL REFERENCES dialogs,
  year_from      int NOT NULL,
  description    text,
  whisper_prompt text,
  PRIMARY KEY (peer_id, year_from)
);

-- ============ мета-анализ / дайджесты ============
-- Всё, что генерят LLM поверх сырья: тематические дайджесты периодов,
-- таймлайны, саммари диалогов, наблюдения для биографии
CREATE TABLE analyses (
  id         bigserial PRIMARY KEY,
  peer_id    bigint REFERENCES dialogs,    -- NULL = кросс-диалоговый анализ
  kind       text NOT NULL,                -- topic-digest | timeline | summary | ...
  span       daterange,                    -- период, который покрывает анализ
  content    text NOT NULL,                -- markdown
  model      text,
  meta       jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX analyses_kind_idx ON analyses (kind, peer_id);

-- ============ чанки для векторного поиска ============
-- Окно сообщений диалога (границы по дням/паузам), текст = склейка
-- «sender: text» с подстановкой транскриптов вместо [голосовое].
-- Размерность 1024 = multilingual-e5-large / bge-m3; сменится модель —
-- новая колонка/таблица + перезаливка (embedding хранит только один вариант).
CREATE TABLE chunks (
  id         bigserial PRIMARY KEY,
  peer_id    bigint NOT NULL REFERENCES dialogs,
  msg_from   bigint NOT NULL,              -- vk_msg_id первого сообщения окна
  msg_to     bigint NOT NULL,
  -- 0 для обычного окна. Реплика длиннее лимита (двадцатиминутное голосовое
  -- даёт 12 тыс. символов) режется на части 1..N — в одном векторе такой
  -- монолог превращается в кашу и не находится по конкретному упоминанию.
  part       smallint NOT NULL DEFAULT 0,
  ts_from    timestamptz,
  ts_to      timestamptz,
  n_messages int,
  text       text NOT NULL,
  embedding  vector(1024),
  emb_model  text,
  meta       jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX chunks_embedding_idx ON chunks
  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_peer_time_idx ON chunks (peer_id, ts_from);

-- Гибридный поиск: та же русская конфигурация, что и у сообщений. FTS даёт
-- точное совпадение слов, косинус — смысл; вместе они закрывают дыры друг
-- друга, а результаты сливаются по RRF (см. scripts/search.py).
ALTER TABLE chunks ADD COLUMN tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED;
CREATE INDEX chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX chunks_meta_idx ON chunks USING gin (meta jsonb_path_ops);

-- Откуда взялся чанк. 'messages' — нарезка переписки (build_chunks.py),
-- 'photo' — текст, распознанный на фотографии (build_photo_chunks.py).
-- Разделение обязательно по двум причинам: у фото своя семантика ключа
-- (part = порядковый номер фото в сообщении, а не часть длинной реплики) и,
-- главное, build_chunks.py убирает из диалога чанки, которых нет в новой
-- нарезке, — без фильтра по source любая пересборка диалога стирала бы все
-- фото-чанки вместе с их эмбеддингами.
ALTER TABLE chunks ADD COLUMN source text NOT NULL DEFAULT 'messages'
  CHECK (source IN ('messages', 'photo'));

-- Границы окна — естественный ключ чанка. Без него повторная нарезка
-- диалога плодила бы дубли или требовала сносить уже посчитанные векторы.
CREATE UNIQUE INDEX chunks_span_part_idx
  ON chunks (peer_id, msg_from, msg_to, part, source);

-- ============ хронология жизни ============
-- То, ради чего всё остальное и собиралось: события биографии, добытые
-- «раскопками» по переписке. Таблица отличается от analyses тем, что там
-- лежит СГЕНЕРИРОВАННЫЙ текст (дайджест диалога, таймлайн периода), а здесь —
-- отдельный установленный факт с цитатами-основаниями. Дайджест переписывается
-- при смене модели целиком; событие однажды найдено и больше не теряется.
--
-- Ключ — slug, а не (title, дата): дата у события обычно уточняется по мере
-- находок («осень 2018» → «12.10.2018»), и привязка к ней означала бы дубль
-- при каждом уточнении.
CREATE TABLE life_events (
  id         bigserial PRIMARY KEY,
  slug       text UNIQUE NOT NULL,         -- стабильный ключ события
  title      text NOT NULL,
  kind       text NOT NULL,                -- media | relationship | work | move |
                                           -- health | education | project | other
  -- Границы события. Обе даты — левый край своего периода: «осень 2018»
  -- пишется как 2018-09-01 с precision='month'. Смотреть на дату без
  -- precision нельзя: 2018-01-01/'year' — это «когда-то в 2018», а не 1 января.
  started_on date,
  ended_on   date,
  -- Точность у каждой границы своя, и это не педантизм: типичная находка —
  -- «началось где-то в октябре 2018, а вот закончилось 16.10.2019, вот цитата».
  -- Одна общая точность заставила бы либо огрублять точный конец, либо
  -- печатать 01.10.2018 как будто это наблюдение, а не левый край месяца.
  -- end_precision = NULL означает «как у начала».
  precision     text NOT NULL DEFAULT 'day'
                CHECK (precision IN ('day', 'month', 'year', 'unknown')),
  end_precision text CHECK (end_precision IN ('day', 'month', 'year', 'unknown')),
  -- Ровно та шкала, что и в остальной работе: verified — видно в архиве
  -- своими глазами, inferred — вывод из увиденного, assumed — догадка по
  -- косвенным признакам. Событие без источников не может быть verified.
  confidence text NOT NULL DEFAULT 'inferred'
             CHECK (confidence IN ('verified', 'inferred', 'assumed')),
  summary    text NOT NULL,
  details    text,
  tags       text[] NOT NULL DEFAULT '{}',
  -- Что осталось непроверенным и где искать дальше. Без этого поля повторная
  -- раскопка начинается с нуля: «искали и не нашли» неотличимо от «не искали».
  gaps       text,
  found_on   date,                         -- когда откопали
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX life_events_started_idx ON life_events (started_on);
CREATE INDEX life_events_tags_idx ON life_events USING gin (tags);

-- Основания. Цитата хранится копией, а не только ссылкой на сообщение:
-- переимпорт архива меняет messages.id, а вторая выгрузка VK вообще может
-- не отдать сообщение, по которому факт был установлен (см. README про
-- пропадающие разделы). Ссылка на (peer_id, vk_msg_id) при этом остаётся —
-- она переживает переимпорт, в отличие от messages.id.
CREATE TABLE life_event_sources (
  id        bigserial PRIMARY KEY,
  event_id  bigint NOT NULL REFERENCES life_events ON DELETE CASCADE,
  peer_id   bigint REFERENCES dialogs,
  vk_msg_id bigint,
  sent_at   timestamptz,
  is_own    boolean,
  role      text NOT NULL DEFAULT 'evidence'
            CHECK (role IN ('evidence', 'context', 'counter')),
  quote     text,
  note      text
);
CREATE INDEX life_event_sources_event_idx ON life_event_sources (event_id);

-- ============ утверждения и их уточнения ============
-- Промежуточный слой между дайджестом и событием хронологии. Модель,
-- прочитавшая кусок переписки, выписывает утверждения («в эти дни обсуждала
-- переезд»); каждое — со ссылками на сообщения-основания. Дешёвая модель
-- ошибается в деталях (путает, кто что сказал, выдаёт подозрение за факт), но
-- каркас событий находит, поэтому утверждения хранятся с низким уровнем и
-- уточняются попутно — когда этот же кусок переписки поднимает исследование.
--
-- Источник истины — JSONL на диске (data/processed/claims/), база его зеркалит
-- и пересобирается целиком (scripts/claims.py sync). Текущее состояние
-- утверждения — свёртка журнала claim_revisions; руками его не правят.
--
-- level — кто последним проверил, а не что модель сама о себе думает
-- (самооценка «прямо/косвенно» оказалась бесполезной: почти всё «прямо»,
-- ошибки тоже):
--   1 — черновик модели, никто не проверял;
--   2 — более сильная модель перечитала сообщения-основания;
--   3 — подтверждено или опровергнуто исследованием с цитатами;
--   4 — слово хозяина архива.
-- status — исход: open (не проверено или проверка ничего не дала), confirmed,
-- corrected (текст исправлен), refuted. Опровергнутое не удаляется: ошибка
-- модели тоже знание — видно, в чём именно она врёт.
CREATE TABLE claims (
  key           text PRIMARY KEY,           -- run + хэш содержания, стабилен
  run           text NOT NULL,              -- прогон, которым выписано
  origin        text NOT NULL,              -- модель или метод
  theme         text,
  peer_id       bigint REFERENCES dialogs,
  period_from   date,                       -- по датам сообщений-оснований
  period_to     date,
  original_text text NOT NULL,
  text          text NOT NULL,              -- текущая редакция
  level         smallint NOT NULL CHECK (level BETWEEN 1 AND 4),
  status        text NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'confirmed', 'corrected', 'refuted')),
  event_slug    text,                       -- если выросло в life_events
  n_revisions   int NOT NULL DEFAULT 0
);
CREATE INDEX claims_period_idx ON claims (peer_id, period_from, period_to);

CREATE TABLE claim_sources (
  claim_key text NOT NULL REFERENCES claims ON DELETE CASCADE,
  peer_id   bigint NOT NULL,
  vk_msg_id bigint NOT NULL,
  PRIMARY KEY (claim_key, peer_id, vk_msg_id)
);

-- Журнал уточнений: кто, когда, каким способом, с каким вердиктом и что
-- поменялось. verdict='unclear' — проверяли, но решить не смогли: уровень и
-- статус не меняются, но «смотрели и не поняли» отличимо от «не смотрели».
CREATE TABLE claim_revisions (
  claim_key  text NOT NULL REFERENCES claims ON DELETE CASCADE,
  seq        int NOT NULL,
  revised_at timestamptz NOT NULL,
  revised_by text NOT NULL,
  method     text,
  verdict    text NOT NULL
             CHECK (verdict IN ('confirmed', 'corrected', 'refuted', 'unclear')),
  old_level  smallint NOT NULL,
  new_level  smallint NOT NULL,
  old_status text NOT NULL,
  new_status text NOT NULL,
  old_text   text,
  new_text   text,
  note       text,
  ref        text,                          -- исследование, событие, документ
  PRIMARY KEY (claim_key, seq)
);
