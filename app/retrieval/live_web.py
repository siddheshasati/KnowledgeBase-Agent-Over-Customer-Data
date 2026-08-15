import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import Settings
from app.models import Evidence, SourceType

logger = logging.getLogger(__name__)


class LiveFlytBaseRetriever:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.live_fetch_timeout_seconds,
            headers={"User-Agent": "FlytBase-Knowledge-Agent/1.0"},
            follow_redirects=True,
        )
        self._sitemap_cache: dict[str, tuple[datetime, list[str]]] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def search_docs(self, query: str) -> list[Evidence]:
        return await self._search_host("docs.flytbase.com", query, SourceType.live_product_docs)

    async def search_releases(self, query: str) -> list[Evidence]:
        return await self._search_host("releases.flytbase.com", query, SourceType.live_release_notes)

    async def _search_host(self, host: str, query: str, source_type: SourceType) -> list[Evidence]:
        if host not in self.settings.live_hosts:
            return []
        urls = await self._candidate_urls(host, query)
        pages = await asyncio.gather(*(self._fetch_page(url) for url in urls[: self.settings.live_max_pages]), return_exceptions=True)
        evidence: list[Evidence] = []
        for page in pages:
            if isinstance(page, Exception) or page is None:
                continue
            score = _score(query, f"{page['title']} {page['text']} {page['url']}")
            if score <= 0:
                continue
            evidence.append(
                Evidence(
                    id=f"{source_type.value}:{hashlib.sha1(page['url'].encode('utf-8')).hexdigest()[:12]}",
                    source_type=source_type,
                    title=page["title"] or page["url"],
                    url=page["url"],
                    snippet=_best_snippet(query, page["text"]),
                    score=score,
                    metadata={"retrieved_at": datetime.utcnow().isoformat(), "host": host},
                )
            )
        return sorted(evidence, key=lambda item: item.score, reverse=True)[: self.settings.evidence_top_k]

    async def _candidate_urls(self, host: str, query: str) -> list[str]:
        urls = await self._sitemap_urls(host)
        if not urls:
            urls = [f"https://{host}/"]
        terms = _terms(query)
        scored = []
        for url in urls:
            lowered = url.lower()
            score = sum(2 for term in terms if term in lowered)
            if any(path_hint in lowered for path_hint in ["release", "changelog", "docs", "guide", "feature"]):
                score += 1
            scored.append((score, url))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [url for _, url in scored[: max(self.settings.live_max_pages * 3, 12)]]

    async def _sitemap_urls(self, host: str) -> list[str]:
        cached = self._sitemap_cache.get(host)
        if cached and cached[0] > datetime.utcnow() - timedelta(minutes=30):
            return cached[1]
        candidates = [f"https://{host}/sitemap.xml", f"https://{host}/sitemap_index.xml"]
        urls: list[str] = []
        for sitemap_url in candidates:
            try:
                response = await self._get(sitemap_url)
                if response.status_code >= 400:
                    continue
                parsed_urls, nested_sitemaps = _parse_sitemap(response.text, host)
                urls.extend(parsed_urls)
                for nested in nested_sitemaps[:8]:
                    nested_response = await self._get(nested)
                    if nested_response.status_code < 400:
                        nested_urls, _ = _parse_sitemap(nested_response.text, host)
                        urls.extend(nested_urls)
            except Exception as exc:
                logger.debug("Sitemap fetch failed for %s: %s", sitemap_url, exc)
        deduped = sorted(set(urls))
        self._sitemap_cache[host] = (datetime.utcnow(), deduped)
        return deduped

    async def _fetch_page(self, url: str) -> dict | None:
        parsed = urlparse(url)
        if parsed.hostname not in self.settings.live_hosts:
            return None
        response = await self._get(url)
        if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", ""):
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        title = (soup.title.string or "").strip() if soup.title else ""
        main = soup.find("main") or soup.body or soup
        text = re.sub(r"\s+", " ", main.get_text(" ", strip=True))
        return {"url": str(response.url), "title": title, "text": text[:20000]}

    @retry(wait=wait_exponential(multiplier=1, min=1, max=6), stop=stop_after_attempt(3))
    async def _get(self, url: str) -> httpx.Response:
        return await self.client.get(url)


def _parse_sitemap(xml_text: str, host: str) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    sitemaps: list[str] = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return urls, sitemaps
    namespace = ""
    if root.tag.startswith("{"):
        namespace = root.tag.split("}")[0] + "}"
    if root.tag.endswith("sitemapindex"):
        for loc in root.findall(f".//{namespace}loc"):
            if loc.text and urlparse(loc.text).hostname == host:
                sitemaps.append(loc.text)
        return urls, sitemaps
    for loc in root.findall(f".//{namespace}loc"):
        if loc.text and urlparse(loc.text).hostname == host:
            urls.append(loc.text)
    return urls, sitemaps


def _terms(query: str) -> list[str]:
    return [term for term in re.findall(r"[a-z0-9][a-z0-9-]{2,}", query.lower()) if term not in {"the", "and", "for", "what", "with", "from", "that"}]


def _score(query: str, text: str) -> float:
    lowered = text.lower()
    return float(sum(1 for term in _terms(query) if term in lowered))


def _best_snippet(query: str, text: str, length: int = 900) -> str:
    lowered = text.lower()
    terms = _terms(query)
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, min(positions) - 180) if positions else 0
    snippet = text[start : start + length].strip()
    return snippet or text[:length].strip()
