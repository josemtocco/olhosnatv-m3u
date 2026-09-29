#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "channels.json"
PLAYLIST_FILE = ROOT / "playlist.m3u"

LOG = logging.getLogger("olhosnatv")

CATEGORY_RE = re.compile(r"/p/[^/?#]+\.html(?:[?#].*)?$", re.I)
LABEL_RE = re.compile(r"/search/label/[^/?#]+(?:[?#].*)?$", re.I)
POST_RE = re.compile(r"/\d{4}/\d{2}/[^/?#]+\.html(?:[?#].*)?$", re.I)
MEDIA_RE = re.compile(
    r"(?P<url>(?:https?:)?//[^\"'<>\s]+?\.(?:m3u8|mpd|mp4|m4v|ts|aac|mp3)(?:\?[^\"'<>\s]*)?)",
    re.I,
)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_url(url: str, base: str) -> str | None:
    if not url:
        return None
    url = html.unescape(url.strip().replace("\\/", "/"))
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(("http://", "https://")):
        url = urljoin(base, url)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def same_domain(url: str, source_url: str) -> bool:
    a = urlparse(url).netloc.lower().split(":")[0]
    b = urlparse(source_url).netloc.lower().split(":")[0]
    return a == b or a == b.removeprefix("www.") or b == a.removeprefix("www.")


def clean_name(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"^(assistir|ver|ao vivo)\s+", "", text, flags=re.I)
    return text or "Canal sem nome"


class Scraper:
    def __init__(self, config: dict):
        self.cfg = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config["user_agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
        })
        self.source = config["source_url"]
        self.host = urlparse(self.source).netloc
        self.visited = set()
        self.sleep_seconds = float(config.get("request_delay_seconds", 0.35))

    def get(self, url: str, timeout: int | None = None) -> requests.Response | None:
        try:
            if self.sleep_seconds:
                time.sleep(self.sleep_seconds)
            response = self.session.get(
                url,
                timeout=timeout or self.cfg["request_timeout"],
                allow_redirects=True,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            LOG.debug("GET falhou %s: %s", url, exc)
            return None

    def discover_categories(self) -> OrderedDict[str, str]:
        result: OrderedDict[str, str] = OrderedDict()
        for page_url in [self.cfg.get("categories_url"), self.source]:
            if not page_url:
                continue
            response = self.get(page_url)
            if not response:
                continue
            soup = BeautifulSoup(response.text, "lxml")
            for a in soup.select("a[href]"):
                href = normalize_url(a.get("href"), response.url)
                text = clean_name(a.get_text(" ", strip=True))
                if not href or not same_domain(href, self.source):
                    continue
                if not (CATEGORY_RE.search(urlparse(href).path) or LABEL_RE.search(urlparse(href).path)):
                    continue
                if text.upper() in {"CATEGORIAS", "INÍCIO", "ADICIONAR CANAL", "CONTATO"}:
                    continue
                result[href.split("#", 1)[0]] = text
        LOG.info("Categorias descobertas: %d", len(result))
        return result

    def discover_posts(self, category_urls: OrderedDict[str, str]) -> dict[str, set[str]]:
        posts: dict[str, set[str]] = {}
        queue = deque((url, category) for url, category in category_urls.items())
        seen_pages = set()
        while queue and len(seen_pages) < self.cfg["max_category_pages"] * max(1, len(category_urls)):
            page_url, category = queue.popleft()
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            response = self.get(page_url)
            if not response:
                continue
            soup = BeautifulSoup(response.text, "lxml")
            for a in soup.select("a[href]"):
                href = normalize_url(a.get("href"), response.url)
                if not href or not same_domain(href, self.source):
                    continue
                href = href.split("#", 1)[0]
                if POST_RE.search(urlparse(href).path):
                    posts.setdefault(href, set()).add(category)
                elif (CATEGORY_RE.search(urlparse(href).path) or LABEL_RE.search(urlparse(href).path)) and href not in seen_pages:
                    queue.append((href, category))
        LOG.info("Páginas de canais descobertas: %d", len(posts))
        return posts

    def extract_channel(self, url: str, categories: set[str]) -> dict | None:
        response = self.get(url)
        if not response:
            return None
        soup = BeautifulSoup(response.text, "lxml")
        title = soup.select_one("h1.post-title, h1.entry-title, h1")
        if title is None:
            title = soup.title
        name = clean_name(title.get_text(" ", strip=True) if title else "Canal")
        name = re.sub(r"\s*[-|–—]\s*(Olhos na TV.*)$", "", name, flags=re.I).strip()
        image = None
        img = soup.select_one("article img, .post img, img")
        if img:
            image = normalize_url(img.get("src") or img.get("data-src"), response.url)

        candidates = self.extract_media_urls(response.text, response.url)
        embeds = []
        for tag in soup.find_all(["iframe", "video", "source"]):
            for attr in ("src", "data-src", "data-url", "href"):
                value = normalize_url(tag.get(attr), response.url)
                if value:
                    embeds.append(value)
        candidates.extend(embeds)

        candidates = list(dict.fromkeys(candidates))
        media = self.resolve_candidates(candidates, response.url)
        if not media:
            LOG.debug("Sem mídia resolvida: %s", url)
            return None

        return {
            "id": stable_id(url),
            "name": name,
            "categories": sorted(c for c in categories if c),
            "source_page": url,
            "logo": image,
            "stream_url": media[0],
            "discovered_streams": media,
            "active": True,
        }

    def extract_media_urls(self, text: str, base: str) -> list[str]:
        text = html.unescape(text).replace("\\/", "/")
        found = []
        for match in MEDIA_RE.finditer(text):
            url = normalize_url(match.group("url"), base)
            if url:
                found.append(url)
        # Common JS/player attributes that don't necessarily include a media extension.
        for pattern in [
            r"(?:file|source|src|url|streamUrl|stream_url|hls|dash)\s*[:=]\s*[\"']([^\"']+)[\"']",
            r'(?:file|source|src|url|streamUrl|stream_url|hls|dash)\s*[:=]\s*([^,}\s"\']+)',
        ]:
            for match in re.finditer(pattern, text, flags=re.I):
                url = normalize_url(match.group(1), base)
                if url:
                    found.append(url)
        return list(dict.fromkeys(found))

    def resolve_candidates(self, candidates: Iterable[str], base: str) -> list[str]:
        final: list[str] = []
        queue = deque((u, 0, base) for u in candidates)
        seen = set()
        while queue:
            url, depth, parent = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            if self.looks_like_media(url):
                if not self.cfg["validate_streams"] or self.validate_stream(url):
                    final.append(url)
                    if not self.cfg.get("keep_multiple_streams_per_channel"):
                        return final
                continue
            if depth >= self.cfg["max_resolve_depth"]:
                continue
            response = self.get(url, timeout=self.cfg["media_timeout"])
            if not response or "text/html" not in response.headers.get("content-type", "text/html"):
                continue
            for media in self.extract_media_urls(response.text, response.url):
                queue.append((media, depth + 1, response.url))
            soup = BeautifulSoup(response.text, "lxml")
            for tag in soup.find_all(["iframe", "video", "source"]):
                for attr in ("src", "data-src", "data-url"):
                    child = normalize_url(tag.get(attr), response.url)
                    if child:
                        queue.append((child, depth + 1, response.url))
        return final

    def looks_like_media(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        return any(path.endswith(ext) for ext in self.cfg["allowed_media_extensions"])

    def validate_stream(self, url: str) -> bool:
        headers = {"User-Agent": self.cfg["user_agent"], "Range": "bytes=0-2048"}
        try:
            r = self.session.get(url, headers=headers, timeout=self.cfg["media_timeout"], stream=True, allow_redirects=True)
            ok = r.status_code < 400
            ctype = r.headers.get("content-type", "").lower()
            if r.status_code == 405:
                r.close()
                r = self.session.head(url, headers=headers, timeout=self.cfg["media_timeout"], allow_redirects=True)
                ok = r.status_code < 400
            r.close()
            LOG.debug("Validação %s => %s (%s)", url, ok, ctype)
            return ok
        except requests.RequestException:
            return False


def stable_id(source_page: str) -> str:
    import hashlib
    return hashlib.sha256(source_page.encode("utf-8")).hexdigest()[:16]


def load_state() -> dict:
    return load_json(STATE_FILE, {"version": 1, "source": "", "updated_at": None, "channels": []})


def merge_state(old: dict, discovered: list[dict]) -> dict:
    # Only channels successfully resolved/validated enter the new playlist. Therefore
    # a channel absent from this run is considered inactive and is removed.
    by_id = {c["id"]: c for c in discovered}
    channels = sorted(by_id.values(), key=lambda c: (c.get("categories") or ["ZZZ"])[0].lower() + "\0" + c["name"].lower())
    return {
        "version": 1,
        "source": "https://www.olhosnatv.com.br/",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "channels": channels,
    }


def m3u_escape(value: str) -> str:
    return (value or "").replace('"', "'").replace("\r", " ").replace("\n", " ").strip()


def render_m3u(state: dict) -> str:
    lines = ["#EXTM3U"]
    for channel in state.get("channels", []):
        categories = channel.get("categories") or ["Sem categoria"]
        # M3U/SS IPTV usa um único group-title por entrada. Quando o site
        # coloca o mesmo canal em várias categorias, criamos uma entrada por
        # categoria para preservar exatamente essa classificação.
        for group in categories:
            lines.append(
                '#EXTINF:-1 tvg-id="{id}" tvg-name="{name}" tvg-logo="{logo}" group-title="{group}",{name}'.format(
                    id=m3u_escape(channel["id"]),
                    name=m3u_escape(channel["name"]),
                    logo=m3u_escape(channel.get("logo") or ""),
                    group=m3u_escape(group),
                )
            )
            lines.append(channel["stream_url"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera playlist M3U do Olhos na TV")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    config = load_json(CONFIG_FILE, {})
    if not config:
        raise SystemExit("config.json inválido ou ausente")

    scraper = Scraper(config)
    categories = scraper.discover_categories()
    posts = scraper.discover_posts(categories)
    discovered = []
    for index, (url, cats) in enumerate(posts.items(), 1):
        LOG.info("[%d/%d] %s", index, len(posts), url)
        channel = scraper.extract_channel(url, cats)
        if channel:
            discovered.append(channel)

    old = load_state()
    new_state = merge_state(old, discovered)

    # Safety guard: don't wipe an existing playlist because the source temporarily failed.
    if posts and not discovered and old.get("channels"):
        raise RuntimeError("Nenhum canal pôde ser resolvido; playlist antiga preservada por segurança.")

    save_json(STATE_FILE, new_state)
    PLAYLIST_FILE.write_text(render_m3u(new_state), encoding="utf-8")

    old_ids = {c.get("id") for c in old.get("channels", [])}
    new_ids = {c.get("id") for c in discovered}
    LOG.info("Ativos: %d | novos: %d | removidos: %d", len(discovered), len(new_ids - old_ids), len(old_ids - new_ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
