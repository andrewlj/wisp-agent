"""
wisp-agent daily briefing generator.

Fetches news from RSS / sitemap / section-page sources, translates titles
and writes Chinese summaries using the local LLM, then renders an HTML report.

Improvements over the hermes version:
  - 1 LLM call per section (translate + summarize combined, JSON output)
  - <think>…</think> stripping for Qwen-family reasoning models
  - Robust JSON extraction with English fallback
  - Embedded HTML template — no external file dependency
  - Section-level error isolation: one failure never blocks the rest
  - Cross-section dedup (ict_research / ict_business) handled cleanly
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

import requests as _req  # already in wisp's venv

# ── Config (set once by agent.py via init_briefing) ──────────────────────────

_BASE_URL:   str        = ""
_API_KEY:    str        = ""
_MODEL:      str        = ""
_OUTPUT_DIR: Path | None = None

_HISTORY_DAYS    = 7
_MAX_ITEMS       = 5
_MAX_CANDIDATES  = _MAX_ITEMS * 4


def init_briefing(base_url: str, api_key: str, model: str, workspace: str) -> None:
    """Called once during agent startup."""
    global _BASE_URL, _API_KEY, _MODEL, _OUTPUT_DIR
    _BASE_URL   = base_url
    _API_KEY    = api_key
    _MODEL      = model
    _OUTPUT_DIR = Path(workspace).expanduser().resolve() / "daily-report"
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Section metadata ──────────────────────────────────────────────────────────

SECTION_LABELS = {
    "political":     "🌍 政治国际",
    "ict_research":  "💡 ICT 前沿研究",
    "ict_business":  "🏢 高科技企业",
    "ireland":       "🇮🇪 爱尔兰新闻",
    "entertainment": "🎬 娱乐",
    "china":         "🇨🇳 中国要闻",
}

SECTION_DIV_IDS = {
    "political":     "political-international",
    "ict_research":  "ict-research-breakthroughs",
    "ict_business":  "ict-business-news",
    "ireland":       "ireland-news",
    "entertainment": "entertainment",
    "china":         "china-news",
}

ALL_SECTIONS = ["political", "ict_research", "ict_business", "ireland", "entertainment", "china"]

# ── Domain whitelists ─────────────────────────────────────────────────────────

_POL_DOMAINS = {
    "reuters.com":  "Reuters",           "apnews.com":  "AP News",
    "bbc.co.uk":    "BBC",               "bbc.com":     "BBC",
    "ft.com":       "Financial Times",   "wsj.com":     "Wall Street Journal",
}
_ICT_DOMAINS = {
    "theverge.com":          "The Verge",
    "arstechnica.com":       "Ars Technica",
    "technologyreview.com":  "MIT Technology Review",
    "nature.com":            "Nature",
    "spectrum.ieee.org":     "IEEE Spectrum",
    "ieee.org":              "IEEE",
    "sciencedaily.com":      "Science Daily",
    "newscientist.com":      "New Scientist",
    "wired.com":             "Wired",
}
_IRE_DOMAINS = {
    "rte.ie":          "RTÉ",
    "irishtimes.com":  "Irish Times",
    "independent.ie":  "Irish Independent",
    "thejournal.ie":   "The Journal",
}
_ENT_DOMAINS = {
    "variety.com":           "Variety",
    "hollywoodreporter.com": "Hollywood Reporter",
    "billboard.com":         "Billboard",
    "deadline.com":          "Deadline",
    "rollingstone.com":      "Rolling Stone",
}

_SECTION_DOMAINS = {
    "political":     _POL_DOMAINS,
    "ict_research":  _ICT_DOMAINS,
    "ict_business":  _ICT_DOMAINS,
    "ireland":       _IRE_DOMAINS,
    "entertainment": _ENT_DOMAINS,
    "china":         _POL_DOMAINS,
}

# ── Keyword filters ───────────────────────────────────────────────────────────

_CHINA_KW = {
    "china", "chinese", "beijing", "shanghai", "xi jinping", "prc", "cpc",
    "huawei", "alibaba", "tencent", "bytedance", "sino", "renminbi", "yuan",
    "belt and road", "taiwan", "hong kong", "xinjiang", "uyghur", "deepseek",
    "li qiang", "pla", "pboc", "mainland china",
}
_CHINA_SIG = {
    "tariff", "trade", "economy", "economic", "export", "manufacturing",
    "policy", "sanction", "diplomat", "military", "ai", "chip", "semiconductor",
    "tech", "acquisition", "market", "finance", "hong kong", "taiwan",
}
_CHINA_EXCL = {"panda", "restaurant", "food", "recipe", "travel", "tourism",
               "concert", "nba", "sport", "wildlife"}

_ICT_R_KW = {
    "ai", "llm", "model", "agent", "gpu", "chip", "semiconductor", "quantum",
    "robot", "robotics", "breakthrough", "research", "study", "scientists",
    "cloud", "inference", "training", "benchmark", "fusion", "battery",
    "security", "cyber", "paper", "dataset", "open source",
}
_ICT_R_STRONG = {
    "breakthrough", "research", "study", "scientists", "paper", "quantum",
    "robotics", "benchmark", "dataset", "inference", "training", "model",
    "llm", "agent", "semiconductor", "fusion",
}
_ICT_R_EXCL = {"promo code", "coupon", "gift guide", "shopping", "deals",
               "review", "hands-on", "preorder", "sale", "discount"}

_ICT_B_KW = {
    "openai", "microsoft", "google", "meta", "amazon", "anthropic", "nvidia",
    "apple", "tesla", "xai", "oracle", "intel", "amd", "tsmc", "samsung",
    "huawei", "bytedance", "tencent", "netflix", "databricks", "snowflake",
    "musk", "altman", "earnings", "funding", "acquisition", "merger", "ipo",
    "layoff", "launch", "antitrust", "lawsuit", "revenue", "profit",
}
_ICT_B_STRONG = {
    "earnings", "funding", "acquisition", "merger", "ipo", "layoff",
    "lawsuit", "antitrust", "revenue", "profit", "investment",
}
_ICT_B_EXCL = {"promo code", "coupon", "gift guide", "shopping", "deal alert"}

_IRE_KW = {
    "ireland", "irish", "dublin", "cork", "galway", "limerick", "waterford",
    "taoiseach", "dail", "fianna fail", "fine gael", "sinn fein",
    "garda", "gardaí", "hse", "luas", "dart",
}
_IRE_SIG = {
    "garda", "gardaí", "court", "crime", "police", "housing", "rent",
    "hospital", "health", "hse", "school", "minister", "government",
    "dail", "taoiseach", "transport", "consumer",
}
_IRE_EXCL = {
    "ukraine", "russia", "gaza", "israel", "iran", "germany", "france",
    "spain", "italy", "china", "nato", "middle east",
    "westlife", "property", "motoring", "car review",
}
_IRE_URL_INC  = ("/news/ireland/", "/ireland/", "/irish-news/", "/news/dublin/",
                 "/news/cork/", "/crime-law/courts/", "/housing/", "/health/")
_IRE_URL_EXCL = ("/sport/", "/culture/", "/life-style/", "/style/", "/opinion/",
                 "/world/", "/middle-east/", "/europe/", "/entertainment/",
                 "/property/", "/motoring/")

# ── Fetch sources ─────────────────────────────────────────────────────────────

_SOURCES: dict[str, list[dict]] = {
    "political": [
        {"type": "rss",  "url": "https://feeds.bbci.co.uk/news/world/rss.xml",       "hostname": "bbc.co.uk"},
        {"type": "rss",  "url": "https://feeds.bbci.co.uk/news/rss.xml",             "hostname": "bbc.co.uk"},
        {"type": "page", "url": "https://apnews.com/world-news",                     "hostname": "apnews.com", "url_pattern": "/article/"},
        {"type": "page", "url": "https://apnews.com/politics",                       "hostname": "apnews.com", "url_pattern": "/article/"},
    ],
    "ict_research": [
        {"type": "rss", "url": "https://www.theverge.com/rss/index.xml",             "hostname": "theverge.com"},
        {"type": "rss", "url": "https://feeds.arstechnica.com/arstechnica/index",    "hostname": "arstechnica.com"},
        {"type": "rss", "url": "https://www.technologyreview.com/topnews.rss",       "hostname": "technologyreview.com"},
        {"type": "rss", "url": "https://www.nature.com/nature/news.rss",             "hostname": "nature.com"},
    ],
    "ict_business": [
        {"type": "rss", "url": "https://www.theverge.com/rss/index.xml",             "hostname": "theverge.com"},
        {"type": "rss", "url": "https://feeds.arstechnica.com/arstechnica/index",    "hostname": "arstechnica.com"},
        {"type": "rss", "url": "https://www.technologyreview.com/topnews.rss",       "hostname": "technologyreview.com"},
        {"type": "rss", "url": "https://www.wired.com/feed/rss",                     "hostname": "wired.com"},
    ],
    "ireland": [
        {"type": "rss",  "url": "https://www.rte.ie/news/rss/news-headlines.xml",    "hostname": "rte.ie"},
        {"type": "rss",  "url": "https://www.thejournal.ie/feed/",                   "hostname": "thejournal.ie"},
        {"type": "rss",  "url": "https://www.independent.ie/rss/",                   "hostname": "independent.ie"},
        {"type": "rss",  "url": "https://www.irishtimes.com/arc/outboundfeeds/rss/", "hostname": "irishtimes.com"},
        {"type": "page", "url": "https://www.rte.ie/news/ireland/",                  "hostname": "rte.ie", "url_pattern": "/news/ireland/"},
    ],
    "entertainment": [
        {"type": "rss", "url": "https://variety.com/feed/",                          "hostname": "variety.com"},
        {"type": "rss", "url": "https://deadline.com/feed/",                         "hostname": "deadline.com"},
        {"type": "rss", "url": "https://www.hollywoodreporter.com/feed/",            "hostname": "hollywoodreporter.com"},
        {"type": "rss", "url": "https://www.billboard.com/feed/",                    "hostname": "billboard.com"},
    ],
    "china": [
        {"type": "page", "url": "https://www.bbc.com/news/world/asia/china",         "hostname": "bbc.com", "url_pattern": "/news/articles/"},
        {"type": "rss",  "url": "https://feeds.bbci.co.uk/news/world/asia/rss.xml",  "hostname": "bbc.co.uk"},
        {"type": "rss",  "url": "https://feeds.bbci.co.uk/news/world/rss.xml",       "hostname": "bbc.co.uk"},
        {"type": "page", "url": "https://apnews.com/hub/china",                      "hostname": "apnews.com", "url_pattern": "/article/"},
    ],
}

# ── URL utilities ─────────────────────────────────────────────────────────────

def _dedup_key(url: str) -> str:
    """Canonical URL key for deduplication across days."""
    p = urlparse(url)
    host = (p.hostname or "").removeprefix("www.")
    host = re.sub(r"bbc\.co\.\w+$", "bbc.com", host)
    return host + p.path.rstrip("/")


def _is_article_url(url: str) -> bool:
    """Reject homepages, section pages, and paginated URLs."""
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


def _has_any(text: str, kws: set) -> bool:
    t = text.lower()
    return any(k in t for k in kws)


def _count(text: str, kws: set) -> int:
    t = text.lower()
    return sum(1 for k in kws if k in t)


def _item_text(item: dict) -> str:
    return " ".join(filter(None, [
        item.get("title", ""), item.get("description", ""),
        item.get("url", ""),   item.get("source", ""),
    ])).lower()

# ── Content filters ───────────────────────────────────────────────────────────

def _keep_ict_research(item: dict) -> bool:
    text  = _item_text(item)
    title = item.get("title", "").lower()
    if _has_any(text, _ICT_R_EXCL):
        return False
    preferred = {"Nature", "MIT Technology Review", "Ars Technica",
                 "IEEE Spectrum", "Science Daily", "New Scientist"}
    if item.get("source") in preferred and _count(text, _ICT_R_KW) >= 1:
        return True
    return _count(text, _ICT_R_STRONG) >= 1 and (
        _count(text, _ICT_R_KW) >= 2 or _has_any(title, _ICT_R_STRONG)
    )


def _keep_ict_business(item: dict) -> bool:
    text  = _item_text(item)
    title = item.get("title", "").lower()
    if _has_any(text, _ICT_B_EXCL):
        return False
    if _count(text, _ICT_B_STRONG) >= 1:
        return True
    return _count(text, _ICT_B_KW) >= 2 and _has_any(title, _ICT_B_KW)


def _keep_ireland(item: dict) -> bool:
    text = _item_text(item)
    url  = item.get("url", "").lower()
    if any(p in url for p in _IRE_URL_EXCL):
        return False
    if _has_any(text, _IRE_EXCL):
        return False
    local = any(p in url for p in _IRE_URL_INC) or _has_any(text, _IRE_KW)
    sig   = _has_any(text, _IRE_SIG)
    return local and sig


def _keep_china(item: dict) -> bool:
    text  = _item_text(item)
    title = item.get("title", "").lower()
    if _has_any(text, _CHINA_EXCL):
        return False
    title_hits = _count(title, _CHINA_KW)
    text_hits  = _count(text,  _CHINA_KW)
    sig_hits   = _count(text,  _CHINA_SIG)
    return (title_hits >= 1 or text_hits >= 2) and sig_hits >= 1


# ── History dedup ─────────────────────────────────────────────────────────────

def _load_history_keys() -> set[str]:
    """Extract article URL keys from the past N days of HTML reports."""
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
    cutoff = date.today() - timedelta(days=_HISTORY_DAYS)
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


# ── RSS / sitemap / page fetchers ─────────────────────────────────────────────

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
        raw, flags=re.I | re.S
    ):
        link = urljoin(page_url, href.strip())
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

def _fetch_section(section: str, history_keys: set[str]) -> list[dict]:
    """Collect, filter, and deduplicate candidates for one section."""
    domains   = _SECTION_DOMAINS[section]
    seen: set[str] = set()
    candidates: list[dict] = []

    for src in _SOURCES.get(section, []):
        host_hint   = src["hostname"]
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
            key = _dedup_key(url_clean)
            if key in seen or key in history_keys:
                continue
            seen.add(key)
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

    # Apply section-specific content filters
    if section == "ict_research":
        candidates = [c for c in candidates if _keep_ict_research(c)]
    elif section == "ict_business":
        candidates = [c for c in candidates if _keep_ict_business(c)]
    elif section == "ireland":
        candidates = [c for c in candidates if _keep_ireland(c)]
    elif section == "china":
        candidates = [c for c in candidates if _keep_china(c)]

    return candidates[:_MAX_ITEMS]


# ── LLM helpers ───────────────────────────────────────────────────────────────

def _strip_thinking(text: str) -> str:
    """Remove <think>…</think> blocks produced by Qwen reasoning models."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _call_llm(prompt: str, max_tokens: int = 2048) -> str:
    """Synchronous non-streaming LLM call via wisp's configured endpoint."""
    resp = _req.post(
        _BASE_URL,
        headers={"Authorization": f"Bearer {_API_KEY}"},
        json={
            "model":      _MODEL,
            "messages":   [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream":     False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _clean_llm_output(raw: str) -> str:
    """Strip thinking tags and markdown fences from LLM output."""
    text = _strip_thinking(raw)
    # Remove ```json ... ``` or ``` ... ``` fences
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.replace("```", "").strip()
    return text


def _fix_json(s: str) -> str:
    """Fix the most common LLM JSON mistakes."""
    # Trailing commas before ] or }
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # Python-style None/True/False
    s = re.sub(r"\bNone\b",  "null",  s)
    s = re.sub(r"\bTrue\b",  "true",  s)
    s = re.sub(r"\bFalse\b", "false", s)
    return s


def _try_json_loads(s: str):
    """Try json.loads; on failure try after _fix_json. Returns parsed object or raises."""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return json.loads(_fix_json(s))


def _parse_llm_json(raw: str) -> list[dict] | None:
    """Multi-strategy extraction of a JSON array from LLM output.

    Strategies (in order):
    1. Parse the whole cleaned text directly.
    2. Extract the first [...] block via regex, parse that.
    3. Same block after _fix_json.
    4. Extract individual {...} objects and reassemble.
    Returns None if all strategies fail.
    """
    text = _clean_llm_output(raw)

    # Strategy 1: direct parse
    try:
        data = _try_json_loads(text)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2 & 3: extract first [...] block
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        block = m.group(0)
        try:
            data = _try_json_loads(block)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 4: extract individual {...} objects
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


def _translate_titles_simple(items: list[dict]) -> list[str]:
    """Fallback: numbered-list translation (simpler format, more reliable)."""
    numbered = "\n".join(f"{i+1}. {item['title']}" for i, item in enumerate(items))
    prompt = (
        "Translate each English headline to Simplified Chinese. "
        "Output ONLY numbered lines in the same order, nothing else.\n\n"
        + numbered
    )
    try:
        raw   = _call_llm(prompt, max_tokens=512)
        text  = _clean_llm_output(raw)
        lines = [re.sub(r"^\d+[.)、]\s*", "", ln).strip()
                 for ln in text.splitlines() if ln.strip()]
        lines = [ln for ln in lines if ln]
        # Pad or truncate to match item count
        while len(lines) < len(items):
            lines.append(items[len(lines)]["title"])
        return lines[:len(items)]
    except Exception:
        return [item["title"] for item in items]


def _translate_and_summarize(items: list[dict]) -> list[dict]:
    """One LLM call: translate titles + write Chinese summaries.

    Returns list of {"title_cn": str, "summary_cn": str}.
    On JSON parse failure, retries with a simpler numbered-list format.
    Falls back to English titles as last resort.
    """
    if not items:
        return []

    fallback = [
        {"title_cn": item["title"], "summary_cn": item.get("description", "")}
        for item in items
    ]

    parts = []
    for i, item in enumerate(items, 1):
        desc = (item.get("description") or "")[:300]
        parts.append(f'{i}. Title: {item["title"]}\n   Abstract: {desc or "(none)"}')

    prompt = (
        "You are a professional news editor. For each numbered news item below, "
        "translate the title to Simplified Chinese and write a 2–3 sentence Chinese "
        "summary (80–150 characters) based on the title and abstract.\n\n"
        "Output ONLY a raw JSON array, no markdown fences, no explanation:\n"
        '[{"title_cn":"<Chinese title>","summary_cn":"<Chinese summary>"},...]\n\n'
        + "\n\n".join(parts)
    )

    try:
        raw  = _call_llm(prompt, max_tokens=2048)
        data = _parse_llm_json(raw)

        if data is not None:
            result = []
            for i, item in enumerate(items):
                entry = data[i] if i < len(data) and isinstance(data[i], dict) else {}
                result.append({
                    "title_cn":   (entry.get("title_cn") or "").strip() or item["title"],
                    "summary_cn": (entry.get("summary_cn") or "").strip() or item.get("description", ""),
                })
            return result

        # JSON completely unparseable — fall back to simple title-only translation
        print(f"      ✗ JSON parse failed, retrying with simpler prompt...")
        titles_cn = _translate_titles_simple(items)
        return [
            {"title_cn": titles_cn[i], "summary_cn": item.get("description", "")}
            for i, item in enumerate(items)
        ]

    except Exception as e:
        print(f"      ✗ LLM error: {e}")
        return fallback


# ── HTML rendering ────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _render_section(items: list[dict], translated: list[dict]) -> str:
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


# ── Embedded HTML template ────────────────────────────────────────────────────

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
  <div class="section"><h2>🌍 政治国际</h2><div id="political-international"></div></div>
  <div class="section"><h2>💡 ICT 前沿研究与技术突破</h2><div id="ict-research-breakthroughs"></div></div>
  <div class="section"><h2>🏢 高科技企业重大新闻</h2><div id="ict-business-news"></div></div>
  <div class="section"><h2>🇮🇪 爱尔兰新闻</h2><div id="ireland-news"></div></div>
  <div class="section"><h2>🎬 娱乐</h2><div id="entertainment"></div></div>
  <div class="section"><h2>🇨🇳 中国要闻</h2><div id="china-news"></div></div>
  <div class="footer">
    wisp-agent · 自动生成 ·
    数据来源：Reuters · AP · BBC · FT · WSJ · Nature · IEEE · MIT TR ·
    The Verge · Ars Technica · Wired · Variety · Deadline · Billboard ·
    RTÉ · Irish Times · Irish Independent · The Journal
  </div>
</div>
</body>
</html>
"""


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_report(
    sections: list[str] | None = None,
    open_browser: bool = True,
) -> str:
    """Generate the daily briefing HTML.  Returns the output file path."""
    if _OUTPUT_DIR is None:
        raise RuntimeError("briefing not initialized — call init_briefing() first")

    active = [s for s in ALL_SECTIONS if s in (sections or ALL_SECTIONS)]
    today  = date.today()
    out    = _OUTPUT_DIR / f"daily-report-{today.strftime('%Y%m%d')}.html"

    print(f"  generating daily briefing  ({len(active)} sections)...")

    # ── 1. history dedup ──────────────────────────────────────────────────────
    history_keys = _load_history_keys()

    # ── 2. fetch all sections (isolated per-section) ──────────────────────────
    sections_data: dict[str, list[dict]] = {}
    for section in active:
        label = SECTION_LABELS[section]
        print(f"  → {label}")
        try:
            items = _fetch_section(section, history_keys)
            print(f"     {len(items)} articles")
        except Exception as e:
            print(f"     ✗ fetch error: {e}")
            items = []
        sections_data[section] = items

    # ── 3. cross-section dedup: ict_research wins over ict_business ──────────
    if "ict_research" in sections_data and "ict_business" in sections_data:
        research_keys = {_dedup_key(i["url"]) for i in sections_data["ict_research"]}
        sections_data["ict_business"] = [
            i for i in sections_data["ict_business"]
            if _dedup_key(i["url"]) not in research_keys
        ][:_MAX_ITEMS]

    # ── 4. translate + summarize (1 LLM call per section) ─────────────────────
    print(f"\n  translating & summarizing...")
    sections_t: dict[str, list[dict]] = {}
    for section in active:
        items = sections_data.get(section, [])
        if not items:
            sections_t[section] = []
            continue
        print(f"  → {SECTION_LABELS[section]}")
        sections_t[section] = _translate_and_summarize(items)

    # ── 5. render HTML ─────────────────────────────────────────────────────────
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    date_str  = f"{today.year}年{today.month}月{today.day}日 {weekdays[today.weekday()]}"
    tpl = _HTML_TEMPLATE.replace("{{REPORT_DATE}}", date_str)

    for section in ALL_SECTIONS:
        div_id = SECTION_DIV_IDS[section]
        items  = sections_data.get(section, [])
        trans  = sections_t.get(section, [])
        tpl    = tpl.replace(f'<div id="{div_id}"></div>', _render_section(items, trans))

    out.write_text(tpl, encoding="utf-8")
    _cleanup_old_reports()

    total = sum(len(v) for v in sections_data.values())
    result = f"ok: daily briefing saved → {out}\n    {len(active)} sections · {total} articles"
    print(f"\n  ✓ {out}")

    # ── 6. open in browser ────────────────────────────────────────────────────
    if open_browser:
        try:
            subprocess.run(["open", str(out)], check=True)
        except Exception as e:
            result += f"\n    note: could not open browser: {e}"

    return result
