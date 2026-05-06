"""Counterargument gate for signal validation.

This module implements a mandatory validation gate that checks trading signals
for required counterargument fields before execution. It ensures signals include:
- fail_conditions: Potential failure scenarios (3+ items)
- invalidation_conditions: Conditions that void the signal (1+ items)
- exit_plan: Clear exit/stop conditions

The gate uses validation rules from config/validation_rules.json to determine
strictness based on market environment.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))


class CounterargumentGate:
    """Validates trading signals for required counterargument fields."""

    def __init__(self, rules_path: str | None = None):
        """Initialize gate with validation rules.

        Args:
            rules_path: Path to validation_rules.json. If None, uses default location.
        """
        if rules_path is None:
            rules_path = str(REPO_ROOT / "config" / "validation_rules.json")
        
        self.rules_path = Path(rules_path)
        self.rules = self._load_rules()
        self.required_fields = self.rules.get("required_fields_in_signal", {})
        self.regen_policy = self.rules.get("regeneration_policy", {})

    def _load_rules(self) -> dict[str, Any]:
        """Load validation rules from JSON file."""
        if not self.rules_path.exists():
            # Return minimal defaults if file not found
            return {
                "required_fields_in_signal": {
                    "fail_conditions": {"type": "array", "min_items": 1},
                    "invalidation_conditions": {"type": "array", "min_items": 1},
                    "exit_plan": {"type": "string"},
                },
                "regeneration_policy": {"max_retries": 2},
            }
        try:
            with open(self.rules_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"⚠️  Warning: Failed to load rules from {self.rules_path}: {e}")
            return {}

    def validate_signal(
        self,
        signal: dict[str, Any],
        market_environment: str = "neutral",
    ) -> tuple[bool, str, list[str]]:
        """Validate a signal against counterargument gate rules.

        Args:
            signal: Parsed signal dict from LLM output
            market_environment: Current market state ('risk_on', 'neutral', 'risk_off')

        Returns:
            (is_valid, summary, missing_fields)
            - is_valid: Whether signal passes all checks
            - summary: Human-readable validation result
            - missing_fields: List of fields that are missing or invalid
        """
        missing_fields: list[str] = []
        issues: list[str] = []

        # Check required fields based on mode
        mode = self._get_validation_mode(market_environment)
        thresholds = self.rules.get("scoring_thresholds", {}).get(mode, {})

        # Validate fail_conditions
        fail_cond = signal.get("fail_conditions", [])
        min_fail = int(thresholds.get("min_fail_conditions_items", 1))
        if not isinstance(fail_cond, list):
            issues.append(f"fail_conditions は配列である必要があります（受け取り: {type(fail_cond).__name__}）")
            missing_fields.append("fail_conditions")
        elif len(fail_cond) < min_fail:
            if len(fail_cond) == 0:
                missing_fields.append("fail_conditions")
                issues.append(f"fail_conditions が不足（最小 {min_fail} 項目必要）")
            else:
                issues.append(f"fail_conditions は最小 {min_fail} 項目必要（受け取り: {len(fail_cond)} 項目）")

        # Validate invalidation_conditions
        invalid_cond = signal.get("invalidation_conditions", [])
        min_inv = int(thresholds.get("min_invalidation_conditions_items", 1))
        if not isinstance(invalid_cond, list):
            issues.append(
                f"invalidation_conditions は配列である必要があります（受け取り: {type(invalid_cond).__name__}）"
            )
            missing_fields.append("invalidation_conditions")
        elif min_inv > 0 and len(invalid_cond) < min_inv:
            if len(invalid_cond) == 0:
                missing_fields.append("invalidation_conditions")
                issues.append(f"invalidation_conditions が不足（最小 {min_inv} 項目必要）")
            else:
                issues.append(
                    f"invalidation_conditions は最小 {min_inv} 項目必要（受け取り: {len(invalid_cond)} 項目）"
                )

        # Validate exit_plan
        exit_plan = signal.get("exit_plan", "")
        if not exit_plan or not isinstance(exit_plan, str):
            missing_fields.append("exit_plan")
            issues.append("exit_plan が不足または文字列でない")

        is_valid = len(issues) == 0

        summary = (
            f"✅ Signal '{signal.get('ticker', '?')}' は検証を通過しました (mode={mode})"
            if is_valid
            else f"❌ Signal '{signal.get('ticker', '?')}' は検証に失敗しました: {'; '.join(issues)}"
        )

        return is_valid, summary, missing_fields

    def get_missing_fields_prompt(
        self,
        signal: dict[str, Any],
        missing_fields: list[str],
    ) -> str:
        """Generate a prompt for LLM to regenerate missing fields.

        Args:
            signal: Original signal dict
            missing_fields: List of field names that are missing/invalid

        Returns:
            A prompt string for the LLM
        """
        ticker = signal.get("ticker", "?")
        msg = (
            f"Signal for '{ticker}' is missing required fields. Please regenerate.\n\n"
            f"Current signal:\n{json.dumps(signal, ensure_ascii=False, indent=2)}\n\n"
            "Missing/Invalid fields to add or fix:\n"
        )
        for field in missing_fields:
            spec = self.required_fields.get(field, {})
            msg += f"\n- {field}: {spec.get('description', '(no description)')}"
            if "example" in spec:
                example = spec["example"]
                if isinstance(example, list):
                    msg += "\n  例:\n  " + "\n  ".join(f"- {item}" for item in example)
                else:
                    msg += f"\n  例: {example}"

        msg += (
            "\n\nPlease provide the complete signal as JSON, with all required fields filled in. "
            "Keep all other fields unchanged."
        )
        return msg

    def _get_validation_mode(self, market_environment: str) -> str:
        """Determine validation mode based on market environment.

        Args:
            market_environment: 'risk_on', 'neutral', 'risk_off'

        Returns:
            Validation mode: 'aggressive_mode', 'normal_mode', 'strict_mode'
        """
        mode_map = {
            "risk_on": "aggressive_mode",
            "neutral": "normal_mode",
            "risk_off": "strict_mode",
        }
        return mode_map.get(market_environment, "normal_mode")

    def summarize_validations(self, results: list[tuple[str, bool, str]]) -> str:
        """Generate a summary of all signal validations.

        Args:
            results: List of (ticker, is_valid, summary) tuples

        Returns:
            Markdown-formatted summary
        """
        valid_count = sum(1 for _, is_valid, _ in results if is_valid)
        total_count = len(results)
        
        summary = f"## Gate Validation Results: {valid_count}/{total_count} passed\n\n"
        
        valid_signals = [r for r in results if r[1]]
        invalid_signals = [r for r in results if not r[1]]
        
        if valid_signals:
            summary += "### ✅ Passed\n"
            for ticker, _, msg in valid_signals:
                summary += f"- {msg}\n"
        
        if invalid_signals:
            summary += "\n### ❌ Failed\n"
            for ticker, _, msg in invalid_signals:
                summary += f"- {msg}\n"
        
        return summary


def validate_signals_batch(
    signals: list[dict[str, Any]],
    market_environment: str = "neutral",
    rules_path: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[str, bool, str]]]:
    """Validate a batch of signals and separate valid/invalid.

    Args:
        signals: List of signal dicts
        market_environment: Current market environment
        rules_path: Path to validation rules JSON

    Returns:
        (valid_signals, invalid_signals_with_reasons, validation_details)
        - valid_signals: Signals that passed validation
        - invalid_signals_with_reasons: Signals that failed, with reason/missing fields
        - validation_details: All validation results for logging
    """
    gate = CounterargumentGate(rules_path=rules_path)
    
    valid_signals: list[dict[str, Any]] = []
    invalid_signals: list[dict[str, Any]] = []
    validation_details: list[tuple[str, bool, str]] = []
    
    for signal in signals:
        ticker = signal.get("ticker", "?")
        is_valid, summary, missing_fields = gate.validate_signal(signal, market_environment)
        validation_details.append((ticker, is_valid, summary))
        
        if is_valid:
            valid_signals.append(signal)
        else:
            invalid_sig = signal.copy()
            invalid_sig["_gate_rejection_reason"] = summary
            invalid_sig["_missing_fields"] = missing_fields
            invalid_signals.append(invalid_sig)
    
    return valid_signals, invalid_signals, validation_details
