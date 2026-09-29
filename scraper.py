#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import OrderedDict
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

POST_RE = re.compile(r"/\d{4}/\d{2}/[^/?#]+\.html(?:[?#].*)?$", re.I)
MEDIA_RE = re.compile(
    r"""(?P<url>(?:https?:)?//[^<>"'\s\\]+?\.(?:m3u8|mpd|mp4|m4v|ts|aac|mp3)(?:\?[^<>"'\s\\]*)?)""",
    re.I,
)

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def normalize_url(url: str | None, base: str) -> str | None:
    if not url:
        return None
    url = html.unescape(str(url)).strip()
    url = url.replace("\\/", "/").replace("\\u0026", "&").replace("\\x26", "&")
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(("http://", "https://")):
        url = urljoin(base, url)
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.netloc:
        return None
    return url

def clean_name(text: str) -> str:
    text = html.unescape(str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(assistir|ver|ao vivo|tv online grátis)\s*[:|-]?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*[-|–—]\s*(Olhos na TV.*)$", "", text, flags=re.I).strip()
    return text or "Canal sem nome"

def is_generic_channel_name(name: str) -> bool:
    n = clean_name(name).casefold()
    return n in {"canal sem nome", "tv online grátis", "tv online gratis", "canal", "ao vivo", "assistir tv", "ver tv"} or len(n) < 2

def name_from_url(url: str) -> str:
    slug = urlparse(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    slug = re.sub(r"[-_]+", " ", slug)
    return clean_name(slug.replace("%20", " "))

def name_from_logo(url: str) -> str:
    if not url:
        return ""
    path = urlparse(url).path
    filename = path.rsplit("/", 1)[-1]
    filename = re.sub(r"\.(?:webp|png|jpe?g|gif|svg)$", "", filename, flags=re.I)
    filename = re.sub(r"(?:^|[-_ ])(?:logo|tv logo)$", "", filename, flags=re.I)
    return clean_name(filename.replace("%20", " ").replace("_", " ").replace("-", " "))

def best_channel_name(candidates: Iterable[str], page_url: str, logo: str = "") -> str:
    for candidate in candidates:
        value = clean_name(candidate)
        if not is_generic_channel_name(value):
            return value
    for candidate in (name_from_logo(logo), name_from_url(page_url)):
        if not is_generic_channel_name(candidate):
            return candidate
    return "Canal sem nome"

def stable_id(source_page: str) -> str:
    import hashlib
    return hashlib.sha256(source_page.encode("utf-8")).hexdigest()[:16]

class Scraper:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": cfg["user_agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
        })
        self.delay = float(cfg.get("request_delay_seconds", 0.10))
        self.max_workers = int(cfg.get("max_workers", 8))
        self._stream_cache = {}

    def get(self, url: str, timeout: int | None = None, referer: str | None = None):
        last = None
        for attempt in range(1, 4):
            try:
                if self.delay:
                    time.sleep(self.delay)
                headers = {}
                if referer:
                    headers["Referer"] = referer
                r = self.session.get(
                    url, headers=headers,
                    timeout=timeout or self.cfg["request_timeout"],
                    allow_redirects=True,
                )
                r.raise_for_status()
                return r
            except requests.RequestException as exc:
                last = exc
                LOG.warning("GET falhou (%d/3): %s -> %s", attempt, url, exc)
                time.sleep(min(2 * attempt, 5))
        return None

    def discover_feed_entries(self) -> OrderedDict[str, dict]:
        """Discover Blogger posts without depending on the legacy blogger.com feed.

        Blogger's global /feeds/<blog-id>/posts/default endpoint can return HTML or
        malformed content in some environments. The source-domain feed is tried first;
        if it is unavailable/malformed, we fall back to the blog's normal HTML pages
        and follow Blogger's "Older Posts" pagination.
        """
        result: OrderedDict[str, dict] = {}
        page_size = int(self.cfg.get("feed_page_size", 150))
        max_pages = int(self.cfg.get("max_feed_pages", 40))
        source = self.cfg["source_url"].rstrip("/") + "/"

        # 1) Native feed on the source domain. This is the current Blogger-supported
        # form and avoids the legacy blogger.com global feed endpoint.
        feed_urls = [
            f"{source}feeds/posts/default?alt=json&max-results={page_size}",
            f"{source}feeds/posts/default?alt=atom&max-results={page_size}",
        ]
        feed_ok = False
        for feed_url in feed_urls:
            LOG.info("Tentando feed do domínio: %s", feed_url)
            r = self.get(feed_url)
            if not r:
                continue
            ctype = (r.headers.get("content-type") or "").lower()
            body = r.content.lstrip()
            # JSON feed: parse directly so malformed XML cannot break the run.
            if "json" in ctype or body.startswith(b"{"):
                try:
                    data = r.json()
                    entries = data.get("feed", {}).get("entry", [])
                    for entry in entries:
                        link = next((x.get("href") for x in entry.get("link", [])
                                     if x.get("rel") == "alternate" and x.get("href")), None)
                        self._add_feed_entry(result, entry, link)
                    LOG.info("Feed JSON: %d entradas", len(result))
                    if result:
                        feed_ok = True
                        break
                except (ValueError, TypeError, AttributeError) as exc:
                    LOG.warning("Feed JSON inválido: %s", exc)
            # Atom XML: use BeautifulSoup XML parsing instead of feedparser so a
            # broken/HTML response is simply rejected and HTML fallback is used.
            elif "xml" in ctype or body.startswith(b"<?xml") or body.startswith(b"<feed"):
                try:
                    soup = BeautifulSoup(r.content, "xml")
                    entries = soup.find_all("entry")
                    for entry in entries:
                        link_tag = entry.find("link", attrs={"rel": "alternate"}) or entry.find("link")
                        link = link_tag.get("href") if link_tag else None
                        categories = [t.get("term", "") for t in entry.find_all("category")]
                        self._add_feed_entry(result, {
                            "title": {"$t": entry.find("title").get_text(" ", strip=True) if entry.find("title") else "Canal"},
                            "link": [{"rel": "alternate", "href": link}] if link else [],
                            "category": [{"term": x} for x in categories],
                            "content": {"$t": entry.find("content").get_text() if entry.find("content") else ""},
                            "summary": {"$t": entry.find("summary").get_text() if entry.find("summary") else ""},
                        }, link)
                    LOG.info("Feed Atom: %d entradas", len(result))
                    if result:
                        feed_ok = True
                        break
                except Exception as exc:
                    LOG.warning("Feed XML inválido: %s", exc)

        if feed_ok:
            return result

        # 2) Robust fallback: crawl Blogger HTML pagination. This works even when
        # the feed endpoint is blocked or malformed.
        LOG.warning("Feed indisponível; usando paginação HTML do blog.")
        next_url = source
        visited = set()
        for page in range(max_pages):
            if not next_url or next_url in visited:
                break
            visited.add(next_url)
            LOG.info("Lendo página HTML do blog: %d", page + 1)
            r = self.get(next_url)
            if not r:
                if page == 0:
                    raise RuntimeError("Não foi possível acessar o site Olhos na TV.")
                break
            soup = BeautifulSoup(r.text, "html.parser")
            found_before = len(result)
            # Blogger post containers vary by template; collecting all links matching
            # /YYYY/MM/slug.html is more stable than relying on CSS class names.
            for a in soup.find_all("a", href=True):
                href = normalize_url(a.get("href"), next_url)
                if not href or not POST_RE.search(urlparse(href).path):
                    continue
                title = clean_name(a.get_text(" ", strip=True))
                # O template atual usa “TV Online Grátis” como texto genérico do link.
                # Nesse caso, o slug da postagem é a fonte correta do nome.
                if is_generic_channel_name(title):
                    title = name_from_url(href)
                # Find category labels near the post link when possible.
                categories = []
                parent = a
                for _ in range(4):
                    parent = getattr(parent, "parent", None)
                    if not parent:
                        break
                    for ca in parent.find_all("a", href=True):
                        txt = clean_name(ca.get_text(" ", strip=True))
                        chref = ca.get("href", "")
                        if txt and "/search/label/" in chref:
                            categories.append(txt)
                result[href.split("#", 1)[0]] = {
                    "name": title,
                    "categories": sorted(set(categories), key=str.casefold),
                    "logo": "",
                }

            LOG.info("Posts encontrados nesta página: +%d (total %d)", len(result) - found_before, len(result))
            # Find Blogger's older-post pagination link.
            candidates = []
            for a in soup.find_all("a", href=True):
                txt = clean_name(a.get_text(" ", strip=True)).casefold()
                href = normalize_url(a.get("href"), next_url)
                if not href:
                    continue
                if ("older posts" in txt or "postagens mais antigas" in txt or
                    "mais antigas" in txt or "older" == txt):
                    candidates.append(href)
                elif "updated-max=" in href and "max-results=" in href:
                    candidates.append(href)
            next_url = next((u for u in candidates if u not in visited), None)
            if not next_url:
                break

        if not result:
            raise RuntimeError("O site foi acessado, mas nenhuma postagem de canal foi encontrada.")
        return result

    def _add_feed_entry(self, result: OrderedDict[str, dict], entry: dict, link: str | None) -> None:
        link = normalize_url(link, self.cfg["source_url"])
        if not link or not POST_RE.search(urlparse(link).path):
            return
        def val(obj, key, default=""):
            x = obj.get(key, default) if isinstance(obj, dict) else default
            if isinstance(x, dict):
                return x.get("$t", default)
            return x
        title = clean_name(val(entry, "title", "Canal"))
        tags = entry.get("tags") or entry.get("category") or []
        categories = []
        for tag in tags:
            term = tag.get("term", "") if isinstance(tag, dict) else ""
            term = clean_name(term)
            if term and term.upper() not in {"BLOG", "UNCATEGORIZED"}:
                categories.append(term)
        content = val(entry, "content", "") or val(entry, "summary", "")
        soup = BeautifulSoup(content, "html.parser")
        img = soup.find("img")
        logo = normalize_url(img.get("src") or img.get("data-src"), link) if img else ""
        result[link.split("#", 1)[0]] = {
            "name": title,
            "categories": sorted(set(categories), key=str.casefold),
            "logo": logo or "",
        }

    def extract_media_urls(self, text: str, base: str) -> list[str]:
        text = html.unescape(text or "")
        text = text.replace("\\/", "/").replace("\\u0026", "&").replace("\\x26", "&")
        found = []
        for m in MEDIA_RE.finditer(text):
            u = normalize_url(m.group("url"), base)
            if u:
                found.append(u)

        patterns = [
            r"""(?:file|source|src|url|streamUrl|stream_url|hls|dash)\s*[:=]\s*["']([^"']+)["']""",
            r"""(?:file|source|src|url|streamUrl|stream_url|hls|dash)\s*[:=]\s*([^,}\s"']+)""",
            r"""["'](?:file|source|src|url|hls|dash)["']\s*:\s*["']([^"']+)["']""",
        ]
        for pattern in patterns:
            for m in re.finditer(pattern, text, re.I):
                u = normalize_url(m.group(1), base)
                if u:
                    found.append(u)
        return list(dict.fromkeys(found))

    def extract_jmv_stream(self, text: str) -> list[str]:
        text = html.unescape(text or "").replace("\\/", "/")
        ids = re.findall(r"""["'](LVW-\d+)["']""", text, re.I)
        if not ids:
            return []
        # O site/players podem mudar o token. Só construímos esta URL quando
        # houver um token plausível no próprio HTML; nunca inventamos um canal.
        tokens = re.findall(r"""["']([A-Za-z0-9_-]{10,80})["']""", text)
        out = []
        for channel_id in ids:
            for token in tokens:
                if token == channel_id:
                    continue
                if token.lower().startswith(("http", "www", "cdn")):
                    continue
                out.append(f"https://cdn.live.br1.jmvstream.com/w/{channel_id}/{token}/playlist.m3u8")
        return list(dict.fromkeys(out[:10]))

    def looks_like_media(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        return any(path.endswith(ext) for ext in self.cfg["allowed_media_extensions"])

    def validate_stream(self, url: str, referer: str | None = None) -> bool:
        headers = {"User-Agent": self.cfg["user_agent"], "Range": "bytes=0-4095"}
        if referer:
            headers["Referer"] = referer
        try:
            r = self.session.get(
                url, headers=headers, timeout=self.cfg["media_timeout"],
                stream=True, allow_redirects=True
            )
            ok = r.status_code < 400
            r.close()
            if ok:
                return True
            if r.status_code in {401, 403, 405}:
                r = self.session.head(
                    url, headers=headers, timeout=self.cfg["media_timeout"],
                    allow_redirects=True
                )
                ok = r.status_code < 400
                r.close()
            return ok
        except requests.RequestException:
            return False

    def resolve_candidates(self, candidates: Iterable[str], base: str) -> list[str]:
        # Keep resolution deliberately shallow: the old implementation recursively
        # crawled every iframe/link and could take >45 minutes on a large blog.
        max_depth = int(self.cfg.get("max_resolve_depth", 2))
        max_candidates = int(self.cfg.get("max_candidates_per_channel", 8))
        queue = [(u, 0, base) for u in list(dict.fromkeys(candidates))[:max_candidates] if u]
        seen = set()
        final = []

        while queue and len(seen) < max_candidates * 3:
            url, depth, parent = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if url in self._stream_cache:
                if self._stream_cache[url]:
                    return [url]
                continue

            if self.looks_like_media(url):
                ok = (not self.cfg.get("validate_streams", True) or
                      self.validate_stream(url, parent))
                self._stream_cache[url] = ok
                if ok:
                    final.append(url)
                    if not self.cfg.get("keep_multiple_streams_per_channel", False):
                        return final
                continue

            if depth >= max_depth:
                continue
            r = self.get(url, timeout=self.cfg["media_timeout"], referer=parent)
            if not r:
                continue
            ctype = r.headers.get("content-type", "").lower()
            if "html" not in ctype and "text" not in ctype and "javascript" not in ctype:
                continue
            text = r.text
            children = []
            children.extend(self.extract_media_urls(text, r.url))
            children.extend(self.extract_jmv_stream(text))
            soup = BeautifulSoup(text, "html.parser")
            # Prefer iframe/video/source over arbitrary anchors. Arbitrary <a> crawling
            # was the main source of the runaway runtime.
            for tag in soup.find_all(["iframe", "video", "source"]):
                for attr in ("src", "data-src", "data-url", "href"):
                    child = normalize_url(tag.get(attr), r.url)
                    if child:
                        children.append(child)
            for child in list(dict.fromkeys(children))[:max_candidates]:
                queue.append((child, depth + 1, r.url))
        return final

    def extract_channel(self, page_url: str, meta: dict, old: dict | None):
        r = self.get(page_url)
        if not r:
            if old and old.get("stream_url") and self.validate_stream(old["stream_url"], page_url):
                old = dict(old)
                old["active"] = True
                old["last_seen"] = datetime.now(timezone.utc).isoformat()
                return old
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        logo = meta.get("logo", "")
        if not logo:
            img = soup.select_one("article img, .post img, img")
            if img:
                logo = normalize_url(img.get("src") or img.get("data-src"), page_url) or ""

        title_candidates = []
        title_node = soup.select_one("h1.post-title, h1.entry-title, h1")
        if title_node:
            title_candidates.append(title_node.get_text(" ", strip=True))
        og = soup.select_one('meta[property="og:title"], meta[name="twitter:title"]')
        if og and og.get("content"):
            title_candidates.append(og.get("content"))
        for img in soup.select("article img[alt], .post img[alt], img[alt]")[:3]:
            if img.get("alt"):
                title_candidates.append(img.get("alt"))
        title_candidates.append(meta.get("name", ""))
        name = best_channel_name(title_candidates, page_url, logo)

        candidates = self.extract_media_urls(r.text, page_url)
        candidates += self.extract_jmv_stream(r.text)
        for tag in soup.find_all(["iframe", "video", "source"]):
            for attr in ("src", "data-src", "data-url", "href"):
                u = normalize_url(tag.get(attr), page_url)
                if u:
                    candidates.append(u)

        media = self.resolve_candidates(list(dict.fromkeys(candidates)), page_url)
        if not media and old and old.get("stream_url"):
            if self.validate_stream(old["stream_url"], page_url):
                media = [old["stream_url"]]

        if not media:
            return None

        return {
            "id": stable_id(page_url),
            "name": name,
            "categories": meta.get("categories") or ["Sem categoria"],
            "logo": logo,
            "source_page": page_url,
            "stream_url": media[0],
            "active": True,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }

def default_state():
    return {
        "version": 2,
        "source": "https://www.olhosnatv.com.br/",
        "updated_at": None,
        "channels": [],
    }

def slugify(value: str) -> str:
    import unicodedata
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "sem-categoria"


def ssiptv_attr(value: str) -> str:
    return str(value or "").replace('"', "'").replace("\r", " ").replace("\n", " ").strip()


def raw_base(cfg: dict) -> str:
    # Ex.: https://raw.githubusercontent.com/josemtocco/olhosnatv-m3u/main
    return cfg.get("github_raw_base", "").rstrip("/")


def render_channel_lines(channel: dict, group: str) -> list[str]:
    safe_name = ssiptv_attr(channel["name"])
    safe_group = ssiptv_attr(group)
    logo = ssiptv_attr(channel.get("logo", ""))
    attrs = (
        f'tvg-id="{ssiptv_attr(channel["id"])}" '
        f'tvg-name="{safe_name}" '
        f'group-title="{safe_group}"'
    )
    if logo:
        attrs += f' tvg-logo="{logo}"'
    return [f"#EXTINF:-1 {attrs},{safe_name}", channel["stream_url"]]


def render_category_playlists(state: dict, cfg: dict) -> dict[str, str]:
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for channel in state.get("channels", []):
        for group in channel.get("categories") or ["Sem categoria"]:
            groups.setdefault(group, []).append(channel)

    result = {}
    used = set()
    for group in sorted(groups, key=str.casefold):
        base = f"categoria-{slugify(group)}"
        filename = base + ".m3u"
        n = 2
        while filename in used:
            filename = f"{base}-{n}.m3u"
            n += 1
        used.add(filename)

        lines = ['#EXTM3U size="medium"']
        for channel in sorted(groups[group], key=lambda c: c["name"].casefold()):
            lines.extend(render_channel_lines(channel, group))
        result[filename] = "\n".join(lines) + "\n"
    return result


def render_root_m3u(state: dict, cfg: dict, category_files: dict[str, str]) -> str:
    base = raw_base(cfg)
    lines = ['#EXTM3U size="medium"']
    # O SS IPTV suporta playlists aninhadas; cada item abaixo abre uma
    # playlist de categoria em vez de tentar reproduzir a própria URL.
    for filename in sorted(category_files, key=str.casefold):
        label = filename[len("categoria-"):-4].replace("-", " ").strip().title()
        # Recupera o nome original da categoria pelo conteúdo do estado.
        for channel in state.get("channels", []):
            for group in channel.get("categories") or []:
                if slugify(group) == filename[len("categoria-"):-4]:
                    label = group
                    break
            if label.casefold() != filename[len("categoria-"):-4].replace("-", " ").casefold():
                break
        url = f"{base}/{filename}" if base else filename
        lines.append(f'#EXTINF:0 type="playlist" tvg-name="{ssiptv_attr(label)}",{ssiptv_attr(label)}')
        lines.append("#EXTSIZE:medium")
        lines.append(url)
    return "\n".join(lines) + "\n"


def cleanup_old_category_files(active_files: set[str]) -> None:
    for path in ROOT.glob("categoria-*.m3u"):
        if path.name not in active_files:
            path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = load_json(CONFIG_FILE, {})
    if not cfg:
        raise SystemExit("config.json ausente ou inválido")

    scraper = Scraper(cfg)
    old_state = load_json(STATE_FILE, default_state())
    old_by_url = {c.get("source_page"): c for c in old_state.get("channels", []) if c.get("source_page")}

    entries = scraper.discover_feed_entries()
    if not entries:
        raise RuntimeError("Nenhum canal descoberto; a playlist anterior não será alterada.")

    discovered = []
    failures = 0
    total = len(entries)
    LOG.info("Processando %d páginas com até %d workers", total, scraper.max_workers)

    def work(item):
        page_url, meta = item
        return page_url, meta, scraper.extract_channel(page_url, meta, old_by_url.get(page_url))

    with ThreadPoolExecutor(max_workers=scraper.max_workers) as pool:
        futures = [pool.submit(work, item) for item in entries.items()]
        for i, future in enumerate(as_completed(futures), 1):
            page_url, meta, channel = future.result()
            LOG.info("[%d/%d] %s -> %s", i, total, meta["name"], "OK" if channel else "falhou")
            if channel:
                discovered.append(channel)
            else:
                failures += 1

    # Nunca apaga uma playlist inteira por falha transitória do site.
    if not discovered:
        raise RuntimeError(
            f"0 canais ativos encontrados entre {total} posts. "
            "A playlist anterior foi preservada."
        )

    # Se quase tudo falhar, interrompe para evitar uma limpeza acidental.
    max_failure_ratio = 0.90
    if total >= 10 and failures / total > max_failure_ratio:
        raise RuntimeError(
            f"{failures}/{total} canais falharam ({failures/total:.0%}). "
            "Possível bloqueio/falha da fonte; playlist anterior preservada."
        )

    by_id = {c["id"]: c for c in discovered}
    channels = sorted(
        by_id.values(),
        key=lambda c: ((c.get("categories") or ["ZZZ"])[0].casefold(), c["name"].casefold())
    )

    new_state = {
        "version": 2,
        "source": cfg["source_url"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "channels": channels,
    }
    category_files = render_category_playlists(new_state, cfg)
    for filename, content in category_files.items():
        (ROOT / filename).write_text(content, encoding="utf-8", newline="\n")
    cleanup_old_category_files(set(category_files))

    save_json(STATE_FILE, new_state)
    PLAYLIST_FILE.write_text(render_root_m3u(new_state, cfg, category_files), encoding="utf-8", newline="\n")

    old_ids = {c.get("id") for c in old_state.get("channels", [])}
    new_ids = {c.get("id") for c in channels}
    LOG.info(
        "RESULTADO: descobertos=%d ativos=%d falhas=%d novos=%d removidos=%d",
        total, len(channels), failures, len(new_ids-old_ids), len(old_ids-new_ids)
    )
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
