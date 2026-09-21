import logging
import os
from typing import Annotated, Any, List

import httpx
from pydantic import Field

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config

_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
_DETECT_URL = "https://translation.googleapis.com/language/translate/v2/detect"

# Common language name → ISO 639-1 code. Google Translate accepts both, but
# spoken names are more natural from voice input.
_LANG_NAMES: dict[str, str] = {
    "afrikaans": "af", "arabic": "ar", "basque": "eu", "belarusian": "be",
    "bulgarian": "bg", "catalan": "ca", "chinese": "zh", "mandarin": "zh",
    "cantonese": "zh-TW", "simplified chinese": "zh-CN", "traditional chinese": "zh-TW",
    "croatian": "hr", "czech": "cs", "danish": "da", "dutch": "nl",
    "english": "en", "estonian": "et", "filipino": "tl", "tagalog": "tl",
    "finnish": "fi", "french": "fr", "galician": "gl", "german": "de",
    "greek": "el", "gujarati": "gu", "haitian creole": "ht", "hebrew": "he",
    "hindi": "hi", "hungarian": "hu", "icelandic": "is", "indonesian": "id",
    "irish": "ga", "italian": "it", "japanese": "ja", "kannada": "kn",
    "korean": "ko", "latin": "la", "latvian": "lv", "lithuanian": "lt",
    "macedonian": "mk", "malay": "ms", "maltese": "mt", "norwegian": "no",
    "persian": "fa", "farsi": "fa", "polish": "pl", "portuguese": "pt",
    "romanian": "ro", "russian": "ru", "serbian": "sr", "slovak": "sk",
    "slovenian": "sl", "spanish": "es", "swahili": "sw", "swedish": "sv",
    "tamil": "ta", "telugu": "te", "thai": "th", "turkish": "tr",
    "ukrainian": "uk", "urdu": "ur", "vietnamese": "vi", "welsh": "cy",
    "yiddish": "yi",
}

_LANG_CODES: set[str] = {
    "af", "ar", "eu", "be", "bg", "ca", "zh", "zh-cn", "zh-tw", "hr", "cs",
    "da", "nl", "en", "et", "tl", "fi", "fr", "gl", "de", "el", "gu", "ht",
    "he", "hi", "hu", "is", "id", "ga", "it", "ja", "kn", "ko", "la", "lv",
    "lt", "mk", "ms", "mt", "no", "fa", "pl", "pt", "ro", "ru", "sr", "sk",
    "sl", "es", "sw", "sv", "ta", "te", "th", "tr", "uk", "ur", "vi", "cy", "yi",
}


def _resolve_lang_code(name: str) -> str | None:
    """Resolve a language name or code to an ISO 639-1 code, or None if unknown."""
    if not name:
        return None
    s = name.strip().lower()
    if s in _LANG_CODES:
        return s
    return _LANG_NAMES.get(s)


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def register(server) -> List[str]:
    log = logging.getLogger("tools.translate")
    config = _load_config()
    api_key = str(config.get("google_api_key") or os.getenv("GOOGLE_API_KEY") or "").strip()

    tool_description, param_meta = get_tool_config(
        config,
        "translate",
        (
            "Translate a word, phrase, or sentence into another language using Google Translate. "
            "Use this when the user asks how to say something in another language, or wants text translated."
        ),
    )
    text_desc, text_alias, text_title = get_param_meta(
        param_meta, "text", "The word, phrase, or sentence to translate."
    )
    target_desc, target_alias, target_title = get_param_meta(
        param_meta,
        "target_language",
        "Language to translate into. Accepts names (Spanish, French, Japanese) or codes (es, fr, ja). Defaults to English.",
    )
    source_desc, source_alias, source_title = get_param_meta(
        param_meta,
        "source_language",
        "Optional source language. Auto-detected if not provided.",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(
        name="translate",
        description=tool_description,
    )
    def translate(
        text: Annotated[str, Field(**_field_kwargs(text_desc, text_alias, text_title))],
        target_language: Annotated[str | None, Field(**_field_kwargs(target_desc, target_alias, target_title))] = None,
        source_language: Annotated[str | None, Field(**_field_kwargs(source_desc, source_alias, source_title))] = None,
    ) -> str:
        log_tool_call(log, "translate", text=text, target_language=target_language, source_language=source_language)

        if not api_key:
            return log_tool_result(log, "translate", "Translation API key is not configured (GOOGLE_API_KEY).")

        input_text = str(text or "").strip()
        if not input_text:
            return log_tool_result(log, "translate", "Text is required for translation.")

        # Resolve target language
        target_raw = str(target_language or "en").strip()
        target_code = _resolve_lang_code(target_raw)
        if not target_code:
            return log_tool_result(
                log, "translate",
                f"Unknown target language: {target_raw!r}. Try a language name like 'Spanish' or a code like 'es'.",
            )

        # Resolve optional source language
        source_code: str | None = None
        if source_language:
            source_raw = str(source_language).strip()
            source_code = _resolve_lang_code(source_raw)
            if not source_code:
                return log_tool_result(
                    log, "translate",
                    f"Unknown source language: {source_raw!r}.",
                )

        try:
            payload: dict[str, Any] = {
                "q": input_text,
                "target": target_code,
                "format": "text",
            }
            if source_code:
                payload["source"] = source_code

            with httpx.Client(timeout=8.0) as client:
                resp = client.post(
                    _TRANSLATE_URL,
                    params={"key": api_key},
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

        except httpx.HTTPStatusError as exc:
            status = getattr(exc.response, "status_code", "?")
            body = ""
            try:
                body = exc.response.text[:500]
            except Exception:
                pass
            log.warning("translate HTTP error status=%s body=%s", status, body)
            return log_tool_result(log, "translate", "Translation request failed.")
        except Exception as exc:
            log.warning("translate error: %r", exc)
            return log_tool_result(log, "translate", "Translation unavailable.")

        try:
            translations = data["data"]["translations"]
            translated = str(translations[0]["translatedText"]).strip()
            detected = translations[0].get("detectedSourceLanguage", "")
        except (KeyError, IndexError, TypeError):
            return log_tool_result(log, "translate", "Translation response was unreadable.")

        # Build a natural spoken result
        target_name = target_raw.capitalize()
        if not translated:
            return log_tool_result(log, "translate", "No translation returned.")

        if detected and not source_code:
            result = f"In {target_name}: {translated}"
        else:
            result = f"In {target_name}: {translated}"

        return log_tool_result(log, "translate", result)

    return ["translate"]
