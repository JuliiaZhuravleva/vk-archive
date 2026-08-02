# Версии настроек транскрипции

Каждый транскрипт помечен версией настроек, при которых он сделан — поле
`settings` в `transcripts/*.meta.json` и колонка `voice_transcripts.settings`
в БД. Нужно, чтобы прицельно перепрогонять старое после улучшений.

Новые файлы получают версию сразу (`SETTINGS_VERSION` в
`scripts/transcribe_voice.py`). Сделанные до появления поля размечены
**по времени файла** скриптом `scripts/label_settings.py` — это оценка,
у таких записей стоит `settings_inferred: true`.

## История версий (ночь 30–31.07.2026)

| Версия | С какого момента | Что изменилось | Качество |
|---|---|---|---|
| `v1-baseline` | начало прогона | temperature-fallback включён, утечка памяти MLX, промпты с опечатками из чата | срывы в повтор, галлюцинации-заготовки |
| `v2-leak-fixed` | 01:01:34 | `clear_cache()` в local-whisper — вылечена утечка памяти | качество то же, что v1 |
| `v3-temp0` | 01:34:59 | **`temperature=0`** в local-whisper | срывы в повтор исчезли, скорость ×3,7 |
| `v4-clean-prompts` | 01:36:40 | чистка соседних реплик от эмодзи/опечаток + требование правильной орфографии в промптах Sonnet | меньше искажений орфографии |
| `v5-boilerplate-filter` | 01:48:47 | фильтр заготовок «Субтитры сделал…», «Продолжение следует» в local-whisper | убраны выдуманные субтитровые фразы |

## Что стоит перепрогнать

**v1 и v2** (≈350 файлов) сделаны до ключевого фикса `temperature=0` — именно
они содержат срывы в повтор и большую часть галлюцинаций. Перепрогон:

```bash
python3 scripts/label_settings.py --reset v1-baseline v2-leak-fixed
# дальше обычный прогон сделает их заново, resume-safe
```

При скорости v3+ (≈9× реального времени) перепрогон 350 файлов — меньше часа.

**v3 и v4** отличаются от v5 только фильтром заготовок; его можно применить и
без перетранскрипции — фразы удаляются из готового текста.

## Контроль качества

```bash
python3 scripts/find_bad_transcripts.py          # показать брак
python3 scripts/find_bad_transcripts.py --reset  # отправить на перепрогон
```

Ищет два вида брака: срыв в повтор (одно слово 4+ раз подряд) и заготовки из
субтитров. В БД те же признаки — `max_repeat` и `suspect`.

Полезные запросы:

```sql
-- сколько чего и какого качества
SELECT settings, count(*), count(*) FILTER (WHERE suspect) AS подозрительных,
       round(avg(max_repeat), 2) AS ср_повтор
  FROM voice_transcripts GROUP BY settings ORDER BY settings;

-- кандидаты на перепрогон
SELECT m.vk_msg_id, v.duration_s, left(v.transcript, 80)
  FROM voice_transcripts v JOIN messages m ON m.id = v.message_id
 WHERE v.settings IN ('v1-baseline', 'v2-leak-fixed') AND v.suspect;
```
