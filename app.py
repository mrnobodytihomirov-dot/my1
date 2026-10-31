# ============================================================
# ПАРСЕР ОТЗЫВОВ APP STORE — веб-приложение на Streamlit
# Запуск:  pip install streamlit requests pandas
#          streamlit run app.py
# Важно: публичная лента Apple отдаёт не более 500 последних отзывов на страну.
# ============================================================
import re
import time
import random
from datetime import datetime, timezone, date

import requests
import pandas as pd
import streamlit as st

RSS = "https://itunes.apple.com/{cc}/rss/customerreviews/page={p}/id={app}/sortby=mostrecent/json"
LOOKUP = "https://itunes.apple.com/lookup"
MAX_PAGES, PAGE_SIZE = 10, 50          # предел ленты Apple: 10 страниц × 50 = 500
SOURCE_MAX = MAX_PAGES * PAGE_SIZE
DELAY, RETRIES, MAX_WAIT = 3.0, 4, 120  # пауза между запросами, повторы, макс. Retry-After
COLUMNS = ["rating", "date", "version", "title", "text"]

COUNTRY_NAMES = {
    "us": "США", "gb": "Великобритания", "de": "Германия", "fr": "Франция", "ru": "Россия",
    "it": "Италия", "es": "Испания", "nl": "Нидерланды", "pl": "Польша", "tr": "Турция",
    "ua": "Украина", "kz": "Казахстан", "by": "Беларусь", "ca": "Канада", "au": "Австралия",
    "br": "Бразилия", "mx": "Мексика", "in": "Индия", "jp": "Япония", "kr": "Южная Корея",
    "cn": "Китай", "ae": "ОАЭ", "se": "Швеция", "ch": "Швейцария", "at": "Австрия",
}


class StopCountry(Exception):
    """Ошибка, после которой сбор по стране прекращается."""


# ---------- Сеть ----------
def get(session, url, params=None, on_wait=None):
    """GET с повторами: 429/5xx/сеть — ждём (учитывая Retry-After), 403 — стоп."""
    problem = ""
    for attempt in range(RETRIES + 1):
        ra = None
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code in (200, 400, 404):
                return r
            if r.status_code == 403:
                raise StopCountry("HTTP 403 — Apple запретил доступ")
            if r.status_code != 429 and r.status_code < 500:
                raise StopCountry(f"неожиданный ответ HTTP {r.status_code}")
            problem, ra = f"HTTP {r.status_code}", r.headers.get("Retry-After")
        except (requests.Timeout, requests.ConnectionError) as e:
            problem = f"сеть: {type(e).__name__}"
        if attempt == RETRIES:
            break
        wait = float(ra) if ra and ra.isdigit() else 5 * 2 ** attempt + random.random()
        if wait > MAX_WAIT:
            raise StopCountry(f"Apple просит ждать {int(wait)} с — запросы остановлены")
        if on_wait:
            on_wait(f"ожидание {int(wait)} с ({problem})")
        time.sleep(wait)
    raise StopCountry(f"не удалось после {RETRIES + 1} попыток ({problem})")


# ---------- Разбор ----------
def extract_app_id(url):
    m = re.match(r"^https?://(?:apps|itunes)\.apple\.com/.*?id(\d{5,12})(?:[/?#].*)?$", (url or "").strip(), re.I)
    return m.group(1) if m else None


def iso(raw):
    """Дата в ISO 8601 (UTC); пустая строка, если даты нет."""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def lab(e, k):
    v = e.get(k)
    return (v.get("label") or "") if isinstance(v, dict) else ""


def parse(js):
    if not isinstance(js, dict) or "feed" not in js:
        raise StopCountry("неожиданный формат ответа")
    entries = js["feed"].get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    out = []
    for e in entries:
        if not isinstance(e, dict) or "im:rating" not in e:   # карточка приложения, не отзыв
            continue
        try:
            rating = int(lab(e, "im:rating"))
        except ValueError:
            continue
        if 1 <= rating <= 5:
            out.append({"id": lab(e, "id"), "rating": rating, "date": iso(lab(e, "updated")),
                        "version": lab(e, "im:version"), "title": lab(e, "title"), "text": lab(e, "content")})
    return out


# ---------- Сбор ----------
def lookup(session, app_id, cc):
    r = get(session, LOOKUP, {"id": app_id, "country": cc})
    if r.status_code != 200:
        return None
    try:
        res = r.json().get("results", [])
    except ValueError:
        raise StopCountry("Lookup API вернул не JSON")
    return next((x for x in res if str(x.get("trackId")) == app_id), None)


def collect(session, app_id, cc, limit, progress, status_line):
    """Собирает отзывы одной страны, обновляя прогресс-бар Streamlit."""
    target = min(limit, SOURCE_MAX)
    got, ids, note, status = [], set(), "", "ok"

    def show(p, s):
        progress.progress(min(len(got) / target, 1.0),
                          text=f"[{cc}] {len(got)}/{target} отзывов · стр. {p}/{MAX_PAGES} · {s}")

    try:
        for p in range(1, MAX_PAGES + 1):
            show(p, "сбор")
            r = get(session, RSS.format(cc=cc, p=p, app=app_id), on_wait=lambda s: show(p, s))
            if r.status_code == 400:
                note = "достигнут предел ленты Apple"; break
            if r.status_code == 404:
                note = "отзывов нет" if p == 1 else "страницы закончились"; break
            try:
                items = parse(r.json())
            except ValueError:
                raise StopCountry("неожиданный формат ответа")
            new = [x for x in items if not x["id"] or x["id"] not in ids][: target - len(got)]
            ids.update(x["id"] for x in new if x["id"])
            got += new
            if len(got) >= target:
                note = "достигнут лимит"; break
            if not new or len(items) < PAGE_SIZE:
                note = "данные закончились"; break
            time.sleep(DELAY)
        else:
            note = "получены все 10 страниц (предел Apple — 500)"
        show(MAX_PAGES if note.startswith("получены") else p, "завершено")
    except StopCountry as e:
        status, note = ("partial" if got else "error"), str(e)
        status_line.warning(f"[{cc}] {note}")
    return got, status, note


def dedupe(rows):
    seen, out = set(), []
    for x in rows:
        key = x["id"] or (x["rating"], x["date"], x["version"],
                          " ".join(x["title"].split()).lower(), " ".join(x["text"].split()).lower())
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out


def to_df(rows):
    df = pd.DataFrame(rows, columns=["id"] + COLUMNS)
    df["_dt"] = pd.to_datetime(df["date"].replace("", None), utc=True, errors="coerce")
    return df.sort_values("_dt", ascending=False, na_position="last")[COLUMNS].reset_index(drop=True)


# ============================================================
# ИНТЕРФЕЙС
# ============================================================
st.set_page_config(page_title="Отзывы App Store", page_icon="⭐", layout="wide")
st.title("⭐ Сбор отзывов Apple App Store")
st.caption("Источник — публичная RSS-лента Apple. Ограничение Apple: не более 500 последних отзывов "
           "на страну. Чтобы получить больше, выберите несколько стран.")

with st.form("params"):
    url = st.text_input("Ссылка на приложение",
                        placeholder="https://apps.apple.com/us/app/название/id123456789")
    countries = st.multiselect(
        "Страны", options=list(COUNTRY_NAMES), default=["us", "gb", "de", "fr"],
        format_func=lambda c: f"{c} — {COUNTRY_NAMES[c]}")
    extra = st.text_input("Другие коды стран через запятую (необязательно)", placeholder="fi, no, pt")
    limit = st.number_input("Максимум отзывов на страну", 1, 1000, 1000, step=50,
                            help="Apple фактически отдаёт не более 500")
    submitted = st.form_submit_button("🚀 Собрать отзывы", type="primary")

if submitted:
    st.session_state.pop("result", None)
    app_id = extract_app_id(url)
    ccs = list(dict.fromkeys(countries + [c.lower() for c in re.split(r"[\s,;]+", extra)
                                          if re.fullmatch(r"[a-zA-Z]{2}", c)]))
    if not app_id:
        st.error("Это не ссылка на приложение App Store. Пример: https://apps.apple.com/us/app/название/id123456789")
        st.stop()
    if not ccs:
        st.error("Выберите хотя бы одну страну.")
        st.stop()
    if limit > SOURCE_MAX:
        st.info(f"Запрошено {limit} на страну, но Apple отдаёт не более {SOURCE_MAX}. Будет собрано всё доступное.")

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Streamlit review collector)"

    # --- Проверка приложения в каждой стране ---
    available, skipped = [], {}
    with st.status(f"Проверяю приложение id{app_id}…", expanded=True) as box:
        for cc in ccs:
            try:
                app = lookup(session, app_id, cc)
            except StopCountry as e:
                skipped[cc] = f"ошибка проверки: {e}"
                st.write(f"❌ {cc}: {e}")
                continue
            if app:
                available.append(cc)
                st.write(f"✅ {cc}: «{app.get('trackName')}» — {app.get('sellerName', '')}")
            else:
                skipped[cc] = "приложение недоступно в этой стране"
                st.write(f"⚠️ {cc}: не найдено, пропуск")
            time.sleep(1)
        if not available:
            box.update(label="Приложение не найдено", state="error")
            st.error("Приложение не найдено ни в одной из выбранных стран. Проверьте ссылку.")
            st.stop()
        box.update(label=f"Приложение найдено в {len(available)} из {len(ccs)} стран", state="complete")

    # --- Сбор ---
    st.subheader("Сбор отзывов")
    all_rows, report = [], []
    for i, cc in enumerate(available):
        rows, status, note = collect(session, app_id, cc, limit, st.progress(0.0, text=f"[{cc}] ожидание"), st.empty())
        all_rows += rows
        report.append({"страна": cc, "отзывов": len(rows), "статус": status, "комментарий": note})
        if i < len(available) - 1:
            time.sleep(DELAY)
    for cc, note in skipped.items():
        report.append({"страна": cc, "отзывов": 0, "статус": "skipped", "комментарий": note})

    uniq = dedupe(all_rows)
    st.session_state["result"] = {
        "df": to_df(uniq) if uniq else pd.DataFrame(columns=COLUMNS),
        "report": pd.DataFrame(report), "total": len(all_rows), "unique": len(uniq),
        "incomplete": any(r["статус"] in ("partial", "error") for r in report),
        "app_id": app_id,
    }

# --- Результат (хранится в session_state, чтобы не пропадал после скачивания) ---
res = st.session_state.get("result")
if res:
    st.subheader("Отчёт")
    c1, c2, c3 = st.columns(3)
    c1.metric("До дедупликации", res["total"])
    c2.metric("Удалено дубликатов", res["total"] - res["unique"])
    c3.metric("Уникальных отзывов", res["unique"])
    st.dataframe(res["report"], hide_index=True, use_container_width=True)
    if res["incomplete"]:
        st.warning("⚠️ Результат неполный: при сборе по некоторым странам были ошибки (см. таблицу).")

    if res["df"].empty:
        st.error("Отзывов не получено — CSV не создаётся.")
    else:
        csv_bytes = res["df"].to_csv(index=False).encode("utf-8-sig")   # UTF-8 с BOM для Excel
        st.download_button("⬇️ Скачать CSV", csv_bytes, type="primary", mime="text/csv",
                           file_name=f"appstore_reviews_{res['app_id']}_{date.today().isoformat()}.csv")
        st.dataframe(res["df"], hide_index=True, use_container_width=True, height=450)
