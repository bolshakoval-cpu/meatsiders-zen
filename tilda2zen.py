#!/usr/bin/env python3
"""
tilda2zen — конвертер блога Тильды (Потоки) в RSS по требованиям Дзена.

Режимы:
  --drip   постепенная выдача по расписанию: сначала 10 статей, далее по одной
           не чаще, чем раз в MIN_INTERVAL_HOURS (~2-4 в неделю). Новые посты
           с сайта встают в очередь первыми. Состояние хранится в --state.
  (без --drip) разовая сборка всей ленты из --csv или --rss (для превью/теста).

Примеры:
  python tilda2zen.py --drip --seed tilda_feed.csv \
      --rss https://meatsiders.ru/rss-feed-814038631341.xml \
      --state published.json -o zen.xml --check-images
  python tilda2zen.py --csv tilda_feed.csv -o zen.xml      # разовая сборка всех

Что делает с каждым постом:
  * полный текст -> <content:encoded> (CDATA), только теги, которые понимает Дзен
  * жирная строка-подзаголовок -> <h2> (без id, чтобы Дзен не строил оглавление)
  * абзацы по <br><br> -> <p>, стили/классы Тильды вычищаются
  * кнопка «КУПИТЬ» -> обычная ссылка «Купить на сайте Meatsiders →»
  * UTM-метки на ссылки внутри текста (на meatsiders.ru)
  * обложка -> <enclosure>, картинки -> <figure><img></figure>
  * guid = ID поста Тильды (защита от дублей), pubDate в RFC 822
"""
import argparse
import csv
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime, parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup, NavigableString, Tag

# ---------------------------------------------------------------- настройки
SITE = "https://meatsiders.ru"
SITE_HOST = "meatsiders.ru"
CHANNEL_TITLE = "Meatsiders — блог"
CHANNEL_DESC = "Мясные деликатесы собственного производства: истории продуктов, рецепты, подборки."
UTM = {"utm_source": "dzen", "utm_medium": "article"}   # пустой dict = без меток
CATEGORIES = ["format-article", "index", "comment-all"]
EXCLUDE_IDS = {          # посты, которые не нужны в Дзене (устаревшие объявления)
    "j1gk1sgsu1",        # График работы на праздниках
    "zxhg6us7n1",        # График работы на майских праздниках
    "71izgh48g1",        # Новогодние наборы (сезонное, <300 знаков)
}
MAX_ITEMS = 500          # лимит Дзена на одну отправку
CTA_TEXT = "Купить на сайте Meatsiders →"

# --- постепенная выдача (режим --drip) ---
MSK = timezone(timedelta(hours=3))
INITIAL_BATCH = 10       # сколько отдать при самом первом запуске (минимум Дзена)
MIN_INTERVAL_HOURS = 48  # пауза между выдачей следующих статей (~3 в неделю)
INITIAL_SPREAD_DAYS = 1  # шаг дат внутри стартовой десятки, чтобы не вышла пачка одной минутой
FEED_KEEP = 60           # сколько последних статей держать в ленте

# ------------------------------------------------------------ транслит Тильды
_TR = dict(zip("абвгдезиклмнопрстуфыэ", "abvgdeziklmnoprstufie"))
_TR.update({"ё": "yo", "ж": "zh", "й": "i", "х": "h", "ц": "ts", "ч": "ch",
            "ш": "sh", "щ": "sch", "ъ": "", "ь": "", "ю": "yu", "я": "ya"})


def tilda_slug(title, maxlen=40):
    s = "".join(_TR.get(c, c) for c in title.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:maxlen].rstrip("-")


def post_url(post_id, title):
    return f"{SITE}/tpost/{post_id}-{tilda_slug(title)}"


# ------------------------------------------------------------- работа с HTML
def add_utm(href):
    if not UTM or not href:
        return href
    p = urlsplit(href)
    if not p.netloc.endswith(SITE_HOST):
        return href
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.startswith("utm_")]
    q += list(UTM.items())
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))


INLINE_OK = {"a", "b", "strong", "i", "em", "u", "s", "del"}


def clean_inline(node):
    """Inline-HTML абзаца: оставляем только допустимые теги без атрибутов (кроме href)."""
    out = []
    for ch in node.children if isinstance(node, Tag) else [node]:
        if isinstance(ch, NavigableString):
            out.append(escape(str(ch)))
        elif isinstance(ch, Tag):
            name = ch.name
            inner = clean_inline(ch)
            if name == "a" and ch.get("href"):
                out.append(f'<a href="{escape(add_utm(ch["href"]), {chr(34): "&quot;"})}">{inner}</a>')
            elif name in ("strong", "b"):
                out.append(f"<b>{inner}</b>" if inner.strip() else inner)
            elif name in ("em", "i"):
                out.append(f"<i>{inner}</i>")
            elif name in ("u", "s", "del"):
                out.append(f"<{name}>{inner}</{name}>")
            else:  # span и прочее — разворачиваем
                out.append(inner)
    return "".join(out)


def split_by_br(div):
    """Режем содержимое div по <br> на строки; списки/картинки — отдельными блоками."""
    blocks, cur = [], []

    def flush():
        if cur:
            blocks.append(("inline", list(cur)))
            cur.clear()

    for ch in list(div.children):
        if isinstance(ch, Tag) and ch.name == "br":
            flush()
        elif isinstance(ch, Tag) and ch.name in ("ul", "ol", "figure", "img", "h1", "h2", "h3", "h4", "blockquote", "div", "p"):
            flush()
            blocks.append(("block", ch))
        else:
            cur.append(ch)
    flush()
    return blocks


def inline_html(nodes):
    wrap = BeautifulSoup("<x></x>", "html.parser").x
    for n in nodes:
        wrap.append(n.__copy__() if isinstance(n, Tag) else NavigableString(str(n)))
    return clean_inline(wrap).strip()


def is_heading(nodes):
    """Строка = один <strong> без ссылок и без хвостового текста -> подзаголовок."""
    tags = [n for n in nodes if isinstance(n, Tag)]
    text_outside = "".join(str(n) for n in nodes if isinstance(n, NavigableString)).strip()
    if len(tags) != 1 or text_outside or tags[0].name not in ("strong", "b"):
        return False
    t = tags[0]
    txt = t.get_text(" ", strip=True)
    return bool(txt) and not t.find("a") and len(txt) <= 120


def is_cta(nodes):
    txt = "".join(n.get_text() if isinstance(n, Tag) else str(n) for n in nodes).strip()
    has_link = any(isinstance(n, Tag) and (n.name == "a" or n.find("a")) for n in nodes)
    return has_link and txt.upper() in ("КУПИТЬ", "ЗАКАЗАТЬ")


def first_link(nodes):
    for n in nodes:
        if isinstance(n, Tag):
            a = n if n.name == "a" else n.find("a")
            if a and a.get("href"):
                return a["href"]
    return None


def figure_html(tag):
    img = tag if tag.name == "img" else tag.find("img")
    src = (img.get("src") if img else None) or tag.get("data-src")
    if not src:
        return "", None
    return f'<figure><img src="{escape(src)}"/></figure>', src


def convert_body(html):
    """Тильда HTML -> HTML для content:encoded. Возвращает (html, [картинки], [h2])."""
    soup = BeautifulSoup(html, "html.parser")
    for h in soup.select("header"):      # в turbo:content заголовок дублируется в <header><h1>
        h.decompose()
    out, images, heads = [], [], []
    hcount = 0

    def emit_list(lst):
        items = []
        for li in lst.find_all("li", recursive=False):
            t = clean_inline(li).strip()
            if t:
                items.append(f"<li>{t}</li>")
        if items:
            out.append(f"<{lst.name}>{''.join(items)}</{lst.name}>")

    def walk(container):
        nonlocal hcount
        for kind, payload in split_by_br(container):
            if kind == "block":
                t = payload
                if t.name in ("figure", "img"):
                    fh, src = figure_html(t)
                    if fh:
                        out.append(fh)
                        images.append(src)
                elif t.name in ("ul", "ol"):
                    emit_list(t)
                elif t.name in ("h1", "h2", "h3", "h4"):
                    txt = t.get_text(" ", strip=True)
                    if txt:
                        hcount += 1
                        heads.append(txt)
                        out.append(f'<h2>{escape(txt)}</h2>')
                else:  # div/p/blockquote — рекурсивно
                    walk(t)
                continue
            nodes = payload
            if not "".join(str(n) for n in nodes).strip():
                continue
            if is_cta(nodes):
                href = first_link(nodes)
                out.append(f'<p><a href="{escape(add_utm(href))}">{CTA_TEXT}</a></p>')
            elif is_heading(nodes):
                txt = BeautifulSoup("".join(str(n) for n in nodes), "html.parser").get_text(" ", strip=True)
                hcount += 1
                heads.append(txt)
                out.append(f'<h2>{escape(txt)}</h2>')
            else:
                p = inline_html(nodes)
                if p:
                    out.append(f"<p>{p}</p>")

    walk(soup)
    return "\n".join(out), images, heads


# ----------------------------------------------------------------- источники
def read_csv(path):
    csv.field_size_limit(10 ** 9)
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    posts = []
    for r in rows:
        if r.get("Visibility", "published") != "published":
            continue
        pid = r["Post ID"].strip()
        posts.append({
            "id": pid,
            "title": r["Title"].strip(),
            "link": post_url(pid, r["Title"].strip()) if not r.get("Alias") else f"{SITE}/tpost/{pid}-{r['Alias'].strip()}",
            "date": datetime.fromisoformat(r["Date"].strip()),
            "description": r.get("Description", "").strip(),
            "html": r["Text"],
            "cover": r.get("Thumb Image", "").strip() or None,
        })
    return posts


def read_rss(src):
    if re.match(r"https?://", src):
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 tilda2zen"})
        data = urllib.request.urlopen(req, timeout=60).read()
    else:
        data = open(src, "rb").read()
    soup = BeautifulSoup(data, "xml")
    posts = []
    for it in soup.find_all("item"):
        link = it.find("link").get_text(strip=True)
        m = re.search(r"/tpost/([a-z0-9]+)-", link)
        content = it.find("turbo:content") or it.find("content")
        posts.append({
            "id": m.group(1) if m else link,
            "title": it.find("title").get_text(strip=True),
            "link": link,
            "date": parsedate_to_datetime(it.find("pubDate").get_text(strip=True)),
            "description": (it.find("description").get_text(strip=True) if it.find("description") else ""),
            "html": content.get_text() if content else "",
            "cover": None,
        })
    return posts


# ------------------------------------------------------------------ сборка
def mime(url):
    u = url.lower().split("?")[0]
    if u.endswith(".png"):
        return "image/png"
    if u.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def build(posts):
    posts = [p for p in posts if p["id"] not in EXCLUDE_IDS]
    posts.sort(key=lambda p: p["date"], reverse=True)
    posts = posts[:MAX_ITEMS]
    items, report = [], []
    for p in posts:
        body, images, heads = convert_body(p["html"])
        cover = p["cover"] or (images[0] if images else None)
        cats = "".join(f"<category>{c}</category>" for c in CATEGORIES)
        enc = f'\n      <enclosure url="{escape(cover)}" type="{mime(cover)}"/>' if cover else ""
        items.append(f"""    <item>
      <title>{escape(p['title'])}</title>
      <link>{escape(p['link'])}</link>
      <guid isPermaLink="false">{escape(p['id'])}</guid>
      <pubDate>{format_datetime(p['date'])}</pubDate>
      <description>{escape(p['description'])}</description>
      {cats}{enc}
      <content:encoded><![CDATA[{body.replace(']]>', ']]&gt;')}]]></content:encoded>
    </item>""")
        report.append((p, body, images, heads, cover))
    # дата последнего поста, а не «сейчас» — чтобы файл менялся только при новых постах
    now = format_datetime(max(p["date"] for p in posts)) if posts else format_datetime(datetime.now(timezone.utc))
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{escape(CHANNEL_TITLE)}</title>
    <link>{SITE}</link>
    <description>{escape(CHANNEL_DESC)}</description>
    <language>ru</language>
    <lastBuildDate>{now}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>
"""
    return xml, report


# -------------------------------------------------- постепенная выдача (drip)
def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"released": {}}


def merge_sources(seed_csv, rss_src):
    """Объединяет бэклог из CSV и новые посты из живого RSS. CSV в приоритете (полнее)."""
    pool = {}
    if rss_src:
        for p in read_rss(rss_src):
            pool[p["id"]] = p
    if seed_csv:
        for p in read_csv(seed_csv):   # перезаписывает RSS-версию более полной CSV
            pool[p["id"]] = p
    return pool


def drip(seed_csv, rss_src, state_path, out_path, now=None):
    """Отдаёт в ленту сначала INITIAL_BATCH статей, далее по одной не чаще MIN_INTERVAL_HOURS.
    pubDate = момент выдачи, поэтому Дзен видит ровный поток, а не пачку задним числом."""
    now = now or datetime.now(MSK)
    state = load_state(state_path)
    released = state.setdefault("released", {})   # id -> ISO-дата выдачи
    pool = {i: p for i, p in merge_sources(seed_csv, rss_src).items() if i not in EXCLUDE_IDS}

    unreleased = sorted((p for i, p in pool.items() if i not in released),
                        key=lambda p: p["date"], reverse=True)   # новые — первыми в очереди

    newly = []
    if not released:
        for k, p in enumerate(unreleased[:INITIAL_BATCH]):
            released[p["id"]] = (now - timedelta(days=k * INITIAL_SPREAD_DAYS)).isoformat()
            newly.append(p)
    elif unreleased:
        last = max(datetime.fromisoformat(t) for t in released.values())
        if now - last >= timedelta(hours=MIN_INTERVAL_HOURS):
            p = unreleased[0]
            released[p["id"]] = now.isoformat()
            newly.append(p)

    out_posts = []
    for i, iso in released.items():
        if i in pool:                       # пост мог быть удалён на сайте — тогда просто не выводим
            q = dict(pool[i])
            q["date"] = datetime.fromisoformat(iso)   # pubDate = дата выдачи
            out_posts.append(q)
    out_posts.sort(key=lambda p: p["date"], reverse=True)
    out_posts = out_posts[:FEED_KEEP]

    xml, report = build(out_posts)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    waiting = len(unreleased) - len(newly)
    print(f"Выдано сейчас: {len(newly)} | в ленте: {len(out_posts)} | ждут очереди: {waiting}")
    for p in newly:
        print("  +", p["date"].date(), p["title"][:60])
    return report


def check_images(report, min_w=700):
    """Проверка ширины картинок (Дзен: от 700 px). Нужен Pillow."""
    from PIL import ImageFile
    seen = {}
    for p, body, images, heads, cover in report:
        for url in dict.fromkeys(([cover] if cover else []) + images):
            if url not in seen:
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    parser = ImageFile.Parser()
                    with urllib.request.urlopen(req, timeout=30) as r:
                        while parser.image is None:
                            chunk = r.read(16384)
                            if not chunk:
                                break
                            parser.feed(chunk)
                    seen[url] = parser.image.size[0] if parser.image else None
                except Exception as e:
                    seen[url] = f"ошибка: {e}"
            w = seen[url]
            if not isinstance(w, int) or w < min_w:
                print(f"  ! {p['title'][:50]}: {url} -> ширина {w}")
    print(f"Проверено картинок: {len(seen)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drip", action="store_true",
                    help="режим постепенной выдачи (10 сразу, далее по 2-4 в неделю)")
    ap.add_argument("--seed", help="CSV-бэклог (исторические посты) для --drip")
    ap.add_argument("--rss", help="живая RSS-лента Тильды")
    ap.add_argument("--csv", help="разовая сборка из CSV (превью/тест)")
    ap.add_argument("--state", default="published.json", help="файл состояния для --drip")
    ap.add_argument("-o", "--out", default="zen.xml")
    ap.add_argument("--check-images", action="store_true", help="проверить ширину картинок >= 700 px")
    a = ap.parse_args()

    if a.drip:
        report = drip(a.seed, a.rss, a.state, a.out)
    else:
        if a.rss:
            posts = read_rss(a.rss)
        elif a.csv:
            posts = read_csv(a.csv)
        else:
            sys.exit("Укажите --csv или --rss (разовая сборка) либо --drip (выдача по расписанию)")
        if not posts:
            sys.exit("Нет постов — проверьте источник")
        xml, report = build(posts)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(xml)
        print(f"OK: {len(report)} материалов -> {a.out}")

    if a.check_images and report:
        check_images(report)


if __name__ == "__main__":
    main()
