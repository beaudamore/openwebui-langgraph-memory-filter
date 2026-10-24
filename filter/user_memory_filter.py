"""
title: User Memory
author: beaudamore
date: 2026-10-24
version: 7.1
license: MIT
description: Per-user fact memory on PostgreSQL + pgvector. Inlet embeds the recent chat and injects the nearest facts into the latest user message (no relevance model); outlet extracts fact changes in the background with the memory-manager model and de-duplicates by vector similarity. Embeddings come from Open WebUI's own configured embedding model and are rebuilt automatically when that model changes. PII: none, built-in regexes, or a local PII-Tracer server (valve pii_engine).
required_open_webui_version: >= 0.11.0
requirements: psycopg[binary], aiohttp
"""

from __future__ import annotations

import json
import logging
import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional
from urllib.parse import quote_plus, urlparse

from pydantic import BaseModel, Field
from fastapi import Request

import aiohttp
import psycopg

# Open WebUI imports
from open_webui.models.users import Users
from open_webui.utils.chat import generate_chat_completion

try:  # the prefixes Open WebUI itself sends to the embedding model (None unless set by env)
    from open_webui.config import RAG_EMBEDDING_QUERY_PREFIX, RAG_EMBEDDING_CONTENT_PREFIX
except Exception:  # pragma: no cover
    RAG_EMBEDDING_QUERY_PREFIX = RAG_EMBEDDING_CONTENT_PREFIX = None

logger = logging.getLogger("openwebui.filters.user_memory")
logger.setLevel(logging.INFO)

# A fact is a dict: {type, subject, value, sentiment, confidence, first_mentioned, last_updated},
# one row per fact in table user_facts, plus an `embedding` vector of "subject: value". Types the extraction prompt uses: identity, status,
# preference, ownership, relationship, goal, skill, event. Any type string is accepted and
# rendered. Facts are unique per user on lower-cased (type, subject), which is how they are
# stored; see _fact_key.


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

# PII-Tracer engine (pii_engine = "pii_tracer"): the nine labels the model emits.
PII_TRACER_LABELS = [
    "private_person",
    "private_email",
    "private_phone",
    "private_address",
    "private_url",
    "private_date",
    "account_number",
    "secret",
    "other_pii",
]
# Default labels for the memory path: names and dates are left out so identity facts
# ("name = Beau", a birthday) stay storable, as with the regex engine, which never matched names.
PII_TRACER_MEMORY_LABELS = [l for l in PII_TRACER_LABELS if l not in ("private_person", "private_date")]




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

        # PostgreSQL address. Independent of how the database is deployed.
        postgres_url: str = Field(
            default="",
            description="URL of the PostgreSQL server: scheme://host or scheme://host:port (port defaults to 5432). The scheme is http, https or postgresql. Examples: http://langgraph-pgvector, http://10.5.10.100:5432, https://db.example.org:5435. Required. The server needs the pgvector extension (image pgvector/pgvector).",
        )
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
        pii_engine: Literal["none", "regex", "pii_tracer"] = Field(
            default="regex",
            description="What finds PII before a fact is stored. 'none': nothing is checked or scrubbed, facts are stored exactly as extracted. 'regex': the built-in patterns (pii_patterns_enabled). 'pii_tracer': Perplexity's PII-Tracer model on a local server (pii_tracer_url, pii_tracer_labels); it reads context, so it also catches values the patterns cannot describe.",
        )
        pii_filter_mode: Literal["remove", "redact"] = Field(
            default="remove",
            description="How to handle facts containing PII. 'remove' = drop the entire fact, 'redact' = store the fact with PII replaced by [REDACTED]",
        )
        pii_scrub_input: bool = Field(
            default=True,
            description="Scrub PII from messages BEFORE sending to extraction model. Prevents PII from even reaching the LLM. Ignored when pii_engine is 'none'.",
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
            description="Which PII pattern types to detect (regex engine, and the regex fallback of the pii_tracer engine). Remove items to allow them through. Options: ssn, credit_card, phone, email, street_address, passport, drivers_license, bank_account, dob, ip_address",
        )
        pii_tracer_url: str = Field(
            default="",
            description="PII-Tracer server base URL, e.g. http://pii-tracer:8193 or http://10.5.10.100:8193. Required when pii_engine is 'pii_tracer'; unused otherwise.",
        )
        pii_tracer_timeout: float = Field(
            default=15.0, description="Per-request timeout to the PII-Tracer server, seconds."
        )
        pii_tracer_labels: List[str] = Field(
            default=list(PII_TRACER_MEMORY_LABELS),
            description="PII-Tracer labels treated as PII on the memory path. Default leaves out private_person and private_date so names and birthdays can still be stored as identity facts. Options: " + ", ".join(PII_TRACER_LABELS),
        )
        pii_tracer_fallback: Literal["regex", "allow"] = Field(
            default="regex",
            description="When the PII-Tracer server is down, unset or times out: 'regex' = use the built-in patterns for this update, 'allow' = treat the text as clean.",
        )

        # Retrieval (vector search; no relevance model)
        query_window_messages: int = Field(
            default=6,
            description="How many of the most recent chat messages (user and assistant turns) are embedded as the search text for memory retrieval. 1 = only the latest user message. Larger windows keep the topic across turns; smaller ones react faster to a topic change.",
        )
        relevance_threshold: float = Field(
            default=0.45,
            description="Minimum cosine similarity (0 to 1) between the search text and a stored fact ('subject: value') for the fact to be a candidate. 0 disables the cutoff so the reranker/max_memories alone decide. The scale depends on the embedding model: measured with bge-m3 on short facts, unrelated facts score 0.40-0.50 and on-topic ones 0.50-0.65, so cosine alone separates weakly and a reranker (Open WebUI Documents > Reranking) does the real ordering. Lower it if memories are missed, raise it if unrelated facts show up. Facts in always_inject_types ignore it.",
        )
        max_memories: int = Field(
            default=20,
            description="Maximum number of facts injected per turn, not counting always_inject_types: the top N after the similarity cutoff, ordered by Open WebUI's reranker when one is configured (Admin Panel > Documents > Reranking), by vector similarity otherwise. Also the number of nearest existing facts the extraction model sees as context. 0 = no cap.",
        )
        always_inject_types: List[str] = Field(
            default=["identity"],
            description="Fact types that are ALWAYS injected regardless of similarity. Identity facts (name, job, location) are usually always useful.",
        )
        dedupe_threshold: float = Field(
            default=0.9,
            description="Cosine similarity (0 to 1) above which a newly extracted fact is treated as the same fact as an existing one of the same type with a different subject wording (e.g. 'job' vs 'occupation'): the existing row is updated instead of a second row being added. 1 disables the merge. Keep it high; 0.85-0.95 is the useful range.",
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
        # Set at the start of each memory update; the PII-Tracer failure is logged once per update.
        self._pii_down_logged = False

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
    # Embeddings: Open WebUI's own configured embedding model
    # ------------------------------------------------------------------
    @staticmethod
    def _fact_text(fact: Dict[str, Any]) -> str:
        """The text that is embedded for a fact. Type is not included: it is a
        category word and is already matched in SQL."""
        return f"{str(fact.get('subject', '')).strip()}: {str(fact.get('value', '')).strip()}"

    async def _embedding_model_name(self, request: Any) -> str:
        """engine/model as Open WebUI has it configured, for change detection.
        Open WebUI >= 0.12 keeps it in the config table (keys rag.embedding_engine,
        rag.embedding_model); older versions on request.app.state.config."""
        engine = model = None
        try:
            from open_webui.models.config import Config  # 0.12+
            cfg = await Config.get_many("rag.embedding_engine", "rag.embedding_model")
            engine, model = cfg.get("rag.embedding_engine"), cfg.get("rag.embedding_model")
        except Exception as e:
            self._log(f"Config table not readable ({type(e).__name__}); using app.state.config", "debug")
        if not model:
            cfg = getattr(getattr(getattr(request, "app", None), "state", None), "config", None)
            engine = getattr(cfg, "RAG_EMBEDDING_ENGINE", engine)
            model = getattr(cfg, "RAG_EMBEDDING_MODEL", None)
        return f"{engine or 'local'}/{model or 'unknown'}"

    async def _embed(
        self, texts: List[str], request: Any, user: Any, prefix: Optional[str]
    ) -> List[List[float]]:
        """Embed texts with request.app.state.EMBEDDING_FUNCTION (list in, list of vectors out)."""
        fn = getattr(getattr(getattr(request, "app", None), "state", None), "EMBEDDING_FUNCTION", None)
        if fn is None:
            raise RuntimeError(
                "Open WebUI has no embedding function configured (Admin Panel > Settings > Documents > Embedding)."
            )
        result = fn(texts, prefix=prefix, user=user)
        if asyncio.iscoroutine(result):
            result = await result
        if not isinstance(result, list) or len(result) != len(texts):
            raise RuntimeError(f"Embedding function returned {type(result).__name__} for {len(texts)} texts")
        return [list(map(float, v)) for v in result]

    @staticmethod
    def _vec(v: List[float]) -> str:
        """pgvector input literal."""
        return "[" + ",".join(repr(x) for x in v) + "]"

    async def _rerank(
        self, query_text: str, facts: List[Dict[str, Any]], request: Any, user: Any
    ) -> Optional[List[float]]:
        """Scores from request.app.state.RERANKING_FUNCTION (Open WebUI's configured
        reranker), one per fact, or None when no reranker is configured."""
        fn = getattr(getattr(getattr(request, "app", None), "state", None), "RERANKING_FUNCTION", None)
        if fn is None or len(facts) < 2:
            return None

        class _Doc:  # the reranker reads .page_content
            __slots__ = ("page_content",)

            def __init__(self, text: str):
                self.page_content = text

        docs = [_Doc(self._fact_text(f)) for f in facts]
        result = await asyncio.to_thread(fn, query_text, docs, user=user)
        if asyncio.iscoroutine(result):
            result = await result
        if not isinstance(result, list) or len(result) != len(facts):
            raise RuntimeError(f"Reranker returned {type(result).__name__} for {len(facts)} facts")
        return [float(x) for x in result]

    # ------------------------------------------------------------------
    # Storage: one row per fact in table user_facts (+ embedding), meta in user_facts_meta
    # ------------------------------------------------------------------
    async def _connect(self) -> psycopg.AsyncConnection:
        """One plain connection per query. No pool: psycopg ships with Open WebUI,
        psycopg-pool does not, and frontmatter requirements are not pip-installed
        on a default Open WebUI install."""
        return await psycopg.AsyncConnection.connect(
            self._conninfo, autocommit=True, connect_timeout=15
        )

    async def _init_store(self, request: Any, user: Any):
        """First use: check the connection, create extension/tables, and make sure
        every row's embedding matches Open WebUI's current embedding model."""
        if self._conninfo is not None:
            return
        async with self._init_lock:
            if self._conninfo is not None:
                return
            url = urlparse(self.valves.postgres_url.strip())
            if url.scheme not in ("http", "https", "postgresql", "postgres") or not url.hostname:
                raise ConnectionError(
                    "Valve postgres_url must be scheme://host or scheme://host:port, "
                    "e.g. http://langgraph-pgvector or http://10.5.10.100:5432."
                )
            host, port = url.hostname, url.port or 5432
            conninfo = (
                f"postgresql://{self.valves.postgres_user}:"
                f"{quote_plus(self.valves.postgres_password)}@"
                f"{host}:{port}/"
                f"{self.valves.postgres_database}"
            )
            self._log(f"Connecting to PostgreSQL at {host}:{port}")
            try:
                async with await psycopg.AsyncConnection.connect(
                    conninfo, autocommit=True, connect_timeout=15
                ) as conn:
                    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    await conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS user_facts (
                            user_id         TEXT        NOT NULL,
                            type            TEXT        NOT NULL,
                            subject         TEXT        NOT NULL,
                            value           TEXT        NOT NULL,
                            sentiment       TEXT,
                            confidence      REAL        NOT NULL DEFAULT 0.8,
                            first_mentioned TIMESTAMPTZ NOT NULL DEFAULT now(),
                            last_updated    TIMESTAMPTZ NOT NULL DEFAULT now(),
                            PRIMARY KEY (user_id, type, subject)
                        )
                        """
                    )
                    await conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS user_facts_meta (
                            id              INT         PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                            embedding_model TEXT        NOT NULL,
                            embedding_dim   INT         NOT NULL,
                            updated         TIMESTAMPTZ NOT NULL DEFAULT now()
                        )
                        """
                    )
                    await self._sync_embeddings(conn, request, user)
                self._conninfo = conninfo
                self._log("User memory store ready (table user_facts, pgvector)", "info")
            except Exception as e:
                self._log(f"Failed to open the memory store: {e}", "error")
                raise ConnectionError(
                    f"Cannot open the memory store at {host}:{port}: {e}"
                )

    async def _sync_embeddings(self, conn: psycopg.AsyncConnection, request: Any, user: Any):
        """Rebuild the embedding column and every row's vector when Open WebUI's
        embedding model or its dimension differs from what the table was built with."""
        model = await self._embedding_model_name(request)
        dim = len((await self._embed(["dimension probe"], request, user, RAG_EMBEDDING_CONTENT_PREFIX))[0])

        cur = await conn.execute("SELECT embedding_model, embedding_dim FROM user_facts_meta WHERE id = 1")
        meta = await cur.fetchone()
        cur = await conn.execute(
            "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'user_facts'::regclass "
            "AND attname = 'embedding' AND NOT attisdropped"
        )
        col = await cur.fetchone()
        col_dim = col[0] if col else None

        if meta and meta[0] == model and meta[1] == dim and col_dim == dim:
            return
        self._log(
            f"Embedding change: stored={meta[0] if meta else None}/{meta[1] if meta else None} "
            f"column={col_dim} current={model}/{dim}. Rebuilding every fact embedding.",
            "warning",
        )
        async with conn.transaction():
            await conn.execute("DROP INDEX IF EXISTS user_facts_embedding_idx")
            await conn.execute("ALTER TABLE user_facts DROP COLUMN IF EXISTS embedding")
            await conn.execute(f"ALTER TABLE user_facts ADD COLUMN embedding vector({dim})")
            cur = await conn.execute("SELECT user_id, type, subject, value FROM user_facts ORDER BY user_id, type, subject")
            rows = await cur.fetchall()
            batch = 64
            for i in range(0, len(rows), batch):
                chunk = rows[i : i + batch]
                vectors = await self._embed(
                    [self._fact_text({"subject": r[2], "value": r[3]}) for r in chunk],
                    request, user, RAG_EMBEDDING_CONTENT_PREFIX,
                )
                for r, v in zip(chunk, vectors):
                    await conn.execute(
                        "UPDATE user_facts SET embedding = %s::vector WHERE user_id = %s AND type = %s AND subject = %s",
                        (self._vec(v), r[0], r[1], r[2]),
                    )
            if dim <= 2000:  # hnsw on `vector` is limited to 2000 dims
                await conn.execute(
                    "CREATE INDEX user_facts_embedding_idx ON user_facts USING hnsw (embedding vector_cosine_ops)"
                )
            await conn.execute(
                """
                INSERT INTO user_facts_meta (id, embedding_model, embedding_dim, updated)
                VALUES (1, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET embedding_model = EXCLUDED.embedding_model,
                    embedding_dim = EXCLUDED.embedding_dim, updated = now()
                """,
                (model, dim),
            )
        self._log(f"Re-embedded {len(rows)} fact(s) with {model} ({dim} dims)", "info")

    @staticmethod
    def _row_to_fact(row: tuple) -> Dict[str, Any]:
        type_, subject, value, sentiment, confidence, first_mentioned, last_updated = row[:7]
        fact = {
            "type": type_,
            "subject": subject,
            "value": value,
            "sentiment": sentiment,
            "confidence": confidence,
            "first_mentioned": first_mentioned.isoformat(),
            "last_updated": last_updated.isoformat(),
        }
        if len(row) > 7:
            fact["similarity"] = round(float(row[7]), 3)
        return fact

    _FACT_COLS = "type, subject, value, sentiment, confidence, first_mentioned, last_updated"

    async def _get_all_facts(self, user_id: str) -> List[Dict[str, Any]]:
        """All stored facts for the user, oldest first (empty list when none)."""
        async with await self._connect() as conn:
            cur = await conn.execute(
                f"SELECT {self._FACT_COLS} FROM user_facts WHERE user_id = %s ORDER BY first_mentioned, type, subject",
                (user_id,),
            )
            rows = await cur.fetchall()
        return [self._row_to_fact(r) for r in rows]

    async def _search_facts(
        self,
        user_id: str,
        query_text: str,
        request: Any,
        user: Any,
        threshold: float,
        limit: int,
    ) -> List[Dict[str, Any]]:
        """always_inject_types facts, plus the facts nearest to query_text:
        cosine similarity >= threshold, ordered by Open WebUI's reranker when one is
        configured (vector order otherwise), at most `limit` (0 = no cap)."""
        always = list(self.valves.always_inject_types)
        has_reranker = getattr(getattr(getattr(request, "app", None), "state", None), "RERANKING_FUNCTION", None) is not None
        # With a reranker, give it more candidates than the final cap to choose from.
        fetch = limit * 3 if (limit > 0 and has_reranker) else limit
        async with await self._connect() as conn:
            cur = await conn.execute(
                f"SELECT {self._FACT_COLS} FROM user_facts WHERE user_id = %s AND type = ANY(%s) "
                "ORDER BY first_mentioned, type, subject",
                (user_id, always),
            )
            always_facts = [self._row_to_fact(r) for r in await cur.fetchall()]
            if not query_text.strip():
                return always_facts
            qvec = self._vec((await self._embed([query_text], request, user, RAG_EMBEDDING_QUERY_PREFIX))[0])
            sql = (
                f"SELECT {self._FACT_COLS}, 1 - (embedding <=> %s::vector) AS similarity "
                "FROM user_facts WHERE user_id = %s AND NOT (type = ANY(%s)) AND embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector"
            )
            params: List[Any] = [qvec, user_id, always, qvec]
            if fetch > 0:
                sql += " LIMIT %s"
                params.append(fetch)
            cur = await conn.execute(sql, params)
            near = [self._row_to_fact(r) for r in await cur.fetchall()]
        self._log(
            "Vector search: " + ", ".join(f"{f['type']}/{f['subject']}={f['similarity']}" for f in near),
            "debug",
        )
        kept = [f for f in near if f["similarity"] >= threshold]
        scores = await self._rerank(query_text, kept, request, user) if has_reranker else None
        if scores is not None:
            for f, sc in zip(kept, scores):
                f["rerank"] = round(sc, 3)
            kept.sort(key=lambda f: f["rerank"], reverse=True)
            self._log(
                "Reranked: " + ", ".join(f"{f['type']}/{f['subject']}={f['rerank']}" for f in kept),
                "debug",
            )
        if limit > 0:
            kept = kept[:limit]
        return always_facts + kept

    async def _apply_changes(
        self, user_id: str, changes: List[Dict[str, Any]], request: Any, user: Any
    ) -> Dict[str, int]:
        """
        Apply the extraction model's change list, one row per fact, in one transaction.

        - Fact flagged removed (see _fact_marked_removed): DELETE by key.
        - Otherwise the fact is embedded ("subject: value"). If no row has this exact
          (type, subject) but a row of the same type is at least dedupe_threshold
          similar, the change is applied to that row instead (merged).
        - INSERT, or UPDATE value/sentiment/confidence/embedding when any differs.
          An unchanged fact is not touched, so last_updated only moves when the fact did.
        - Facts the model did not mention are left alone.
        Returns counts: updated, added, removed, merged, skipped.
        """
        counts = {"updated": 0, "added": 0, "removed": 0, "merged": 0, "skipped": 0}
        upserts: List[tuple] = []
        removals: List[tuple] = []
        for fact in changes:
            type_, subject = _fact_key(fact)
            if not type_ or not subject:
                self._log(f"Skipping fact without type/subject: {fact}", "warning")
                counts["skipped"] += 1
                continue
            if _fact_marked_removed(fact):
                removals.append((type_, subject))
                continue
            value = str(fact.get("value") or "").strip()
            if not value:
                self._log(f"Skipping fact without value: {fact}", "warning")
                counts["skipped"] += 1
                continue
            sentiment = str(fact.get("sentiment") or "").strip().lower() or None
            try:
                confidence = float(fact.get("confidence", 0.8))
            except (TypeError, ValueError):
                confidence = 0.8
            upserts.append((type_, subject, value, sentiment, confidence))

        vectors = (
            await self._embed(
                [self._fact_text({"subject": u[1], "value": u[2]}) for u in upserts],
                request, user, RAG_EMBEDDING_CONTENT_PREFIX,
            )
            if upserts else []
        )

        async with await self._connect() as conn:
            async with conn.transaction():
                for type_, subject in removals:
                    cur = await conn.execute(
                        "DELETE FROM user_facts WHERE user_id = %s AND type = %s AND subject = %s",
                        (user_id, type_, subject),
                    )
                    counts["removed"] += cur.rowcount
                for (type_, subject, value, sentiment, confidence), vec in zip(upserts, vectors):
                    vlit = self._vec(vec)
                    if self.valves.dedupe_threshold < 1:
                        cur = await conn.execute(
                            "SELECT subject, 1 - (embedding <=> %s::vector) AS similarity FROM user_facts "
                            "WHERE user_id = %s AND type = %s AND subject <> %s AND embedding IS NOT NULL "
                            "AND NOT EXISTS (SELECT 1 FROM user_facts e WHERE e.user_id = %s AND e.type = %s AND e.subject = %s) "
                            "ORDER BY embedding <=> %s::vector LIMIT 1",
                            (vlit, user_id, type_, subject, user_id, type_, subject, vlit),
                        )
                        near = await cur.fetchone()
                        if near and float(near[1]) >= self.valves.dedupe_threshold:
                            self._log(
                                f"Merging {type_}/{subject} into existing {type_}/{near[0]} (similarity {float(near[1]):.3f})",
                                "info",
                            )
                            subject = near[0]
                            counts["merged"] += 1
                    cur = await conn.execute(
                        """
                        INSERT INTO user_facts (user_id, type, subject, value, sentiment, confidence, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::vector)
                        ON CONFLICT (user_id, type, subject) DO UPDATE
                            SET value        = EXCLUDED.value,
                                sentiment    = EXCLUDED.sentiment,
                                confidence   = EXCLUDED.confidence,
                                embedding    = EXCLUDED.embedding,
                                last_updated = now()
                            WHERE user_facts.value      IS DISTINCT FROM EXCLUDED.value
                               OR user_facts.sentiment  IS DISTINCT FROM EXCLUDED.sentiment
                               OR user_facts.confidence IS DISTINCT FROM EXCLUDED.confidence
                        RETURNING (xmax = 0) AS inserted
                        """,
                        (user_id, type_, subject, value, sentiment, confidence, vlit),
                    )
                    row = await cur.fetchone()
                    if row is None:
                        continue  # identical to the stored fact: not written
                    counts["added" if row[0] else "updated"] += 1
        self._log(
            f"Applied changes for user {user_id[:8]}...: {counts['updated']} updated, "
            f"{counts['added']} added, {counts['removed']} removed, {counts['merged']} merged, "
            f"{counts['skipped']} skipped",
            "info",
        )
        return counts

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

    # ------------------------------------------------------------------
    # PII engine: the built-in regexes or the PII-Tracer model (valve pii_engine)
    # ------------------------------------------------------------------

    def _pii_enabled(self) -> bool:
        return self.valves.pii_engine != "none"

    def _pii_use_model(self) -> bool:
        return self.valves.pii_engine == "pii_tracer"

    def _pii_fallback(self, err: BaseException) -> bool:
        """Log a PII-Tracer failure once per memory update. True when the regexes should take over."""
        if not self._pii_down_logged:
            self._pii_down_logged = True
            self._log(
                f"PII-Tracer unavailable at {self.valves.pii_tracer_url} ({type(err).__name__}: {err}); "
                f"fallback={self.valves.pii_tracer_fallback}",
                "warning",
            )
        return self.valves.pii_tracer_fallback == "regex"

    async def _pii_tracer_spans(self, text: str) -> List[Dict[str, Any]]:
        """Spans {label, start, end, score} from the PII-Tracer server, limited to pii_tracer_labels. Raises on failure."""
        if not text or not text.strip():
            return []
        if not self.valves.pii_tracer_url.strip():
            raise RuntimeError("valve pii_tracer_url is empty")
        url = self.valves.pii_tracer_url.strip().rstrip("/") + "/v1/pii/detect"
        timeout = aiohttp.ClientTimeout(total=self.valves.pii_tracer_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json={"text": text, "mask": False}) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = await resp.json()
        wanted = set(self.valves.pii_tracer_labels)
        return [s for s in data.get("spans", []) if s.get("label") in wanted]

    async def _pii_detect(self, text: str) -> List[Dict[str, str]]:
        """Findings [{label, match}] in text with the configured engine. For the model engine `match` is kept
        out of log lines by the callers; the regex engine's callers log it as before."""
        if not self._pii_use_model():
            return [{"label": p["label"], "match": p["match"]} for p in detect_pii(text)]
        try:
            spans = await self._pii_tracer_spans(text)
        except Exception as e:  # connection, timeout, bad status, bad JSON
            if self._pii_fallback(e):
                return [{"label": p["label"], "match": p["match"]} for p in detect_pii(text)]
            return []
        return [{"label": str(s["label"]), "match": text[int(s["start"]):int(s["end"])]} for s in spans]

    async def _pii_scrub(self, text: str) -> str:
        """text with every PII finding replaced by [REDACTED], with the configured engine."""
        if not self._pii_use_model():
            return scrub_pii(text, replacement="[REDACTED]", patterns=self.valves.pii_patterns_enabled)
        try:
            spans = await self._pii_tracer_spans(text)
        except Exception as e:
            if self._pii_fallback(e):
                return scrub_pii(text, replacement="[REDACTED]", patterns=self.valves.pii_patterns_enabled)
            return text
        for s in sorted(spans, key=lambda s: -int(s["start"])):
            text = text[: int(s["start"])] + "[REDACTED]" + text[int(s["end"]) :]
        return text

    async def _validate_fact(self, fact: Dict[str, Any]) -> Dict[str, Any]:
        """validate_fact_no_pii with the configured engine (same return shape)."""
        if not self._pii_use_model():
            return validate_fact_no_pii(fact)
        reasons: List[str] = []
        subject = str(fact.get("subject", "") or "")
        value = str(fact.get("value", "") or "")
        if subject.lower().strip() in PII_SUBJECT_BLOCKLIST:
            reasons.append(f"Blocked subject: '{subject}'")
        # The model engine's reasons name the label only, never the matched text.
        reasons.extend(f"{h['label']} detected in value" for h in await self._pii_detect(value))
        reasons.extend(f"{h['label']} detected in subject" for h in await self._pii_detect(subject))
        if not reasons:
            return {"clean": True, "blocked_reasons": [], "scrubbed_fact": fact}
        return {
            "clean": False,
            "blocked_reasons": reasons,
            "scrubbed_fact": {
                **fact,
                "value": await self._pii_scrub(value),
                "subject": await self._pii_scrub(subject),
            },
        }

    async def _filter_facts_pii(self, facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """filter_facts_pii with the configured engine: drop or redact facts with PII (pii_filter_mode)."""
        if not self._pii_use_model():
            return filter_facts_pii(
                facts,
                mode=self.valves.pii_filter_mode,
                logger_fn=lambda msg: self._log(f"PII filter: {msg}", "warning"),
            )
        cleaned: List[Dict[str, Any]] = []
        for fact in facts:
            result = await self._validate_fact(fact)
            if result["clean"]:
                cleaned.append(fact)
                continue
            self._log(
                f"PII filter: PII blocked in fact ({fact.get('type')}/{fact.get('subject')}): "
                f"{', '.join(result['blocked_reasons'])}",
                "warning",
            )
            if self.valves.pii_filter_mode == "redact":
                cleaned.append(result["scrubbed_fact"])
        return cleaned

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
        Ask the extraction model which facts change because of `new_message`
        and apply those changes row by row (see _apply_changes).
        Runs in the background after the reply (see outlet).
        """
        try:
            self._log(f"Starting memory update for user {user_id[:8]}...", "info")
            self._pii_down_logged = False
            await self._init_store(request, user)

            async def _scrub(msg: Dict[str, str]) -> Dict[str, str]:
                text = content_to_text(msg.get("content", ""))
                if self._pii_enabled() and self.valves.pii_scrub_input:
                    scrubbed = await self._pii_scrub(text)
                    if scrubbed != text:
                        self._log(f"PII pre-scrub ({self.valves.pii_engine}): redacted PII in message", "info")
                    text = scrubbed
                return {"role": msg.get("role", "user"), "content": text}

            earlier_scrubbed = [await _scrub(m) for m in earlier_messages]
            new_scrubbed = await _scrub(new_message)

            # Context for the extraction model: the always-inject facts plus the
            # existing facts nearest to the new message (no similarity cutoff, so a
            # fact being corrected is seen even when worded differently).
            existing_facts = await self._search_facts(
                user_id, new_scrubbed["content"], request, user,
                threshold=0.0, limit=self.valves.max_memories,
            )

            def _for_prompt(f: Dict[str, Any]) -> Dict[str, Any]:
                out = {"type": f.get("type"), "subject": f.get("subject"), "value": f.get("value")}
                if f.get("sentiment"):
                    out["sentiment"] = f["sentiment"]
                return out

            # Local date of the server (honours the container's TZ), so "this year"
            # and "last month" resolve the way the user means them.
            merge_prompt = f"""TODAY: {datetime.now().astimezone().strftime("%Y-%m-%d")}

EXISTING FACTS:
{json.dumps([_for_prompt(f) for f in existing_facts], indent=2)}

EARLIER MESSAGES (context only, already processed):
{json.dumps(earlier_scrubbed, indent=2)}

NEW MESSAGE:
{json.dumps(new_scrubbed, indent=2)}"""

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

            if self._pii_enabled() and changes:
                pre_count = len(changes)
                changes = await self._filter_facts_pii(changes)
                if pre_count - len(changes) > 0:
                    self._log(
                        f"PII post-validation: blocked {pre_count - len(changes)} fact(s) containing PII",
                        "warning",
                    )

            if not changes:
                self._log("No changes - skipping store", "info")
                return

            counts = await asyncio.wait_for(
                self._apply_changes(user_id, changes, request, user), timeout=60.0
            )
            if not (counts["updated"] or counts["added"] or counts["removed"]):
                self._log(f"No changes to facts ({len(existing_facts)} candidates unchanged)", "info")
                return
            self._log(
                f"Memory updated for user {user_id[:8]}: "
                f"{counts['updated']} updated, {counts['added']} added, {counts['removed']} removed, "
                f"{counts['merged']} merged",
                "info",
            )

        except asyncio.TimeoutError:
            self._log("Memory update timed out after 60 seconds", "warning")
        except Exception as e:
            self._log(
                f"Failed to update memory state: {type(e).__name__}: {e}", "error"
            )
            import traceback

            self._log(f"State update traceback: {traceback.format_exc()}", "error")

    def _query_text(self, messages: List[Dict[str, Any]]) -> str:
        """The search text: the last query_window_messages user/assistant messages."""
        n = max(1, int(self.valves.query_window_messages))
        window = [m for m in messages if m.get("role") in ("user", "assistant")][-n:]
        return "\n".join(t for t in (content_to_text(m.get("content")) for m in window) if t)

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
        Inlet: embed the recent chat, pull the nearest facts, append them to the
        latest user message. No model call; extraction runs after the reply.
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
            messages = body.get("messages", [])
            # Wait for a still-running extraction from the previous turn so the
            # newest facts are included.
            async with self._user_lock(user_id):
                await self._init_store(__request__, user_obj)
                injected_facts = await self._search_facts(
                    user_id, self._query_text(messages), __request__, user_obj,
                    threshold=self.valves.relevance_threshold, limit=self.valves.max_memories,
                )
            self._log(f"Retrieved {len(injected_facts)} facts", "info")

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
                await status(f"💭 Recalled {len(injected_facts)} memories relevant to this chat", True)
            else:
                await status("👋 Getting to know you...", True)

            self._log("=== INLET COMPLETE ===", "info")
        except ConnectionError as e:
            self._log(f"Memory store error: {e}", "error")
            await status("⚠️ Cannot open the memory database. Check postgres_url and the server log.", True)
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
