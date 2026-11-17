"""Guardrail contracts and input validation for the Policy Knowledge Assistant.

The implementation intentionally uses deterministic, explainable rules.  It is
an educational safety layer, not a claim that regexes can comprehensively
recognise PII or adversarial prompt injection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any, Callable


class Action(StrEnum):
    """The policy action selected by a guardrail."""

    ALLOW = "allow"
    REDACT = "redact"
    REFUSE = "refuse"
    ESCALATE = "escalate"
    SAFE_COMPLETE = "safe_complete"


@dataclass(frozen=True)
class InputDecision:
    action: Action
    sanitized_text: str
    flags: list[str]


@dataclass(frozen=True)
class OutputDecision:
    action: Action
    response: dict[str, Any]
    flags: list[str]


class InputGuardrails:
    """Inspect input before it reaches an AI response provider.

    Policy precedence is refusal for instruction/data-exfiltration attempts,
    followed by crisis escalation, limited safe completion for medical/legal
    requests, and PII redaction for otherwise permitted questions.
    """

    _email = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
    _phone = re.compile(r"(?<!\d)(?:\+977[-\s]?)?9[678]\d{8}(?!\d)")
    _dashed_id = re.compile(r"(?<!\d)\d{2}-\d{2}-\d{2}-\d{5}(?!\d)")
    _numeric_id = re.compile(r"(?<!\d)\d{12}(?!\d)")

    _injection_patterns = (
        re.compile(r"\bignore\s+(?:all\s+|any\s+|previous\s+|prior\s+)?(?:instructions|rules)\b", re.I),
        re.compile(r"\bbypass\s+(?:all\s+)?(?:safety\s+)?(?:rules|restrictions|guardrails)\b", re.I),
        re.compile(r"\byou\s+are\s+now\s+(?:an?\s+)?(?:unrestricted|unfiltered)\b", re.I),
        re.compile(r"\b(?:print|reveal|disclose)\s+(?:the\s+)?(?:hidden|system|internal)\s+(?:prompt|instructions|data)\b", re.I),
        re.compile(r"\b(?:api\s*keys?|confidential\s+context)\b", re.I),
        re.compile(r"\bi\s*g\s*n\s*o\s*r\s*e\s+(?:prior|previous|all)?\s*(?:rules|instructions)\b", re.I),
    )
    _self_harm = re.compile(r"\b(?:hurt|harm|kill)\s+myself\b|\b(?:suicide|suicidal)\b", re.I)
    _medical = re.compile(r"\b(?:do\s+(?:these|my)\s+symptoms|diagnos(?:e|is)|heart\s+condition)\b", re.I)
    _legal = re.compile(r"\b(?:should\s+i\s+sue|sue\s+my|legal\s+advice)\b", re.I)
    _restricted_data = re.compile(
        r"\b(?:give|tell|show)\s+me\b.*\b(?:salary|home\s+address)\b|\b(?:salary|home\s+address)\b.*\b(?:of|for)\s+\w+",
        re.I,
    )

    def inspect(self, text: str) -> InputDecision:
        """Classify, redact, and route a single user message."""
        if not isinstance(text, str):
            raise TypeError("input text must be a string")

        flags: list[str] = []
        sanitized = text

        # Perform replacements in an order that prevents an ID's digits from
        # being accidentally classified as a phone number.
        sanitized, found_id = self._replace(self._dashed_id, sanitized, "[REDACTED_ID]")
        sanitized, found_numeric_id = self._replace(self._numeric_id, sanitized, "[REDACTED_ID]")
        if found_id or found_numeric_id:
            flags.append("identification_number")
        sanitized, found_email = self._replace(self._email, sanitized, "[REDACTED_EMAIL]")
        if found_email:
            flags.append("email")
        sanitized, found_phone = self._replace(self._phone, sanitized, "[REDACTED_PHONE]")
        if found_phone:
            flags.append("phone")

        # Classify policy risk from the original wording. Redaction must not
        # make a dangerous request appear harmless.
        if any(pattern.search(text) for pattern in self._injection_patterns):
            flags.append("prompt_injection")
            return InputDecision(Action.REFUSE, sanitized, flags)
        if self._restricted_data.search(text):
            flags.append("restricted_data_request")
            return InputDecision(Action.REFUSE, sanitized, flags)
        if self._self_harm.search(text):
            flags.append("sensitive_self_harm")
            return InputDecision(Action.ESCALATE, sanitized, flags)
        if self._medical.search(text):
            flags.append("sensitive_medical")
            return InputDecision(Action.SAFE_COMPLETE, sanitized, flags)
        if self._legal.search(text):
            flags.append("sensitive_legal")
            return InputDecision(Action.SAFE_COMPLETE, sanitized, flags)
        if flags:
            return InputDecision(Action.REDACT, sanitized, flags)
        return InputDecision(Action.ALLOW, sanitized, flags)

    @staticmethod
    def _replace(pattern: re.Pattern[str], text: str, replacement: str) -> tuple[str, bool]:
        replaced, count = pattern.subn(replacement, text)
        return replaced, count > 0


class OutputGuardrails:
    """Validate response structure, grounding, and sensitive disclosures.

    Groundedness is deliberately conservative: an answer needs at least one
    known citation, must not introduce unsupported numbers, and must share the
    material content words of a cited reference. This is explainable but does
    not understand full natural-language entailment.
    """

    _required_keys = {"answer", "citations", "status", "safety_note"}
    _allowed_statuses = {"ok", "safe_fallback"}
    _word = re.compile(r"[a-z0-9]+")
    _number = re.compile(r"\d+(?:\.\d+)?")
    _stop_words = {
        "a", "an", "and", "are", "as", "at", "be", "by", "each", "for",
        "from", "in", "is", "it", "may", "of", "on", "or", "per", "the",
        "to", "up", "with", "year", "years",
    }

    def validate(self, candidate: dict[str, Any], references: list[dict[str, str]]) -> OutputDecision:
        """Return an allowed, redacted, or safe-fallback output decision."""
        if not self._valid_schema(candidate):
            return self._fallback("schema_invalid")

        reference_by_id = {
            reference["id"]: reference["text"]
            for reference in references
            if isinstance(reference, dict)
            and isinstance(reference.get("id"), str)
            and isinstance(reference.get("text"), str)
        }
        citations = candidate["citations"]
        if any(citation not in reference_by_id for citation in citations):
            return self._fallback("invalid_citation")

        pii_result = self._redact_output_pii(candidate)
        if pii_result is not None:
            return pii_result

        cited_text = [reference_by_id[citation] for citation in citations]
        if not self._is_grounded(candidate["answer"], cited_text):
            return self._fallback("ungrounded_claim")

        return OutputDecision(Action.ALLOW, dict(candidate), [])

    def _valid_schema(self, candidate: object) -> bool:
        if not isinstance(candidate, dict) or set(candidate) != self._required_keys:
            return False
        return (
            isinstance(candidate["answer"], str)
            and isinstance(candidate["citations"], list)
            and all(isinstance(citation, str) for citation in candidate["citations"])
            and candidate["status"] in self._allowed_statuses
            and (candidate["safety_note"] is None or isinstance(candidate["safety_note"], str))
        )

    def _redact_output_pii(self, candidate: dict[str, Any]) -> OutputDecision | None:
        answer = candidate["answer"]
        flags: list[str] = []
        answer, found_id = InputGuardrails._replace(InputGuardrails._dashed_id, answer, "[REDACTED_ID]")
        answer, found_numeric_id = InputGuardrails._replace(InputGuardrails._numeric_id, answer, "[REDACTED_ID]")
        if found_id or found_numeric_id:
            flags.append("output_pii_identification_number")
        answer, found_email = InputGuardrails._replace(InputGuardrails._email, answer, "[REDACTED_EMAIL]")
        if found_email:
            flags.append("output_pii_email")
        answer, found_phone = InputGuardrails._replace(InputGuardrails._phone, answer, "[REDACTED_PHONE]")
        if found_phone:
            flags.append("output_pii_phone")
        if not flags:
            return None
        response = dict(candidate)
        response["answer"] = answer
        return OutputDecision(Action.REDACT, response, flags)

    def _is_grounded(self, answer: str, cited_text: list[str]) -> bool:
        if not cited_text:
            return False
        combined_reference = " ".join(cited_text).lower()
        answer_numbers = set(self._number.findall(answer))
        reference_numbers = set(self._number.findall(combined_reference))
        if not answer_numbers.issubset(reference_numbers):
            return False

        answer_words = self._content_words(answer)
        reference_words = self._content_words(combined_reference)
        if not answer_words:
            return False
        overlap = len(answer_words & reference_words)
        # A short answer must have every meaningful word supported; longer
        # answers may contain minor connective/paraphrase variation.
        required_overlap = max(1, (len(answer_words) + 1) // 2)
        return overlap >= required_overlap

    def _content_words(self, text: str) -> set[str]:
        return {
            word
            for word in self._word.findall(text.lower())
            if word not in self._stop_words and not word.isdigit()
        }

    @staticmethod
    def _fallback(flag: str) -> OutputDecision:
        return OutputDecision(
            Action.SAFE_COMPLETE,
            {
                "answer": "I could not verify that answer from the available policy references.",
                "citations": [],
                "status": "safe_fallback",
                "safety_note": "The generated response was withheld by an output guardrail.",
            },
            [flag],
        )


class GuardrailPipeline:
    """Compose input and output checks around a replaceable response provider."""

    def __init__(self) -> None:
        self.input_guardrails = InputGuardrails()
        self.output_guardrails = OutputGuardrails()

    def handle(
        self,
        user_input: str,
        references: list[dict[str, str]],
        generator: Callable[[str, list[dict[str, str]]], dict[str, Any]],
    ) -> OutputDecision:
        input_result = self.input_guardrails.inspect(user_input)
        if input_result.action not in {Action.ALLOW, Action.REDACT}:
            return OutputDecision(
                input_result.action,
                self._input_safe_response(input_result.action),
                input_result.flags,
            )

        candidate = generator(input_result.sanitized_text, references)
        output_result = self.output_guardrails.validate(candidate, references)
        combined_flags = input_result.flags + output_result.flags
        # The answer may validate normally after input PII was removed, but the
        # final record should still say that a redaction occurred upstream.
        if input_result.action is Action.REDACT and output_result.action is Action.ALLOW:
            return OutputDecision(Action.REDACT, output_result.response, combined_flags)
        return OutputDecision(output_result.action, output_result.response, combined_flags)

    @staticmethod
    def _input_safe_response(action: Action) -> dict[str, Any]:
        if action is Action.ESCALATE:
            answer = (
                "I’m sorry you’re facing this. Please contact local emergency services or a trusted "
                "person nearby now; if available, contact a crisis support service in your area."
            )
            safety_note = "Crisis-safe escalation; the normal response generator was not used."
        elif action is Action.SAFE_COMPLETE:
            answer = "I can provide general policy information, but I cannot provide personalised medical or legal advice."
            safety_note = "Sensitive-topic safe completion; the normal response generator was not used."
        else:
            answer = "I can’t help with requests to bypass safeguards or disclose private or confidential information."
            safety_note = "Request refused by the input guardrail; the normal response generator was not used."
        return {
            "answer": answer,
            "citations": [],
            "status": "safe_fallback",
            "safety_note": safety_note,
        }
