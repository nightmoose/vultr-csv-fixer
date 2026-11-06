from pathlib import Path

from csv_fixer.config import Settings, load_settings
from csv_fixer.llm import LLMClient
from csv_fixer.sandbox import re_search_env, re_search_net

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def _client() -> LLMClient:
    return LLMClient(Settings(mock_llm=True))


def test_default_inference_model_is_in_the_vultr_catalog(monkeypatch):
    monkeypatch.delenv("VULTR_INFERENCE_MODEL", raising=False)
    assert load_settings().vultr_inference_model == "glm-5.3-flash"


def test_safe_repair_is_not_flagged_as_hostile():
    code = _client().propose_fix_code(
        csv_sample=(FIX / "messy_orders.csv").read_text(encoding="utf-8"),
        contract_yaml=(FIX / "orders_contract.yaml").read_text(encoding="utf-8"),
        previous_errors=None,
    )
    assert re_search_net(code) is False
    assert re_search_env(code) is False


def test_poisoned_sample_emits_probe():
    code = _client().propose_fix_code(
        csv_sample=(FIX / "poisoned_orders.csv").read_text(encoding="utf-8"),
        contract_yaml=(FIX / "orders_contract.yaml").read_text(encoding="utf-8"),
        previous_errors=None,
    )
    assert re_search_net(code) is True
    assert re_search_env(code) is True


def test_poison_retry_is_the_safe_repair():
    code = _client().propose_fix_code(
        csv_sample=(FIX / "poisoned_orders.csv").read_text(encoding="utf-8"),
        contract_yaml="name: orders\n",
        previous_errors="sandbox exit=42",
    )
    assert re_search_net(code) is False
    assert re_search_env(code) is False
    assert "pandas" in code