"""
title: User Memory
author: beaudamore
date: 2026-01-10
version: 5.2
license: MIT
description: Per-user fact memory. Inlet injects the relevant facts into the latest user message; outlet extracts new facts in the background. One PostgreSQL table, no LangGraph.
required_open_webui_version: >= 0.11.0
requirements: psycopg[binary]
"""

from __future__ import annotations

import json
import logging
import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional
from urllib.parse import quote_plus

from pydantic import BaseModel, Field
from fastapi import Request

import psycopg

# Open WebUI imports
from open_webui.models.users import Users
from open_webui.utils.chat import generate_chat_completion

logger = logging.getLogger("openwebui.filters.user_memory")
logger.setLevel(logging.INFO)

# A fact is a dict: {type, subject, value, sentiment, confidence, first_mentioned, last_updated}.
# Types the extraction prompt uses: identity, status, preference, ownership, relationship,
# goal, skill, event. Any type string is accepted and rendered. Facts are unique per user
# on lower-cased (type, subject); see _fact_key / upsert_facts.


# ============================================================================
# PII Detection & Scrubbing
# ============================================================================

# Compiled regex patterns for PII detection - ordered by specificity
PII_PATTERNS: List[Dict[str, Any]] = [
    # Social Security Numbers (XXX-XX-XXXX, XXXXXXXXX, XXX XX XXXX)
    {
        "name": "ssn",
        "label": "Social Security Number",
        "pattern": re.compile(r"\b(\d{3}[-\s]?\d{2}[-\s]?\d{4})\b"),
        "validator": lambda m: not m.group(1)
        .replace("-", "")
        .replace(" ", "")
        .startswith("000"),
    },
    # Credit Card Numbers (major brands, with or without separators)
    {
        "name": "credit_card",
        "label": "Credit Card Number",
        "pattern": re.compile(r"\b([3-6]\d{3}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{3,4})\b"),
    },
    # US Phone Numbers (various formats)
    {
        "name": "phone",
        "label": "Phone Number",
        "pattern": re.compile(
            r"(?<!\d)"  # not preceded by digit
            r"(?:\+?1[-.\s]?)?"  # optional country code
            r"(?:\(?\d{3}\)?[-.\s]?)"  # area code
            r"(?:\d{3}[-.\s]?\d{4})"  # subscriber number
            r"(?!\d)",  # not followed by digit
        ),
    },
    # Email Addresses
    {
        "name": "email",
        "label": "Email Address",
        "pattern": re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    },
    # US Street Addresses (number + street name + suffix)
    {
        "name": "street_address",
        "label": "Street Address",
        "pattern": re.compile(
            r"\b\d{1,6}\s+"  # house number
            r"(?:[NSEW]\.?\s+)?"  # optional direction
            r"[A-Z][a-zA-Z\s]{1,30}\s+"  # street name
            r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr"
            r"|Lane|Ln|Road|Rd|Court|Ct|Circle|Cir|Way|Place|Pl"
            r"|Terrace|Ter|Trail|Trl|Parkway|Pkwy|Highway|Hwy)"
            r"\.?"
            r"(?:\s*(?:#|Apt|Suite|Ste|Unit|Fl|Floor)\s*\w+)?",  # optional unit
            re.IGNORECASE,
        ),
    },
    # Passport Numbers (US format: 1 letter + 8 digits, or 9 digits)
    {
        "name": "passport",
        "label": "Passport Number",
        "pattern": re.compile(
            r"\b(?:passport\s*(?:#|number|num|no)?:?\s*)([A-Z]\d{8}|\d{9})\b",
            re.IGNORECASE,
        ),
    },
    # Driver's License (state-pattern agnostic: mentioned + alphanumeric ID)
    {
        "name": "drivers_license",
        "label": "Driver's License Number",
        "pattern": re.compile(
            r"(?:driver'?s?\s*license\s*(?:#|number|num|no)?:?\s*)([A-Z0-9]{5,15})",
            re.IGNORECASE,
        ),
    },
    # Bank Account / Routing Numbers (when labelled)
    {
        "name": "bank_account",
        "label": "Bank Account Number",
        "pattern": re.compile(
            r"(?:account|routing|acct)\s*(?:#|number|num|no)?:?\s*(\d{6,17})",
            re.IGNORECASE,
        ),
    },
    # Date of Birth (when labelled — we allow general dates in context)
    {
        "name": "dob",
        "label": "Date of Birth",
        "pattern": re.compile(
            r"(?:(?:date\s*of\s*birth|dob|born\s*on|birthday\s*is)\s*:?\s*)"
            r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
            re.IGNORECASE,
        ),
    },
    # IP Addresses (v4)
    {
        "name": "ip_address",
        "label": "IP Address",
        "pattern": re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"),
        "validator": lambda m: all(0 <= int(x) <= 255 for x in m.group(1).split(".")),
    },
]

# Fact-type allowlist: these types are safe to store (no PII scrub needed at type level)
# The PII scrub happens on the *values*, not types — this is just for docs/reference
SAFE_FACT_TYPES = {
    "preference",
    "ownership",
    "relationship",
    "goal",
    "skill",
    "event",
    "personality",
    "interest",
    "hobby",
    "trait",
    "opinion",
}

# Fact subjects that should ALWAYS be blocked even if the regex doesn't fire
PII_SUBJECT_BLOCKLIST = {
    "ssn",
    "social security",
    "social security number",
    "credit card",
    "credit card number",
    "card number",
    "bank account",
    "routing number",
    "account number",
    "passport",
    "passport number",
    "drivers license",
    "driver's license",
    "license number",
    "ip address",
}




def content_to_text(content: Any) -> str:
    """
    Flatten an Open WebUI message `content` field to plain text.

    When a user attaches an image, `content` is a list of parts
    ({"type": "text", ...}, {"type": "image_url", ...}) instead of a string.
    Only the text parts are kept; image data is dropped so it never reaches
    the PII regexes or the extraction/relevance models. No length cap is
    applied: a raw paste larger than the helper model's context makes that
    helper call fail, and the filter then skips the memory update for the
    turn (keep "Paste Large Text as File" enabled in Open WebUI).
    """
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        text = "\n".join(p for p in parts if p)
    elif isinstance(content, dict) and content.get("type") == "text":
        text = str(content.get("text", ""))
    else:
        text = str(content)
    return text


def _fact_key(fact: Dict[str, Any]) -> tuple:
    """Identity of a fact for upsert purposes: normalized type + subject."""
    return (
        str(fact.get("type", "other")).strip().lower(),
        str(fact.get("subject", "")).strip().lower(),
    )


def _fact_marked_removed(fact: Dict[str, Any]) -> bool:
    """True when the extraction model explicitly asks to drop this fact."""
    if fact.get("remove") is True or fact.get("removed") is True:
        return True
    status = str(fact.get("status", "")).strip().lower()
    return status in ("removed", "remove", "deleted", "superseded", "obsolete")


def upsert_facts(
    existing: List[Dict[str, Any]],
    incoming: List[Dict[str, Any]],
    logger_fn: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    """
    Merge `incoming` (the model's output) onto `existing` without losing facts.

    - Same type+subject: incoming values overwrite, first_mentioned is kept.
    - New type+subject: appended.
    - Existing facts the model omitted: kept untouched.
    - Incoming fact flagged removed (see _fact_marked_removed): dropped.
    """
    log = logger_fn or (lambda _m: None)
    merged: Dict[tuple, Dict[str, Any]] = {}
    order: List[tuple] = []
    for f in existing:
        k = _fact_key(f)
        if k not in merged:
            order.append(k)
        merged[k] = dict(f)

    kept_count = len(merged)
    updated = added = removed = 0
    for f in incoming:
        if not f.get("type") or not f.get("subject"):
            continue
        k = _fact_key(f)
        if _fact_marked_removed(f):
            if k in merged:
                del merged[k]
                order.remove(k)
                removed += 1
                log(f"removed fact {k}")
            continue
        clean = {kk: v for kk, v in f.items() if kk not in ("remove", "removed", "status")}
        if k in merged:
            prev = merged[k]
            clean.setdefault("first_mentioned", prev.get("first_mentioned"))
            if clean.get("first_mentioned") is None:
                clean.pop("first_mentioned", None)
            # Keep the stored spelling of type/subject; only the content changes
            merged[k] = {**prev, **clean, "type": prev["type"], "subject": prev["subject"]}
            updated += 1
        else:
            merged[k] = clean
            order.append(k)
            added += 1
    omitted = kept_count - (updated + removed)
    log(
        f"upsert: {updated} updated, {added} added, {removed} removed, "
        f"{omitted} existing kept (not returned by model)"
    )
    return [merged[k] for k in order]


def detect_pii(text: str, patterns: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """
    Detect PII in text. Returns list of matches with type and matched value.

    Args:
        text: Text to scan
        patterns: Optional list of pattern names to check (None = all)

    Returns:
        List of {"type": "ssn", "label": "Social Security Number", "match": "123-45-6789"}
    """
    if not text:
        return []

    findings = []
    for pii in PII_PATTERNS:
        if patterns and pii["name"] not in patterns:
            continue
        for match in pii["pattern"].finditer(text):
            # Run optional validator (e.g., SSN can't start with 000)
            validator = pii.get("validator")
            if validator and not validator(match):
                continue
            findings.append(
                {
                    "type": pii["name"],
                    "label": pii["label"],
                    "match": match.group(0),
                }
            )
    return findings


def scrub_pii(
    text: str, replacement: str = "[REDACTED]", patterns: Optional[List[str]] = None
) -> str:
    """
    Replace all PII matches in text with a redaction placeholder.

    Args:
        text: Text to scrub
        replacement: What to replace PII with (default: [REDACTED])
        patterns: Optional list of pattern names to scrub (None = all)

    Returns:
        Scrubbed text
    """
    if not text:
        return text

    for pii in PII_PATTERNS:
        if patterns and pii["name"] not in patterns:
            continue

        def _replace(match):
            validator = pii.get("validator")
            if validator and not validator(match):
                return match.group(0)  # keep non-matching
            return replacement

        text = pii["pattern"].sub(_replace, text)

    return text


def validate_fact_no_pii(fact: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate a single extracted fact for PII. Returns a dict with:
      - "clean": bool — True if the fact is safe to store
      - "blocked_reasons": list of reasons if not clean
      - "scrubbed_fact": the fact with PII redacted (if partially salvageable)
    """
    reasons = []

    # Check subject against blocklist
    subject_lower = fact.get("subject", "").lower().strip()
    if subject_lower in PII_SUBJECT_BLOCKLIST:
        reasons.append(f"Blocked subject: '{fact.get('subject')}'")

    # Check value for PII patterns
    value = fact.get("value", "")
    pii_found = detect_pii(value)
    if pii_found:
        for p in pii_found:
            reasons.append(f"{p['label']} detected in value: '{p['match']}'")

    # Check subject field for PII patterns too
    subject = fact.get("subject", "")
    subject_pii = detect_pii(subject)
    if subject_pii:
        for p in subject_pii:
            reasons.append(f"{p['label']} detected in subject: '{p['match']}'")

    if reasons:
        return {
            "clean": False,
            "blocked_reasons": reasons,
            "scrubbed_fact": {
                **fact,
                "value": scrub_pii(value),
                "subject": scrub_pii(subject),
            },
        }

    return {"clean": True, "blocked_reasons": [], "scrubbed_fact": fact}


def filter_facts_pii(
    facts: List[Dict[str, Any]],
    mode: str = "remove",
    logger_fn: Optional[Callable] = None,
) -> List[Dict[str, Any]]:
    """
    Filter a list of extracted facts, removing or scrubbing PII.

    Args:
        facts: List of fact dicts from extraction
        mode: "remove" = drop facts with PII entirely
              "redact" = keep fact but replace PII with [REDACTED]
        logger_fn: Optional logging function

    Returns:
        Cleaned list of facts
    """
    cleaned = []
    for fact in facts:
        result = validate_fact_no_pii(fact)
        if result["clean"]:
            cleaned.append(fact)
        else:
            if logger_fn:
                logger_fn(
                    f"PII blocked in fact ({fact.get('type')}/{fact.get('subject')}): "
                    f"{', '.join(result['blocked_reasons'])}"
                )
            if mode == "redact":
                cleaned.append(result["scrubbed_fact"])
            # else "remove" → skip entirely
    return cleaned


# ============================================================================
# Filter Implementation
# ============================================================================


class Filter:
    class Valves(BaseModel):
        """Configuration for the User Memory filter"""

        # Execution Priority
        priority: int = Field(
            default=10,
            description="Filter execution priority. Lower numbers run first. Safety filters (antivirus, content policy, prompt injection, llamaguard) should run BEFORE memory (e.g., -100 to -10), so set this to positive (10-50) to run after safety checks pass.",
        )

        # PostgreSQL Configuration
        postgres_host: str = Field(
            default="langgraph-postgres",
            description="PostgreSQL host (docker service name or IP)",
        )
        postgres_port: int = Field(default=5432, description="PostgreSQL port")
        postgres_database: str = Field(
            default="langgraph_memory", description="PostgreSQL database name"
        )
        postgres_user: str = Field(
            default="langgraph", description="PostgreSQL username"
        )
        postgres_password: str = Field(
            default="langgraph_password_change_me",
            description="PostgreSQL password (CHANGE IN PRODUCTION!)",
        )

        # LLM Configuration for Extraction
        extraction_model_id: str = Field(
            default="memory-manager",
            description="Open WebUI model id for fact extraction. Create a workspace model with prompt/MERGE_PROMPT_memory-manager.md as its system prompt.",
        )

        # UI Configuration
        show_status: bool = Field(
            default=True, description="Show memory processing status messages"
        )
        # PII Protection
        pii_filter_enabled: bool = Field(
            default=True,
            description="Enable PII detection and filtering. When enabled, phone numbers, SSNs, addresses, credit cards, and other PII are blocked from memory storage.",
        )
        pii_filter_mode: Literal["remove", "redact"] = Field(
            default="remove",
            description="How to handle facts containing PII. 'remove' = drop the entire fact, 'redact' = store the fact with PII replaced by [REDACTED]",
        )
        pii_scrub_input: bool = Field(
            default=True,
            description="Scrub PII from messages BEFORE sending to extraction model. Prevents PII from even reaching the LLM.",
        )
        pii_patterns_enabled: List[str] = Field(
            default=[
                "ssn",
                "credit_card",
                "phone",
                "email",
                "street_address",
                "passport",
                "drivers_license",
                "bank_account",
                "dob",
                "ip_address",
            ],
            description="Which PII pattern types to detect. Remove items to allow them through. Options: ssn, credit_card, phone, email, street_address, passport, drivers_license, bank_account, dob, ip_address",
        )

        # Relevance Filtering
        relevance_filter_enabled: bool = Field(
            default=True,
            description="When enabled, only memories relevant to the current conversation are injected instead of all stored facts. Requires a separate relevance model.",
        )
        relevance_model_id: str = Field(
            default="memory-relevance",
            description="Open WebUI model id for relevance selection. Create a workspace model with prompt/RELEVANCE_PROMPT.md as its system prompt.",
        )
        always_inject_types: List[str] = Field(
            default=["identity"],
            description="Fact types that ALWAYS get injected regardless of relevance filtering. Identity facts (name, job, location) are usually always useful.",
        )

        # Debug
        debug_mode: bool = Field(
            default=False, description="Enable detailed debug logging"
        )

    class UserValves(BaseModel):
        """Per-user configuration"""

        enabled: bool = Field(
            default=True, description="Enable memory for this user"
        )
        show_status: bool = Field(
            default=True, description="Show status messages for this user"
        )

    def __init__(self):
        self.name = "User Memory"
        self.valves = self.Valves()
        self._conninfo: Optional[str] = None
        self._init_lock = asyncio.Lock()
        # One lock per user: the outlet's background extraction and the next
        # inlet's facts read for the same user never overlap.
        self._user_locks: Dict[str, asyncio.Lock] = {}
        # Keep references to background extraction tasks so they are not
        # garbage-collected before they finish.
        self._bg_tasks: set = set()

    def _log(self, message: str, level: str = "info"):
        """Centralized logging"""
        if level == "debug" and not self.valves.debug_mode:
            return
        # Print to stdout for Docker logs visibility
        print(f"[User Memory] [{level.upper()}] {message}", flush=True)
        getattr(logger, level, logger.info)(f"[User Memory] {message}")

    async def _resolve_user(self, __user__: Optional[Dict[str, Any]]):
        """
        Resolve the __user__ dict into a real UserModel.

        generate_chat_completion() and its downstream cache key builders use
        attribute access (user.id, user.role) — e.g. openai.get_all_models is
        decorated with key_builder=lambda ...: f"openai_all_models_{user.id}".
        Passing the raw __user__ dict raises:
            AttributeError: 'dict' object has no attribute 'id'
        """
        if not __user__:
            return None
        if not isinstance(__user__, dict):
            return __user__

        user_id = __user__.get("id")
        if not user_id:
            return None

        try:
            result = Users.get_user_by_id(user_id)
            # Users.get_user_by_id is async in Open WebUI >= 0.11, sync before that
            if asyncio.iscoroutine(result):
                result = await result
            if result is None:
                self._log(f"UserModel not found for {user_id[:8]}...", "error")
            return result
        except Exception as e:
            self._log(
                f"Failed to resolve UserModel for {user_id[:8]}...: {type(e).__name__}: {e}",
                "error",
            )
            return None

    # ------------------------------------------------------------------
    # Storage: one row per user in table user_facts (facts JSONB)
    # ------------------------------------------------------------------
    async def _connect(self) -> psycopg.AsyncConnection:
        """One plain connection per query. No pool: psycopg ships with Open WebUI,
        psycopg-pool does not, and frontmatter requirements are not pip-installed
        on a default Open WebUI install."""
        return await psycopg.AsyncConnection.connect(
            self._conninfo, autocommit=True, connect_timeout=15
        )

    async def _init_store(self):
        """Check the connection and create the table on first use."""
        if self._conninfo is not None:
            return
        async with self._init_lock:
            if self._conninfo is not None:
                return
            conninfo = (
                f"postgresql://{self.valves.postgres_user}:"
                f"{quote_plus(self.valves.postgres_password)}@"
                f"{self.valves.postgres_host}:{self.valves.postgres_port}/"
                f"{self.valves.postgres_database}"
            )
            self._log(
                f"Connecting to PostgreSQL at {self.valves.postgres_host}:{self.valves.postgres_port}"
            )
            try:
                async with await psycopg.AsyncConnection.connect(
                    conninfo, autocommit=True, connect_timeout=15
                ) as conn:
                    await conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS user_facts (
                            user_id    TEXT PRIMARY KEY,
                            facts      JSONB NOT NULL DEFAULT '[]'::jsonb,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                        )
                        """
                    )
                self._conninfo = conninfo
                self._log("User memory store ready (table user_facts)", "info")
            except Exception as e:
                self._log(f"Failed to open the memory store: {e}", "error")
                raise ConnectionError(
                    f"Cannot connect to PostgreSQL at {self.valves.postgres_host}:{self.valves.postgres_port}: {e}"
                )

    async def _get_facts(self, user_id: str) -> List[Dict[str, Any]]:
        """All stored facts for the user (empty list when none)."""
        await self._init_store()
        async with await self._connect() as conn:
            cur = await conn.execute(
                "SELECT facts FROM user_facts WHERE user_id = %s", (user_id,)
            )
            row = await cur.fetchone()
        facts = row[0] if row else []
        if not isinstance(facts, list):
            self._log(f"Stored facts for {user_id[:8]}... are not a list - treating as empty", "warning")
            return []
        return facts

    async def _save_facts(self, user_id: str, facts: List[Dict[str, Any]]) -> int:
        """Replace the user's fact list. Stamps first_mentioned/last_updated/confidence."""
        await self._init_store()
        now = datetime.now(timezone.utc).isoformat()
        clean: List[Dict[str, Any]] = []
        for fact in facts:
            if not fact.get("type") or not fact.get("subject"):
                self._log(f"Skipping fact without type/subject: {fact}", "warning")
                continue
            fact = dict(fact)
            fact.setdefault("first_mentioned", now)
            fact["last_updated"] = now
            fact.setdefault("confidence", 0.8)
            clean.append(fact)
        async with await self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO user_facts (user_id, facts, updated_at)
                VALUES (%s, %s::jsonb, now())
                ON CONFLICT (user_id) DO UPDATE
                    SET facts = EXCLUDED.facts, updated_at = now()
                """,
                (user_id, json.dumps(clean)),
            )
        self._log(f"Stored {len(clean)} facts for user {user_id[:8]}...", "info")
        return len(clean)

    def _user_lock(self, user_id: str) -> asyncio.Lock:
        lock = self._user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_locks[user_id] = lock
        return lock

    def _clean_model_response(self, text: str) -> str:
        """Strip <think> tags and other noise from model response"""
        if not text:
            return ""

        # Remove <think> blocks (thinking models like DeepSeek-R1, QwQ, etc)
        # re.DOTALL matches newlines, re.IGNORECASE for case insensitivity
        cleaned = re.sub(
            r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE
        )

        return cleaned.strip()

    async def _call_extraction_model(
        self,
        prompt: str,
        user: Optional[Dict[str, Any]] = None,
        request: Optional[Any] = None,
    ) -> Optional[str]:
        """
        Call LLM to extract structured information using OpenWebUI's internal library.

        The extraction_model_id can be:
        1. A base model: "llama3.2:latest", "gpt-4o-mini", etc.
        2. A custom model with system prompt: Create in Admin Panel → Models with a system
           prompt optimized for memory extraction (see EXTRACTION_SYSTEM_PROMPT below)

        This allows flexible control over extraction quality without code changes.
        """
        try:
            self._log(f"=== CALLING EXTRACTION MODEL ===", "info")
            self._log(f"Model ID: {self.valves.extraction_model_id}", "info")
            self._log(f"User provided: {user is not None}", "info")
            self._log(f"Request provided: {request is not None}", "info")

            if user is None:
                self._log(
                    "WARNING: user is None - generate_chat_completion may fail!",
                    "error",
                )
            if request is None:
                self._log(
                    "WARNING: request is None - generate_chat_completion may fail!",
                    "error",
                )

            # Build payload for generate_chat_completion
            payload = {
                "model": self.valves.extraction_model_id,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }

            self._log(
                f"Extraction payload: model={payload['model']}, prompt_len={len(prompt)}",
                "info",
            )

            # Call OpenWebUI's internal API (bypasses filters to avoid recursion)
            self._log("Calling generate_chat_completion...", "info")
            response = await generate_chat_completion(
                request=request,
                form_data=payload,
                user=user,
                bypass_filter=True,
            )
            self._log(
                f"generate_chat_completion returned type: {type(response)}", "info"
            )

            # Parse response
            if isinstance(response, dict):
                self._log(f"Response keys: {list(response.keys())}", "info")
                choices = response.get("choices", [])
                if not choices:
                    self._log(
                        f"No choices in extraction model response. Full response: {response}",
                        "error",
                    )
                    return None

                message = choices[0].get("message", {})
                response_text = message.get("content", "")

                if not response_text:
                    self._log("Extraction model returned empty content", "warning")
                    return None

                self._log(
                    f"Extraction model response length: {len(response_text)} chars",
                    "debug",
                )
                return self._clean_model_response(response_text)
            else:
                self._log(
                    f"Unexpected response type from extraction model: {type(response)}. Response: {response}",
                    "error",
                )
                return None

        except Exception as e:
            self._log(f"Extraction model call failed: {type(e).__name__}: {e}", "error")
            import traceback

            self._log(f"Extraction traceback: {traceback.format_exc()}", "debug")
            return None

    async def _update_user_memory_state(
        self,
        user_id: str,
        conversation_id: str,
        earlier_messages: List[Dict[str, str]],
        new_message: Dict[str, str],
        user: Optional[Dict[str, Any]] = None,
        request: Optional[Any] = None,
    ):
        """
        Ask the extraction model which facts change because of `new_message`,
        upsert that onto the stored facts, and persist when something changed.
        Runs in the background after the reply (see outlet).
        """
        try:
            self._log(f"Starting memory update for user {user_id[:8]}...", "info")
            existing_facts = await self._get_facts(user_id)

            def _for_prompt(f: Dict[str, Any]) -> Dict[str, Any]:
                out = {"type": f.get("type"), "subject": f.get("subject"), "value": f.get("value")}
                if f.get("sentiment"):
                    out["sentiment"] = f["sentiment"]
                return out

            def _scrub(msg: Dict[str, str]) -> Dict[str, str]:
                text = content_to_text(msg.get("content", ""))
                if self.valves.pii_filter_enabled and self.valves.pii_scrub_input:
                    scrubbed = scrub_pii(
                        text,
                        replacement="[REDACTED]",
                        patterns=self.valves.pii_patterns_enabled,
                    )
                    if scrubbed != text:
                        self._log("PII pre-scrub: redacted PII in message", "info")
                    text = scrubbed
                return {"role": msg.get("role", "user"), "content": text}

            # Local date of the server (honours the container's TZ), so "this year"
            # and "last month" resolve the way the user means them.
            merge_prompt = f"""TODAY: {datetime.now().astimezone().strftime("%Y-%m-%d")}

EXISTING FACTS:
{json.dumps([_for_prompt(f) for f in existing_facts], indent=2)}

EARLIER MESSAGES (context only, already processed):
{json.dumps([_scrub(m) for m in earlier_messages], indent=2)}

NEW MESSAGE:
{json.dumps(_scrub(new_message), indent=2)}"""

            merged_json = await self._call_extraction_model(
                merge_prompt, user=user, request=request
            )
            self._log(
                f"Merge model returned: {merged_json[:500] if merged_json else 'NONE/EMPTY'}",
                "info",
            )

            merged_data = None
            if merged_json:
                cleaned_json = merged_json.strip()
                if cleaned_json.startswith("```"):
                    lines = cleaned_json.split("\n")
                    cleaned_json = "\n".join(lines[1:-1] if len(lines) > 2 else lines)
                if cleaned_json.startswith("json"):
                    cleaned_json = cleaned_json[4:].strip()
                try:
                    merged_data = json.loads(cleaned_json)
                except json.JSONDecodeError as e:
                    self._log(f"Failed to parse merge JSON: {e}", "error")
            else:
                self._log("Merge model returned empty", "warning")

            if not isinstance(merged_data, dict):
                # Nothing usable came back: keep the stored facts, write nothing.
                self._log("No merge result - skipping store", "info")
                return

            changes = merged_data.get("facts", []) or []
            if not isinstance(changes, list):
                self._log("Merge result 'facts' is not a list - skipping store", "warning")
                return
            self._log(f"Merge model returned {len(changes)} change(s)", "info")

            if self.valves.pii_filter_enabled and changes:
                pre_count = len(changes)
                changes = filter_facts_pii(
                    changes,
                    mode=self.valves.pii_filter_mode,
                    logger_fn=lambda msg: self._log(f"PII filter: {msg}", "warning"),
                )
                if pre_count - len(changes) > 0:
                    self._log(
                        f"PII post-validation: blocked {pre_count - len(changes)} fact(s) containing PII",
                        "warning",
                    )

            if not changes:
                self._log("No changes - skipping store", "info")
                return

            new_facts = upsert_facts(
                existing_facts,
                changes,
                logger_fn=lambda msg: self._log(f"Merge {msg}", "info"),
            )

            def _sig(facts: List[Dict[str, Any]]):
                return sorted(
                    (str(f.get("type", "")), str(f.get("subject", "")), str(f.get("value", "")))
                    for f in facts
                )

            if _sig(existing_facts) == _sig(new_facts):
                self._log(
                    f"No changes to facts ({len(existing_facts)} facts unchanged) - skipping store",
                    "info",
                )
                return

            self._log(
                f"Facts changed: {len(existing_facts)} -> {len(new_facts)} - storing",
                "info",
            )
            stored = await asyncio.wait_for(self._save_facts(user_id, new_facts), timeout=30.0)
            self._log(f"Memory updated for user {user_id[:8]}: {stored} facts stored", "info")

        except asyncio.TimeoutError:
            self._log("Memory update timed out after 30 seconds", "warning")
        except Exception as e:
            self._log(
                f"Failed to update memory state: {type(e).__name__}: {e}", "error"
            )
            import traceback

            self._log(f"State update traceback: {traceback.format_exc()}", "error")

    async def _select_relevant_memories(
        self,
        facts: List[Dict[str, Any]],
        messages: List[Dict[str, str]],
        user: Optional[Dict[str, Any]] = None,
        request: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """
        Ask the relevance model which stored facts matter for the current
        conversation. It answers with (type, subject) keys only; the stored
        fact objects are injected unchanged. Facts whose type is in
        always_inject_types bypass the model. Any failure falls back to all facts.
        """
        if not facts:
            return []

        always_facts = [
            f for f in facts if f.get("type", "other") in self.valves.always_inject_types
        ]
        filterable_facts = [
            f for f in facts if f.get("type", "other") not in self.valves.always_inject_types
        ]
        if not filterable_facts:
            return always_facts

        window = [m for m in messages[-6:] if m.get("role") in ("user", "assistant")]
        recent_conversation = [
            {"role": m.get("role"), "content": content_to_text(m.get("content"))}
            for m in window
        ]
        candidates = [
            {"type": f.get("type"), "subject": f.get("subject"), "value": f.get("value")}
            for f in filterable_facts
        ]
        relevance_prompt = f"""STORED FACTS:
{json.dumps(candidates, indent=2)}

CURRENT CONVERSATION:
{json.dumps(recent_conversation, indent=2)}"""

        try:
            payload = {
                "model": self.valves.relevance_model_id,
                "messages": [{"role": "user", "content": relevance_prompt}],
                "stream": False,
            }
            response = await generate_chat_completion(
                request=request,
                form_data=payload,
                user=user,
                bypass_filter=True,
            )
            response_text = None
            if isinstance(response, dict):
                choices = response.get("choices", [])
                if choices:
                    response_text = choices[0].get("message", {}).get("content", "")
            if not response_text:
                self._log("Relevance model returned empty - falling back to all facts", "warning")
                return facts

            cleaned = self._clean_model_response(response_text)
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(lines[1:-1] if len(lines) > 2 else lines)
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            parsed = json.loads(cleaned)
            picked = parsed.get("relevant", parsed.get("relevant_facts", []))
            keys = {
                _fact_key(k) for k in picked if isinstance(k, dict) and k.get("subject")
            }
            relevant = [f for f in filterable_facts if _fact_key(f) in keys]
            self._log(
                f"Relevance filter: {len(filterable_facts)} filterable -> {len(relevant)} relevant "
                f"(+ {len(always_facts)} always-inject)",
                "info",
            )
            return always_facts + relevant

        except json.JSONDecodeError as e:
            self._log(f"Relevance model returned invalid JSON: {e} - falling back to all facts", "warning")
            return facts
        except Exception as e:
            self._log(f"Relevance filtering failed: {type(e).__name__}: {e} - falling back to all facts", "warning")
            return facts

    def _format_memory_context(self, facts: List[Dict[str, Any]]) -> str:
        """Render the selected facts grouped by type. Every type is rendered; no caps."""
        if not facts:
            return ""
        by_type: Dict[str, List[Dict[str, Any]]] = {}
        for f in facts:
            by_type.setdefault(str(f.get("type", "other")), []).append(f)
        headers = {
            "identity": "About You",
            "status": "Current Status",
            "ownership": "You Own",
            "relationship": "Relationships",
            "preference": "Preferences",
            "skill": "Skills/Interests",
            "goal": "Goals",
            "event": "Events/Dates",
        }
        order = list(headers) + sorted(t for t in by_type if t not in headers)
        parts = ["=== USER MEMORY PROFILE ==="]
        for t in order:
            if t not in by_type:
                continue
            parts.append("")
            parts.append(f"{headers.get(t, t.replace('_', ' ').title())}:")
            for f in by_type[t]:
                subject = str(f.get("subject", "")).replace("_", " ").title()
                value = f.get("value", "")
                if t == "preference" and f.get("sentiment") == "positive":
                    parts.append(f"  - Likes: {value}")
                elif t == "preference" and f.get("sentiment") == "negative":
                    parts.append(f"  - Dislikes: {value}")
                else:
                    parts.append(f"  - {subject}: {value}")
        parts.append("")
        parts.append("=== END MEMORY PROFILE ===")
        return "\n".join(parts)

    async def inlet(
        self,
        body: Dict[str, Any],
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __request__: Optional[Request] = None,
    ) -> Dict[str, Any]:
        """
        Inlet: read the user's facts, pick the relevant ones, append them to the
        latest user message. No extraction here; that runs after the reply.
        """
        self._log("=== INLET START ===", "info")
        if not __user__ or not __user__.get("id"):
            self._log("No user ID, skipping memory processing", "info")
            return body

        user_id = __user__["id"]
        conversation_id = body.get("chat_id", "default")
        self._log(f"Processing user={user_id[:8]}... chat={str(conversation_id)[:8]}...", "info")
        user_obj = await self._resolve_user(__user__)

        async def status(text: str, done: bool):
            if self.valves.show_status and __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": text, "done": done}}
                )

        try:
            await status("🔍 Loading your memories...", False)
            # Wait for a still-running extraction from the previous turn so the
            # newest facts are included.
            async with self._user_lock(user_id):
                all_facts = await self._get_facts(user_id)
            total_stored = len(all_facts)
            self._log(f"Loaded {total_stored} facts", "info")

            injected_facts = all_facts
            messages = body.get("messages", [])
            if self.valves.relevance_filter_enabled and all_facts and messages:
                await status("🔎 Filtering relevant memories...", False)
                injected_facts = await self._select_relevant_memories(
                    all_facts, messages, user=user_obj, request=__request__
                )
                self._log(f"Relevance filter: {total_stored} total -> {len(injected_facts)} relevant", "info")

            memory_context = self._format_memory_context(injected_facts)
            if memory_context and messages:
                # Append the block to the latest user message. The persona system
                # prompt, tool specs and history stay byte-identical turn to turn, so
                # the server reuses its prompt cache; works with every chat template;
                # survives Open WebUI's tool-call continuation requests, which are
                # not filtered again. (A separate system message does not work: Open
                # WebUI merges extra system messages into the first one, measured
                # 2026-10-16.)
                last_user = max(
                    (i for i, m in enumerate(messages) if m.get("role") == "user"),
                    default=None,
                )
                if last_user is not None:
                    user_msg = messages[last_user]
                    content = user_msg.get("content")
                    block = f"\n\n[Relevant memories about the user]\n{memory_context}"
                    if isinstance(content, str):
                        user_msg["content"] = content + block
                    elif isinstance(content, list):
                        content.append({"type": "text", "text": block})
                body["messages"] = messages

            if injected_facts:
                if self.valves.relevance_filter_enabled and len(injected_facts) < total_stored:
                    await status(
                        f"💭 Recalled {len(injected_facts)} of {total_stored} memories relevant to this chat",
                        True,
                    )
                else:
                    await status(f"💭 Recalled {len(injected_facts)} memories about you", True)
            else:
                await status("👋 Getting to know you...", True)

            self._log("=== INLET COMPLETE ===", "info")
        except ConnectionError as e:
            self._log(f"PostgreSQL connection error: {e}", "error")
            await status("⚠️ Cannot connect to memory database. Check PostgreSQL.", True)
        except ImportError as e:
            self._log(f"Missing dependencies: {e}", "error")
            await status("⚠️ Missing dependencies. Install: psycopg[binary]", True)
        except Exception as e:
            self._log(f"Inlet processing error: {type(e).__name__}: {e}", "error")
            import traceback

            self._log(f"Inlet traceback: {traceback.format_exc()}", "error")
            await status(f"⚠️ Memory system error: {str(e)[:60]}", True)

        return body

    async def outlet(
        self,
        body: Dict[str, Any],
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __request__: Optional[Request] = None,
    ) -> Dict[str, Any]:
        """
        Outlet: start fact extraction for the latest user message in the
        background and return immediately, so the reply is never delayed.
        """
        if not __user__ or not __user__.get("id"):
            return body
        user_id = __user__["id"]
        conversation_id = body.get("chat_id", "default")

        # Only USER messages are sent: system prompts carry persona text that the
        # extraction model would misread as facts about the user.
        user_messages = [m for m in body.get("messages", []) if m.get("role") == "user"]
        if not user_messages:
            return body
        new_message = user_messages[-1]
        earlier_messages = user_messages[-4:-1]

        user_obj = await self._resolve_user(__user__)

        async def run():
            async with self._user_lock(user_id):
                await self._update_user_memory_state(
                    user_id,
                    conversation_id,
                    earlier_messages,
                    new_message,
                    user=user_obj,
                    request=__request__,
                )

        task = asyncio.create_task(run())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        self._log("Outlet: background memory update scheduled", "info")
        return body
