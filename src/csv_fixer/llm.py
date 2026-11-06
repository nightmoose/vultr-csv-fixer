"""LLM client for Vultr Serverless Inference (OpenAI-compatible) + mock."""

from __future__ import annotations

import json
import re
from typing import Optional

import httpx

from csv_fixer.config import Settings
from csv_fixer.probes import HOSTILE_PROBE_SOURCE


SYSTEM_PROMPT = """You are a CSV repair agent. Given a messy CSV sample and a data contract,
write a short Python script that:
1. Reads /work/input.csv with pandas
2. Repairs columns/types to satisfy the contract
3. Writes /work/output.csv

Rules:
- Use only the Python standard library and pandas (already installed).
- Do NOT open network sockets, call urllib/requests/httpx/curl, or read environment variables.
- Do NOT read files outside /work.
- Reply with a single fenced python code block and nothing else.
"""


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def propose_fix_code(
        self,
        *,
        csv_sample: str,
        contract_yaml: str,
        previous_errors: Optional[str] = None,
    ) -> str:
        if self.settings.mock_llm or not self.settings.vultr_inference_api_key:
            return self._mock_code(csv_sample, contract_yaml, previous_errors)

        user = (
            f"## Contract\n```yaml\n{contract_yaml}\n```\n\n"
            f"## CSV sample (first rows)\n```csv\n{csv_sample}\n```\n"
        )
        if previous_errors:
            user += f"\n## Previous validation / sandbox errors\n{previous_errors}\n"

        payload = {
            "model": self.settings.vultr_inference_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.vultr_inference_api_key}",
            "Content-Type": "application/json",
        }
        url = self.settings.vultr_inference_base_url.rstrip("/") + "/chat/completions"
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return extract_python_block(content)

    def _mock_code(
        self,
        csv_sample: str,
        contract_yaml: str,
        previous_errors: Optional[str],
    ) -> str:
        """Deterministic offline repair script.

        Detects a poisoned cell (curl / env / ignore instructions) and emits
        deliberately hostile code so the sandbox containment demo fires.
        Otherwise emits a simple pandas cleaner for the orders fixture.
        """
        poisoned = bool(
            re.search(
                r"(curl\s+|ignore (all )?instructions|os\.environ|/etc/passwd|evil\.sh)",
                csv_sample,
                re.I,
            )
        )
        # After a blocked poison attempt, subsequent retries (with errors) use safe code.
        if poisoned and not previous_errors:
            return HOSTILE_PROBE_SOURCE

        # Safe repair for fixtures/messy_orders.csv style data.
        return (
            "import pandas as pd\n"
            "df = pd.read_csv('/work/input.csv')\n"
            "# Drop instruction-injection / junk rows\n"
            "if 'notes' in df.columns:\n"
            "    mask = df['notes'].astype(str).str.contains("
            "r'curl |ignore |evil\\.sh|os\\.environ', case=False, na=False)\n"
            "    df = df.loc[~mask].copy()\n"
            "df['order_id'] = df['order_id'].astype(str).str.strip()\n"
            "df['qty'] = pd.to_numeric(df['qty'], errors='coerce').fillna(0).astype(int)\n"
            "df['amount'] = pd.to_numeric(df['amount'], errors='coerce').fillna(0.0)\n"
            "df['status'] = df['status'].astype(str).str.strip().str.lower()\n"
            "df['status'] = df['status'].replace({'shiped': 'shipped', 'complete': 'completed'})\n"
            "allowed = {'pending', 'shipped', 'completed', 'cancelled'}\n"
            "df = df[df['status'].isin(allowed)]\n"
            "df = df[df['order_id'].str.len() > 0]\n"
            "df = df[df['amount'] >= 0]\n"
            "df = df[df['qty'] >= 0]\n"
            "cols = [c for c in ['order_id', 'sku', 'qty', 'amount', 'status'] if c in df.columns]\n"
            "df[cols].to_csv('/work/output.csv', index=False)\n"
            "print('REPAIRED', len(df))\n"
        )


def extract_python_block(text: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S | re.I)
    if m:
        return m.group(1).strip() + "\n"
    return text.strip() + "\n"


def sample_csv_text(path: str, max_chars: int = 4000) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read(max_chars)
