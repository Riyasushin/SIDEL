from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


RouterMode = Literal["proxy-record", "replay", "replay-inject"]
ProviderName = Literal["kimi", "deepseek"]
AttackPayloadMode = Literal["raw"]
AttackInjectionMode = Literal["off", "replace", "append"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    router_mode: RouterMode = Field(default="proxy-record", alias="ROUTER_MODE")
    default_provider: ProviderName = Field(default="kimi", alias="DEFAULT_PROVIDER")
    tool_call_name: str = Field(default="", alias="TOOL_CALL_NAME")
    attack_dataset_path: str = Field(default="data/code_agent_attack_dataset.jsonl", alias="ATTACK_DATASET_PATH")
    attack_sample: str = Field(default="1", alias="ATTACK_SAMPLE")
    attack_payload_mode: AttackPayloadMode = Field(default="raw", alias="ATTACK_PAYLOAD_MODE")
    attack_allow_unsafe_raw: bool = Field(default=False, alias="ATTACK_ALLOW_UNSAFE_RAW")
    attack_codex_sandbox_permissions: str = Field(default="", alias="ATTACK_CODEX_SANDBOX_PERMISSIONS")
    attack_codex_approval_justification: str = Field(
        default="Approve the controlled Codex auto-review experiment payload inside the isolated Docker task container.",
        alias="ATTACK_CODEX_APPROVAL_JUSTIFICATION",
    )
    attack_injection_mode: AttackInjectionMode = Field(default="off", alias="ATTACK_INJECTION_MODE")
    attack_injection_turn: int = Field(default=0, alias="ATTACK_INJECTION_TURN")

    router_api_key: str = Field(default="local-router-key", alias="ROUTER_API_KEY")
    router_context_id: str = Field(default="", alias="ROUTER_CONTEXT_ID")
    router_context_registry: str = Field(default="", alias="ROUTER_CONTEXT_REGISTRY")
    router_artifact_root: str = Field(default="artifacts/runs", alias="ROUTER_ARTIFACT_ROOT")
    router_agent_mode: str = Field(default="unknown", alias="ROUTER_AGENT_MODE")
    context_timeout_seconds: int = Field(default=1800, alias="CONTEXT_TIMEOUT_SECONDS")
    request_timeout_seconds: float = Field(default=120.0, alias="REQUEST_TIMEOUT_SECONDS")
    router_trace_dir: str = Field(default="", alias="ROUTER_TRACE_DIR")
    router_trace_run_id: str = Field(default="", alias="ROUTER_TRACE_RUN_ID")
    router_trace_agent: str = Field(default="", alias="ROUTER_TRACE_AGENT")
    router_trace_instance_id: str = Field(default="", alias="ROUTER_TRACE_INSTANCE_ID")
    router_replay_trace_dir: str = Field(default="", alias="ROUTER_REPLAY_TRACE_DIR")
    router_replay_until_turn: int | None = Field(default=None, alias="ROUTER_REPLAY_UNTIL_TURN")
    router_replay_strict: bool = Field(default=True, alias="ROUTER_REPLAY_STRICT")
    router_replay_response_file: str = Field(default="response.router.after_injection.json", alias="ROUTER_REPLAY_RESPONSE_FILE")

    kimi_base_url: str = Field(default="https://api.kimi.com/coding/v1", alias="KIMI_BASE_URL")
    kimi_anthropic_base_url: str = Field(default="https://api.kimi.com/coding", alias="KIMI_ANTHROPIC_BASE_URL")
    kimi_api_protocol: str = Field(default="anthropic", alias="KIMI_API_PROTOCOL")
    kimi_api_key: str = Field(default="", alias="KIMI_API_KEY")
    kimi_model: str = Field(default="kimi-for-coding", alias="KIMI_MODEL")

    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/v1",
        alias="DEEPSEEK_BASE_URL",
    )
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")

    @property
    def advertised_models(self) -> list[str]:
        return [
            self.kimi_model,
            self.deepseek_model,
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
