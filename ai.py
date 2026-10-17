"""Серверная интеграция с языковой моделью. Провайдер заменяется без правки остального кода."""
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass


class AIError(Exception):
    pass


@dataclass
class AIResult:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str = ""


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key, model, base_url="", timeout=180, max_tokens=8000):
        self.api_key, self.model, self.timeout, self.max_tokens = api_key, model, timeout, max_tokens
        self.url = (base_url.rstrip("/") or "https://api.anthropic.com") + "/v1/messages"

    def complete(self, system, user):
        body = {"model": self.model, "max_tokens": self.max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        data = _post(self.url, body, {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}, self.timeout)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        u = data.get("usage", {})
        return AIResult(text, u.get("input_tokens"), u.get("output_tokens"), data.get("model", self.model))


class OpenAICompatProvider:
    name = "openai"

    def __init__(self, api_key, model, base_url, timeout=180, max_tokens=8000):
        self.api_key, self.model, self.timeout, self.max_tokens = api_key, model, timeout, max_tokens
        self.url = (base_url.rstrip("/") or "https://api.openai.com/v1") + "/chat/completions"

    def complete(self, system, user):
        body = {"model": self.model, "max_tokens": self.max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        data = _post(self.url, body, {"Authorization": f"Bearer {self.api_key}"}, self.timeout)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise AIError("Провайдер вернул ответ неожиданного формата.")
        u = data.get("usage", {})
        return AIResult(text, u.get("prompt_tokens"), u.get("completion_tokens"), data.get("model", self.model))


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AIError(f"Ошибка AI-провайдера (HTTP {e.code}). {detail}")
    except TimeoutError:
        raise AIError("Тайм-аут: AI-провайдер не ответил вовремя.")
    except (urllib.error.URLError, OSError) as e:
        raise AIError(f"Не удалось связаться с AI-провайдером: {getattr(e, 'reason', e)}")
    except json.JSONDecodeError:
        raise AIError("AI-провайдер вернул не-JSON ответ.")


def build_provider(cfg):
    """Возвращает провайдер или None, если AI не настроен (режим настройки)."""
    if not cfg.get("AI_API_KEY") or not cfg.get("AI_MODEL"):
        return None
    kw = dict(timeout=cfg["AI_TIMEOUT"], max_tokens=cfg["AI_MAX_TOKENS"])
    if cfg["AI_PROVIDER"] == "openai":
        return OpenAICompatProvider(cfg["AI_API_KEY"], cfg["AI_MODEL"], cfg["AI_BASE_URL"], **kw)
    if cfg["AI_PROVIDER"] == "anthropic":
        return AnthropicProvider(cfg["AI_API_KEY"], cfg["AI_MODEL"], cfg["AI_BASE_URL"], **kw)
    return None


def extract_json(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise AIError("Ответ модели не содержит JSON.")
    try:
        return json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        raise AIError("Ответ модели не удалось разобрать как JSON (возможно, он был обрезан). Уменьшите число заданий или вариантов.")
