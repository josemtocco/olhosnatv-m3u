#!/usr/bin/env python3
"""
Construtor de playlist M3U para canais públicos encontrados no Olhos na TV.

A estratégia é deliberadamente conservadora:
- usa Chromium/Playwright para capturar streams iniciados por JavaScript e iframes;
- só grava URLs HTTP(S);
- ignora páginas HTML quando não parecem ser streams;
- remove duplicidades por URL;
- guarda metadados em canais.json;
- não incorpora credenciais/cookies;
- não tenta contornar DRM, autenticação ou bloqueios.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    sync_playwright = None
    PlaywrightTimeoutError = Exception


SOURCE_URL = os.getenv("SOURCE_URL", "https://www.olhosnatv.com.br/")
OUTPUT_FILE = Path(os.getenv("OUTPUT_FILE", "lista.m3u"))
STATE_FILE = Path(os.getenv("STATE_FILE", "canais.json"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))
CHECK_STREAMS = os.getenv("CHECK_STREAMS", "false").lower() in {
    "1", "true", "yes", "on"
}

USER_AGENT = (
    "Mozilla/5.0 (compatible; OlhosNaTV-M3U/1.0; "
    "+https://github.com/)"
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"})


@dataclass
class Channel:
    name: str
    url: str
    page_url: str
    group: str = "Olhos na TV"
    logo: str = ""


def log(message: str) -> None:
    print(f"[olhosnatv] {message}", flush=True)


def normalize_url(url: str, base: str = SOURCE_URL) -> str:
    url = url.strip().strip("'\"")
    if not url:
        return ""
    url = urljoin(base, url)
    url, _ = urldefrag(url)
    return url


def is_http_url(url: str) -> bool:
    try:
        return urlparse(url).scheme.lower() in {"http", "https"}
    except Exception:
        return False


def fetch(url: str) -> str:
    response = SESSION.get(
        url,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def clean_name(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(
        r"^(assistir|ao vivo|tv online)\s+",
        "",
        text,
        flags=re.I,
    )
    return text[:150] or "Canal"


def extract_channel_pages(home_html: str) -> list[tuple[str, str]]:
    """Extrai links internos que parecem apontar para páginas de canais."""
    soup = BeautifulSoup(home_html, "lxml")
    found: dict[str, tuple[str, str]] = {}

    for a in soup.select("a[href]"):
        href = normalize_url(a.get("href", ""), SOURCE_URL)
        text = clean_name(a.get_text(" ", strip=True))

        if not is_http_url(href):
            continue

        parsed = urlparse(href)
        source_host = urlparse(SOURCE_URL).netloc
        if parsed.netloc != source_host:
            continue

        # Blogger usa /YYYY/MM/... para postagens e /p/... para páginas.
        if not (
            re.search(r"/\d{4}/\d{2}/", parsed.path)
            or parsed.path.startswith("/p/")
        ):
            continue

        if not text or text.lower() in {
            "tecnologia do blogger",
            "categorias",
            "sobre nós",
            "adicionar canal de tv",
            "pular para o conteúdo principal",
        }:
            continue

        found[href] = (text, href)

    return list(found.values())


def extract_urls_from_html(html: str, page_url: str) -> set[str]:
    """
    Procura URLs de transmissão/player sem executar JavaScript.

    São considerados:
    - src/href/data-*;
    - URLs em atributos;
    - URLs em scripts;
    - m3u8, m3u, mp4 e outros padrões de streaming.
    """
    soup = BeautifulSoup(html, "lxml")
    urls: set[str] = set()

    attr_names = (
        "src", "href", "data-src", "data-url", "data-video",
        "data-stream", "data-embed", "data-player", "content",
    )

    for tag in soup.find_all(True):
        for attr in attr_names:
            value = tag.get(attr)
            if not value:
                continue

            # meta content pode conter URLs; nos demais atributos normalmente
            # existe uma URL única.
            for candidate in re.findall(
                r'https?://[^\s\'"<>]+', str(value)
            ):
                candidate = normalize_url(candidate, page_url)
                if is_http_url(candidate):
                    urls.add(candidate)

            if is_http_url(str(value).strip()):
                urls.add(normalize_url(str(value).strip(), page_url))

    # Scripts podem conter URLs escapadas ou dentro de JSON/JS.
    for script in soup.find_all("script"):
        content = script.string or script.get_text(" ", strip=False)
        if not content:
            continue

        for candidate in re.findall(
            r'https?://[^\s\'"<>\\]+', content
        ):
            candidate = candidate.rstrip("),;")
            candidate = normalize_url(candidate, page_url)
            if is_http_url(candidate):
                urls.add(candidate)

    # Filtra recursos que são claramente páginas/arquivos de navegação.
    bad_extensions = {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
        ".css", ".js", ".ico", ".woff", ".woff2", ".ttf",
    }

    cleaned = set()
    for url in urls:
        path = urlparse(url).path.lower()
        if any(path.endswith(ext) for ext in bad_extensions):
            continue
        cleaned.add(url)

    return cleaned


def looks_like_stream(url: str) -> bool:
    lower = url.lower()
    path = urlparse(url).path.lower()

    if any(x in lower for x in (
        ".m3u8", ".m3u", ".mpd", ".mp4",
        "manifest", "playlist", "stream",
        "live", "hls", "dvr",
    )):
        return True

    # Alguns provedores escondem o formato no caminho/query.
    return any(x in path for x in (
        "/hls/", "/live/", "/stream/", "/play/",
    ))


def verify_stream(url: str) -> bool:
    if not CHECK_STREAMS:
        return True

    try:
        # GET parcial é mais compatível que HEAD com servidores de vídeo.
        response = SESSION.get(
            url,
            headers={"Range": "bytes=0-2047"},
            timeout=min(REQUEST_TIMEOUT, 12),
            stream=True,
            allow_redirects=True,
        )
        ok = response.status_code < 400
        response.close()
        return ok
    except requests.RequestException:
        return False


def extract_streams_with_browser(page_url: str) -> set[str]:
    """
    Abre a página em Chromium e captura requisições de mídia feitas pelo
    player, inclusive quando o Olhos na TV usa iframe/JavaScript.
    """
    streams: set[str] = set()

    if sync_playwright is None:
        return streams

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-gpu",
                ],
            )
            page = browser.new_page(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 720},
            )

            def on_response(response):
                url = response.url
                content_type = (response.headers.get("content-type") or "").lower()
                if (
                    looks_like_stream(url)
                    or "mpegurl" in content_type
                    or "dash+xml" in content_type
                    or "video/" in content_type
                ):
                    streams.add(normalize_url(url, page_url))

            page.on("response", on_response)

            try:
                page.goto(
                    page_url,
                    wait_until="domcontentloaded",
                    timeout=int(REQUEST_TIMEOUT * 1000),
                )
                # Muitos players iniciam a transmissão depois do carregamento.
                page.wait_for_timeout(7000)
            except PlaywrightTimeoutError:
                # Mesmo com timeout, as requisições já capturadas podem ser úteis.
                pass
            finally:
                browser.close()

    except Exception as exc:
        log(f"browser não conseguiu processar {page_url}: {exc}")

    return {u for u in streams if is_http_url(u) and looks_like_stream(u)}


def page_to_channels(name: str, page_url: str) -> list[Channel]:
    try:
        html = fetch(page_url)
    except requests.RequestException as exc:
        log(f"falha ao abrir {page_url}: {exc}")
        return []

    soup = BeautifulSoup(html, "lxml")
    logo = ""
    og_image = soup.select_one('meta[property="og:image"]')
    if og_image and og_image.get("content"):
        logo = normalize_url(og_image["content"], page_url)

    # Primeiro tenta capturar o stream real através do navegador.
    streams = extract_streams_with_browser(page_url)

    # Fallback sem navegador para páginas que expõem o stream no HTML/JS.
    if not streams:
        candidates = extract_urls_from_html(html, page_url)
        streams = {u for u in candidates if looks_like_stream(u)}

    result = []
    for stream in sorted(set(streams)):
        if verify_stream(stream):
            result.append(
                Channel(
                    name=name,
                    url=stream,
                    page_url=page_url,
                    logo=logo,
                )
            )

    return result


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"channels": {}}

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"channels": {}}


def save_state(channels: Iterable[Channel]) -> None:
    data = {"channels": {}}

    for channel in channels:
        key = channel.url
        data["channels"][key] = asdict(channel)

    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


def deduplicate(channels: Iterable[Channel]) -> list[Channel]:
    by_url: dict[str, Channel] = {}

    for channel in channels:
        url = normalize_url(channel.url)
        if not is_http_url(url):
            continue

        if url not in by_url:
            channel.url = url
            by_url[url] = channel

    return sorted(
        by_url.values(),
        key=lambda c: (c.group.lower(), c.name.lower(), c.url),
    )


def write_m3u(channels: list[Channel]) -> None:
    lines = ["#EXTM3U"]

    for channel in channels:
        attrs = [
            f'tvg-name="{channel.name.replace(chr(34), chr(39))}"',
        ]

        if channel.logo:
            attrs.append(
                f'tvg-logo="{channel.logo.replace(chr(34), chr(39))}"'
            )

        attrs.append(f'group-title="{channel.group}"')

        lines.append("#EXTINF:-1 " + " ".join(attrs) + "," + channel.name)
        lines.append(channel.url)

    content = "\n".join(lines) + "\n"

    tmp = OUTPUT_FILE.with_suffix(OUTPUT_FILE.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(OUTPUT_FILE)


def main() -> int:
    started = time.time()
    log(f"fonte: {SOURCE_URL}")

    try:
        home_html = fetch(SOURCE_URL)
    except requests.RequestException as exc:
        log(f"não foi possível acessar a fonte: {exc}")
        return 2

    pages = extract_channel_pages(home_html)
    log(f"páginas de canais encontradas: {len(pages)}")

    discovered: list[Channel] = []

    with ThreadPoolExecutor(max_workers=max(1, MAX_WORKERS)) as executor:
        future_map = {
            executor.submit(page_to_channels, name, url): (name, url)
            for name, url in pages
        }

        for future in as_completed(future_map):
            name, url = future_map[future]
            try:
                channels = future.result()
            except Exception as exc:
                log(f"erro processando {name}: {exc}")
                continue

            if channels:
                log(f"{name}: {len(channels)} stream(s)")
                discovered.extend(channels)

    # Estado anterior é usado somente como memória de metadados.
    # Um canal antigo não é mantido se ele não for encontrado novamente:
    # isso implementa a remoção de canais inativos/desaparecidos.
    previous = load_state()
    previous_count = len(previous.get("channels", {}))

    final = deduplicate(discovered)
    save_state(final)
    write_m3u(final)

    elapsed = time.time() - started
    log(f"canais/streams ativos encontrados: {len(final)}")
    log(f"entradas anteriores no estado: {previous_count}")
    log(f"playlist: {OUTPUT_FILE}")
    log(f"tempo: {elapsed:.1f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
