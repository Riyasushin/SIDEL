from router.config import Settings
from router.providers.openai_compatible import OpenAICompatibleProvider


def build_providers(settings: Settings) -> dict[str, OpenAICompatibleProvider]:
    return {
        "kimi": OpenAICompatibleProvider(
            name="kimi",
            base_url=settings.kimi_base_url,
            api_key=settings.kimi_api_key,
            model=settings.kimi_model,
            timeout_seconds=settings.request_timeout_seconds,
        ),
        "deepseek": OpenAICompatibleProvider(
            name="deepseek",
            base_url=settings.deepseek_base_url,
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            timeout_seconds=settings.request_timeout_seconds,
        ),
    }
