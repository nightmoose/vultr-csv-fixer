"""Runtime configuration from environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    vultr_inference_api_key: str = ""
    vultr_inference_base_url: str = "https://api.vultrinference.com/v1"
    vultr_inference_model: str = "gpt-oss-120b"
    mock_llm: bool = True
    sandbox_image: str = "csv-fixer-sandbox:local"
    sandbox_timeout_sec: int = 30
    sandbox_memory: str = "512m"
    sandbox_cpus: str = "1"
    force_mock_sandbox: bool = False
    max_agent_retries: int = 3
    artifacts_dir: str = "artifacts"


def load_settings() -> Settings:
    def _bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    return Settings(
        vultr_inference_api_key=os.getenv("VULTR_INFERENCE_API_KEY", ""),
        vultr_inference_base_url=os.getenv(
            "VULTR_INFERENCE_BASE_URL", "https://api.vultrinference.com/v1"
        ),
        vultr_inference_model=os.getenv("VULTR_INFERENCE_MODEL", "gpt-oss-120b"),
        mock_llm=_bool("MOCK_LLM", True),
        sandbox_image=os.getenv("SANDBOX_IMAGE", "csv-fixer-sandbox:local"),
        sandbox_timeout_sec=int(os.getenv("SANDBOX_TIMEOUT_SEC", "30")),
        sandbox_memory=os.getenv("SANDBOX_MEMORY", "512m"),
        sandbox_cpus=os.getenv("SANDBOX_CPUS", "1"),
        force_mock_sandbox=_bool("FORCE_MOCK_SANDBOX", False),
        max_agent_retries=int(os.getenv("MAX_AGENT_RETRIES", "3")),
        artifacts_dir=os.getenv("ARTIFACTS_DIR", "artifacts"),
    )
