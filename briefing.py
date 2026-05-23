"""
wisp-agent daily briefing generator.

All section/source/filter configuration lives in briefing.yaml.
This file handles only the processing pipeline:
  RSS/page fetch → domain filter → content filter → dedup →
  LLM translate+summarise → HTML render → open browser.
"""

from __future__ import annotations

import html as _html
import json
import re
import ssl
import subprocess
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse, urljoin
from xml.etree import ElementTree
import urllib.request

import requests as _req
import yaml as _yaml

# ── Runtime config (set once by init_briefing) ───────────────────────────────

_BASE_URL:   str         = ""
_API_KEY:    str         = ""
_MODEL:      str         = ""
_OUTPUT_DIR: Path | None = None
_SECTIONS:   list[dict]  = []   # loaded from briefing.yaml

_HISTORY_DAYS   = 7
_MAX_ITEMS      = 5
_MAX_CANDIDATES = _MAX_ITEMS * 4


def init_briefing(base_url: str, api_key: str, model: str, workspace: str,
                  config_path: str | None = None) -> None:
    """Called once during agent startup."""
    global _BASE_URL, _API_KEY, _MODEL, _OUTPUT_DIR, _SECTIONS
    _BASE_URL   = base_url
    _API_KEY    = api_key
    _MODEL      = model
    _OUTPUT_DIR = Path(workspace).expanduser().resolve() / "daily-report"
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cfg_path    = Path(config_path) if config_path else Path(__file__).parent / "briefing.yaml"
    _SECTIONS   = _load_sections(cfg_path)


def _load_sections(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"briefing config not found: {path}")
    raw = _yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw.get("sections", [])


# ── URL / text utilities ──────────────────────────────────────────────────────

def _dedup_key(url: str) -> str:
    p    = urlparse(url)
    host = (p.hostname or "").removeprefix("www.")
    host = re.sub(r"bbc\.co\.\w+$", "bbc.com", host)
    return host + p.path.rstrip("/")


def _is_article_url(url: str) -> bool:
    path = urlparse(url).path.rstrip("/")
    if not path:
        return False
    if re.search(r"/page/\d+", path):
        return False
    segs = [s for s in path.split("/") if s]
    if len(segs) < 2:
        return False
    last = segs[-1]
    if len(last) < 8 and last.isalpha():
        return False
    return True


def _domain_source(hostname: str, domains: dict) -> str | None:
    for domain, name in domains.items():
        if hostname == domain or hostname.endswith("." + domain):
            return name
    return None


def _http_get(url: str, timeout: int = 15) -> bytes:
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return resp.read()


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return _html.unescape(re.sub(r"\s+", " ", text)).strip()


def _entry_text(entry, *local_names: str) -> str:
    wanted = set(local_names)
    for child in entry.iter():
        name = child.tag.rsplit("}", 1)[-1].lower()
        if name in wanted and (child.text or "").strip():
            return child.text.strip()
    return ""


def _entry_link(entry) -> str:
    for child in entry.iter():
        if child.tag.rsplit("}", 1)[-1].lower() != "link":
            continue
        href = (child.get("href") or "").strip()
        if href:
            return href
        text = (child.text or "").strip()
        if text:
            return text
    return ""


def _has_any(text: str, kws: set | list) -> bool:
    t = text.lower()
    return any(k in t for k in kws)


def _count(text: str, kws: set | list) -> int:
    t = text.lower()
    return sum(1 for k in kws if k in t)


def _item_text(item: dict) -> str:
    return " ".join(filter(None, [
        item.get("title", ""), item.get("description", ""),
        item.get("url", ""),   item.get("source", ""),
    ])).lower()


# ── Generic content filter ────────────────────────────────────────────────────

def _apply_filter(item: dict, f: dict) -> bool:
    """Apply filter config `f` to `item`. Returns True if the item should be kept.

    Filter evaluation order:
      1. url_excl         — URL path exclusions (always applied first)
      2. excl_keywords    — any match → reject
      3. strong_keywords  — any match → pass immediately
      4. trusted_sources  — source name in list → pass immediately
      5. title_required   — at least one keyword must appear in title
      6. prefer_sources   — relaxed threshold for named sources
      7. title_bypass     — strong title signal skips sig_min check
      8. local_min        — minimum local_keywords hits in full item text
      9. title_min        — minimum local_keywords hits in title
      10. sig_min         — minimum sig_keywords hits in full item text
    """
    if not f:
        return True   # empty filter config → pass everything

    text   = _item_text(item)
    title  = item.get("title", "").lower()
    url    = item.get("url",   "").lower()
    source = item.get("source", "")

    # 1. URL path exclusions
    for p in f.get("url_excl", []):
        if p in url:
            return False

    # 2. Keyword exclusions
    excl_kw = f.get("excl_keywords", [])
    if excl_kw and _has_any(text, excl_kw):
        return False

    # 3. Strong signals → pass immediately
    strong_kw = f.get("strong_keywords", [])
    if strong_kw and _count(text, strong_kw) >= 1:
        return True

    # 4. Trusted sources → pass immediately
    if source in set(f.get("trusted_sources", [])):
        return True

    local_kw  = f.get("local_keywords",  [])
    sig_kw    = f.get("sig_keywords",    [])
    title_req = f.get("title_required",  [])
    url_inc   = f.get("url_inc",         [])
    local_min = int(f.get("local_min",           0))
    title_min = int(f.get("title_min",           0))
    sig_min   = int(f.get("sig_min",             0))
    title_byp = int(f.get("title_bypass_count",  0))

    # No further keyword config → pass
    if not local_kw and not sig_kw and not title_req:
        return True

    # 5. title_required: at least one must appear in title
    if title_req and not _has_any(title, title_req):
        return False

    # 6. prefer_sources: apply relaxed thresholds
    prefer_sources   = set(f.get("prefer_sources", []))
    prefer_title_min = int(f.get("prefer_title_min", 1))
    prefer_local_min = int(f.get("prefer_local_min", 0))
    if source in prefer_sources:
        title_ok = _count(title, local_kw) >= prefer_title_min if prefer_title_min > 0 else True
        local_ok = _count(text,  local_kw) >= prefer_local_min if prefer_local_min > 0 else True
        return title_ok and local_ok

    # 7. title_bypass: strong title signal skips sig_min check
    if title_byp > 0 and local_kw:
        if _count(title, local_kw) >= title_byp:
            return True

    # 8. local_min (URL path match counts as a local hit)
    url_match = any(p in url for p in url_inc)
    if local_min > 0 and not url_match:
        if _count(text, local_kw) < local_min:
            return False

    # 9. title_min
    if title_min > 0 and local_kw:
        if _count(title, local_kw) < title_min:
            return False

    # 10. sig_min
    if sig_min > 0 and sig_kw:
        if _count(text, sig_kw) < sig_min:
            return False

    return True


# ── History dedup ─────────────────────────────────────────────────────────────

def _load_history_keys() -> set[str]:
    if _OUTPUT_DIR is None:
        return set()
    keys: set[str] = set()
    today = date.today()
    for delta in range(1, _HISTORY_DAYS + 1):
        path = _OUTPUT_DIR / f"daily-report-{(today - timedelta(days=delta)).strftime('%Y%m%d')}.html"
        if not path.exists():
            continue
        try:
            for m in re.finditer(r'href="(https?://[^"]+)"\s+class="url"', path.read_text()):
                keys.add(_dedup_key(m.group(1)))
        except Exception:
            pass
    if keys:
        print(f"    history dedup: {len(keys)} keys loaded ({_HISTORY_DAYS}d)")
    return keys


def _cleanup_old_reports() -> None:
    if _OUTPUT_DIR is None:
        return
    cutoff  = date.today() - timedelta(days=_HISTORY_DAYS)
    removed = 0
    for f in _OUTPUT_DIR.glob("daily-report-????????.html"):
        m = re.fullmatch(r"daily-report-(\d{4})(\d{2})(\d{2})\.html", f.name)
        if not m:
            continue
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if d < cutoff:
                f.unlink()
                removed += 1
        except (ValueError, OSError):
            pass
    if removed:
        print(f"    cleaned up {removed} old report(s)")


# ── RSS / page fetchers ───────────────────────────────────────────────────────

def _fetch_rss(feed_url: str, source_name: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(_http_get(feed_url, timeout=15))
    except Exception as e:
        print(f"      ✗ rss {feed_url[:55]}: {e}")
        return []
    entries = (root.findall(".//item")
               or root.findall(".//{*}entry")
               or root.findall(".//entry"))
    items = []
    for e in entries:
        title = _clean(_entry_text(e, "title"))
        link  = _entry_link(e)
        if not title or not link:
            continue
        desc = _clean(_entry_text(e, "description", "summary", "content", "encoded"))[:600]
        host = (urlparse(link).hostname or "").removeprefix("www.")
        items.append({"title": title, "url": link, "description": desc,
                      "hostname": host, "source": source_name})
    return items


def _fetch_sitemap(sitemap_url: str, source_name: str, max_children: int = 2) -> list[dict]:
    try:
        root = ElementTree.fromstring(_http_get(sitemap_url, timeout=20))
    except Exception as e:
        print(f"      ✗ sitemap {sitemap_url[:55]}: {e}")
        return []
    if root.tag.lower().endswith("sitemapindex"):
        children = [
            el.findtext("{*}loc", "").strip()
            for el in root.findall("{*}sitemap")
        ]
        items = []
        for child_url in filter(None, children[:max_children]):
            items.extend(_fetch_sitemap(child_url, source_name, max_children=0))
        return items
    items = []
    for url_el in root.findall("{*}url"):
        loc = (url_el.findtext("{*}loc") or "").strip()
        if not loc:
            continue
        title_el = url_el.find(
            "{http://www.google.com/schemas/sitemap-news/0.9}news"
            "/{http://www.google.com/schemas/sitemap-news/0.9}title"
        )
        title = (title_el.text or "").strip() if title_el is not None else ""
        host  = (urlparse(loc).hostname or "").removeprefix("www.")
        items.append({"title": title, "url": loc, "description": "",
                      "hostname": host, "source": source_name})
    return items


def _fetch_page(page_url: str, source_name: str, url_pattern: str = "") -> list[dict]:
    try:
        raw = _http_get(page_url, timeout=20).decode("utf-8", "ignore")
    except Exception as e:
        print(f"      ✗ page {page_url[:55]}: {e}")
        return []
    items = []
    for href, anchor in re.findall(
        r"<a\b[^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
        raw, flags=re.I | re.S,
    ):
        link  = urljoin(page_url, href.strip())
        if url_pattern and url_pattern not in link:
            continue
        title = _clean(anchor)
        if not title or len(title) < 8:
            continue
        host = (urlparse(link).hostname or "").removeprefix("www.")
        items.append({"title": title, "url": link, "description": "",
                      "hostname": host, "source": source_name})
    return items


# ── Section fetcher ───────────────────────────────────────────────────────────

def _fetch_section(sec_cfg: dict, history_keys: set[str]) -> list[dict]:
    """Collect, filter, and deduplicate candidates for one section.

    Uses per_source_cap from sec_cfg to prevent one prolific feed from
    crowding out others.  Content filter is applied after collection.
    """
    domains      = sec_cfg.get("domains", {})
    filter_cfg   = sec_cfg.get("filter",  {})
    per_src_cap  = int(sec_cfg.get("per_source_cap", _MAX_ITEMS))

    seen: dict[str, int]   = {}   # label → count
    deduped: set[str]      = set()
    candidates: list[dict] = []

    for src in sec_cfg.get("sources", []):
        host_hint   = src.get("hostname", "")
        source_name = domains.get(host_hint) or host_hint

        if src["type"] == "rss":
            raw = _fetch_rss(src["url"], source_name)
        elif src["type"] == "sitemap":
            raw = _fetch_sitemap(src["url"], source_name,
                                 max_children=src.get("max_children", 2))
        elif src["type"] == "page":
            raw = _fetch_page(src["url"], source_name,
                              url_pattern=src.get("url_pattern", ""))
        else:
            continue

        for item in raw:
            url_clean = item["url"].split("?")[0]
            if not _is_article_url(url_clean):
                continue
            label = _domain_source(item["hostname"], domains)
            if not label:
                continue
            if seen.get(label, 0) >= per_src_cap:
                continue
            key = _dedup_key(url_clean)
            if key in deduped or key in history_keys:
                continue
            deduped.add(key)
            seen[label] = seen.get(label, 0) + 1
            candidates.append({
                **item,
                "title":       (item["title"] or label).strip(),
                "description": (item.get("description") or "").strip()[:600],
                "source":      label,
            })
            if len(candidates) >= _MAX_CANDIDATES:
                break
        if len(candidates) >= _MAX_CANDIDATES:
            break

    return [c for c in candidates if _apply_filter(c, filter_cfg)][:_MAX_ITEMS]


# ── LLM helpers ───────────────────────────────────────────────────────────────

def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _call_llm(prompt: str, max_tokens: int = 2048, json_mode: bool = False) -> str:
    payload: dict = {
        "model":      _MODEL,
        "messages":   [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream":     False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    resp = _req.post(
        _BASE_URL,
        headers={"Authorization": f"Bearer {_API_KEY}"},
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _clean_llm_output(raw: str) -> str:
    text = _strip_thinking(raw)
    text = re.sub(r"```(?:json)?\s*", "", text)
    return text.replace("```", "").strip()


def _fix_json(s: str) -> str:
    s = re.sub(r",\s*([}\]])", r"\1", s)
    s = re.sub(r"\bNone\b",  "null",  s)
    s = re.sub(r"\bTrue\b",  "true",  s)
    s = re.sub(r"\bFalse\b", "false", s)
    return s


def _try_json_loads(s: str):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return json.loads(_fix_json(s))


def _parse_llm_json(raw: str) -> list[dict] | None:
    text = _clean_llm_output(raw)
    try:
        data = _try_json_loads(text)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            data = _try_json_loads(m.group(0))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
    objects = re.findall(r'\{[^{}]+\}', text, re.DOTALL)
    if objects:
        result = []
        for obj in objects:
            try:
                result.append(_try_json_loads(obj))
            except (json.JSONDecodeError, ValueError):
                pass
        if result:
            return result
    return None


def _is_english(text: str) -> bool:
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return False
    return sum(1 for c in alpha if c.isascii()) / len(alpha) > 0.65


_TRANSLATE_BATCH = 3


def _translate_batch(items: list[dict]) -> list[dict]:
    n        = len(items)
    fallback = [{"title_cn": item["title"], "summary_cn": item.get("description", "")}
                for item in items]
    if n == 0:
        return []

    parts = []
    for i, item in enumerate(items, 1):
        desc = (item.get("description") or "")[:300]
        parts.append(f'{i}. Title: {item["title"]}\n   Abstract: {desc or "(none)"}')

    prompt = (
        "You are a professional news editor. For each numbered item, translate the "
        "title to Simplified Chinese and write a 2–3 sentence Chinese summary "
        f"(80–150 characters).\n\n"
        f"IMPORTANT: return EXACTLY {n} objects in the array — one per input, in order.\n"
        'JSON format: {"items":[{"title_cn":"...","summary_cn":"..."}]}\n\n'
        + "\n\n".join(parts)
    )

    try:
        raw  = _call_llm(prompt, max_tokens=600, json_mode=True)
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("items") or data.get("results") or list(data.values())[0]
        if not isinstance(data, list):
            raise ValueError(f"unexpected shape: {type(data)}")

        result = []
        for i, item in enumerate(items):
            entry      = data[i] if i < len(data) and isinstance(data[i], dict) else {}
            title_cn   = str(entry.get("title_cn")   or "").strip()
            summary_cn = str(entry.get("summary_cn") or "").strip()
            if not title_cn or _is_english(title_cn):
                title_cn = item["title"]
            if not summary_cn or re.match(r"^[\d\s.,/%-]+$", summary_cn) or len(summary_cn) < 15:
                summary_cn = item.get("description", "")
            result.append({"title_cn": title_cn, "summary_cn": summary_cn})

        while len(result) < n:
            result.append(fallback[len(result)])
        return result

    except Exception as e:
        print(f"      ✗ batch translate error: {e}")
        return fallback


def _translate_and_summarize(items: list[dict]) -> list[dict]:
    if not items:
        return []
    result = []
    for i in range(0, len(items), _TRANSLATE_BATCH):
        result.extend(_translate_batch(items[i: i + _TRANSLATE_BATCH]))
    return result


# ── HTML rendering ────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _render_section_body(items: list[dict], translated: list[dict]) -> str:
    if not items:
        return '<div class="empty-state">暂无内容</div>'
    parts = []
    for i, item in enumerate(items):
        t   = translated[i]["title_cn"]   if i < len(translated) else item["title"]
        d   = translated[i]["summary_cn"] if i < len(translated) else item.get("description", "")
        src = item.get("source", "")
        url = item["url"]
        parts.append(
            f'<div class="news-item">'
            f'<h3>{_esc(t)}</h3>'
            f'<div class="source">{_esc(src)}</div>'
            f'<div class="summary">{_esc(d)}</div>'
            f'<a href="{_esc(url)}" class="url" target="_blank">🔗 原文链接</a>'
            f'</div>'
        )
    return "\n".join(parts)


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>每日晨报 · {{REPORT_DATE}}</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
     background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh;padding:20px}
.container{max-width:1200px;margin:0 auto}
.header{background:rgba(255,255,255,.95);border-radius:16px;padding:30px;
        margin-bottom:25px;box-shadow:0 8px 32px rgba(0,0,0,.15)}
.header h1{color:#667eea;font-size:28px;margin-bottom:10px}
.header .date{color:#666;font-size:14px}
.section{background:rgba(255,255,255,.95);border-radius:16px;padding:25px;
         margin-bottom:20px;box-shadow:0 4px 16px rgba(0,0,0,.1)}
.section h2{color:#667eea;font-size:20px;margin-bottom:15px;padding-bottom:10px;
            border-bottom:2px solid #667eea}
.news-item{padding:15px 0;border-bottom:1px solid #eee}
.news-item:last-child{border-bottom:none}
.news-item h3{color:#333;font-size:16px;margin-bottom:8px}
.source{font-size:12px;color:#667eea;font-weight:500}
.summary{color:#555;font-size:14px;line-height:1.6;margin-top:8px}
.url{font-size:12px;color:#999;text-decoration:none;margin-top:10px;display:inline-block}
.url:hover{color:#667eea;text-decoration:underline}
.empty-state{color:#999;font-size:14px;text-align:center;padding:20px}
.footer{text-align:center;color:rgba(255,255,255,.8);font-size:13px;margin-top:20px}
@media(max-width:768px){.header h1{font-size:24px}.section h2{font-size:18px}.news-item h3{font-size:15px}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>📰 每日晨报</h1>
    <div class="date">{{REPORT_DATE}}</div>
  </div>
  {{SECTIONS_HTML}}
  <div class="footer">{{FOOTER_TEXT}}</div>
</div>
</body>
</html>
"""


def _build_sections_html(active: list[dict], sections_data: dict, sections_t: dict) -> str:
    parts = []
    for sec in active:
        sid   = sec["id"]
        label = sec["label"]
        did   = sec["div_id"]
        body  = _render_section_body(sections_data.get(sid, []), sections_t.get(sid, []))
        parts.append(
            f'  <div class="section">'
            f'<h2>{_esc(label)}</h2>'
            f'<div id="{_esc(did)}">{body}</div>'
            f'</div>'
        )
    return "\n".join(parts)


def _build_footer(sections: list[dict]) -> str:
    # Collect all unique source names from domains across all sections
    seen: dict[str, bool] = {}
    names = []
    for sec in sections:
        for name in sec.get("domains", {}).values():
            if name not in seen:
                seen[name] = True
                names.append(name)
    sources_str = " · ".join(names) if names else ""
    return f"wisp-agent · 自动生成 · 数据来源：{sources_str}"


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_report(
    sections: list[str] | None = None,
    open_browser: bool = True,
) -> str:
    """Generate the daily briefing HTML.  Returns the output file path."""
    if _OUTPUT_DIR is None:
        raise RuntimeError("briefing not initialized — call init_briefing() first")
    if not _SECTIONS:
        raise RuntimeError("no sections configured — check briefing.yaml")

    # Filter to requested sections (or all)
    requested = set(sections) if sections else None
    active    = [s for s in _SECTIONS if requested is None or s["id"] in requested]
    today     = date.today()
    out       = _OUTPUT_DIR / f"daily-report-{today.strftime('%Y%m%d')}.html"

    print(f"  generating daily briefing  ({len(active)} sections)...")

    # ── 1. history dedup ──────────────────────────────────────────────────────
    history_keys = _load_history_keys()

    # ── 2. fetch all sections ─────────────────────────────────────────────────
    sections_data: dict[str, list[dict]] = {}
    for sec in active:
        label = sec["label"]
        print(f"  → {label}")
        try:
            items = _fetch_section(sec, history_keys)
            print(f"     {len(items)} articles")
        except Exception as e:
            print(f"     ✗ fetch error: {e}")
            items = []
        sections_data[sec["id"]] = items

    # ── 3. cross-section dedup: earlier sections win ──────────────────────────
    seen_keys: set[str] = set()
    for sec in active:
        sid        = sec["id"]
        before     = sections_data[sid]
        after      = [i for i in before if _dedup_key(i["url"]) not in seen_keys][:_MAX_ITEMS]
        seen_keys |= {_dedup_key(i["url"]) for i in after}
        sections_data[sid] = after

    # ── 4. translate + summarise ──────────────────────────────────────────────
    print(f"\n  translating & summarizing...")
    sections_t: dict[str, list[dict]] = {}
    for sec in active:
        sid   = sec["id"]
        items = sections_data.get(sid, [])
        if not items:
            sections_t[sid] = []
            continue
        print(f"  → {sec['label']}")
        sections_t[sid] = _translate_and_summarize(items)

    # ── 5. render HTML ────────────────────────────────────────────────────────
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    date_str = f"{today.year}年{today.month}月{today.day}日 {weekdays[today.weekday()]}"

    html = (
        _HTML_TEMPLATE
        .replace("{{REPORT_DATE}}",   date_str)
        .replace("{{SECTIONS_HTML}}", _build_sections_html(active, sections_data, sections_t))
        .replace("{{FOOTER_TEXT}}",   _build_footer(active))
    )
    out.write_text(html, encoding="utf-8")
    _cleanup_old_reports()

    total  = sum(len(v) for v in sections_data.values())
    result = f"ok: daily briefing saved → {out}\n    {len(active)} sections · {total} articles"
    print(f"\n  ✓ {out}")

    # ── 6. open in browser ────────────────────────────────────────────────────
    if open_browser:
        try:
            subprocess.run(["open", str(out)], check=True)
        except Exception as e:
            result += f"\n    note: could not open browser: {e}"

    return result
