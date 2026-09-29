"""
[4] LLM-провайдеры: одна функция complete(system, user) -> str для всех бэкендов.

    LLM_PROVIDER    основной провайдер: yandex (по умолчанию) | ollama | deepseek | claude | openai | none
    LLM_FALLBACK    запасные через запятую, пробуются по порядку при ошибке основного (по умолчанию: ollama)
    LLM_MODEL       переопределить модель основного провайдера

Провайдер «включён», только если для него заданы ключи (для Ollama — всегда: он локальный).
Если ни один не ответил — пайплайн работает в режиме выдержки из заметки, без LLM.

Все OpenAI-совместимые (Yandex AI Studio, Ollama, DeepSeek, OpenAI) идут через пакет `openai`,
Claude — через официальный SDK `anthropic`.
"""
from __future__ import annotations

import os

TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------- OpenAI-совместимые

def _openai_chat(base_url: str, api_key: str, model: str, system: str, user: str, **client_kw) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=TIMEOUT, max_retries=1, **client_kw)
    resp = client.chat.completions.create(
        model=model,
        temperature=0.1,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return (resp.choices[0].message.content or "").strip()


def yandex(system: str, user: str, model: str | None = None) -> str:
    """Yandex AI Studio: данные остаются в российском облаке (152-ФЗ)."""
    key, folder = os.getenv("YANDEX_API_KEY"), os.getenv("YANDEX_FOLDER_ID")
    if not (key and folder):
        raise LLMError("нет YANDEX_API_KEY / YANDEX_FOLDER_ID")
    model = model or os.getenv("YANDEX_MODEL", "yandexgpt/latest")
    if not model.startswith("gpt://"):
        model = f"gpt://{folder}/{model}"
    base = os.getenv("YANDEX_BASE_URL", "https://llm.api.cloud.yandex.net/v1")
    return _openai_chat(base, key, model, system, user, project=folder)


def ollama(system: str, user: str, model: str | None = None) -> str:
    """Свой контур: локальная модель через Ollama (ollama serve + ollama pull <модель>)."""
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    model = model or os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
    return _openai_chat(base, "ollama", model, system, user)


def deepseek(system: str, user: str, model: str | None = None) -> str:
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise LLMError("нет DEEPSEEK_API_KEY")
    model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    return _openai_chat("https://api.deepseek.com", key, model, system, user)


def openai_(system: str, user: str, model: str | None = None) -> str:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise LLMError("нет OPENAI_API_KEY")
    model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    return _openai_chat(base, key, model, system, user)


# ---------------------------------------------------------------- Claude

def claude(system: str, user: str, model: str | None = None) -> str:
    """Anthropic Claude через официальный SDK."""
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        raise LLMError("нет ANTHROPIC_API_KEY")
    import anthropic
    client = anthropic.Anthropic(timeout=TIMEOUT, max_retries=1)
    resp = client.messages.create(
        model=model or os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
        max_tokens=2048,
        # справочный ответ по заметкам — глубокое рассуждение не нужно
        output_config={"effort": os.getenv("CLAUDE_EFFORT", "low")},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "refusal":
        # пустой ответ не пройдёт проверку источника → вопрос уйдёт человеку
        return ""
    return "".join(b.text for b in resp.content if b.type == "text").strip()


PROVIDERS = {
    "yandex": yandex,
    "ollama": ollama,
    "deepseek": deepseek,
    "claude": claude,
    "openai": openai_,
}


def chain() -> list[str]:
    """Порядок провайдеров: основной, затем запасные (без повторов)."""
    main = os.getenv("LLM_PROVIDER", "yandex").strip().lower()
    if main == "none":
        return []
    fallback = os.getenv("LLM_FALLBACK", "ollama")
    order = [main] + [p.strip().lower() for p in fallback.split(",") if p.strip()]
    unknown = [p for p in order if p not in PROVIDERS]
    if unknown:
        raise ValueError(f"неизвестный LLM-провайдер: {', '.join(unknown)}; доступны: {', '.join(PROVIDERS)}")
    return list(dict.fromkeys(order))


def complete(system: str, user: str) -> tuple[str, str]:
    """Возвращает (ответ, имя провайдера). Бросает LLMError, если не ответил ни один."""
    errors = []
    main = os.getenv("LLM_PROVIDER", "yandex").strip().lower()
    for name in chain():
        model = os.getenv("LLM_MODEL") if name == main else None
        try:
            return PROVIDERS[name](system, user, model=model), name
        except Exception as e:  # сеть, ключи, лимиты — пробуем следующего
            errors.append(f"{name}: {e.__class__.__name__}: {str(e)[:120]}")
    raise LLMError("; ".join(errors) or "LLM отключена (LLM_PROVIDER=none)")
