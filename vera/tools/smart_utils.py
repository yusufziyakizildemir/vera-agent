"""VERA Akıllı Yardımcı Araçlar (Güvenli Hesaplama, Birim Dönüşümü, Dünya Saatleri, YouTube Meta)."""

from __future__ import annotations

import ast
import math
import operator
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from vera.tools.registry import ToolContext, ToolDef, ToolRegistry

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_SAFE_FUNCS: dict[str, Any] = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "abs": abs,
    "fabs": math.fabs,
    "round": lambda x, nd=0: round(x, int(nd)),
    "pow": pow,
    "ceil": math.ceil,
    "floor": math.floor,
}

_SAFE_CONSTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}

_UNIT_ALIASES: dict[str, str] = {
    "metre": "m", "meter": "m", "meters": "m",
    "kilometre": "km", "kilometer": "km", "kilometers": "km",
    "santimetre": "cm", "centimeter": "cm",
    "milimetre": "mm", "millimeter": "mm",
    "mil": "mi", "mile": "mi", "miles": "mi",
    "fit": "ft", "foot": "ft", "feet": "ft",
    "inç": "in", "inc": "in", "inch": "in", "inches": "in",
    "kilogram": "kg", "kilo": "kg",
    "gram": "g", "miligram": "mg",
    "pound": "lb", "pounds": "lb", "lbs": "lb", "libre": "lb",
    "ons": "oz", "ounce": "oz", "ounces": "oz", "ton": "ton", "t": "ton",
    "celsius": "c", "santigrat": "c", "°c": "c",
    "fahrenheit": "f", "fahrenayt": "f", "°f": "f",
    "kelvin": "k",
    "byte": "b", "bayt": "b",
    "km/h": "kmh", "km/s": "kmh", "m/s": "ms",
}

# (group_name, factor_to_base)
_LINEAR_UNITS: dict[str, tuple[str, float]] = {
    "m": ("length", 1.0),
    "km": ("length", 1000.0),
    "cm": ("length", 0.01),
    "mm": ("length", 0.001),
    "mi": ("length", 1609.344),
    "ft": ("length", 0.3048),
    "in": ("length", 0.0254),
    "kg": ("weight", 1.0),
    "g": ("weight", 0.001),
    "mg": ("weight", 0.000001),
    "lb": ("weight", 0.45359237),
    "oz": ("weight", 0.028349523125),
    "ton": ("weight", 1000.0),
    "b": ("data", 1.0),
    "kb": ("data", 1024.0),
    "mb": ("data", 1024.0**2),
    "gb": ("data", 1024.0**3),
    "tb": ("data", 1024.0**4),
    "ms": ("speed", 1.0),
    "kmh": ("speed", 1.0 / 3.6),
    "mph": ("speed", 0.44704),
}

_CITY_ZONES: dict[str, str] = {
    "istanbul": "Europe/Istanbul",
    "İstanbul": "Europe/Istanbul",
    "ankara": "Europe/Istanbul",
    "izmir": "Europe/Istanbul",
    "london": "Europe/London",
    "londra": "Europe/London",
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "frankfurt": "Europe/Berlin",
    "amsterdam": "Europe/Amsterdam",
    "rome": "Europe/Rome",
    "roma": "Europe/Rome",
    "madrid": "Europe/Madrid",
    "zurich": "Europe/Zurich",
    "zürih": "Europe/Zurich",
    "athens": "Europe/Athens",
    "atina": "Europe/Athens",
    "moscow": "Europe/Moscow",
    "moskova": "Europe/Moscow",
    "baku": "Asia/Baku",
    "bakü": "Asia/Baku",
    "dubai": "Asia/Dubai",
    "doha": "Asia/Qatar",
    "riyadh": "Asia/Riyadh",
    "riyad": "Asia/Riyadh",
    "cairo": "Africa/Cairo",
    "kahire": "Africa/Cairo",
    "new york": "America/New_York",
    "washington": "America/New_York",
    "chicago": "America/Chicago",
    "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "toronto": "America/Toronto",
    "sao paulo": "America/Sao_Paulo",
    "tokyo": "Asia/Tokyo",
    "seoul": "Asia/Seoul",
    "seul": "Asia/Seoul",
    "beijing": "Asia/Shanghai",
    "pekin": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "singapore": "Asia/Singapore",
    "singapur": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong",
    "mumbai": "Asia/Kolkata",
    "bangkok": "Asia/Bangkok",
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "utc": "UTC",
}


def _eval_math_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_math_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.Name) and node.id in _SAFE_CONSTS:
        return _SAFE_CONSTS[node.id]
    if isinstance(node, ast.UnaryOp):
        val = _eval_math_node(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +val
        if isinstance(node.op, ast.USub):
            return -val
        raise ValueError("Desteklenmeyen tekli operatör")
    if isinstance(node, ast.BinOp):
        left = _eval_math_node(node.left)
        right = _eval_math_node(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > 1000:
                raise ValueError("Üs değeri güvenlik sınırı (+/-1000) dışında.")
            return float(pow(left, right))
        fn = _BIN_OPS.get(type(node.op))
        if fn is None:
            raise ValueError("Desteklenmeyen işlem operatörü")
        return float(fn(left, right))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SAFE_FUNCS:
        if node.keywords:
            raise ValueError("Fonksiyonlarda isimli argüman desteklenmez.")
        fn = _SAFE_FUNCS[node.func.id]
        args = [_eval_math_node(a) for a in node.args]
        if fn is pow and len(args) == 2 and abs(args[1]) > 1000:
            raise ValueError("Üs değeri güvenlik sınırı dışında.")
        return float(fn(*args))
    raise ValueError("İfade yalnızca sayı ve güvenli matematik fonksiyonları içerebilir.")


def _convert_temperature(val: float, src: str, dst: str) -> float:
    if src == "c":
        celsius = val
    elif src == "f":
        celsius = (val - 32.0) * 5.0 / 9.0
    else:
        celsius = val - 273.15

    if dst == "c":
        return celsius
    if dst == "f":
        return celsius * 9.0 / 5.0 + 32.0
    return celsius + 273.15


def register_smart_utils_tools(registry: ToolRegistry) -> None:
    """Registers fast zero-latency utility tools (math, units, clocks, YouTube oEmbed)."""

    async def calculate_math(ctx: ToolContext, expression: str) -> dict[str, Any]:
        expr = (expression or "").strip().replace("^", "**")
        if not expr:
            return {"error": "Hesaplanacak ifade boş."}
        try:
            tree = ast.parse(expr, mode="eval")
            result = _eval_math_node(tree)
            if math.isfinite(result) and result.is_integer():
                clean_res: int | float = int(result)
            else:
                clean_res = round(result, 10)
            return {"expression": expression, "result": clean_res}
        except Exception as exc:
            return {"error": f"Hesaplama hatası: {exc}"}

    async def convert_units(
        ctx: ToolContext,
        value: float,
        from_unit: str,
        to_unit: str,
    ) -> dict[str, Any]:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return {"error": "Sayısal 'value' gerekli."}
        src = _UNIT_ALIASES.get((from_unit or "").strip().lower(), (from_unit or "").strip().lower())
        dst = _UNIT_ALIASES.get((to_unit or "").strip().lower(), (to_unit or "").strip().lower())

        temps = {"c", "f", "k"}
        if src in temps and dst in temps:
            res = round(_convert_temperature(num, src, dst), 4)
            return {"value": num, "from": src, "to": dst, "result": res}

        if src not in _LINEAR_UNITS or dst not in _LINEAR_UNITS:
            return {"error": f"Bilinmeyen birim: {from_unit} -> {to_unit}"}
        src_group, src_factor = _LINEAR_UNITS[src]
        dst_group, dst_factor = _LINEAR_UNITS[dst]
        if src_group != dst_group:
            return {"error": f"Farklı birim kategorileri dönüştürülemez: {src} ({src_group}) vs {dst} ({dst_group})"}
        res = round((num * src_factor) / dst_factor, 6)
        return {"value": num, "from": src, "to": dst, "result": res}

    async def world_clock(
        ctx: ToolContext,
        cities: str = "Istanbul,London,New York,Tokyo",
    ) -> dict[str, Any]:
        items = [c.strip() for c in (cities or "Istanbul,London,New York,Tokyo").split(",") if c.strip()]
        clocks: list[dict[str, Any]] = []
        now_utc = datetime.now(ZoneInfo("UTC"))
        for item in items[:12]:
            tz_name = _CITY_ZONES.get(item) or _CITY_ZONES.get(item.lower()) or item
            try:
                local_dt = now_utc.astimezone(ZoneInfo(tz_name))
                clocks.append(
                    {
                        "city": item,
                        "timezone": tz_name,
                        "date": local_dt.strftime("%Y-%m-%d"),
                        "time": local_dt.strftime("%H:%M:%S"),
                        "weekday": local_dt.strftime("%A"),
                        "utc_offset": local_dt.strftime("%z"),
                    }
                )
            except Exception:
                clocks.append({"city": item, "error": f"Saat dilimi bulunamadı: {tz_name}"})
        return {"clocks": clocks}

    async def get_youtube_info(ctx: ToolContext, url: str) -> dict[str, Any]:
        raw_url = (url or "").strip()
        parsed = urlparse(raw_url)
        host = parsed.netloc.lower()
        if parsed.scheme not in {"http", "https"} or not any(
            h in host for h in ("youtube.com", "youtu.be")
        ):
            return {"error": "Geçerli bir YouTube URL'si girin."}
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(
                    "https://www.youtube.com/oembed",
                    params={"format": "json", "url": raw_url},
                )
                resp.raise_for_status()
                data = resp.json()
            return {
                "title": data.get("title", ""),
                "author_name": data.get("author_name", ""),
                "author_url": data.get("author_url", ""),
                "thumbnail_url": data.get("thumbnail_url", ""),
                "url": raw_url,
            }
        except Exception as exc:
            return {"error": f"YouTube bilgi hatası: {exc}"}

    registry.register(
        ToolDef(
            name="calculate_math",
            description="Güvenli AST tabanlı bilimsel hesap makinesi (dört işlem, üs, sqrt, log, trigonometri, pi).",
            parameters={
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Matematiksel ifade (ör: sqrt(144) + 15 * 3.5)"},
                },
                "required": ["expression"],
            },
            handler=calculate_math,
            category="utils",
        )
    )

    registry.register(
        ToolDef(
            name="convert_units",
            description="Uzunluk, ağırlık, sıcaklık (C/F/K), veri (MB/GB) ve hız birimlerini dönüştürür.",
            parameters={
                "type": "object",
                "properties": {
                    "value": {"type": "number", "description": "Dönüştürülecek sayı"},
                    "from_unit": {"type": "string", "description": "Kaynak birim (km, mi, kg, lb, c, f, gb, mb, kmh...)"},
                    "to_unit": {"type": "string", "description": "Hedef birim"},
                },
                "required": ["value", "from_unit", "to_unit"],
            },
            handler=convert_units,
            category="utils",
        )
    )

    registry.register(
        ToolDef(
            name="world_clock",
            description="Belirtilen şehirlerin veya saat dilimlerinin anlık yerel tarih, saat ve UTC farkını döndürür.",
            parameters={
                "type": "object",
                "properties": {
                    "cities": {"type": "string", "description": "Virgülle ayrılmış şehirler (ör: Istanbul,London,New York,Tokyo)"},
                },
            },
            handler=world_clock,
            category="utils",
        )
    )

    registry.register(
        ToolDef(
            name="get_youtube_info",
            description="Bir YouTube videosunun başlığını, kanal adını ve meta bilgilerini API anahtarı olmadan getirir.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "YouTube video URL'si"},
                },
                "required": ["url"],
            },
            handler=get_youtube_info,
            category="utils",
        )
    )
