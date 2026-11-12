"""Opt-in AI assistant for residue items (Section 2.4 & 4.3): code snippets only, human-reviewed."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from accelerator.config.client_config import AiAssistConfig
from accelerator.config.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class AiSuggestion:
    proposed_code: str
    rationale: str
    model_used: str
    confidence: float


class ResidueAiAssistant:
    """Proposes target conversions for flagged opaque/manual snippets without ever auto-applying."""

    def suggest_fix(
        self,
        original_snippet: str,
        reason_code: str,
        source_dialect: str,
        target_platform: str,
        ai_config: AiAssistConfig,
    ) -> AiSuggestion:
        if not ai_config.enabled:
            raise PermissionError("AI assist is disabled in the client configuration.")

        settings = get_settings()
        endpoint = ai_config.endpoint or settings.ai_endpoint
        model = ai_config.model or settings.ai_model

        if endpoint and settings.ai_enabled:
            try:
                return self._call_llm_endpoint(
                    endpoint=endpoint,
                    model=model,
                    api_key=settings.ai_api_key,
                    snippet=original_snippet,
                    reason_code=reason_code,
                    source_dialect=source_dialect,
                    target_platform=target_platform,
                )
            except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
                # Fall through to deterministic pattern assistant when external LLM is unreachable
                logger.warning(
                    "External AI endpoint unavailable, using deterministic fallback: %s", exc
                )

        return self._deterministic_fallback_proposal(
            snippet=original_snippet,
            reason_code=reason_code,
            source_dialect=source_dialect,
            target_platform=target_platform,
        )

    def _call_llm_endpoint(
        self,
        endpoint: str,
        model: str,
        api_key: str,
        snippet: str,
        reason_code: str,
        source_dialect: str,
        target_platform: str,
    ) -> AiSuggestion:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        prompt = (
            f"Convert the following {source_dialect} legacy code snippet (no business data) "
            f"to {target_platform}. Flag reason: {reason_code}.\n\n"
            f"Snippet:\n{snippet}\n\n"
            "Return only target code and a short comment explaining manual checks."
        )
        resp = httpx.post(
            endpoint,
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a data migration code assistant."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return AiSuggestion(
            proposed_code=content.strip(),
            rationale=f"Proposed by LLM endpoint ({model}) for {reason_code}",
            model_used=model,
            confidence=0.78,
        )

    def _deterministic_fallback_proposal(
        self,
        snippet: str,
        reason_code: str,
        source_dialect: str,
        target_platform: str,
    ) -> AiSuggestion:
        """Pattern-driven proposal when external LLM is not configured (safe local proposal)."""
        lower = snippet.lower()
        if "scripttask" in reason_code.lower() or "dts." in lower or "csharp" in lower:
            if target_platform == "databricks":
                proposed = (
                    "# Proposed PySpark replacement for SSIS ScriptTask (requires human review)\n"
                    "from pyspark.sql import functions as F\n\n"
                    "def transform(df):\n"
                    "    # TODO: verify ScriptTask custom logic against original C#/VB snippet\n"
                    "    return df.withColumn('_l2c_audit_ts', F.current_timestamp())\n"
                )
            elif target_platform == "snowflake":
                proposed = (
                    "-- Proposed Snowflake SQL/Python Snowpark replacement for ScriptTask\n"
                    "-- Review carefully against original script logic before approval\n"
                    "SELECT *, CURRENT_TIMESTAMP() AS _l2c_audit_ts FROM {{ ref('source_stage') }};\n"
                )
            else:
                proposed = (
                    "-- Proposed Fabric T-SQL / Notebook replacement for ScriptTask\n"
                    "SELECT *, SYSUTCDATETIME() AS _l2c_audit_ts FROM source_stage;\n"
                )
            return AiSuggestion(
                proposed_code=proposed,
                rationale="Converted ScriptTask control logic into a target-native audit/transform scaffold.",
                model_used="l2c-rule-assistant-v1",
                confidence=0.65,
            )

        if "cursor" in lower or "while" in lower:
            proposed = (
                f"-- Proposed set-based SQL refactor for procedural cursor/loop ({target_platform})\n"
                "-- Replace row-by-row iteration with MERGE / window function\n"
                + re.sub(r"\bWHILE\b|\bCURSOR\b", "-- REVIEW:", snippet, flags=re.IGNORECASE)
            )
            return AiSuggestion(
                proposed_code=proposed,
                rationale="Annotated procedural loop for set-based MERGE refactoring.",
                model_used="l2c-rule-assistant-v1",
                confidence=0.60,
            )

        return AiSuggestion(
            proposed_code=(
                f"-- Proposed {target_platform} template for '{reason_code}' ({source_dialect})\n"
                f"-- Original snippet preserved for verification:\n"
                + "\n".join(f"-- {line}" for line in snippet.splitlines())
                + "\nSELECT 1 AS manual_review_placeholder;\n"
            ),
            rationale=f"Generated safe {target_platform} review template for '{reason_code}'.",
            model_used="l2c-rule-assistant-v1",
            confidence=0.50,
        )
