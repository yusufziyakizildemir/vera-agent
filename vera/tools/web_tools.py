"""Ücretsiz web araştırma, sayfa okuma, hava durumu ve fiyat/haber takip araçları."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import quote_plus, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from vera.tools.registry import ToolContext, ToolDef, ToolRegistry

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


async def fetch_clean_text(url: str, selector: str = "", max_chars: int = 8000) -> dict[str, Any]:
    """Fetches a webpage cleanly using lightweight HTTP (zero Chromium RAM overhead)."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"error": "Sadece http/https URL'leri desteklenir."}

    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.string or "").strip() if soup.title else ""

    for tag in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav"]):
        tag.decompose()

    if selector:
        nodes = soup.select(selector)
        text = "\n".join(n.get_text(" ", strip=True) for n in nodes)
    else:
        main = soup.find("main") or soup.find("article") or soup.body or soup
        text = main.get_text("\n", strip=True)

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return {
        "url": str(resp.url),
        "title": title,
        "hash": digest,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


def register_web_tools(registry: ToolRegistry) -> None:
    async def web_search(ctx: ToolContext, query: str, max_results: int = 6) -> dict[str, Any]:
        """Free web search via DuckDuckGo HTML endpoint (no API key required)."""
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        results: list[dict[str, str]] = []
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"},
        ) as client:
            resp = await client.get(search_url)
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        for res in soup.select(".result")[: max(1, min(10, max_results))]:
            a = res.select_one(".result__a")
            snippet_el = res.select_one(".result__snippet")
            if not a:
                continue
            href = a.get("href", "")
            # Decode DDG redirect links
            if "uddg=" in href:
                try:
                    href = unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
                except Exception:
                    pass
            results.append(
                {
                    "title": a.get_text(" ", strip=True),
                    "url": href,
                    "snippet": snippet_el.get_text(" ", strip=True) if snippet_el else "",
                }
            )
        return {"query": query, "count": len(results), "results": results}

    async def fetch_webpage(
        ctx: ToolContext, url: str, selector: str = "", max_chars: int = 6000
    ) -> dict[str, Any]:
        return await fetch_clean_text(url=url, selector=selector, max_chars=min(12000, max_chars))

    async def get_weather(ctx: ToolContext, city: str = "Istanbul") -> dict[str, Any]:
        """Free weather forecast via wttr.in JSON API (no API key needed)."""
        url = f"https://wttr.in/{quote_plus(city)}?format=j1&lang=tr"
        async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": "curl/8.0"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        current = data.get("current_condition", [{}])[0]
        desc = (
            current.get("lang_tr", [{}])[0].get("value")
            if current.get("lang_tr")
            else current.get("weatherDesc", [{}])[0].get("value", "")
        )
        today = data.get("weather", [{}])[0]
        return {
            "city": city,
            "temp_c": current.get("temp_C"),
            "feels_like_c": current.get("FeelsLikeC"),
            "humidity_pct": current.get("humidity"),
            "wind_kmph": current.get("windspeedKmph"),
            "condition": desc,
            "min_c": today.get("mintempC"),
            "max_c": today.get("maxtempC"),
        }

    async def add_web_watcher(
        ctx: ToolContext,
        name: str,
        url: str,
        instruction: str,
        selector: str = "",
        interval_minutes: int = 60,
    ) -> dict[str, Any]:
        wid = ctx.db.add_web_watcher(
            name=name,
            url=url,
            instruction=instruction,
            selector=selector,
            interval_minutes=interval_minutes,
        )
        # Capture baseline snapshot immediately
        try:
            snap = await fetch_clean_text(url=url, selector=selector, max_chars=1500)
            if "hash" in snap:
                ctx.db.update_web_watcher_state(wid, snap["hash"], snap["text"])
        except Exception:
            pass
        return {
            "status": "created",
            "watcher_id": wid,
            "name": name,
            "url": url,
            "interval_minutes": max(10, interval_minutes),
        }

    async def list_web_watchers(ctx: ToolContext) -> dict[str, Any]:
        watchers = ctx.db.list_web_watchers()
        return {"count": len(watchers), "watchers": watchers}

    async def delete_web_watcher(ctx: ToolContext, watcher_id: str) -> dict[str, Any]:
        ok = ctx.db.delete_web_watcher(watcher_id)
        return {"deleted": ok, "watcher_id": watcher_id}

    registry.register(
        ToolDef(
            name="web_search",
            category="web",
            description="İnternette güncel bilgi, haber, fiyat veya dokümantasyon araması yapar.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Arama sorgusu."},
                    "max_results": {"type": "integer", "description": "Sonuç sayısı (1-10)."},
                },
                "required": ["query"],
            },
            handler=web_search,
        )
    )

    registry.register(
        ToolDef(
            name="fetch_webpage",
            category="web",
            description="Verilen web sayfasının ana metnini çeker ve temizler.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Okunacak web sayfasının tam URL'si."},
                    "selector": {"type": "string", "description": "Opsiyonel CSS seçici."},
                    "max_chars": {"type": "integer", "description": "Maksimum karakter limiti."},
                },
                "required": ["url"],
            },
            handler=fetch_webpage,
        )
    )

    registry.register(
        ToolDef(
            name="get_weather",
            category="web",
            description="Belirtilen şehir için anlık hava durumu ve günlük sıcaklık tahmini getirir.",
            parameters={
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "Şehir adı (örn. Istanbul, Ankara)."},
                },
                "required": [],
            },
            handler=get_weather,
        )
    )

    registry.register(
        ToolDef(
            name="add_web_watcher",
            category="web",
            description=(
                "Bir web sayfasını (ürün fiyatı, duyuru, haber, rakip sayfa) periyodik olarak "
                "7/24 izler ve değişiklik olduğunda Telegram'dan bildirir."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Takipçi adı."},
                    "url": {"type": "string", "description": "İzlenecek URL."},
                    "instruction": {
                        "type": "string",
                        "description": "Ne takip edilmeli? (örn. Fiyat 15.000 TL altına inerse haber ver)",
                    },
                    "selector": {"type": "string", "description": "Opsiyonel CSS seçici."},
                    "interval_minutes": {
                        "type": "integer",
                        "description": "Kontrol aralığı (dakika, en az 10).",
                    },
                },
                "required": ["name", "url", "instruction"],
            },
            handler=add_web_watcher,
        )
    )

    registry.register(
        ToolDef(
            name="list_web_watchers",
            category="web",
            description="Aktif 7/24 web/fiyat/haber takipçilerini listeler.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=list_web_watchers,
        )
    )

    registry.register(
        ToolDef(
            name="delete_web_watcher",
            category="web",
            description="Belirtilen ID'ye sahip web takipçisini siler.",
            parameters={
                "type": "object",
                "properties": {
                    "watcher_id": {"type": "string", "description": "Silinecek takipçi ID'si."},
                },
                "required": ["watcher_id"],
            },
            handler=delete_web_watcher,
        )
    )
