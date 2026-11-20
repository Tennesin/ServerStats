"""Помощники для эмодзи в анализаторе: показ символа и подписи. Без сети."""
import botroot
from emoji_utils import normalize_unicode

def _build_display_map() -> dict:
    """нормализованный символ -> полная форма для красивого показа (с FE0F, где нужно)."""
    m: dict = {}
    try:
        import emoji as lib
        fq = lib.STATUS["fully_qualified"]
        for e, d in lib.EMOJI_DATA.items():
            if d.get("status") != fq:
                continue
            n = normalize_unicode(e)
            if n not in m or len(e) < len(m[n]):
                m[n] = e                      # из вариантов берём самый короткий (без оттенков и пола)
    except Exception:
        pass
    return m

_DISPLAY = None

def display_unicode(ch: str) -> str:
    global _DISPLAY
    if _DISPLAY is None:
        _DISPLAY = _build_display_map()
    full = _DISPLAY.get(ch)
    if full:
        return full
    if len(ch) == 1 and ord(ch) >= 0x2190:
        return ch + "\ufe0f"
    return ch

def info_map(df) -> dict:
    """Таблица data_emoji_info -> {ключ: (имя, анимированный, внешний)}."""
    if df is None or len(df) == 0:
        return {}
    return {str(k): (str(n or ""), int(a), int(e))
            for k, n, a, e in zip(df["key"], df["name"], df["animated"], df["external"])}

def label_text(key: str, info: dict) -> str:
    """Текстовая подпись для xlsx и таблиц: символ или :имя:, у внешних — «(внешний)»."""
    if key.startswith("c:"):
        name, _anim, ext = info.get(key, ("", 0, 0))
        return f":{name or key[2:]}:" + (" (внешний)" if ext else "")
    return display_unicode(key[2:])