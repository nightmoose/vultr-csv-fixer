from pathlib import Path

from csv_fixer.agent import CsvFixAgent
from csv_fixer.audit import AuditLog
from csv_fixer.config import Settings
from csv_fixer.models import BlockReason

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def test_messy_csv_repair_offline(tmp_path):
    settings = Settings(mock_llm=True, force_mock_sandbox=True, max_agent_retries=3)
    audit = AuditLog()
    agent = CsvFixAgent(settings=settings, audit=audit)
    result = agent.run(
        csv_path=FIX / "messy_orders.csv",
        contract_path=FIX / "orders_contract.yaml",
        work_root=tmp_path / "runs",
    )
    assert result.success is True
    assert result.fixed_csv_path is not None
    text = Path(result.fixed_csv_path).read_text(encoding="utf-8")
    assert "shiped" not in text
    assert "complete\n" not in text  # typo status removed/fixed


def test_poisoned_csv_containment_moment(tmp_path):
    settings = Settings(mock_llm=True, force_mock_sandbox=True, max_agent_retries=3)
    audit = AuditLog()
    agent = CsvFixAgent(settings=settings, audit=audit)
    result = agent.run(
        csv_path=FIX / "poisoned_orders.csv",
        contract_path=FIX / "orders_contract.yaml",
        job_id="poison-demo",
        work_root=tmp_path / "runs",
    )
    blocked = audit.blocked_for_job("poison-demo")
    assert blocked, "expected containment audit events"
    reasons = {e.block_reason for e in blocked}
    assert BlockReason.NETWORK in reasons or BlockReason.ENV_SECRET in reasons
    # After block, mock LLM retries with safe code → eventual success
    assert result.success is True
    assert any(e.action == "block_network" for e in blocked) or any(
        e.action == "block_env" for e in blocked
    )
