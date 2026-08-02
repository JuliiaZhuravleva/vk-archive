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
