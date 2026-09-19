"""
title: LangGraph Memory Graph - Enterprise (Rerank)
author: beaudamore
date: 2026-09-18
version: 4.0
license: MIT
description: Fact-level upsert memory with pgvector retrieval, cross-encoder reranking for recall, and PostgreSQL persistence
required_open_webui_version: >= 0.11.0
requirements: langgraph>=1.0.0, langgraph-checkpoint-postgres, psycopg[binary], psycopg-pool
"""

from __future__ import annotations

import json
import logging
import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, TypedDict
from urllib.parse import quote_plus

import aiohttp
from pydantic import BaseModel, Field
from fastapi import Request

# CRITICAL: Import psycopg BEFORE any LangGraph imports
# psycopg_binary requires psycopg to be imported first
import psycopg
import psycopg_pool

# LangGraph imports
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import StateGraph, END

# OpenWebUI imports for internal API access
from open_webui.main import app as webui_app
from open_webui.models.users import UserModel, Users
from open_webui.utils.chat import generate_chat_completion

# Set up logging
logger = logging.getLogger("openwebui.filters.langgraph_memory")
logger.setLevel(logging.INFO)

# Schema version - increment when making breaking changes to data structure
SCHEMA_VERSION = 5

# Migration history for documentation and potential rollback
SCHEMA_MIGRATIONS = [
    {
        "version": 1,
        "date": "2026-01-09",
        "description": "Initial schema with first_mentioned/last_updated fields",
        "changes": [],
    },
    {
        "version": 2,
        "date": "2026-01-09",
        "description": "Preference evolution tracking - keep all data points",
        "changes": [],
    },
    {
        "version": 3,
        "date": "2026-01-10",
        "description": "Simplified to flexible fact-based schema",
        "changes": [],
    },
    {
        "version": 4,
        "date": "2026-01-10",
        "description": "LLM-powered semantic merge replaces code-based deduplication",
        "changes": [],
    },
    {
        "version": 5,
        "date": "2026-09-18",
        "description": "Facts moved to memory_facts table with pgvector embeddings; LLM emits fact-level add/update/delete ops instead of rewriting the whole list",
        "changes": [
            {
                "type": "schema_change",
                "description": "New table memory_facts (one row per fact version, status active/superseded/deleted, embedding vector)",
            },
            {
                "type": "behavior_change",
                "description": "Extraction candidates and injected memories are selected by cosine similarity; identity facts always included",
            },
            {
                "type": "node_removed",
                "description": "Removed process_merged node; checkpointer holds only metadata and summary",
            },
        ],
    },
]


# ============================================================================
# Graph State Definitions
# ============================================================================


class Fact(TypedDict):
    """
    A single fact about the user - one row in memory_facts.

    Types: identity, preference, ownership, relationship, goal, skill, event
    But these are suggestions - any type string is valid.
    """

    id: str  # UUID of the memory_facts row
    type: str
    subject: str
    value: str
    sentiment: Optional[str]
    confidence: float
    first_mentioned: str  # ISO datetime
    last_updated: str  # ISO datetime


class MemoryGraphState(TypedDict):
    """Per-user metadata state. Facts themselves live in memory_facts."""

    user_id: str
    conversation_id: str

    # Counts of the ops applied in this invocation (per-invocation, cleared after)
    _op_counts: Dict[str, int]

    # Metadata
    last_updated: str
    total_facts: int
    memory_summary: str


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


class MemoryOps(BaseModel):
    """Fact-level operations returned by the memory manager model"""

    ops: List[Dict[str, Any]] = Field(default_factory=list)


# ============================================================================
# Filter Implementation
# ============================================================================


class Filter:
    class Valves(BaseModel):
        """Configuration for LangGraph Memory Filter"""

        # Execution Priority
        priority: int = Field(
            default=10,
            description="Filter execution priority. Lower numbers run first. Safety filters (antivirus, content policy, prompt injection, llamaguard) should run BEFORE memory (e.g., -100 to -10), so set this to positive (10-50) to run after safety checks pass.",
        )

        # PostgreSQL Configuration
        postgres_host: str = Field(
            default="langgraph-pgvector",
            description="PostgreSQL host (docker service name or IP). Must run the pgvector image.",
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

        # Embedding Configuration
        embedding_api_base_url: str = Field(
            default="http://100.79.200.70:8006/v1",
            description="OpenAI-compatible embeddings endpoint base URL (POST {base}/embeddings)",
        )
        embedding_api_key: str = Field(
            default="",
            description="Bearer token for the embeddings endpoint (blank = none)",
        )
        embedding_model: str = Field(
            default="bge-m3",
            description="Embedding model id served at embedding_api_base_url",
        )
        embedding_dim: int = Field(
            default=1024,
            description="Vector size of the embedding model. Must match the memory_facts.embedding column; changing it requires a new table.",
        )
        embedding_timeout: int = Field(
            default=30, description="Seconds to wait for an embeddings response"
        )

        # Rerank Configuration (this variant scores vector candidates with a cross-encoder)
        rerank_api_base_url: str = Field(
            default="http://100.79.200.70:8007/v1",
            description="Rerank endpoint base URL (POST {base}/rerank, vLLM/Cohere style)",
        )
        rerank_api_key: str = Field(
            default="",
            description="Bearer token for the rerank endpoint (blank = none)",
        )
        rerank_model: str = Field(
            default="bge-reranker-v2-m3",
            description="Reranker model id served at rerank_api_base_url",
        )
        rerank_score_threshold: float = Field(
            default=0.01,
            description="Minimum reranker relevance_score for a candidate to be injected. Scores are not calibrated like cosine; tune from the per-candidate scores in the log.",
        )
        rerank_timeout: int = Field(
            default=15, description="Seconds to wait for a rerank response"
        )

        # Retrieval Configuration
        retrieval_top_k: int = Field(
            default=30,
            description="Max facts fetched by vector similarity, both as merge candidates and for injection (identity facts are added on top)",
        )
        similarity_threshold: float = Field(
            default=0.35,
            description="Minimum cosine similarity (0-1) for a fact to be considered relevant for injection. Merge candidates ignore this floor.",
        )

        # LLM Configuration for Extraction
        extraction_model_id: str = Field(
            default="memory-manager",
            description='OpenWebUI Model ID for memory extraction. Its system prompt must produce the {"ops": [...]} format.',
        )
        extraction_model_max_tokens: int = Field(
            default=4000, description="Max tokens for extraction model response"
        )

        # Memory Processing Configuration
        extraction_threshold: int = Field(
            default=1,
            description="Number of user messages before triggering memory extraction (1 = extract every message)",
        )

        max_injected_memories: int = Field(
            default=10,
            description="Maximum number of memory facts to inject into context",
        )
        memory_injection_format: Literal["structured", "natural", "bullet"] = Field(
            default="structured",
            description="Format for injecting memories into context",
        )

        # UI Configuration
        show_status: bool = Field(
            default=True, description="Show memory processing status messages"
        )
        show_injected_memories: bool = Field(
            default=False, description="Show which memories were injected (debug mode)"
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
            default=False,
            description="FALLBACK ONLY in this variant: when the reranker call fails, run the relevance model over the vector candidates. When disabled, a rerank failure injects the raw vector candidates.",
        )
        relevance_model_id: str = Field(
            default="memory-relevance",
            description="OpenWebUI Model ID for relevance filtering. Create a model with RELEVANCE_PROMPT.md as system prompt.",
        )
        relevance_model_max_tokens: int = Field(
            default=4000, description="Max tokens for relevance model response"
        )
        always_inject_types: List[str] = Field(
            default=["identity"],
            description="Fact types that ALWAYS get injected and are always sent as merge candidates, regardless of similarity.",
        )

        # Debug
        debug_mode: bool = Field(
            default=False, description="Enable detailed debug logging"
        )

    class UserValves(BaseModel):
        """Per-user configuration"""

        enabled: bool = Field(
            default=True, description="Enable LangGraph memory for this user"
        )
        show_status: bool = Field(
            default=True, description="Show status messages for this user"
        )

    def __init__(self):
        self.name = "LangGraph Memory Graph (Rerank)"
        self.valves = self.Valves()
        self.memory_graph = None
        self.checkpointer = None
        self._pool = None
        self._initialized = False

    def _log(self, message: str, level: str = "info"):
        """Centralized logging"""
        if level == "debug" and not self.valves.debug_mode:
            return
        # Print to stdout for Docker logs visibility
        print(f"[LangGraph Memory Rerank] [{level.upper()}] {message}", flush=True)
        getattr(logger, level, logger.info)(f"[LangGraph Memory Rerank] {message}")

    async def _resolve_user(self, __user__: Optional[Dict[str, Any]]):
        """
        Resolve the __user__ dict into a real UserModel.

        generate_chat_completion() and its downstream cache key builders use
        attribute access (user.id, user.role). Passing the raw __user__ dict raises
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
    # Initialization
    # ------------------------------------------------------------------

    def _conn_string(self) -> str:
        return (
            f"postgresql://{self.valves.postgres_user}:"
            f"{quote_plus(self.valves.postgres_password)}@"
            f"{self.valves.postgres_host}:{self.valves.postgres_port}/"
            f"{self.valves.postgres_database}"
        )

    def _ensure_memory_tables(self, conn) -> None:
        """Create the pgvector extension, the memory_facts table and its indexes (idempotent)."""
        dim = int(self.valves.embedding_dim)
        with conn.cursor() as cur:
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception as e:
                raise RuntimeError(
                    f"pgvector extension unavailable on {self.valves.postgres_host}: {e}. "
                    f"The database must run the pgvector/pgvector image."
                )
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS memory_facts (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    value TEXT NOT NULL,
                    sentiment TEXT,
                    confidence REAL NOT NULL DEFAULT 0.8,
                    source_chat_id TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    superseded_by UUID,
                    first_mentioned TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    embedding vector({dim})
                )
                """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS memory_facts_user_status_idx "
                "ON memory_facts (user_id, status)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS memory_facts_embedding_idx "
                "ON memory_facts USING hnsw (embedding vector_cosine_ops)"
            )

    async def _initialize_graph(self):
        """Initialize LangGraph with PostgreSQL checkpointer and the memory_facts table"""
        if self._initialized:
            return

        try:
            conn_string = self._conn_string()
            self._log(
                f"Connecting to PostgreSQL at {self.valves.postgres_host}:{self.valves.postgres_port}"
            )

            try:
                self._pool = psycopg_pool.ConnectionPool(
                    conninfo=conn_string,
                    min_size=1,
                    max_size=5,
                    open=True,
                )
                self._log("PostgreSQL connection pool created", "debug")

                self.checkpointer = PostgresSaver(self._pool)
                self._log("PostgreSQL checkpointer created successfully", "debug")

                with self._pool.connection() as test_conn:
                    with test_conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        result = cur.fetchone()
                        self._log(f"Connection pool test successful: {result}", "debug")
            except Exception as e:
                self._log(f"Failed to create PostgreSQL checkpointer: {e}", "error")
                raise ConnectionError(
                    f"Cannot connect to PostgreSQL at {self.valves.postgres_host}:{self.valves.postgres_port}. "
                    f"Ensure PostgreSQL is running and credentials are correct. Error: {e}"
                )

            # Create checkpoint tables using a direct connection with autocommit
            # (setup() uses CREATE INDEX CONCURRENTLY which can't run in a transaction)
            try:
                with psycopg.connect(conn_string, autocommit=True) as setup_conn:
                    setup_saver = PostgresSaver(conn=setup_conn)
                    setup_saver.setup()

                    with setup_conn.cursor() as cur:
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS schema_migrations (
                                version INTEGER PRIMARY KEY,
                                applied_at TIMESTAMPTZ DEFAULT NOW(),
                                description TEXT,
                                changes JSONB
                            )
                        """)
                        cur.execute(
                            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                        )
                        current_version = cur.fetchone()[0]

                        if current_version < SCHEMA_VERSION:
                            self._log(
                                f"Schema upgrade needed: v{current_version} → v{SCHEMA_VERSION}",
                                "info",
                            )
                            for migration in SCHEMA_MIGRATIONS:
                                if migration["version"] > current_version:
                                    cur.execute(
                                        "INSERT INTO schema_migrations (version, description, changes) VALUES (%s, %s, %s)",
                                        (
                                            migration["version"],
                                            migration["description"],
                                            json.dumps(migration["changes"]),
                                        ),
                                    )
                                    self._log(
                                        f"Applied migration v{migration['version']}: {migration['description']}",
                                        "info",
                                    )
                        else:
                            self._log(
                                f"Schema is current (v{current_version})", "debug"
                            )

                    # memory_facts + pgvector (idempotent)
                    self._ensure_memory_tables(setup_conn)

                self._log("PostgreSQL tables initialized", "debug")
            except Exception as e:
                if "already exists" in str(e).lower():
                    self._log("Tables already exist", "debug")
                else:
                    self._log(f"Failed to setup tables: {e}", "error")
                    raise RuntimeError(f"Failed to initialize database tables: {e}")

            try:
                self.memory_graph = self._create_memory_graph()
                self._log("Memory graph workflow compiled successfully", "debug")
            except Exception as e:
                self._log(f"Failed to create memory graph: {e}", "error")
                raise RuntimeError(f"Failed to create LangGraph workflow: {e}")

            self._initialized = True
            self._log(
                "LangGraph memory system initialized with PostgreSQL + pgvector", "info"
            )

        except ImportError as e:
            self._log(
                f"Failed to import LangGraph dependencies: {e}. "
                f"Ensure dependencies are installed: "
                f"pip install langgraph>=1.0.0 langgraph-checkpoint-postgres psycopg[binary] psycopg-pool",
                "error",
            )
            raise
        except Exception as e:
            self._log(f"Failed to initialize LangGraph: {e}", "error")
            raise

    def _create_memory_graph(self) -> StateGraph:
        """Create the LangGraph workflow: metadata update -> summary"""

        workflow = StateGraph(MemoryGraphState)
        workflow.add_node("update_memory", self._update_memory_store_node)
        workflow.add_node("summarize", self._create_summary_node)
        workflow.set_entry_point("update_memory")
        workflow.add_edge("update_memory", "summarize")
        workflow.add_edge("summarize", END)
        return workflow.compile(checkpointer=self.checkpointer)

    # ------------------------------------------------------------------
    # memory_facts access (sync; called from executor threads or graph nodes)
    # ------------------------------------------------------------------

    @staticmethod
    def _vec_literal(vec: List[float]) -> str:
        return "[" + ",".join(f"{float(x):.7g}" for x in vec) + "]"

    @staticmethod
    def _row_to_fact(row) -> Dict[str, Any]:
        (
            fid,
            ftype,
            subject,
            value,
            sentiment,
            confidence,
            first_mentioned,
            last_updated,
        ) = row[:8]
        fact: Dict[str, Any] = {
            "id": str(fid),
            "type": ftype,
            "subject": subject,
            "value": value,
            "sentiment": sentiment,
            "confidence": float(confidence) if confidence is not None else 0.8,
            "first_mentioned": first_mentioned.isoformat() if first_mentioned else None,
            "last_updated": last_updated.isoformat() if last_updated else None,
        }
        if len(row) > 8 and row[8] is not None:
            fact["similarity"] = round(float(row[8]), 4)
        return fact

    _FACT_COLS = (
        "id, type, subject, value, sentiment, confidence, first_mentioned, last_updated"
    )

    def _db_load_active_facts(
        self,
        user_id: str,
        types: Optional[List[str]] = None,
        exclude_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        sql = f"SELECT {self._FACT_COLS} FROM memory_facts WHERE user_id = %s AND status = 'active'"
        params: List[Any] = [user_id]
        if types:
            sql += " AND type = ANY(%s)"
            params.append(list(types))
        if exclude_types:
            sql += " AND NOT (type = ANY(%s))"
            params.append(list(exclude_types))
        sql += " ORDER BY first_mentioned"
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return [self._row_to_fact(r) for r in cur.fetchall()]

    def _db_search_facts(
        self,
        user_id: str,
        query_vec: List[float],
        top_k: int,
        exclude_types: Optional[List[str]] = None,
        min_similarity: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Nearest active facts by cosine similarity."""
        vec = self._vec_literal(query_vec)
        sql = (
            f"SELECT {self._FACT_COLS}, 1 - (embedding <=> %s::vector) AS similarity "
            f"FROM memory_facts WHERE user_id = %s AND status = 'active' AND embedding IS NOT NULL"
        )
        params: List[Any] = [vec, user_id]
        if exclude_types:
            sql += " AND NOT (type = ANY(%s))"
            params.append(list(exclude_types))
        sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params.extend([vec, int(top_k)])
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [self._row_to_fact(r) for r in cur.fetchall()]
        if min_similarity > 0:
            rows = [r for r in rows if r.get("similarity", 0) >= min_similarity]
        return rows

    def _db_count_active(self, user_id: str) -> int:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM memory_facts WHERE user_id = %s AND status = 'active'",
                    (user_id,),
                )
                return int(cur.fetchone()[0])

    def _db_apply_ops(
        self, user_id: str, ops: List[Dict[str, Any]], source_chat_id: str
    ) -> Dict[str, int]:
        """
        Apply validated ops in one transaction.
        add    -> insert active row
        update -> insert new active row, mark old row superseded (history kept)
        delete -> mark row deleted
        Rows are only ever touched when they belong to user_id.
        """
        counts = {"add": 0, "update": 0, "delete": 0, "skipped": 0}
        insert_sql = (
            "INSERT INTO memory_facts "
            "(user_id, type, subject, value, sentiment, confidence, source_chat_id, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector) RETURNING id"
        )
        with self._pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    for op in ops:
                        kind = op.get("op")
                        fact = op.get("fact") or {}
                        emb = op.get("_embedding")
                        vec = self._vec_literal(emb) if emb else None

                        if kind == "add":
                            cur.execute(
                                insert_sql,
                                (
                                    user_id,
                                    fact["type"],
                                    fact["subject"],
                                    fact["value"],
                                    fact.get("sentiment"),
                                    float(fact.get("confidence", 0.8)),
                                    source_chat_id,
                                    vec,
                                ),
                            )
                            counts["add"] += 1

                        elif kind == "update":
                            cur.execute(
                                "SELECT first_mentioned FROM memory_facts "
                                "WHERE id = %s AND user_id = %s AND status = 'active'",
                                (op["id"], user_id),
                            )
                            old = cur.fetchone()
                            if not old:
                                counts["skipped"] += 1
                                continue
                            cur.execute(
                                insert_sql,
                                (
                                    user_id,
                                    fact["type"],
                                    fact["subject"],
                                    fact["value"],
                                    fact.get("sentiment"),
                                    float(fact.get("confidence", 0.8)),
                                    source_chat_id,
                                    vec,
                                ),
                            )
                            new_id = cur.fetchone()[0]
                            # keep the original first_mentioned on the replacement row
                            cur.execute(
                                "UPDATE memory_facts SET first_mentioned = %s WHERE id = %s",
                                (old[0], new_id),
                            )
                            cur.execute(
                                "UPDATE memory_facts SET status = 'superseded', superseded_by = %s, "
                                "last_updated = NOW() WHERE id = %s AND user_id = %s",
                                (new_id, op["id"], user_id),
                            )
                            counts["update"] += 1

                        elif kind == "delete":
                            cur.execute(
                                "UPDATE memory_facts SET status = 'deleted', last_updated = NOW() "
                                "WHERE id = %s AND user_id = %s AND status = 'active'",
                                (op["id"], user_id),
                            )
                            if cur.rowcount:
                                counts["delete"] += 1
                            else:
                                counts["skipped"] += 1
        return counts

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    @staticmethod
    def _message_text(content: Any) -> str:
        """Flatten a message content field (string or multimodal list) to text."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
                elif isinstance(part, str):
                    parts.append(part)
            return "\n".join(parts)
        return "" if content is None else str(content)

    @staticmethod
    def _text_for_fact(fact: Dict[str, Any]) -> str:
        return f"{fact.get('type', '')} - {fact.get('subject', '')}: {fact.get('value', '')}"

    async def _embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Embed texts with the OpenAI-compatible /embeddings endpoint. Raises on failure."""
        if not texts:
            return []
        url = self.valves.embedding_api_base_url.rstrip("/") + "/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.valves.embedding_api_key:
            headers["Authorization"] = f"Bearer {self.valves.embedding_api_key}"
        payload = {
            "model": self.valves.embedding_model,
            "input": [t[:4000] if t else " " for t in texts],
        }
        timeout = aiohttp.ClientTimeout(total=self.valves.embedding_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Embeddings HTTP {resp.status} from {url}: {body[:200]}"
                    )
                data = await resp.json()
        items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        vectors = [item["embedding"] for item in items]
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"Embeddings count mismatch: sent {len(texts)}, got {len(vectors)}"
            )
        dim = int(self.valves.embedding_dim)
        for v in vectors:
            if len(v) != dim:
                raise RuntimeError(
                    f"Embedding dim {len(v)} != configured embedding_dim {dim}"
                )
        return vectors

    async def _rerank_facts(
        self, query: str, facts: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Score facts against the query with the cross-encoder rerank endpoint.
        Returns the facts with a 'rerank_score' field, highest first. Raises on failure.
        """
        if not facts:
            return []
        url = self.valves.rerank_api_base_url.rstrip("/") + "/rerank"
        headers = {"Content-Type": "application/json"}
        if self.valves.rerank_api_key:
            headers["Authorization"] = f"Bearer {self.valves.rerank_api_key}"
        payload = {
            "model": self.valves.rerank_model,
            "query": query[:4000],
            "documents": [self._text_for_fact(f)[:2000] for f in facts],
            "top_n": len(facts),
        }
        timeout = aiohttp.ClientTimeout(total=self.valves.rerank_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Rerank HTTP {resp.status} from {url}: {body[:200]}"
                    )
                data = await resp.json()
        results = data.get("results")
        if not isinstance(results, list):
            raise RuntimeError(
                f"Rerank response has no results list: {str(data)[:200]}"
            )
        scored: List[Dict[str, Any]] = []
        for item in results:
            idx = item.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(facts):
                continue
            fact = dict(facts[idx])
            fact["rerank_score"] = round(float(item.get("relevance_score", 0.0)), 6)
            scored.append(fact)
        scored.sort(key=lambda f: f["rerank_score"], reverse=True)
        return scored

    # ------------------------------------------------------------------
    # Graph nodes
    # ------------------------------------------------------------------

    def _update_memory_store_node(self, state: MemoryGraphState) -> MemoryGraphState:
        """Update metadata (sync for LangGraph)"""
        self._log("=== UPDATE_MEMORY_STORE NODE ENTERED ===", "info")
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        state["total_facts"] = self._db_count_active(state["user_id"])
        self._log(f"Updated total_facts to {state['total_facts']}", "info")
        return state

    def _create_summary_node(self, state: MemoryGraphState) -> MemoryGraphState:
        """Create natural language summary of all active facts, grouped by type"""
        self._log("=== SUMMARY NODE ENTERED ===", "info")

        facts_by_type: Dict[str, List[Dict[str, Any]]] = {}
        for fact in self._db_load_active_facts(state["user_id"]):
            facts_by_type.setdefault(fact.get("type", "other"), []).append(fact)

        summary_parts = []

        if "identity" in facts_by_type:
            summary_parts.append(
                "Identity: "
                + ", ".join(
                    f"{f['subject']}: {f['value']}" for f in facts_by_type["identity"]
                )
            )
        if "ownership" in facts_by_type:
            summary_parts.append(
                "Owns: "
                + ", ".join(
                    f"{f['subject']}: {f['value']}" for f in facts_by_type["ownership"]
                )
            )
        if "relationship" in facts_by_type:
            summary_parts.append(
                "Relationships: "
                + ", ".join(
                    f"{f['subject']}: {f['value']}"
                    for f in facts_by_type["relationship"]
                )
            )
        if "preference" in facts_by_type:
            pref_texts = []
            for f in sorted(
                facts_by_type["preference"],
                key=lambda x: x.get("confidence", 0),
                reverse=True,
            ):
                sentiment = f.get("sentiment", "neutral")
                if sentiment == "positive":
                    pref_texts.append(f"likes {f['value']}")
                elif sentiment == "negative":
                    pref_texts.append(f"dislikes {f['value']}")
                else:
                    pref_texts.append(f["value"])
            summary_parts.append("Preferences: " + ", ".join(pref_texts[:5]))
        if "goal" in facts_by_type:
            summary_parts.append(
                "Goals: " + ", ".join(f["value"] for f in facts_by_type["goal"][:3])
            )
        if "skill" in facts_by_type:
            summary_parts.append(
                "Skills: "
                + ", ".join(
                    f"{f['subject']}: {f['value']}" for f in facts_by_type["skill"][:5]
                )
            )
        if "event" in facts_by_type:
            summary_parts.append(
                "Events: "
                + ", ".join(
                    f"{f['subject']}: {f['value']}" for f in facts_by_type["event"][:5]
                )
            )

        state["memory_summary"] = "\n".join(summary_parts)
        state["_op_counts"] = {}
        self._log(f"Summary generated: {len(summary_parts)} sections", "info")
        return state

    # ------------------------------------------------------------------
    # Model calls
    # ------------------------------------------------------------------

    def _clean_model_response(self, text: str) -> str:
        """Strip <think> tags, code fences and other noise from a model response"""
        if not text:
            return ""
        cleaned = re.sub(
            r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE
        ).strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1] if len(lines) > 2 else lines).strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        return cleaned

    async def _call_extraction_model(
        self,
        prompt: str,
        user: Optional[Dict[str, Any]] = None,
        request: Optional[Any] = None,
    ) -> Optional[str]:
        """Call the memory-manager model through OpenWebUI's internal API."""
        try:
            self._log("=== CALLING EXTRACTION MODEL ===", "info")
            self._log(f"Model ID: {self.valves.extraction_model_id}", "info")

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

            payload = {
                "model": self.valves.extraction_model_id,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "max_tokens": self.valves.extraction_model_max_tokens,
            }
            self._log(
                f"Extraction payload: model={payload['model']}, prompt_len={len(prompt)}",
                "info",
            )

            response = await generate_chat_completion(
                request=request,
                form_data=payload,
                user=user,
                bypass_filter=True,
            )

            if isinstance(response, dict):
                choices = response.get("choices", [])
                if not choices:
                    self._log(
                        f"No choices in extraction model response. Full response: {response}",
                        "error",
                    )
                    return None
                response_text = choices[0].get("message", {}).get("content", "")
                if not response_text:
                    self._log("Extraction model returned empty content", "warning")
                    return None
                finish = choices[0].get("finish_reason")
                if finish == "length":
                    self._log(
                        "Extraction model hit max_tokens (finish_reason=length) - raise extraction_model_max_tokens",
                        "warning",
                    )
                self._log(
                    f"Extraction model response length: {len(response_text)} chars",
                    "debug",
                )
                return self._clean_model_response(response_text)

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
    # State
    # ------------------------------------------------------------------

    def _empty_state(self, user_id: str, conversation_id: str) -> Dict[str, Any]:
        return {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "facts": [],
            "_op_counts": {},
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "total_facts": 0,
            "memory_summary": "",
        }

    async def _get_user_memory_state(
        self, user_id: str, conversation_id: str
    ) -> Dict[str, Any]:
        """Active facts from memory_facts plus metadata/summary from the checkpointer"""

        if not self._initialized:
            await self._initialize_graph()

        config = {"configurable": {"thread_id": user_id}}
        state = self._empty_state(user_id, conversation_id)

        try:
            loop = asyncio.get_event_loop()
            self._log(f"Retrieving memory state for user {user_id[:8]}...", "debug")

            facts = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: self._db_load_active_facts(user_id)),
                timeout=10.0,
            )
            state["facts"] = facts
            state["total_facts"] = len(facts)

            try:
                snapshot = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, lambda: self.memory_graph.get_state(config)
                    ),
                    timeout=10.0,
                )
                if snapshot and snapshot.values:
                    state["memory_summary"] = snapshot.values.get("memory_summary", "")
                    state["last_updated"] = snapshot.values.get(
                        "last_updated", state["last_updated"]
                    )
            except Exception as e:
                self._log(
                    f"Checkpoint summary unavailable: {type(e).__name__}: {e}", "debug"
                )

            self._log(f"Found memory state with {len(facts)} active facts", "debug")
            return state

        except asyncio.TimeoutError:
            self._log("Memory state retrieval timed out after 10 seconds", "warning")
            return self._empty_state(user_id, conversation_id)
        except Exception as e:
            self._log(
                f"Failed to retrieve memory state: {type(e).__name__}: {e}", "error"
            )
            import traceback

            self._log(f"State retrieval traceback: {traceback.format_exc()}", "debug")
            return self._empty_state(user_id, conversation_id)

    # ------------------------------------------------------------------
    # Ops parsing and validation
    # ------------------------------------------------------------------

    @staticmethod
    def _assistant_names(
        model: Optional[Dict[str, Any]], messages: List[Dict[str, Any]]
    ) -> set:
        """
        Names the current assistant goes by, lowercased: tokens of the workspace
        model's display name/id plus 'You are X' from any system message.
        Used to refuse storing the persona's name as the user's name.
        """
        names: set = set()
        if isinstance(model, dict):
            for key in ("name", "id"):
                val = model.get(key)
                if isinstance(val, str):
                    names.add(val.strip().lower())
                    for tok in re.split(r"[^A-Za-z']+", val):
                        if len(tok) >= 3:
                            names.add(tok.lower())
            info = model.get("info") or {}
            if isinstance(info, dict) and isinstance(info.get("name"), str):
                names.add(info["name"].strip().lower())
        for msg in messages or []:
            if msg.get("role") != "system":
                continue
            text = Filter._message_text(msg.get("content"))
            for m in re.finditer(
                r"\b(?:you are|your name is|you're)\s+([A-Z][A-Za-z'\-]+)", text
            ):
                names.add(m.group(1).lower())
        # generic words that must not block real names
        return {n for n in names if n not in {"the", "and", "you", "base", "test"}}

    def _parse_ops(self, text: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        if not text:
            return None
        cleaned = self._clean_model_response(text)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            self._log(f"Failed to parse ops JSON: {e}", "error")
            return None
        if isinstance(data, list):
            data = {"ops": data}
        if not isinstance(data, dict) or not isinstance(data.get("ops"), list):
            self._log(f"Ops JSON has no 'ops' list: {cleaned[:200]}", "error")
            return None
        return MemoryOps.model_validate(data).ops

    def _validate_ops(
        self,
        ops: List[Dict[str, Any]],
        candidates_by_id: Dict[str, Dict[str, Any]],
        assistant_names: set,
    ) -> List[Dict[str, Any]]:
        valid: List[Dict[str, Any]] = []
        for op in ops:
            kind = str(op.get("op", "")).lower().strip()
            if kind not in ("add", "update", "delete"):
                self._log(f"Rejected op with unknown kind: {op}", "warning")
                continue

            if kind in ("update", "delete"):
                fid = str(op.get("id", "")).strip()
                if fid not in candidates_by_id:
                    self._log(
                        f"Rejected {kind} for unknown/uncandidated id {fid!r}",
                        "warning",
                    )
                    continue
                op["id"] = fid

            if kind in ("add", "update"):
                fact = op.get("fact")
                if not isinstance(fact, dict):
                    self._log(f"Rejected {kind} without fact object: {op}", "warning")
                    continue
                ftype = str(fact.get("type", "")).strip().lower()
                subject = str(fact.get("subject", "")).strip()
                value = str(fact.get("value", "")).strip()
                if not ftype or not subject or not value:
                    self._log(
                        f"Rejected {kind} with missing type/subject/value: {fact}",
                        "warning",
                    )
                    continue
                try:
                    confidence = float(fact.get("confidence", 0.8))
                except (TypeError, ValueError):
                    confidence = 0.8
                fact = {
                    "type": ftype,
                    "subject": subject,
                    "value": value,
                    "sentiment": fact.get("sentiment") or "neutral",
                    "confidence": max(0.0, min(1.0, confidence)),
                }

                # Identity guard: the persona's name is never the user's name
                if ftype == "identity" and "name" in subject.lower():
                    val_l = value.lower()
                    val_tokens = {t for t in re.split(r"[^A-Za-z']+", val_l) if t}
                    if val_l in assistant_names or (
                        val_tokens and val_tokens <= assistant_names
                    ):
                        self._log(
                            f"Identity guard: refused to store assistant name {value!r} as user name",
                            "warning",
                        )
                        continue

                # PII post-validation
                if self.valves.pii_filter_enabled:
                    kept = filter_facts_pii(
                        [fact],
                        mode=self.valves.pii_filter_mode,
                        logger_fn=lambda msg: self._log(
                            f"PII filter: {msg}", "warning"
                        ),
                    )
                    if not kept:
                        continue
                    fact = kept[0]

                # No-op update (identical to the candidate) -> skip
                if kind == "update":
                    old = candidates_by_id[op["id"]]
                    if (
                        old.get("type") == fact["type"]
                        and old.get("subject") == fact["subject"]
                        and old.get("value") == fact["value"]
                        and (old.get("sentiment") or "neutral") == fact["sentiment"]
                    ):
                        self._log(f"Skipped no-op update for {op['id']}", "debug")
                        continue

                op["fact"] = fact

            valid.append(
                {k: v for k, v in op.items() if k in ("op", "id", "fact", "reason")}
            )
        return valid

    # ------------------------------------------------------------------
    # Memory update (upsert)
    # ------------------------------------------------------------------

    async def _update_user_memory_state(
        self,
        user_id: str,
        conversation_id: str,
        new_messages: List[Dict[str, str]],
        user: Optional[Dict[str, Any]] = None,
        request: Optional[Any] = None,
        assistant_names: Optional[set] = None,
    ):
        """Retrieve candidate facts, ask the manager for ops, apply them, refresh metadata."""

        if not self._initialized:
            await self._initialize_graph()

        config = {"configurable": {"thread_id": user_id}}
        assistant_names = assistant_names or set()
        loop = asyncio.get_event_loop()

        try:
            self._log(f"Starting memory update for user {user_id[:8]}...", "info")

            # PII pre-scrub
            messages_for_extraction = []
            for msg in new_messages:
                text = self._message_text(msg.get("content", ""))
                if self.valves.pii_filter_enabled and self.valves.pii_scrub_input:
                    scrubbed = scrub_pii(
                        text,
                        replacement="[REDACTED]",
                        patterns=self.valves.pii_patterns_enabled,
                    )
                    if scrubbed != text:
                        self._log("PII pre-scrub: redacted PII in message", "info")
                    text = scrubbed
                messages_for_extraction.append(
                    {"role": msg.get("role", "user"), "content": text}
                )

            query_text = "\n".join(
                m["content"] for m in messages_for_extraction if m["content"]
            )
            if not query_text.strip():
                self._log("No text in new messages - skipping extraction", "info")
                return

            # Step 1: candidate facts = always-inject types + nearest by similarity
            always_types = list(self.valves.always_inject_types)
            query_vec = (await self._embed_texts([query_text]))[0]
            always_facts, similar_facts = await asyncio.gather(
                loop.run_in_executor(
                    None,
                    lambda: self._db_load_active_facts(user_id, types=always_types),
                ),
                loop.run_in_executor(
                    None,
                    lambda: self._db_search_facts(
                        user_id,
                        query_vec,
                        self.valves.retrieval_top_k,
                        exclude_types=always_types,
                    ),
                ),
            )
            candidates: List[Dict[str, Any]] = []
            seen = set()
            for f in always_facts + similar_facts:
                if f["id"] not in seen:
                    seen.add(f["id"])
                    candidates.append(f)
            candidates_by_id = {f["id"]: f for f in candidates}
            self._log(
                f"Step 1: {len(candidates)} candidate facts ({len(always_facts)} always-inject, "
                f"{len(similar_facts)} by similarity) for {len(messages_for_extraction)} new messages",
                "info",
            )

            # Step 2: ask the manager for ops
            candidate_view = [
                {
                    "id": f["id"],
                    "type": f["type"],
                    "subject": f["subject"],
                    "value": f["value"],
                    "sentiment": f.get("sentiment"),
                    "confidence": f.get("confidence"),
                }
                for f in candidates
            ]
            merge_prompt = f"""CANDIDATE FACTS (already stored; ids are authoritative):
{json.dumps(candidate_view, indent=2)}

NEW CONVERSATION (user messages only):
{json.dumps(messages_for_extraction, indent=2)}"""

            ops_json = await self._call_extraction_model(
                merge_prompt, user=user, request=request
            )
            self._log(
                f"Manager returned: {ops_json[:500] if ops_json else 'NONE/EMPTY'}",
                "info",
            )
            ops = self._parse_ops(ops_json)
            if ops is None:
                self._log(
                    "Manager returned nothing usable - store unchanged", "warning"
                )
                return

            valid_ops = self._validate_ops(ops, candidates_by_id, assistant_names)
            self._log(f"Ops: {len(ops)} returned, {len(valid_ops)} valid", "info")
            if not valid_ops:
                self._log("No ops to apply - store unchanged", "info")
                return

            # Step 3: embed the facts being written
            to_embed = [op for op in valid_ops if op["op"] in ("add", "update")]
            if to_embed:
                vectors = await self._embed_texts(
                    [self._text_for_fact(op["fact"]) for op in to_embed]
                )
                for op, vec in zip(to_embed, vectors):
                    op["_embedding"] = vec

            # Step 4: apply in one transaction
            counts = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self._db_apply_ops(user_id, valid_ops, conversation_id),
                ),
                timeout=30.0,
            )
            self._log(f"Applied ops for user {user_id[:8]}: {counts}", "info")

            # Step 5: refresh metadata + summary through the graph
            state = self._empty_state(user_id, conversation_id)
            state.pop("facts", None)
            state["_op_counts"] = counts
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: self.memory_graph.invoke(state, config)
                ),
                timeout=30.0,
            )
            self._log(
                f"Memory updated for user {user_id[:8]}: {result.get('total_facts', 0)} active facts",
                "info",
            )

        except asyncio.TimeoutError:
            self._log("Memory update timed out", "warning")
        except Exception as e:
            self._log(
                f"Failed to update memory state: {type(e).__name__}: {e}", "error"
            )
            import traceback

            self._log(f"State update traceback: {traceback.format_exc()}", "error")

    # ------------------------------------------------------------------
    # Retrieval for injection
    # ------------------------------------------------------------------

    async def _select_relevant_memories(
        self,
        user_id: str,
        messages: List[Dict[str, Any]],
        user: Optional[Dict[str, Any]] = None,
        request: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """
        Always-inject types come whole. Everything else is retrieved by cosine
        similarity to the latest user turn (top_k, above similarity_threshold),
        then optionally re-ranked by the relevance model.
        """
        loop = asyncio.get_event_loop()
        always_types = list(self.valves.always_inject_types)

        always_facts = await loop.run_in_executor(
            None, lambda: self._db_load_active_facts(user_id, types=always_types)
        )

        last_user_text = ""
        for m in reversed(messages or []):
            if m.get("role") == "user":
                last_user_text = self._message_text(m.get("content"))
                break
        if not last_user_text.strip():
            return always_facts

        try:
            query_vec = (await self._embed_texts([last_user_text]))[0]
            candidates = await loop.run_in_executor(
                None,
                lambda: self._db_search_facts(
                    user_id,
                    query_vec,
                    self.valves.retrieval_top_k,
                    exclude_types=always_types,
                    min_similarity=self.valves.similarity_threshold,
                ),
            )
        except Exception as e:
            self._log(
                f"Vector retrieval failed: {type(e).__name__}: {e} - injecting always-inject facts only",
                "warning",
            )
            return always_facts

        self._log(
            f"Vector retrieval: {len(candidates)} facts >= {self.valves.similarity_threshold} "
            f"(+ {len(always_facts)} always-inject)",
            "info",
        )
        if not candidates:
            return always_facts

        # --- Cross-encoder rerank (primary recall selector in this variant) ---
        try:
            scored = await self._rerank_facts(last_user_text, candidates)
            threshold = self.valves.rerank_score_threshold
            kept = [f for f in scored if f["rerank_score"] >= threshold]
            for f in scored:
                self._log(
                    f"Rerank score {f['rerank_score']:.6f} "
                    f"{'PASS' if f['rerank_score'] >= threshold else 'drop'} "
                    f"(cos {f.get('similarity', 0):.3f}) {f['type']}/{f['subject']}: {f['value'][:60]}",
                    "info",
                )
            self._log(
                f"Rerank: {len(candidates)} candidates → {len(kept)} at or above {threshold} "
                f"(+ {len(always_facts)} always-inject)",
                "info",
            )
            return always_facts + kept
        except Exception as e:
            self._log(
                f"Rerank failed: {type(e).__name__}: {e} - "
                + (
                    "falling back to relevance model"
                    if self.valves.relevance_filter_enabled
                    else "injecting vector candidates"
                ),
                "warning",
            )

        if not self.valves.relevance_filter_enabled:
            return always_facts + candidates

        recent_conversation = [
            {"role": m.get("role"), "content": self._message_text(m.get("content"))}
            for m in messages[-6:]
            if m.get("role") in ("user", "assistant")
        ]
        filterable = [
            {
                k: f.get(k)
                for k in ("type", "subject", "value", "sentiment", "confidence")
            }
            for f in candidates
        ]
        relevance_prompt = f"""STORED FACTS:
{json.dumps(filterable, indent=2)}

CURRENT CONVERSATION:
{json.dumps(recent_conversation, indent=2)}"""

        try:
            payload = {
                "model": self.valves.relevance_model_id,
                "messages": [{"role": "user", "content": relevance_prompt}],
                "stream": False,
                "max_tokens": self.valves.relevance_model_max_tokens,
            }
            response = await generate_chat_completion(
                request=request, form_data=payload, user=user, bypass_filter=True
            )
            response_text = None
            if isinstance(response, dict):
                choices = response.get("choices", [])
                if choices:
                    response_text = choices[0].get("message", {}).get("content", "")
            if not response_text:
                self._log(
                    "Relevance model returned empty - using vector candidates",
                    "warning",
                )
                return always_facts + candidates

            parsed = json.loads(self._clean_model_response(response_text))
            relevant = parsed.get("relevant_facts", [])
            self._log(
                f"Relevance filter: {len(candidates)} candidates → {len(relevant)} relevant "
                f"(+ {len(always_facts)} always-inject)",
                "info",
            )
            return always_facts + relevant

        except json.JSONDecodeError as e:
            self._log(
                f"Relevance model returned invalid JSON: {e} - using vector candidates",
                "warning",
            )
            return always_facts + candidates
        except Exception as e:
            self._log(
                f"Relevance filtering failed: {type(e).__name__}: {e} - using vector candidates",
                "warning",
            )
            return always_facts + candidates

    def _format_memory_context(self, memory_state: Dict[str, Any]) -> str:
        """Format memory state for injection into model context"""

        if not memory_state:
            return ""

        facts = memory_state.get("facts", [])
        if not facts:
            return ""

        if self.valves.memory_injection_format == "structured":
            parts = ["=== USER MEMORY PROFILE ===\n"]

            # Group facts by type
            facts_by_type = {}
            for fact in facts:
                fact_type = fact.get("type", "other")
                if fact_type not in facts_by_type:
                    facts_by_type[fact_type] = []
                facts_by_type[fact_type].append(fact)

            # Identity facts
            if "identity" in facts_by_type:
                parts.append("About You:")
                for f in facts_by_type["identity"][: self.valves.max_injected_memories]:
                    parts.append(f"  - {f['subject'].title()}: {f['value']}")

            # Ownership facts
            if "ownership" in facts_by_type:
                parts.append("\nYou Own:")
                for f in facts_by_type["ownership"][:5]:
                    parts.append(f"  - {f['value']}")

            # Relationship facts
            if "relationship" in facts_by_type:
                parts.append("\nRelationships:")
                for f in facts_by_type["relationship"][:5]:
                    parts.append(f"  - {f['subject'].title()}: {f['value']}")

            # Preference facts
            if "preference" in facts_by_type:
                parts.append("\nPreferences:")
                for f in sorted(
                    facts_by_type["preference"],
                    key=lambda x: x.get("confidence", 0),
                    reverse=True,
                )[:5]:
                    sentiment = f.get("sentiment", "neutral")
                    if sentiment == "positive":
                        parts.append(f"  - Likes: {f['value']}")
                    elif sentiment == "negative":
                        parts.append(f"  - Dislikes: {f['value']}")
                    else:
                        parts.append(f"  - {f['subject'].title()}: {f['value']}")

            # Skill facts
            if "skill" in facts_by_type:
                parts.append("\nSkills/Interests:")
                for f in facts_by_type["skill"][:5]:
                    parts.append(f"  - {f['value']}")

            # Goal facts
            if "goal" in facts_by_type:
                parts.append("\nGoals:")
                for f in facts_by_type["goal"][:3]:
                    parts.append(f"  - {f['value']}")

            # Event facts
            if "event" in facts_by_type:
                parts.append("\nImportant Dates:")
                for f in facts_by_type["event"][:5]:
                    parts.append(f"  - {f['subject'].title()}: {f['value']}")

            parts.append("\n=== END MEMORY PROFILE ===")
            return "\n".join(parts)

        elif self.valves.memory_injection_format == "natural":
            return f"""Based on previous conversations, I know the following about you:

{memory_state.get('memory_summary', '')}

I'll use this context to personalize my responses."""

        else:  # bullet
            return f"""Previous conversations revealed:
{memory_state.get('memory_summary', '')}"""

    # ------------------------------------------------------------------
    # Inlet / Outlet
    # ------------------------------------------------------------------

    async def inlet(
        self,
        body: Dict[str, Any],
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __request__: Optional[Request] = None,
        __model__: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Inlet: inject relevant memories into context, then upsert facts from the user's messages
        """
        self._log("=== INLET START ===", "info")

        if not __user__ or not __user__.get("id"):
            self._log("No user ID, skipping memory processing", "info")
            return body

        user_id = __user__["id"]
        conversation_id = body.get("chat_id", "default")
        self._log(
            f"Processing user={user_id[:8]}... chat={conversation_id[:8] if conversation_id else 'default'}...",
            "info",
        )

        user_obj = await self._resolve_user(__user__)

        async def status(description: str, done: bool):
            if self.valves.show_status and __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {"description": description, "done": done},
                    }
                )

        try:
            if not self._initialized:
                self._log("First run - initializing graph...", "info")
                await status("🧠 Initializing LangGraph memory system...", False)
                await self._initialize_graph()
                await status("✅ Memory system ready", True)

            await status("🔍 Loading your memories...", False)
            memory_state = await self._get_user_memory_state(user_id, conversation_id)
            total_stored = memory_state.get("total_facts", 0)
            self._log(f"Got memory state with {total_stored} facts", "info")

            messages = body.get("messages", [])

            # --- Inject memories ---
            if total_stored and messages:
                await status("🔎 Recalling relevant memories...", False)
                injected_facts = await self._select_relevant_memories(
                    user_id, messages, user=user_obj, request=__request__
                )
                self._log(
                    f"Injecting {len(injected_facts)} of {total_stored} facts", "info"
                )

                memory_context = self._format_memory_context(
                    {
                        "facts": injected_facts,
                        "total_facts": len(injected_facts),
                        "memory_summary": memory_state.get("memory_summary", ""),
                    }
                )
                if memory_context:
                    system_msg = None
                    for msg in messages:
                        if msg.get("role") == "system":
                            system_msg = msg
                            break
                    if system_msg:
                        system_msg["content"] = (
                            f"{memory_context}\n\n{system_msg['content']}"
                        )
                    else:
                        messages.insert(
                            0, {"role": "system", "content": memory_context}
                        )
                    body["messages"] = messages

                if injected_facts:
                    if len(injected_facts) < total_stored:
                        await status(
                            f"💭 Recalled {len(injected_facts)} of {total_stored} memories relevant to this chat",
                            True,
                        )
                    else:
                        await status(
                            f"💭 Recalled {len(injected_facts)} memories about you",
                            True,
                        )
                else:
                    await status("👋 Getting to know you...", True)

            # --- Upsert facts from the user's messages ---
            user_messages = [msg for msg in messages if msg.get("role") == "user"]
            self._log(
                f"Extraction check: {len(user_messages)} user messages, threshold={self.valves.extraction_threshold}",
                "info",
            )

            if len(user_messages) >= self.valves.extraction_threshold:
                self._log("Threshold met! Triggering extraction...", "info")
                await status("🧩 Updating memory graph...", False)

                # USER messages only. System messages carry the persona and assistant
                # turns carry its voice; neither may become a fact about the user.
                recent_messages = [
                    {"role": "user", "content": self._message_text(msg.get("content"))}
                    for msg in messages[-10:]
                    if msg.get("role") == "user"
                ]
                assistant_names = self._assistant_names(__model__, messages)
                self._log(
                    f"Assistant names guarded: {sorted(assistant_names)}", "debug"
                )

                self._log("Starting memory update (awaiting completion)...", "info")
                try:
                    await self._update_user_memory_state(
                        user_id,
                        conversation_id,
                        recent_messages,
                        user=user_obj,
                        request=__request__,
                        assistant_names=assistant_names,
                    )
                    self._log("Memory update completed successfully", "info")
                except Exception as update_err:
                    self._log(
                        f"Memory update FAILED: {type(update_err).__name__}: {update_err}",
                        "error",
                    )
                    import traceback

                    self._log(
                        f"Update error traceback: {traceback.format_exc()}", "error"
                    )

                await status("✨ Memories updated", True)
            else:
                self._log(
                    f"Threshold NOT met: {len(user_messages)} < {self.valves.extraction_threshold}",
                    "info",
                )

            self._log("=== INLET COMPLETE ===", "info")
        except ConnectionError as e:
            self._log(f"PostgreSQL connection error: {e}", "error")
            await status(
                "⚠️ Cannot connect to memory database. Check PostgreSQL.", True
            )
        except ImportError as e:
            self._log(f"Missing dependencies: {e}", "error")
            await status(
                "⚠️ Missing dependencies. Install: langgraph, psycopg-binary", True
            )
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
        """Outlet: no processing"""
        return body
