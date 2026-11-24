"""Ücretsiz, API anahtarsız canlı istihbarat araçları (Döviz, Kripto, Haber, Akademik, Vikipedi, Namaz, Tatil)."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

from vera.tools.registry import ToolContext, ToolDef, ToolRegistry

logger = logging.getLogger("vera.tools.live_intel")

HTTP_HEADERS = {
    "User-Agent": "VERA-PersonalAssistant/1.0 (https://github.com/yusufziyakizildemir/vera-agent)",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

CRYPTO_ALIASES: dict[str, str] = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "xrp": "ripple",
    "bnb": "binancecoin",
    "ada": "cardano",
    "doge": "dogecoin",
    "avax": "avalanche-2",
    "dot": "polkadot",
    "ton": "the-open-network",
    "trx": "tron",
    "usdt": "tether",
    "usdc": "usd-coin",
}


async def _fetch_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=HTTP_HEADERS) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


async def _fetch_text(url: str, params: dict[str, Any] | None = None) -> str:
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=HTTP_HEADERS) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.text


def register_live_intel_tools(registry: ToolRegistry) -> None:
    """Registers free keyless real-time intelligence tools."""

    async def get_exchange_rates(
        ctx: ToolContext,
        base: str = "USD",
        targets: str = "TRY,EUR,GBP",
        amount: float = 1.0,
    ) -> dict[str, Any]:
        base_code = (base or "USD").strip().upper()
        try:
            qty = float(amount)
        except (TypeError, ValueError):
            qty = 1.0
        target_codes = [c.strip().upper() for c in (targets or "TRY,EUR,GBP").split(",") if c.strip()]
        try:
            data = await _fetch_json(f"https://open.er-api.com/v6/latest/{quote(base_code)}")
            if not isinstance(data, dict) or data.get("result") != "success":
                return {"error": f"Döviz kuru alınamadı: {data}"}
            all_rates = data.get("rates", {})
            rates = {code: all_rates[code] for code in target_codes if code in all_rates}
            converted = {code: round(qty * rate, 4) for code, rate in rates.items()}
            return {
                "base": base_code,
                "amount": qty,
                "rates": rates,
                "converted": converted,
                "updated_utc": data.get("time_last_update_utc", ""),
            }
        except Exception as exc:
            return {"error": f"Döviz kuru hatası: {exc}"}

    async def get_crypto_prices(
        ctx: ToolContext,
        coins: str = "bitcoin,ethereum,solana",
        vs: str = "try,usd",
    ) -> dict[str, Any]:
        raw_coins = [c.strip().lower() for c in (coins or "bitcoin,ethereum").split(",") if c.strip()]
        coin_ids = ",".join(CRYPTO_ALIASES.get(c, c) for c in raw_coins)
        vs_list = ",".join(v.strip().lower() for v in (vs or "try,usd").split(",") if v.strip())
        try:
            data = await _fetch_json(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_ids, "vs_currencies": vs_list, "include_24hr_change": "true"},
            )
            return {"coins": data}
        except Exception as exc:
            return {"error": f"Kripto fiyat hatası: {exc}"}

    async def get_news_headlines(
        ctx: ToolContext,
        topic: str = "",
        lang: str = "tr",
        limit: int = 6,
    ) -> dict[str, Any]:
        max_items = max(1, min(15, int(limit or 6)))
        is_tr = (lang or "tr").strip().lower().startswith("tr")
        locale_q = "hl=tr&gl=TR&ceid=TR:tr" if is_tr else "hl=en-US&gl=US&ceid=US:en"
        clean_topic = (topic or "").strip()
        if clean_topic:
            rss_url = f"https://news.google.com/rss/search?q={quote_plus(clean_topic)}&{locale_q}"
        else:
            rss_url = f"https://news.google.com/rss?{locale_q}"

        try:
            xml_text = await _fetch_text(rss_url)
            root = ET.fromstring(xml_text)
            items = []
            for item in root.findall("./channel/item")[:max_items]:
                src_el = item.find("source")
                items.append(
                    {
                        "title": (item.findtext("title") or "").strip(),
                        "source": (src_el.text or "").strip() if src_el is not None and src_el.text else "",
                        "published": (item.findtext("pubDate") or "").strip(),
                        "link": (item.findtext("link") or "").strip(),
                    }
                )
            return {"topic": clean_topic or "Gündem", "count": len(items), "headlines": items}
        except Exception as exc:
            return {"error": f"Haber akışı hatası: {exc}"}

    async def _search_crossref(query: str, max_items: int) -> list[dict[str, Any]]:
        data = await _fetch_json(
            "https://api.crossref.org/works",
            params={"query": query, "rows": max_items, "select": "DOI,title,author,published,container-title,URL"},
        )
        items = data.get("message", {}).get("items", []) if isinstance(data, dict) else []
        out = []
        for w in items:
            authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in w.get("author", [])[:3]]
            pub_parts = w.get("published", {}).get("date-parts", [[None]])
            out.append(
                {
                    "title": (w.get("title") or [""])[0],
                    "authors": [a for a in authors if a],
                    "year": pub_parts[0][0] if pub_parts and pub_parts[0] else None,
                    "venue": (w.get("container-title") or [""])[0],
                    "doi": w.get("DOI", ""),
                    "url": w.get("URL", ""),
                }
            )
        return out

    async def _search_arxiv(query: str, max_items: int) -> list[dict[str, Any]]:
        xml_text = await _fetch_text(
            "https://export.arxiv.org/api/query",
            params={"search_query": f"all:{query}", "start": 0, "max_results": max_items},
        )
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml_text)
        out = []
        for entry in root.findall("atom:entry", ns)[:max_items]:
            title = " ".join((entry.findtext("atom:title", default="", namespaces=ns) or "").split())
            summary = " ".join((entry.findtext("atom:summary", default="", namespaces=ns) or "").split())
            authors = [
                (a.findtext("atom:name", default="", namespaces=ns) or "").strip()
                for a in entry.findall("atom:author", ns)[:3]
            ]
            out.append(
                {
                    "title": title,
                    "authors": [a for a in authors if a],
                    "published": (entry.findtext("atom:published", default="", namespaces=ns) or "")[:10],
                    "summary": summary[:320],
                    "url": (entry.findtext("atom:id", default="", namespaces=ns) or "").strip(),
                }
            )
        return out

    async def _search_pubmed(query: str, max_items: int) -> list[dict[str, Any]]:
        search_data = await _fetch_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={"db": "pubmed", "term": query, "retmax": max_items, "retmode": "json"},
        )
        ids = search_data.get("esearchresult", {}).get("idlist", []) if isinstance(search_data, dict) else []
        if not ids:
            return []
        sum_data = await _fetch_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
        )
        res = sum_data.get("result", {}) if isinstance(sum_data, dict) else {}
        out = []
        for pid in ids:
            item = res.get(pid)
            if not isinstance(item, dict):
                continue
            authors = [a.get("name", "") for a in item.get("authors", [])[:3] if a.get("name")]
            out.append(
                {
                    "pmid": pid,
                    "title": item.get("title", ""),
                    "authors": authors,
                    "published": item.get("pubdate", ""),
                    "venue": item.get("fulljournalname", ""),
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
                }
            )
        return out

    async def search_academic_papers(
        ctx: ToolContext,
        query: str,
        source: str = "crossref",
        limit: int = 5,
    ) -> dict[str, Any]:
        clean_q = (query or "").strip()
        if not clean_q:
            return {"error": "Arama sorgusu boş olamaz."}
        max_items = max(1, min(10, int(limit or 5)))
        src = (source or "crossref").strip().lower()
        handlers = {
            "crossref": _search_crossref,
            "arxiv": _search_arxiv,
            "pubmed": _search_pubmed,
        }
        fn = handlers.get(src)
        if not fn:
            return {"error": "Desteklenen akademik kaynaklar: crossref, arxiv, pubmed"}
        try:
            papers = await fn(clean_q, max_items)
            return {"source": src, "query": clean_q, "count": len(papers), "papers": papers}
        except Exception as exc:
            return {"error": f"Akademik arama hatası ({src}): {exc}"}

    async def lookup_wikipedia(ctx: ToolContext, query: str, lang: str = "tr") -> dict[str, Any]:
        clean_q = (query or "").strip()
        if not clean_q:
            return {"error": "Vikipedi sorgusu boş olamaz."}
        lang_code = (lang or "tr").strip().lower()[:5]
        try:
            slug = quote(clean_q.replace(" ", "_"), safe="")
            try:
                data = await _fetch_json(f"https://{lang_code}.wikipedia.org/api/rest_v1/page/summary/{slug}")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                search = await _fetch_json(
                    f"https://{lang_code}.wikipedia.org/w/api.php",
                    params={"action": "opensearch", "search": clean_q, "limit": 3, "format": "json"},
                )
                titles = search[1] if isinstance(search, list) and len(search) > 1 else []
                if not titles:
                    return {"query": clean_q, "lang": lang_code, "found": False}
                top_slug = quote(str(titles[0]).replace(" ", "_"), safe="")
                data = await _fetch_json(f"https://{lang_code}.wikipedia.org/api/rest_v1/page/summary/{top_slug}")

            if not isinstance(data, dict):
                return {"query": clean_q, "lang": lang_code, "found": False}
            return {
                "found": True,
                "title": data.get("title", clean_q),
                "description": data.get("description", ""),
                "extract": data.get("extract", "")[:1400],
                "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
            }
        except Exception as exc:
            return {"error": f"Vikipedi hatası: {exc}"}

    async def get_prayer_times(
        ctx: ToolContext,
        city: str = "Istanbul",
        country: str = "Turkey",
    ) -> dict[str, Any]:
        clean_city = (city or "Istanbul").strip()
        clean_country = (country or "Turkey").strip()
        params: dict[str, Any] = {"city": clean_city, "country": clean_country}
        if clean_country.lower() in {"turkey", "türkiye", "turkiye", "tr"}:
            params["method"] = 13  # Diyanet İşleri Başkanlığı
        try:
            data = await _fetch_json("https://api.aladhan.com/v1/timingsByCity", params=params)
            if not isinstance(data, dict) or data.get("code") != 200:
                return {"error": f"Namaz vakitleri alınamadı: {data}"}
            d = data["data"]
            t = d.get("timings", {})
            return {
                "city": clean_city,
                "date": d.get("date", {}).get("readable", ""),
                "timings": {
                    "Imsak": t.get("Imsak", ""),
                    "Gunes": t.get("Sunrise", ""),
                    "Ogle": t.get("Dhuhr", ""),
                    "Ikindi": t.get("Asr", ""),
                    "Aksam": t.get("Maghrib", ""),
                    "Yatsi": t.get("Isha", ""),
                },
            }
        except Exception as exc:
            return {"error": f"Namaz vakitleri hatası: {exc}"}

    async def get_public_holidays(
        ctx: ToolContext,
        year: int = 0,
        country_code: str = "TR",
    ) -> dict[str, Any]:
        target_year = int(year) if year and int(year) > 1900 else datetime.now(ctx.settings.tz).year
        cc = (country_code or "TR").strip().upper()[:2]
        try:
            data = await _fetch_json(f"https://date.nager.at/api/v3/PublicHolidays/{target_year}/{cc}")
            if not isinstance(data, list):
                return {"error": f"Resmi tatil verisi alınamadı: {data}"}
            holidays = [
                {
                    "date": h.get("date", ""),
                    "localName": h.get("localName", ""),
                    "name": h.get("name", ""),
                }
                for h in data
            ]
            return {"year": target_year, "country_code": cc, "count": len(holidays), "holidays": holidays}
        except Exception as exc:
            return {"error": f"Resmi tatil hatası: {exc}"}

    registry.register(
        ToolDef(
            name="get_exchange_rates",
            description="Canlı döviz kurlarını ve tutar çevirimini getirir (USD, TRY, EUR, GBP vb., ücretsiz).",
            parameters={
                "type": "object",
                "properties": {
                    "base": {"type": "string", "description": "Baz para birimi (ör: USD, EUR, TRY)"},
                    "targets": {"type": "string", "description": "Virgülle ayrılmış hedef birimler (ör: TRY,EUR,GBP)"},
                    "amount": {"type": "number", "description": "Çevrilecek miktar (varsayılan 1)"},
                },
            },
            handler=get_exchange_rates,
            category="intel",
        )
    )

    registry.register(
        ToolDef(
            name="get_crypto_prices",
            description="Canlı kripto para fiyatlarını ve 24 saatlik değişimi getirir (Bitcoin, Ethereum, Solana vb.).",
            parameters={
                "type": "object",
                "properties": {
                    "coins": {"type": "string", "description": "Virgülle ayrılmış coinler (ör: bitcoin,ethereum,btc)"},
                    "vs": {"type": "string", "description": "Hedef para birimleri (ör: try,usd)"},
                },
            },
            handler=get_crypto_prices,
            category="intel",
        )
    )

    registry.register(
        ToolDef(
            name="get_news_headlines",
            description="Google News RSS üzerinden güncel Türkiye veya dünya haber başlıklarını çeker.",
            parameters={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Konu/anahtar kelime (boş bırakılırsa genel gündem)"},
                    "lang": {"type": "string", "description": "Dil kodu ('tr' veya 'en')"},
                    "limit": {"type": "integer", "description": "Maksimum haber sayısı (1-15)"},
                },
            },
            handler=get_news_headlines,
            category="intel",
        )
    )

    registry.register(
        ToolDef(
            name="search_academic_papers",
            description="Crossref, arXiv veya PubMed üzerinden akademik makale, DOI, yazar ve özet arar.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Akademik konu, başlık veya yazar sorgusu"},
                    "source": {"type": "string", "enum": ["crossref", "arxiv", "pubmed"], "description": "Veritabanı"},
                    "limit": {"type": "integer", "description": "Sonuç sayısı (1-10)"},
                },
                "required": ["query"],
            },
            handler=search_academic_papers,
            category="intel",
        )
    )

    registry.register(
        ToolDef(
            name="lookup_wikipedia",
            description="Vikipedi üzerinden konu, kişi, kavram veya tarih özeti getirir.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Aranacak başlık veya kavram"},
                    "lang": {"type": "string", "description": "Dil kodu ('tr' veya 'en')"},
                },
                "required": ["query"],
            },
            handler=lookup_wikipedia,
            category="intel",
        )
    )

    registry.register(
        ToolDef(
            name="get_prayer_times",
            description="Şehir ve ülkeye göre günlük ezan / namaz vakitlerini (Diyanet uyumlu) getirir.",
            parameters={
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "Şehir (ör: Istanbul, Ankara)"},
                    "country": {"type": "string", "description": "Ülke (ör: Turkey)"},
                },
            },
            handler=get_prayer_times,
            category="intel",
        )
    )

    registry.register(
        ToolDef(
            name="get_public_holidays",
            description="Ülke ve yıla göre resmi tatilleri listeler (varsayılan bu yıl, TR).",
            parameters={
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Yıl (0 ise bulunduğumuz yıl)"},
                    "country_code": {"type": "string", "description": "2 harfli ülke kodu (ör: TR, US, DE)"},
                },
            },
            handler=get_public_holidays,
            category="intel",
        )
    )
