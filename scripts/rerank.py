#!/usr/bin/env python3
"""Пересортировка выдачи кросс-энкодером.

Зачем это поверх готового поиска. bge-m3 кодирует запрос и чанк по отдельности,
они друг друга никогда не видят — вектор чанка один и тот же для всех запросов.
Кросс-энкодер читает пару целиком и отвечает на вопрос «этот кусок отвечает
именно на этот запрос?». Он на порядок дороже, поэтому применяется не к корпусу,
а к сотне кандидатов, которых уже отобрал быстрый поиск.

Модель качается с Hugging Face при первом запуске (~2,2 ГБ в ~/.cache).
Прямой запуск — самопроверка на пáрах с заведомо известным ответом:

  .venv/bin/python scripts/rerank.py
"""
import os
import sys
import threading
import time

MODEL = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
# Чанк — до ~2600 символов, по-русски это примерно 900–1300 токенов XLM-R.
# 1024 накрывает почти весь чанк; обрезка идёт с конца длинной пары.
MAX_LEN = int(os.environ.get("RERANK_MAX_LEN", 1024))
BATCH = int(os.environ.get("RERANK_BATCH", 16))

_state: dict = {}
# Лок нужен потому, что второй ярус будет звать поиск из нескольких потоков:
# без него два первых запроса одновременно увидят пустой _state и оба потянут
# модель на 2,2 ГБ. Ошибки при этом не будет — просто вдвое больше памяти и
# времени, то есть очередной тихий перерасход, который никто не заметит.
_load_lock = threading.Lock()
# Форвард тоже сериализуем: потокобезопасность MPS-бэкенда не проверена, а
# цена — единицы миллисекунд на фоне 41 мс счёта.
_infer_lock = threading.Lock()


def load():
    """Модель и токенизатор — один раз на процесс. Загрузка занимает секунды,
    поэтому греть её на каждый запрос нельзя, а держать глобально дёшево."""
    if _state:
        return _state
    import torch                                     # noqa: PLC0415
    from transformers import (                       # noqa: PLC0415
        AutoModelForSequenceClassification, AutoTokenizer)

    with _load_lock:
        if _state:                     # пока ждали лок, другой поток успел
            return _state
        device = os.environ.get("RERANK_DEVICE") or (
            "mps" if torch.backends.mps.is_available() else "cpu")
        dtype = torch.float16 if device == "mps" else torch.float32

        t0 = time.time()
        tok = AutoTokenizer.from_pretrained(MODEL)
        model = AutoModelForSequenceClassification.from_pretrained(
            MODEL, dtype=dtype).to(device).eval()
        _state.update(tok=tok, model=model, device=device, torch=torch,
                      load_sec=time.time() - t0)
    return _state


def scores(query: str, docs: list[str], batch: int = BATCH) -> list[float]:
    """Балл релевантности на каждый документ. Шкала — сырой логит, примерно
    от −11 до +11; сравнивать её можно только внутри одного запроса, между
    запросами она не калибрована. Порядок ответа совпадает с порядком docs."""
    if not docs:
        return []
    st = load()
    torch, out = st["torch"], []
    with torch.inference_mode():
        for i in range(0, len(docs), batch):
            part = docs[i:i + batch]
            enc = st["tok"]([query] * len(part), part, padding=True,
                            truncation=True, max_length=MAX_LEN,
                            return_tensors="pt").to(st["device"])
            with _infer_lock:
                raw = st["model"](**enc).logits
            # У bge-reranker одна голова, но MODEL берётся из переменной
            # окружения: подставь модель с двумя метками — и view(-1) вернёт
            # вдвое больше чисел, порядок поедет, а ошибки не будет. Берём
            # последний столбец («релевантно») и сверяем количество.
            logits = (raw.view(-1) if raw.shape[-1] == 1 else raw[:, -1]).float()
            if logits.numel() != len(part):
                raise RuntimeError(
                    f"модель вернула {logits.numel()} баллов на {len(part)} пар "
                    f"— форма логитов {tuple(raw.shape)}, сортировать нечем")
            # Проверяем результат, а не принимаем на веру: fp16 на MPS может
            # дать NaN, и тогда сортировка молча превратится в случайную —
            # выдача останется правдоподобной на вид. Падаем сразу.
            if not torch.isfinite(logits).all():
                raise RuntimeError(
                    f"кросс-энкодер вернул NaN/inf в батче {i // batch} "
                    f"({st['device']}, {st['model'].dtype}) — считать нечем")
            out.extend(logits.tolist())
    return out


def rerank(query: str, docs: list[str], batch: int = BATCH) -> list[int]:
    """Индексы docs в порядке убывания релевантности."""
    sc = scores(query, docs, batch)
    return sorted(range(len(docs)), key=lambda i: sc[i], reverse=True)


def main() -> int:
    """Самопроверка: правильный ответ должен обойти правдоподобный мусор."""
    q = "когда мы познакомились"
    docs = [
        "Купила молоко и хлеб, стою в очереди на кассе.",
        "Мы с тобой первый раз встретились на том концерте в апреле, "
        "ты тогда ещё опоздала на полчаса.",
        "Познакомь меня со своей сестрой как-нибудь.",
    ]
    st = load()
    print(f"{MODEL} · {st['device']} · {st['model'].dtype} · "
          f"загрузка {st['load_sec']:.1f} с")
    t0 = time.time()
    sc = scores(q, docs)
    print(f"счёт за {time.time() - t0:.2f} с\n")
    order = sorted(range(len(docs)), key=lambda i: sc[i], reverse=True)
    for place, i in enumerate(order, 1):
        print(f"[{place}] {sc[i]:+7.3f}  {docs[i][:70]}")
    ok = order[0] == 1
    print("\n" + ("OK: правильный ответ первый" if ok
                  else "ПЛОХО: правильный ответ не первый"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
