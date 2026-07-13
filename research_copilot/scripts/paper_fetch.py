#!/usr/bin/env python3
"""
Paper Fetch — Research Copilot no-agent cron script.

Focuses on community-vetted sources instead of raw arXiv:

  Tier 1 — HF Daily Papers (community upvoted, human-curated)
  Tier 2 — Semantic Scholar (citation signals + influential rankings)
  Tier 3 — Tavily search (web, gated to quality domains)
  Tier 4 — GitHub Trending (repo stars = real adoption)

arXiv-only papers are intentionally excluded from the default pipeline —
if a paper matters it appears on HF, Semantic Scholar, or PapersWithCode
first. Set ``RESEARCH_COPILOT_ARXIV_FALLBACK=1`` to opt back into raw arXiv.

Runs as a no-agent cron (twice daily at 06:00 / 18:00 by default).

Environment variables:
  HERMES_HOME                      — profile home; defaults to ~/.hermes.
  TAVILY_API_KEY                   — optional; enables Tavily topic search.
  RESEARCH_COPILOT_GMAIL_ADDR      — optional; Gmail address for the
                                      ResearchFeeds newsletter ingestion.
  GMAIL_APP_PASSWORD               — optional; Gmail app password for IMAP.
  RESEARCH_COPILOT_HTTP_PROXY      — optional; HTTP proxy URL for
                                      GFW-blocked sources.
  RESEARCH_COPILOT_ARXIV_FALLBACK  — set to 1/true/yes/on to opt back in.
"""
import json
import os
import re
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen, ProxyHandler, build_opener
from urllib.error import URLError, HTTPError

# ── Paths (delegated to research_copilot.storage) ────────────────────────────
# Storage helpers resolve HERMES_HOME so this script stays profile-safe and
# never writes to another profile's tree.
try:
    from research_copilot.storage import get_hermes_home, get_data_dir
except ImportError:
    # Standalone-invocation fallback: cron runs the file directly without the
    # repo on PYTHONPATH. Mirrors storage.get_hermes_home() / get_data_dir()
    # exactly; do not diverge.
    def get_hermes_home() -> Path:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()

    def get_data_dir() -> Path:
        return get_hermes_home() / "research-copilot"


HERMES_HOME = get_hermes_home()
DATA_DIR = get_data_dir()
TOPICS_PATH = DATA_DIR / "topics.json"
SOURCE_REGISTRY_PATH = DATA_DIR / "source_registry.json"
RESEARCH_PROFILE_PATH = DATA_DIR / "research_profile.json"
CANDIDATES_PATH = DATA_DIR / "candidates.jsonl"
STATE_PATH = DATA_DIR / "state.json"

TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "")
GMAIL_ADDR = os.environ.get("RESEARCH_COPILOT_GMAIL_ADDR", "")
GMAIL_PW = os.environ.get("GMAIL_APP_PASSWORD", "")

# Source quality tiers for scoring
SOURCE_TIERS = {
    "hf_daily": 1.0,       # Community-curated, highest signal
    "lab_blog": 1.0,       # Frontier lab/company research blog
    "company_blog": 0.95,  # Product/research narrative from frontier AI companies
    "technical_report": 0.95,
    "newsletter": 0.9,     # Human-curated email newsletters (high trust)
    "semantic_scholar": 0.85,  # Citation-indexed, influential rankings
    "academic_blog": 0.82,
    "researcher_blog": 0.82,
    "alphaxiv": 0.85,      # Community discussion signal
    "github_trending": 0.8,    # Stars = adoption signal
    "community_discussion": 0.72,
    "tavily": 0.7,             # Web search, gated to quality domains
    "source_registry_search": 0.72,
    "arxiv_api_fallback": 0.5, # Raw arXiv, noisy but broad coverage
}

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "text/html,application/json,*/*",
}

# Proxy for GFW-blocked sources (HF Daily Papers, AlphaXiv, etc.). Empty
# string disables the proxy; the fetcher falls back to direct connections.
PROXY_URL = os.environ.get("RESEARCH_COPILOT_HTTP_PROXY", "http://127.0.0.1:7890")

# Temporary safety valve: raw arXiv fallback is noisy and can swamp the
# profile-aware research-signal pipeline. Keep disabled unless explicitly
# re-enabled after source-registry dry-run quality is verified.
ENABLE_ARXIV_FALLBACK = os.environ.get("RESEARCH_COPILOT_ARXIV_FALLBACK", "0").lower() in {"1", "true", "yes", "on"}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default=None):
    if not path.exists():
        return default or {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default or {}


def _load_jsonl(path: Path):
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").strip().splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return items


def _append_jsonl(path: Path, item: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _fetch(url: str, timeout: int = 15) -> str:
    """Fetch a URL and return text content."""
    req = Request(url, headers=HTTP_HEADERS)
    try:
        resp = urlopen(req, timeout=timeout)
        return resp.read().decode("utf-8", errors="replace")
    except (URLError, HTTPError, OSError) as e:
        print(f"  fetch failed: {url[:60]} — {e}", file=sys.stderr)
        return ""


class _RateLimitedError(RuntimeError):
    """Raised when an upstream API returns 429 so per-topic loops can bail out
    rather than continuing to hammer the endpoint."""


def _fetch_via_proxy(url: str, timeout: int = 20) -> str:
    """Fetch a URL through the Mihomo proxy for GFW-blocked sources."""
    if not PROXY_URL:
        return _fetch(url, timeout)
    try:
        proxy_handler = ProxyHandler({
            "http": PROXY_URL,
            "https": PROXY_URL,
        })
        opener = build_opener(proxy_handler)
        req = Request(url, headers=HTTP_HEADERS)
        resp = opener.open(req, timeout=timeout)
        return resp.read().decode("utf-8", errors="replace")
    except (URLError, HTTPError, OSError) as e:
        print(f"  proxy fetch failed: {url[:60]} — {e}", file=sys.stderr)
        return ""


def _clean_html_text(text: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text or "", flags=re.DOTALL | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_feed_datetime(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    # RSS dates are inconsistent. Keep date prefix when ISO-like; otherwise store raw.
    m = re.search(r"(20\d{2})[-/](\d{2})[-/](\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return raw[:32]


def _source_type_to_item_type(source_type: str) -> str:
    return {
        "company_blog": "blog_post",
        "lab_blog": "blog_post",
        "academic_blog": "blog_post",
        "researcher_blog": "blog_post",
        "newsletter": "newsletter_item",
        "community_discussion": "discussion",
        "technical_report": "technical_report",
    }.get(source_type, "research_signal")


def _topic_matches_text(topic: dict, text: str) -> bool:
    hay = text.lower()
    excludes = [x.lower() for x in topic.get("exclude", [])]
    if any(x and x in hay for x in excludes):
        return False
    keywords = topic.get("include", []) + [topic.get("name", ""), topic.get("id", "")]
    return any(kw and kw.lower() in hay for kw in keywords)


def _topic_ids_to_names(active_topics: list) -> dict:
    return {t.get("id"): t.get("name", t.get("id")) for t in active_topics}


def _source_relevant_to_topic(source: dict, topic: dict) -> bool:
    source_topics = set(source.get("topics", []))
    return not source_topics or topic.get("id") in source_topics or topic.get("name") in source_topics


def _arxiv_id_from_url(url: str) -> str:
    m = re.search(r'arxiv\.org/(?:abs|pdf)/(\d+\.\d+)', url)
    return m.group(1) if m else ""


def _normalize_title(title: str) -> str:
    t = title.lower().strip()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()[:80]


def _papers_match(a: dict, b: dict) -> bool:
    au = (a.get("url") or "").rstrip("/")
    bu = (b.get("url") or "").rstrip("/")
    if au and bu and au == bu:
        return True
    aid = a.get("arxiv_id", "") or _arxiv_id_from_url(a.get("url", ""))
    bid = b.get("arxiv_id", "") or _arxiv_id_from_url(b.get("url", ""))
    if aid and bid and aid == bid:
        return True
    at = _normalize_title(a.get("title", ""))
    bt = _normalize_title(b.get("title", ""))
    if at and bt and at == bt:
        aa = (a.get("authors") or [None])[0] if isinstance(a.get("authors"), list) else None
        ba = (b.get("authors") or [None])[0] if isinstance(b.get("authors"), list) else None
        if aa and ba and aa == ba:
            return True
    return False


def _is_duplicate(new: dict, existing: list) -> bool:
    for ex in existing:
        if _papers_match(new, ex):
            return True
    return False


# ── Source 1: Hugging Face Daily Papers ──────────────────────────────────────

def fetch_hf_daily_papers() -> list:
    """
    Scrape huggingface.co/papers for the daily community-curated list.

    Returns list of dicts with title, url, authors, summary.
    """
    html = _fetch_via_proxy("https://huggingface.co/papers")
    if not html:
        return []

    papers = []
    # HF papers page uses paper cards. Look for paper links.
    # Pattern: /papers/<id> with title and metadata
    paper_blocks = re.findall(
        r'<article[^>]*>.*?href="(/papers/[^"]+)".*?</article>',
        html, re.DOTALL
    )

    # Alternative: simpler title+link extraction
    links = re.findall(
        r'href="(https://huggingface\.co/papers/[^"]+)"[^>]*>(.*?)</a>',
        html
    )
    seen_ids = set()
    for url, title_text in links:
        paper_id = url.split("/papers/")[-1].split("?")[0].split("#")[0]
        if paper_id in seen_ids:
            continue
        seen_ids.add(paper_id)

        title = re.sub(r'<[^>]+>', '', title_text).strip()
        if not title or len(title) < 5:
            continue

        papers.append({
            "title": title,
            "url": url,
            "arxiv_id": "",  # will be resolved later
            "summary": "",
            "authors": [],
            "published": _now_iso()[:10],
            "source": "hf_daily",
        })

    # Fallback: try JSON endpoint
    if len(papers) < 3:
        try:
            json_data = _fetch_via_proxy("https://huggingface.co/api/daily_papers")
            if json_data:
                api_papers = json.loads(json_data)
                for p in api_papers[:30]:
                    title = p.get("title", "")
                    if not title:
                        continue
                    paper_url = f"https://huggingface.co/papers/{p.get('id', '')}"
                    papers.append({
                        "title": title,
                        "url": paper_url,
                        "arxiv_id": p.get("id", ""),
                        "summary": p.get("summary", "") or "",
                        "authors": [a.get("name", "") for a in p.get("authors", []) if a.get("name")],
                        "published": (p.get("publishedAt") or _now_iso())[:10],
                        "source": "hf_daily",
                    })
        except (json.JSONDecodeError, OSError):
            pass

    return papers


# ── Source 2: Semantic Scholar API ──────────────────────────────────────────

def fetch_semantic_scholar(query: str, limit: int = 5) -> list:
    """
    Search Semantic Scholar API. Free, no key required for basic search.
    Returns papers ranked by relevance with citation counts.

    Raises ``_RateLimitedError`` on HTTP 429 so callers can back off for
    the rest of the run rather than hammering the endpoint per topic.
    """
    import urllib.parse
    q = urllib.parse.quote(query)
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/search"
        f"?query={q}&limit={limit}&fields=title,url,authors,year,externalIds,publicationDate"
    )
    req = Request(url, headers=HTTP_HEADERS)
    try:
        resp = urlopen(req, timeout=15)
        raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        if e.code == 429:
            raise _RateLimitedError(f"Semantic Scholar 429 on query {query!r}") from None
        print(f"  fetch failed: {url[:60]} — {e}", file=sys.stderr)
        return []
    except (URLError, OSError) as e:
        print(f"  fetch failed: {url[:60]} — {e}", file=sys.stderr)
        return []

    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    results = []
    for p in data.get("data", []):
        title = p.get("title", "")
        if not title:
            continue

        ext_ids = p.get("externalIds", {}) or {}
        arxiv_id = ext_ids.get("ArXiv", "")
        paper_url = p.get("url", "") or f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""

        results.append({
            "title": title,
            "url": paper_url,
            "arxiv_id": arxiv_id,
            "summary": "",
            "authors": [a.get("name", "") for a in (p.get("authors") or []) if a.get("name")],
            "published": (p.get("publicationDate") or "")[:10],
            "source": "semantic_scholar",
        })

    return results


# ── Source 3: GitHub Trending ────────────────────────────────────────────────

def fetch_github_trending(language: str = "python") -> list:
    """
    Scrape GitHub Trending for AI/ML repositories.
    """
    html = _fetch(f"https://github.com/trending/{language}?since=daily")
    if not html:
        return []

    repos = []
    # Extract repo blocks
    blocks = re.findall(
        r'<article class="Box-row">(.*?)</article>',
        html, re.DOTALL
    )
    for block in blocks[:15]:
        name_m = re.search(r'href="/([^"]+)"', block)
        desc_m = re.search(r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', block, re.DOTALL)
        stars_m = re.search(
            r'<span class="d-inline-block float-sm-right">\s*(\d[\d,]*)\s*stars\s*today',
            block
        )

        if not name_m:
            continue
        repo_path = name_m.group(1)
        repo_url = f"https://github.com/{repo_path}"
        description = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip() if desc_m else ""
        stars = stars_m.group(1) if stars_m else ""

        # Only keep AI/ML-related repos
        desc_lower = description.lower()
        keywords = ["llm", "agent", "ai", "language model", "transformer", "diffusion",
                     "deep learning", "machine learning", "neural", "rl", "reinforcement",
                     "benchmark", "evaluation", "dataset", "training", "inference"]
        if not any(k in desc_lower for k in keywords):
            continue

        repos.append({
            "title": repo_path.split("/")[-1],
            "url": repo_url,
            "arxiv_id": "",
            "summary": f"{description}  ⭐ {stars} today" if stars else description,
            "authors": [repo_path.split("/")[0]],
            "published": _now_iso()[:10],
            "source": "github_trending",
        })
    repos.sort(key=lambda r: -(r.get("stars", 0)))
    return repos[:15]


# ── Source 4b: arXiv API (fallback, tier 0.5) ──────────────────────────────

def search_arxiv_by_keyword(query: str, max_results: int = 5) -> list:
    """Search arXiv API directly. Free, no key needed.
    Marked as fallback because raw arXiv is noisy (AIGC slop).

    Enforces a ~3s inter-request delay (per arXiv API TOS) so batch queries
    don't hit HTTP 429 after the first few. Retries once on 429 with a
    longer backoff.
    """
    import urllib.parse
    # arXiv recommends >=3s between requests. Sleep before every call so
    # per-topic batch loops don't burst.
    _now = time.monotonic()
    global _LAST_ARXIV_CALL_AT  # type: ignore[name-defined]
    try:
        _last = _LAST_ARXIV_CALL_AT  # type: ignore[name-defined]
    except NameError:
        _last = 0.0
    gap = _now - _last
    if gap < 3.5:
        time.sleep(3.5 - gap)
    _q_raw = query.strip()
    # Wrap multi-word queries as a phrase so arxiv does not AND the tokens across all fields
    # (unquoted "speculative decoding" matches any paper containing both words anywhere, which
    # buries topical papers under thousands of loose matches).
    if " " in _q_raw:
        q = urllib.parse.quote(f'"{_q_raw}"')
    else:
        q = urllib.parse.quote(_q_raw)
    url = f"https://export.arxiv.org/api/query?search_query=all:{q}&start=0&max_results={max_results}&sortBy=submittedDate&sortOrder=descending"
    attempt_max = 2
    xml = ""
    for attempt in range(1, attempt_max + 1):
        try:
            resp = urlopen(url, timeout=15)
            xml = resp.read().decode()
            break
        except HTTPError as e:
            if e.code == 429 and attempt < attempt_max:
                time.sleep(15)
                continue
            print(f"  arXiv error: {e}", file=sys.stderr)
            _LAST_ARXIV_CALL_AT = time.monotonic()  # type: ignore[name-defined]
            return []
        except (URLError, OSError) as e:
            print(f"  arXiv error: {e}", file=sys.stderr)
            _LAST_ARXIV_CALL_AT = time.monotonic()  # type: ignore[name-defined]
            return []
    _LAST_ARXIV_CALL_AT = time.monotonic()  # type: ignore[name-defined]
    if not xml:
        return []

    papers = []
    entries = re.findall(r'<entry>(.*?)</entry>', xml, re.DOTALL)
    for entry in entries:
        title_m = re.search(r'<title>(.*?)</title>', entry, re.DOTALL)
        id_m = re.search(r'<id>(.*?)</id>', entry)
        summary_m = re.search(r'<summary>(.*?)</summary>', entry, re.DOTALL)
        published_m = re.search(r'<published>(.*?)</published>', entry)
        authors = re.findall(r'<name>(.*?)</name>', entry)
        title = title_m.group(1).strip() if title_m else ""
        paper_id = id_m.group(1).strip() if id_m else ""
        summary = summary_m.group(1).strip() if summary_m else ""
        published = published_m.group(1).strip()[:10] if published_m else ""
        arxiv_id = _arxiv_id_from_url(paper_id)
        url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else paper_id
        papers.append({
            "title": title, "url": url, "arxiv_id": arxiv_id,
            "summary": summary[:400], "authors": authors[:5],
            "published": published, "source": "arxiv_api_fallback",
        })
    return papers


# ── Source 4c: AlphaXiv (recently discussed papers) ──────────────────────────

def fetch_alphaxiv_recent() -> list:
    """
    Scrape AlphaXiv for recently discussed papers. AlphaXiv is a platform
    for commenting on arXiv papers — active discussion is a community signal.
    """
    html = _fetch_via_proxy("https://alphaxiv.org")
    if not html:
        return []

    papers = []
    # Look for paper links: /abs/YYMM.NNNNN or /p/YYMM.NNNNN
    links = re.findall(
        r'href="[^"]*/(?:abs|p)/(\d+\.\d+)"[^>]*>(.*?)</a>',
        html, re.IGNORECASE
    )
    seen = set()
    for arxiv_id, title_text in links:
        if arxiv_id in seen:
            continue
        seen.add(arxiv_id)
        title = re.sub(r'<[^>]+>', '', title_text).strip()
        if not title:
            title = f"arXiv {arxiv_id}"
        papers.append({
            "title": title,
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "arxiv_id": arxiv_id,
            "summary": "",
            "authors": [],
            "published": _now_iso()[:10],
            "source": "alphaxiv",
        })

    # Fallback: parse main page
    if len(papers) < 3:
        links2 = re.findall(
            r'href="(https?://alphaxiv\.org/[^"]+)"',
            html
        )
        for link in links2[:20]:
            arxiv_id = re.search(r'/(\d+\.\d+)', link)
            if arxiv_id and arxiv_id.group(1) not in seen:
                seen.add(arxiv_id.group(1))
                papers.append({
                    "title": f"arXiv {arxiv_id.group(1)}",
                    "url": f"https://arxiv.org/abs/{arxiv_id.group(1)}",
                    "arxiv_id": arxiv_id.group(1),
                    "summary": "",
                    "authors": [],
                    "published": _now_iso()[:10],
                    "source": "alphaxiv",
                })

    return papers


# ── Source 5: Gmail Newsletters (ResearchFeeds label) ────────────────────────

def fetch_newsletters() -> list:
    """Read emails from Gmail's ResearchFeeds label via IMAP.
    Extracts paper links (arXiv, Semantic Scholar) from email bodies.
    Returns list of candidate dicts with source='newsletter' (tier 0.9).
    """
    if not GMAIL_PW:
        return []
    try:
        import imaplib, email as eml
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_ADDR, GMAIL_PW)
    except Exception as e:
        print(f"Gmail IMAP login failed: {e}", file=sys.stderr)
        return []

    papers = []
    seen_urls = set()

    try:
        status, _data = mail.select('"ResearchFeeds"', readonly=True)
        if status != "OK":
            print("  ResearchFeeds label not found", file=sys.stderr)
            mail.logout()
            return []

        _since = datetime.now(timezone.utc).strftime("%d-%b-%Y")
        status, ids = mail.search(None, f'(SINCE {_since})')
        if status != "OK" or not ids[0]:
            print("  No recent emails in ResearchFeeds", file=sys.stderr)
            mail.logout()
            return []

        msg_ids = ids[0].split()
        print(f"  {len(msg_ids)} emails in ResearchFeeds", file=sys.stderr)

        for mid in msg_ids[-20:]:
            try:
                _st, data = mail.fetch(mid, "(RFC822)")
                if _st != "OK":
                    continue
                raw = eml.message_from_bytes(data[0][1])
                body = ""
                if raw.is_multipart():
                    for part in raw.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True) or b""
                            body = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
                            break
                else:
                    body = raw.get_payload(decode=True) or b""
                    body = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)

                for m in re.finditer(r'arxiv\.org/abs/(\d+\.\d+)', body):
                    aid = m.group(1)
                    url = f"https://arxiv.org/abs/{aid}"
                    if url not in seen_urls:
                        seen_urls.add(url)
                        papers.append({
                            "title": aid, "url": url, "arxiv_id": aid,
                            "summary": "", "authors": [],
                            "published": "", "source": "newsletter",
                        })

                for m in re.finditer(r'semanticscholar\.org/[^\s")]+', body):
                    url = f"https://www.{m.group(0).rstrip('.)')}"
                    if url not in seen_urls:
                        seen_urls.add(url)
            except Exception as e:
                print(f"  email {mid}: {e}", file=sys.stderr)
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return papers


# ── Source 6: Tavily (gated to quality domains) ─────────────────────────────

def fetch_tavily(query: str, max_results: int = 5) -> list:
    """Tavily search restricted to high-quality academic/community domains."""
    if not TAVILY_KEY:
        return []
    url = "https://api.tavily.com/search"
    payload = json.dumps({
        "api_key": TAVILY_KEY,
        "query": query,
        "search_depth": "advanced",
        "max_results": max_results,
        "include_domains": [
        "huggingface.co/papers",
        "paperswithcode.com",
        "semanticscholar.org",
        "openreview.net",
        "alphaxiv.org",
        "blog.google",
        "openai.com",
        "anthropic.com",
        "deepmind.google",
        "research.google",
        "ai.meta.com",
        "allenai.org",
        "bair.berkeley.edu",
        "crfm.stanford.edu",
        "interconnects.ai",
        "latent.space",
        "lesswrong.com",
        "alignmentforum.org",
        "github.com",
        ],
    }).encode()
    req = Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        resp = urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
        results = []
        for r in data.get("results", []):
            title = r.get("title", "")
            url = r.get("url", "")
            if not title or not url:
                continue
            results.append({
                "title": title,
                "url": url,
                "arxiv_id": _arxiv_id_from_url(url),
                "summary": (r.get("content", "") or "")[:500],
                "authors": [],
                "published": "",
                "source": "tavily",
            })
        return results
    except (URLError, OSError, json.JSONDecodeError) as e:
        print(f"Tavily error: {e}", file=sys.stderr)
        return []


# ── Source 7: Curated source registry feeds and directed search ──────────────

def fetch_source_feed(source: dict, active_topics: list, limit: int = 8) -> list:
    """Fetch RSS/Atom items from a curated source registry entry.

    This captures frontier lab/company blogs and researcher/newsletter sources
    that often precede polished papers by weeks or months.
    """
    feed_url = (source.get("feed_url") or "").strip()
    if not feed_url:
        return []
    raw = _fetch_via_proxy(feed_url, timeout=20)
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  feed parse failed: {source.get('id')} — {e}", file=sys.stderr)
        return []

    items = []
    ns_atom = "{http://www.w3.org/2005/Atom}"
    entries = root.findall(f".//{ns_atom}entry")
    mode = "atom"
    if not entries:
        entries = root.findall(".//item")
        mode = "rss"

    for entry in entries[:limit]:
        if mode == "atom":
            title = entry.findtext(f"{ns_atom}title", "").strip()
            summary = entry.findtext(f"{ns_atom}summary", "") or entry.findtext(f"{ns_atom}content", "") or ""
            published = entry.findtext(f"{ns_atom}published", "") or entry.findtext(f"{ns_atom}updated", "")
            link = ""
            for link_el in entry.findall(f"{ns_atom}link"):
                rel = link_el.attrib.get("rel", "alternate")
                href = link_el.attrib.get("href", "")
                if href and rel in {"alternate", ""}:
                    link = href
                    break
            if not link:
                link = entry.findtext(f"{ns_atom}id", "")
        else:
            title = entry.findtext("title", "").strip()
            summary = entry.findtext("description", "") or entry.findtext("content", "") or ""
            published = entry.findtext("pubDate", "") or entry.findtext("published", "")
            link = entry.findtext("link", "").strip()

        title = _clean_html_text(title)
        summary = _clean_html_text(summary)[:700]
        if not title or not link:
            continue
        text = f"{title} {summary}"
        matched = [t for t in active_topics if _source_relevant_to_topic(source, t) and _topic_matches_text(t, text)]
        if not matched:
            continue
        for topic in matched:
            source_type = source.get("type", "source_registry_search")
            items.append({
                "title": title,
                "url": link,
                "arxiv_id": _arxiv_id_from_url(link),
                "summary": summary,
                "authors": [source.get("name", "")],
                "published": _parse_feed_datetime(published) or _now_iso()[:10],
                "source": source_type,
                "source_id": source.get("id", ""),
                "source_name": source.get("name", ""),
                "item_type": _source_type_to_item_type(source_type),
                "bias_note": source.get("bias_note", ""),
                "matched_topic": topic.get("name", ""),
            })
    return items


def fetch_tavily_for_source_registry(topic: dict, sources: list, max_results: int = 4) -> list:
    """Topic-directed web search over curated domains from source_registry.json."""
    if not TAVILY_KEY:
        return []
    domains = []
    for source in sources:
        if _source_relevant_to_topic(source, topic):
            domains.extend(source.get("domains", []))
    domains = sorted(set(d for d in domains if d))[:20]
    if not domains:
        return []

    primary_terms = topic.get("include", [])[:3] or [topic.get("name", "")]
    query = f"{topic.get('name', '')} " + " OR ".join(primary_terms)
    url = "https://api.tavily.com/search"
    payload = json.dumps({
        "api_key": TAVILY_KEY,
        "query": query,
        "search_depth": "advanced",
        "max_results": max_results,
        "include_domains": domains,
    }).encode()
    req = Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        resp = urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
    except (URLError, OSError, json.JSONDecodeError) as e:
        print(f"Tavily registry error: {topic.get('id')} — {e}", file=sys.stderr)
        return []

    results = []
    for r in data.get("results", []):
        title = r.get("title", "")
        result_url = r.get("url", "")
        if not title or not result_url:
            continue
        host = re.sub(r"^www\.", "", (result_url.split("/")[2] if "://" in result_url else ""))
        source_match = next((s for s in sources if any(host.endswith(d) for d in s.get("domains", []))), {})
        source_type = source_match.get("type", "source_registry_search")
        results.append({
            "title": title,
            "url": result_url,
            "arxiv_id": _arxiv_id_from_url(result_url),
            "summary": (r.get("content", "") or "")[:700],
            "authors": [source_match.get("name", "") or host],
            "published": "",
            "source": "source_registry_search",
            "source_id": source_match.get("id", ""),
            "source_name": source_match.get("name", ""),
            "item_type": _source_type_to_item_type(source_type),
            "bias_note": source_match.get("bias_note", ""),
            "matched_topic": topic.get("name", ""),
        })
    return results


# ── Post-processing ──────────────────────────────────────────────────────────

def _resolve_arxiv_id(candidate: dict) -> dict:
    """Try to fill missing arxiv_id from URL."""
    if not candidate.get("arxiv_id"):
        candidate["arxiv_id"] = _arxiv_id_from_url(candidate.get("url", ""))
    return candidate


def to_candidate(item: dict, topic_name: str) -> dict:
    """Convert raw item to normalized ResearchSignal candidate with source tier."""
    item = _resolve_arxiv_id(item)
    source_name = item.get("source", "unknown")
    source_id = item.get("source_id", "")
    item_type = item.get("item_type")
    if not item_type:
        item_type = "project" if source_name == "github_trending" else ("paper" if item.get("arxiv_id") else "research_signal")
    source_record = {
        "name": source_name,
        "topic": topic_name,
    }
    if source_id:
        source_record["id"] = source_id
    if item.get("source_name"):
        source_record["display_name"] = item.get("source_name")
    return {
        "id": item.get("arxiv_id", "") or f"sig_{uuid.uuid4().hex[:12]}",
        "type": item_type,
        "title": item.get("title", "Untitled").strip(),
        "authors": item.get("authors", []),
        "url": item.get("url", ""),
        "arxiv_id": item.get("arxiv_id", ""),
        "summary": (item.get("summary", "") or "")[:800],
        "published": item.get("published", "")[:32],
        "discovered_at": _now_iso(),
        "sources": [source_record],
        "source_tier": item.get("source_tier", SOURCE_TIERS.get(source_name, 0.5)),
        "topics": [{"name": topic_name, "confidence": 0.5}],
        "signal_role_candidates": [],
        "bias_note": item.get("bias_note", ""),
        "status": "candidate",
        "scored": False,
    }


# ── Main ─────────────────────────────────────────────────────────────────────


def _stage_persist(new_candidates: list, cand: dict) -> None:
    """Append a fresh candidate to the in-memory list AND flush it to disk
    immediately so a mid-run interrupt (timeout, SIGTERM) doesn't discard the
    work of earlier stages. Idempotent when combined with _is_duplicate."""
    if cand.get('_persisted'):
        return
    new_candidates.append(cand)
    _append_jsonl(CANDIDATES_PATH, cand)
    cand['_persisted'] = True

def main():
    topics = _load_json(TOPICS_PATH, {"topics": []})
    source_registry = _load_json(SOURCE_REGISTRY_PATH, {"sources": []})
    _research_profile = _load_json(RESEARCH_PROFILE_PATH, {})
    active_topics = [t for t in topics.get("topics", []) if t.get("status") == "active"]
    sources = source_registry.get("sources", [])
    if not active_topics:
        print("No active topics configured.", file=sys.stderr)
        return

    existing = _load_jsonl(CANDIDATES_PATH)
    new_candidates = []

    # 1. HF Daily Papers — the highest signal source, checked every cycle
    print("Fetching HF Daily Papers...", file=sys.stderr)
    hf_papers = fetch_hf_daily_papers()
    print(f"  → {len(hf_papers)} papers found", file=sys.stderr)

    # Tag HF papers with matching topics
    for hf in hf_papers:
        title_lower = hf.get("title", "").lower()
        summary_lower = hf.get("summary", "").lower()
        text = title_lower + " " + summary_lower
        matched_topics = []
        for topic in active_topics:
            keywords = topic.get("include", []) + [topic.get("name", "")]
            if any(kw.lower() in text for kw in keywords):
                matched_topics.append(topic["name"])
        if matched_topics:
            for tn in matched_topics:
                cand = to_candidate(hf, tn)
                if not _is_duplicate(cand, existing + new_candidates):
                    # Boost source tier — HF is top quality
                    cand["source_tier"] = 1.0
                    _stage_persist(new_candidates, cand)

    # 2. Semantic Scholar — citation-signaled results per topic.
    # Uses ``max(2, KEYWORDS_PER_TOPIC)`` include keywords per topic and bails
    # out of the whole S2 stage on the first HTTP 429 so we don't burn the
    # rest of the topics on retries.
    print("Fetching Semantic Scholar...", file=sys.stderr)
    _S2_KW_LIMIT = int(os.environ.get("RESEARCH_COPILOT_S2_KEYWORDS_PER_TOPIC", "5") or "5")
    _s2_rate_limited = False
    _s2_added = 0
    for topic in active_topics:
        if _s2_rate_limited:
            break
        # Prefer the topic name plus the top-N include keywords, deduped and
        # in original order so priority keywords go first.
        seen: set = set()
        kw_pool: list = []
        for kw in [topic.get("name", "")] + (topic.get("include") or []):
            k = (kw or "").strip()
            if k and k.lower() not in seen:
                seen.add(k.lower())
                kw_pool.append(k)
            if len(kw_pool) >= _S2_KW_LIMIT:
                break
        for kw in kw_pool:
            try:
                results = fetch_semantic_scholar(kw, limit=5)
            except _RateLimitedError as e:
                print(f"  Semantic Scholar rate-limited; skipping remaining S2 queries this run. {e}", file=sys.stderr)
                _s2_rate_limited = True
                break
            for result in results:
                cand = to_candidate(result, topic["name"])
                if not _is_duplicate(cand, existing + new_candidates):
                    _stage_persist(new_candidates, cand)
                    _s2_added += 1
    print(f"  → semantic_scholar added {_s2_added} candidates", file=sys.stderr)

    # 3. GitHub Trending for AI repos
    print("Fetching GitHub Trending...", file=sys.stderr)
    for repo in fetch_github_trending():
        text = (repo.get("title", "") + " " + repo.get("summary", "")).lower()
        for topic in active_topics:
            keywords = topic.get("include", []) + [topic.get("name", "")]
            if any(kw.lower() in text for kw in keywords):
                cand = to_candidate(repo, topic["name"])
                if not _is_duplicate(cand, existing + new_candidates):
                    _stage_persist(new_candidates, cand)

    # 4. AlphaXiv — recently discussed papers
    print("Fetching AlphaXiv...", file=sys.stderr)
    alpha_papers = fetch_alphaxiv_recent()
    print(f"  ˢ→ {len(alpha_papers)} discussed papers", file=sys.stderr)
    for ap in alpha_papers:
        text = (ap.get("title", "") + " " + ap.get("summary", "")).lower()
        for topic in active_topics:
            keywords = topic.get("include", []) + [topic.get("name", "")]
            if any(kw.lower() in text for kw in keywords):
                cand = to_candidate(ap, topic["name"])
                if not _is_duplicate(cand, existing + new_candidates):
                    _stage_persist(new_candidates, cand)

    # 5. Gmail Newsletters — curated emails from ResearchFeeds
    print("Fetching Newsletters...", file=sys.stderr)
    newsletter_papers = fetch_newsletters()
    print(f"  -> {len(newsletter_papers)} paper links from newsletters", file=sys.stderr)
    for np in newsletter_papers:
        text = (np.get("title", "") + " " + np.get("summary", "")).lower()
        for topic in active_topics:
            keywords = topic.get("include", []) + [topic.get("name", "")]
            if any(kw.lower() in text for kw in keywords) or not text:
                cand = to_candidate(np, topic["name"])
                if not _is_duplicate(cand, existing + new_candidates):
                    _stage_persist(new_candidates, cand)

    # 6. Source Registry feeds — lab/company/researcher/community signals
    print("Fetching Source Registry feeds...", file=sys.stderr)
    registry_items = []
    for source in sources:
        feed_items = fetch_source_feed(source, active_topics, limit=8)
        registry_items.extend(feed_items)
    print(f"  -> {len(registry_items)} registry feed items", file=sys.stderr)
    for item in registry_items:
        topic_name = item.get("matched_topic") or "Research Signal"
        cand = to_candidate(item, topic_name)
        if not _is_duplicate(cand, existing + new_candidates):
            _stage_persist(new_candidates, cand)

    # 7. Source Registry directed search — curated domains per topic
    print("Fetching Source Registry Tavily searches...", file=sys.stderr)
    for topic in active_topics:
        for result in fetch_tavily_for_source_registry(topic, sources, max_results=4):
            cand = to_candidate(result, topic.get("name", "Research Signal"))
            if not _is_duplicate(cand, existing + new_candidates):
                _stage_persist(new_candidates, cand)

    # 8. arXiv fallback — broad coverage but noisy. Off by default.
    # When enabled, uses ``max(2, RESEARCH_COPILOT_ARXIV_KEYWORDS_PER_TOPIC)``
    # include keywords per topic so niche topics with priority terms deeper in
    # the list (e.g. ``speculative decoding`` at position 7) still get covered.
    if ENABLE_ARXIV_FALLBACK:
        print("Fetching arXiv (fallback)...", file=sys.stderr)
        _arxiv_kw_limit = int(os.environ.get("RESEARCH_COPILOT_ARXIV_KEYWORDS_PER_TOPIC", "5") or "5")
        _arxiv_added = 0
        for topic in active_topics:
            name = topic.get("name", "")
            seen: set = set()
            kw_pool: list = []
            for kw in [name] + (topic.get("include") or []):
                k = (kw or "").strip()
                if k and k.lower() not in seen:
                    seen.add(k.lower())
                    kw_pool.append(k)
                if len(kw_pool) >= _arxiv_kw_limit:
                    break
            for kw in kw_pool:
                for result in search_arxiv_by_keyword(kw, max_results=30):
                    cand = to_candidate(result, name)
                    if not _is_duplicate(cand, existing + new_candidates):
                        _stage_persist(new_candidates, cand)
                        _arxiv_added += 1
        print(f"  → arxiv_api_fallback added {_arxiv_added} candidates", file=sys.stderr)
    else:
        print("Skipping arXiv fallback (RESEARCH_COPILOT_ARXIV_FALLBACK=0).", file=sys.stderr)

    # 9. Tavily — broad gated quality domains, per topic.
    # Same keyword-coverage expansion as the S2 stage so niche include-list
    # terms actually get queried.
    print("Fetching Tavily...", file=sys.stderr)
    _tavily_kw_limit = int(os.environ.get("RESEARCH_COPILOT_TAVILY_KEYWORDS_PER_TOPIC", "3") or "3")
    _tavily_added = 0
    for topic in active_topics:
        name = topic.get("name", "")
        seen: set = set()
        kw_pool: list = []
        for kw in [name] + (topic.get("include") or []):
            k = (kw or "").strip()
            if k and k.lower() not in seen:
                seen.add(k.lower())
                kw_pool.append(k)
            if len(kw_pool) >= _tavily_kw_limit:
                break
        for kw in kw_pool:
            for result in fetch_tavily(kw, max_results=5):
                cand = to_candidate(result, name)
                if not _is_duplicate(cand, existing + new_candidates):
                    _stage_persist(new_candidates, cand)
                    _tavily_added += 1
    print(f"  → tavily added {_tavily_added} candidates", file=sys.stderr)

    # Append any final in-memory candidates that weren't persisted yet.
    # Each per-source stage above already flushes as it goes, so this loop is
    # a belt-and-suspenders no-op on the happy path.
    for c in new_candidates:
        if not c.get('_persisted'):
            _append_jsonl(CANDIDATES_PATH, c)
            c['_persisted'] = True

    # Update state
    state = _load_json(STATE_PATH)
    state["last_fetch_at"] = _now_iso()
    state["last_fetch_count"] = len(new_candidates)
    _save_json(STATE_PATH, state)

    # Summary
    by_source = {}
    for c in new_candidates:
        src = c.get("sources", [{}])[0].get("name", "?")
        by_source[src] = by_source.get(src, 0) + 1
    src_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_source.items()))
    print(f"Fetched {len(new_candidates)} new candidates: {src_summary}")


if __name__ == "__main__":
    main()
