"""
title: LangGraph Memory Graph - Enterprise
author: beaudamore
date: 2026-01-10
version: 2.1.0
license: MIT
description: LLM-powered semantic memory merge with PostgreSQL persistence
required_open_webui_version: >= 0.5.0
requirements: psycopg[binary,pool]>=3.1.0, langgraph-checkpoint-postgres>=1.0.0, langgraph>=0.2.0
"""

from __future__ import annotations

import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, TypedDict
from urllib.parse import quote_plus

from pydantic import BaseModel, Field
from fastapi import Request

# CRITICAL: Import psycopg BEFORE any LangGraph imports
# psycopg_binary requires psycopg to be imported first
import psycopg
import psycopg_pool

# LangGraph imports
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import StateGraph, END

# Open WebUI imports
from open_webui.main import app as webui_app
from open_webui.models.users import UserModel, Users
from open_webui.utils.chat import generate_chat_completion

# Set up logging
logger = logging.getLogger("openwebui.filters.langgraph_memory")
logger.setLevel(logging.INFO)

# Schema version - increment when making breaking changes to data structure
SCHEMA_VERSION = 4

# Migration history for documentation and potential rollback
SCHEMA_MIGRATIONS = [
    {
        "version": 1,
        "date": "2026-01-09",
        "description": "Initial schema with first_mentioned/last_updated fields",
        "changes": []
    },
    {
        "version": 2,
        "date": "2026-01-09",
        "description": "Preference evolution tracking - keep all data points",
        "changes": [
            {"type": "field_rename", "entity": "Preference", "old": "first_mentioned", "new": "mentioned_at"},
            {"type": "field_remove", "entity": "Preference", "field": "last_updated"},
            {"type": "field_add", "entity": "Preference", "field": "context", "default": None},
            {"type": "behavior_change", "entity": "Preference", "description": "No longer deduplicate - keep all entries for evolution tracking"}
        ]
    },
    {
        "version": 3,
        "date": "2026-01-10",
        "description": "Simplified to flexible fact-based schema",
        "changes": [
            {"type": "schema_change", "description": "Replaced rigid types with generic Fact"},
            {"type": "behavior_change", "description": "Facts deduplicated by (type, subject, value)"}
        ]
    },
    {
        "version": 4,
        "date": "2026-01-10",
        "description": "LLM-powered semantic merge replaces code-based deduplication",
        "changes": [
            {"type": "behavior_change", "description": "LLM merges existing facts with new extractions"},
            {"type": "node_removed", "description": "Removed deduplicate_memories_node"}
        ]
    }
]


# ============================================================================
# Graph State Definitions - Flexible Fact-Based Approach
# ============================================================================

class Fact(TypedDict):
    """
    A single fact about the user - flexible and generic.
    
    Types: identity, preference, ownership, relationship, goal, skill, event
    But these are suggestions - any type string is valid.
    """
    type: str  # identity, preference, ownership, relationship, goal, skill, event, etc.
    subject: str  # what this fact is about (name, vehicle, food, wife, etc.)
    value: str  # the specific information
    sentiment: Optional[str]  # positive, negative, neutral, or None
    confidence: float  # 0.0 to 1.0
    first_mentioned: str  # ISO datetime
    last_updated: str  # ISO datetime


class MemoryGraphState(TypedDict):
    """Complete state of the user's memory graph - simplified fact-based"""
    user_id: str
    conversation_id: str
    
    # All memories stored as flexible facts
    facts: List[Fact]
    
    # Temporary input for extraction (NOT persisted - cleared after processing)
    _messages_to_process: List[Dict[str, str]]
    
    # Metadata
    last_updated: str
    total_facts: int
    memory_summary: str


class MemoryExtraction(BaseModel):
    """Merged memory facts from LLM - replaces all existing facts"""
    facts: List[Dict[str, Any]] = Field(default_factory=list)


# ============================================================================
# Filter Implementation
# ============================================================================

class Filter:
    class Valves(BaseModel):
        """Configuration for LangGraph Memory Filter"""
        
        # Execution Priority
        priority: int = Field(
            default=10,
            description="Filter execution priority. Lower numbers run first. Safety filters (antivirus, content policy, prompt injection, llamaguard) should run BEFORE memory (e.g., -100 to -10), so set this to positive (10-50) to run after safety checks pass."
        )
        
        # PostgreSQL Configuration
        postgres_host: str = Field(
            default="langgraph-postgres",
            description="PostgreSQL host (docker service name or IP)"
        )
        postgres_port: int = Field(
            default=5432,
            description="PostgreSQL port"
        )
        postgres_database: str = Field(
            default="langgraph_memory",
            description="PostgreSQL database name"
        )
        postgres_user: str = Field(
            default="langgraph",
            description="PostgreSQL username"
        )
        postgres_password: str = Field(
            default="langgraph_password_change_me",
            description="PostgreSQL password (CHANGE IN PRODUCTION!)"
        )
        
        # LLM Configuration for Extraction
        extraction_model_id: str = Field(
            default="memory-manager",
            description="OpenWebUI Model ID for memory extraction. Model must use included prompt."
        )
        extraction_model_temperature: float = Field(
            default=0.1,
            description="Temperature for extraction model (lower = more consistent JSON)"
        )
        extraction_model_max_tokens: int = Field(
            default=1000,
            description="Max tokens for extraction model response"
        )
        
        # Memory Processing Configuration
        extraction_threshold: int = Field(
            default=1,
            description="Number of user messages before triggering memory extraction (1 = extract every message)"
        )
        
        # Retrieval Configuration
        max_injected_memories: int = Field(
            default=10,
            description="Maximum number of memory facts to inject into context"
        )
        memory_injection_format: Literal["structured", "natural", "bullet"] = Field(
            default="structured",
            description="Format for injecting memories into context"
        )
        
        # UI Configuration
        show_status: bool = Field(
            default=True,
            description="Show memory processing status messages"
        )
        show_injected_memories: bool = Field(
            default=False,
            description="Show which memories were injected (debug mode)"
        )
        
        # Debug
        debug_mode: bool = Field(
            default=False,
            description="Enable detailed debug logging"
        )

    class UserValves(BaseModel):
        """Per-user configuration"""
        enabled: bool = Field(
            default=True,
            description="Enable LangGraph memory for this user"
        )
        show_status: bool = Field(
            default=True,
            description="Show status messages for this user"
        )

    def __init__(self):
        self.name = "LangGraph Memory Graph"
        self.valves = self.Valves()
        self.memory_graph = None
        self.checkpointer = None
        self._pool = None
        self._initialized = False
        # Extraction result is computed in async _update_user_memory_state 
        # and processed by sync _extract_information_node
        self._extraction_result = None
        
    def _log(self, message: str, level: str = "info"):
        """Centralized logging"""
        if level == "debug" and not self.valves.debug_mode:
            return
        # Print to stdout for Docker logs visibility
        print(f"[LangGraph Memory] [{level.upper()}] {message}", flush=True)
        getattr(logger, level, logger.info)(f"[LangGraph Memory] {message}")

    async def _initialize_graph(self):
        """Initialize LangGraph with PostgreSQL checkpointer"""
        if self._initialized:
            return
            
        try:
            # Build PostgreSQL connection string
            conn_string = (
                f"postgresql://{self.valves.postgres_user}:"
                f"{quote_plus(self.valves.postgres_password)}@"
                f"{self.valves.postgres_host}:{self.valves.postgres_port}/"
                f"{self.valves.postgres_database}"
            )
            
            self._log(f"Connecting to PostgreSQL at {self.valves.postgres_host}:{self.valves.postgres_port}")
            
            # Initialize PostgreSQL checkpointer using connection pool
            # PostgresSaver.from_conn_string() returns a context manager, so we need to 
            # create a persistent connection pool instead
            try:
                # Create a connection pool that persists
                self._pool = psycopg_pool.ConnectionPool(
                    conninfo=conn_string,
                    min_size=1,
                    max_size=5,
                    open=True,
                )
                self._log("PostgreSQL connection pool created", "debug")
                
                # Create checkpointer from pool
                self.checkpointer = PostgresSaver(self._pool)
                self._log("PostgreSQL checkpointer created successfully", "debug")
                
                # Test the connection pool with a simple query
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
                # PostgresSaver.setup() is a classmethod that creates tables
                # We need to create a temporary saver with autocommit connection
                with psycopg.connect(conn_string, autocommit=True) as setup_conn:
                    setup_saver = PostgresSaver(conn=setup_conn)
                    setup_saver.setup()
                    
                    # Create schema migrations table if it doesn't exist
                    with setup_conn.cursor() as cur:
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS schema_migrations (
                                version INTEGER PRIMARY KEY,
                                applied_at TIMESTAMPTZ DEFAULT NOW(),
                                description TEXT,
                                changes JSONB
                            )
                        """)
                        
                        # Check current schema version
                        cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
                        current_version = cur.fetchone()[0]
                        
                        # Apply any pending migrations
                        if current_version < SCHEMA_VERSION:
                            self._log(f"Schema upgrade needed: v{current_version} → v{SCHEMA_VERSION}", "info")
                            for migration in SCHEMA_MIGRATIONS:
                                if migration["version"] > current_version:
                                    cur.execute(
                                        "INSERT INTO schema_migrations (version, description, changes) VALUES (%s, %s, %s)",
                                        (migration["version"], migration["description"], json.dumps(migration["changes"]))
                                    )
                                    self._log(f"Applied migration v{migration['version']}: {migration['description']}", "info")
                        else:
                            self._log(f"Schema is current (v{current_version})", "debug")
                        
                self._log("PostgreSQL checkpoint tables initialized", "debug")
            except Exception as e:
                # Tables might already exist, which is fine
                if "already exists" in str(e).lower():
                    self._log("Checkpoint tables already exist", "debug")
                else:
                    self._log(f"Failed to setup checkpoint tables: {e}", "error")
                    raise RuntimeError(f"Failed to initialize database tables: {e}")
            
            # Create the memory graph
            try:
                self.memory_graph = self._create_memory_graph()
                self._log("Memory graph workflow compiled successfully", "debug")
            except Exception as e:
                self._log(f"Failed to create memory graph: {e}", "error")
                raise RuntimeError(f"Failed to create LangGraph workflow: {e}")
            
            self._initialized = True
            self._log("LangGraph memory system initialized with PostgreSQL", "info")
            
        except ImportError as e:
            self._log(f"Failed to import LangGraph dependencies: {e}. "
                     f"Ensure dependencies are installed: "
                     f"pip install langgraph>=1.0.0 langgraph-checkpoint-postgres psycopg[binary] psycopg-pool", 
                     "error")
            raise
        except Exception as e:
            self._log(f"Failed to initialize LangGraph: {e}", "error")
            raise

    def _create_memory_graph(self) -> StateGraph:
        """Create the LangGraph workflow for memory management"""
        
        workflow = StateGraph(MemoryGraphState)
        
        # Add nodes for memory operations
        # Note: No deduplicate node - LLM handles merge/dedup in extraction
        workflow.add_node("process_merged", self._process_merged_facts_node)
        workflow.add_node("update_memory", self._update_memory_store_node)
        workflow.add_node("summarize", self._create_summary_node)
        
        # Define workflow edges
        workflow.set_entry_point("process_merged")
        workflow.add_edge("process_merged", "update_memory")
        workflow.add_edge("update_memory", "summarize")
        workflow.add_edge("summarize", END)
        
        # Compile with PostgreSQL checkpointer
        return workflow.compile(checkpointer=self.checkpointer)

    def _process_merged_facts_node(self, state: MemoryGraphState) -> MemoryGraphState:
        """
        Process LLM-merged facts into state (sync for LangGraph).
        
        The LLM has already merged existing facts with new extractions.
        This node REPLACES all facts with the merged result.
        """
        
        self._log("=== PROCESS_MERGED NODE ENTERED ===", "info")
        
        # Check for merged data (set by _update_user_memory_state BEFORE invoke)
        merged_data = getattr(self, '_extraction_result', None)
        
        self._log(f"Merged data available: {merged_data is not None}", "info")
        
        if not merged_data:
            self._log("No merged data found - keeping existing facts", "info")
            return state
        
        try:
            # Parse the merged facts
            extraction = MemoryExtraction.model_validate(merged_data)
            self._log(f"Parsed merged result: {len(extraction.facts)} facts", "info")
            
            now = datetime.now(timezone.utc).isoformat()
            valid_facts = []
            
            for fact in extraction.facts:
                # Ensure required fields exist
                if not fact.get("type") or not fact.get("subject"):
                    self._log(f"Skipping invalid fact (missing type/subject): {fact}", "warning")
                    continue
                
                # Add/update timestamps
                if "first_mentioned" not in fact:
                    fact["first_mentioned"] = now
                fact["last_updated"] = now
                
                # Ensure confidence has a default
                if "confidence" not in fact:
                    fact["confidence"] = 0.8
                
                valid_facts.append(fact)
            
            # REPLACE all facts with merged result (LLM handled dedup)
            state["facts"] = valid_facts
            self._log(f"Replaced facts with {len(valid_facts)} merged facts", "info")
        
        except Exception as e:
            self._log(f"Failed to process merged data: {type(e).__name__}: {e}", "error")
            import traceback
            self._log(f"Merge processing traceback: {traceback.format_exc()}", "error")
        finally:
            # Clear the extraction result
            self._extraction_result = None
        
        # Clear messages after processing
        state["_messages_to_process"] = []
        
        self._log(f"=== PROCESS_MERGED NODE COMPLETE: {len(state.get('facts', []))} facts ===", "info")
        
        return state

    def _update_memory_store_node(self, state: MemoryGraphState) -> MemoryGraphState:
        """Update metadata (sync for LangGraph)"""
        
        self._log("=== UPDATE_MEMORY_STORE NODE ENTERED ===", "info")
        
        # Update timestamp
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        
        # Count total facts
        state["total_facts"] = len(state.get("facts", []))
        
        self._log(f"Updated total_facts to {state['total_facts']}", "info")
        
        return state

    def _create_summary_node(self, state: MemoryGraphState) -> MemoryGraphState:
        """Create natural language summary of all memories, grouped by type"""
        
        self._log("=== SUMMARY NODE ENTERED ===", "info")
        
        # Group facts by type
        facts_by_type = {}
        for fact in state.get("facts", []):
            fact_type = fact.get("type", "other")
            if fact_type not in facts_by_type:
                facts_by_type[fact_type] = []
            facts_by_type[fact_type].append(fact)
        
        summary_parts = []
        
        # Identity facts (name, age, location, job, etc.)
        if "identity" in facts_by_type:
            identity_texts = []
            for f in facts_by_type["identity"]:
                identity_texts.append(f"{f['subject']}: {f['value']}")
            summary_parts.append("Identity: " + ", ".join(identity_texts))
        
        # Ownership facts
        if "ownership" in facts_by_type:
            own_texts = [f"{f['subject']}: {f['value']}" for f in facts_by_type["ownership"]]
            summary_parts.append("Owns: " + ", ".join(own_texts))
        
        # Relationship facts
        if "relationship" in facts_by_type:
            rel_texts = [f"{f['subject']}: {f['value']}" for f in facts_by_type["relationship"]]
            summary_parts.append("Relationships: " + ", ".join(rel_texts))
        
        # Preference facts (with sentiment)
        if "preference" in facts_by_type:
            pref_texts = []
            for f in sorted(facts_by_type["preference"], key=lambda x: x.get("confidence", 0), reverse=True):
                sentiment = f.get("sentiment", "neutral")
                if sentiment == "positive":
                    pref_texts.append(f"likes {f['value']}")
                elif sentiment == "negative":
                    pref_texts.append(f"dislikes {f['value']}")
                else:
                    pref_texts.append(f['value'])
            summary_parts.append("Preferences: " + ", ".join(pref_texts[:5]))
        
        # Goal facts
        if "goal" in facts_by_type:
            goal_texts = [f['value'] for f in facts_by_type["goal"]]
            summary_parts.append("Goals: " + ", ".join(goal_texts[:3]))
        
        # Skill facts
        if "skill" in facts_by_type:
            skill_texts = [f"{f['subject']}: {f['value']}" for f in facts_by_type["skill"]]
            summary_parts.append("Skills: " + ", ".join(skill_texts[:5]))
        
        # Event facts
        if "event" in facts_by_type:
            event_texts = [f"{f['subject']}: {f['value']}" for f in facts_by_type["event"]]
            summary_parts.append("Events: " + ", ".join(event_texts[:5]))
        
        state["memory_summary"] = "\n".join(summary_parts)
        self._log(f"Summary generated: {len(summary_parts)} sections", "info")
        
        return state

    async def _call_extraction_model(
        self, 
        prompt: str, 
        user: Optional[Dict[str, Any]] = None,
        request: Optional[Any] = None
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
                self._log("WARNING: user is None - generate_chat_completion may fail!", "error")
            if request is None:
                self._log("WARNING: request is None - generate_chat_completion may fail!", "error")
            
            # Build payload for generate_chat_completion
            payload = {
                "model": self.valves.extraction_model_id,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "temperature": self.valves.extraction_model_temperature,
                "max_tokens": self.valves.extraction_model_max_tokens,
            }
            
            self._log(f"Extraction payload: model={payload['model']}, temp={payload['temperature']}, prompt_len={len(prompt)}", "info")
            
            # Call OpenWebUI's internal API (bypasses filters to avoid recursion)
            self._log("Calling generate_chat_completion...", "info")
            response = await generate_chat_completion(
                request=request,
                form_data=payload,
                user=user,
                bypass_filter=True,
            )
            self._log(f"generate_chat_completion returned type: {type(response)}", "info")
            
            # Parse response
            if isinstance(response, dict):
                self._log(f"Response keys: {list(response.keys())}", "info")
                choices = response.get("choices", [])
                if not choices:
                    self._log(f"No choices in extraction model response. Full response: {response}", "error")
                    return None
                
                message = choices[0].get("message", {})
                response_text = message.get("content", "")
                
                if not response_text:
                    self._log("Extraction model returned empty content", "warning")
                    return None
                
                self._log(f"Extraction model response length: {len(response_text)} chars", "debug")
                return response_text
            else:
                self._log(f"Unexpected response type from extraction model: {type(response)}. Response: {response}", "error")
                return None
            
        except Exception as e:
            self._log(f"Extraction model call failed: {type(e).__name__}: {e}", "error")
            import traceback
            self._log(f"Extraction traceback: {traceback.format_exc()}", "debug")
            return None

    async def _get_user_memory_state(self, user_id: str, conversation_id: str) -> Dict[str, Any]:
        """Retrieve user's memory graph state from PostgreSQL"""
        
        if not self._initialized:
            await self._initialize_graph()
        
        # Always use user_id as thread - memories are USER-WIDE, not per-conversation
        # (Chat already maintains conversation context by design)
        config = {
            "configurable": {
                "thread_id": user_id
            }
        }
        
        try:
            # Get current state from checkpointer
            # Run in executor to avoid blocking the async event loop
            self._log(f"Retrieving memory state for user {user_id[:8]}...", "debug")
            
            loop = asyncio.get_event_loop()
            snapshot = await asyncio.wait_for(
                loop.run_in_executor(
                    None, 
                    lambda: self.memory_graph.get_state(config)
                ),
                timeout=10.0  # 10 second timeout
            )
            
            if snapshot and snapshot.values:
                self._log(f"Found existing memory state with {snapshot.values.get('total_facts', 0)} facts", "debug")
                return snapshot.values
            
            # Initialize new state
            self._log(f"Initializing new memory state for user {user_id[:8]}...", "debug")
            return {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "facts": [],
                "_messages_to_process": [],
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "total_facts": 0,
                "memory_summary": ""
            }
        
        except asyncio.TimeoutError:
            self._log("Memory state retrieval timed out after 10 seconds", "warning")
            return {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "facts": [],
                "_messages_to_process": [],
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "total_facts": 0,
                "memory_summary": ""
            }
        except Exception as e:
            self._log(f"Failed to retrieve memory state: {type(e).__name__}: {e}", "error")
            import traceback
            self._log(f"State retrieval traceback: {traceback.format_exc()}", "debug")
            # Return empty state on error
            return {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "facts": [],
                "_messages_to_process": [],
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "total_facts": 0,
                "memory_summary": ""
            }

    async def _update_user_memory_state(
        self,
        user_id: str,
        conversation_id: str,
        new_messages: List[Dict[str, str]],
        user: Optional[Dict[str, Any]] = None,
        request: Optional[Any] = None
    ):
        """Update user's memory graph with new conversation messages"""
        
        if not self._initialized:
            await self._initialize_graph()
        
        # Always use user_id as thread - memories are USER-WIDE, not per-conversation
        config = {
            "configurable": {
                "thread_id": user_id
            }
        }
        
        try:
            # Get current state
            self._log(f"Starting memory update for user {user_id[:8]}...", "info")
            current_state = await self._get_user_memory_state(user_id, conversation_id)
            existing_facts = current_state.get("facts", [])
            
            # =====================================================================
            # STEP 1: Call LLM to MERGE existing facts with new conversation
            # LLM handles extraction + deduplication + conflict resolution
            # =====================================================================
            self._log(f"Step 1: Merging {len(existing_facts)} existing facts with {len(new_messages)} new messages...", "info")
            
            # Format existing facts for prompt
            existing_facts_json = json.dumps(existing_facts, indent=2) if existing_facts else "[]"
            
            # Build merge prompt - LLM returns complete merged fact list
            merge_prompt = f"""EXISTING FACTS:
{existing_facts_json}

NEW CONVERSATION:
{json.dumps(new_messages, indent=2)}

Return the COMPLETE MERGED fact list as JSON. Update existing facts if changed, remove if contradicted, add new ones.

{{"facts": [...]}}"""

            # Call extraction model (async - works properly here!)
            merged_json = await self._call_extraction_model(
                merge_prompt, 
                user=user, 
                request=request
            )
            
            self._log(f"Merge model returned: {merged_json[:500] if merged_json else 'NONE/EMPTY'}", "info")
            
            # Parse merge result
            merged_data = None
            if merged_json:
                # Clean JSON (remove markdown code blocks if present)
                cleaned_json = merged_json.strip()
                if cleaned_json.startswith("```"):
                    lines = cleaned_json.split("\n")
                    cleaned_json = "\n".join(lines[1:-1] if len(lines) > 2 else lines)
                if cleaned_json.startswith("json"):
                    cleaned_json = cleaned_json[4:].strip()
                
                self._log(f"Cleaned merge JSON: {cleaned_json[:500]}", "info")
                
                try:
                    merged_data = json.loads(cleaned_json)
                    self._log(f"Parsed merged data: {len(merged_data.get('facts', []))} facts", "info")
                except json.JSONDecodeError as e:
                    self._log(f"Failed to parse merge JSON: {e}", "error")
            else:
                self._log("Merge model returned empty - keeping existing facts", "warning")
            
            # =====================================================================
            # STEP 2: Store merged result for graph node to process
            # =====================================================================
            self._extraction_result = merged_data
            
            # Set messages in state (for tracking, cleared after extraction node)
            current_state["_messages_to_process"] = new_messages
            
            # =====================================================================
            # STEP 3: Invoke graph to store merged facts and update summary
            # =====================================================================
            self._log("Step 2: Invoking memory graph workflow...", "info")
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self.memory_graph.invoke(current_state, config)
                ),
                timeout=30.0  # 30 second timeout for full workflow
            )
            
            self._log("Graph workflow completed!", "info")
            self._log(f"Memory updated for user {user_id[:8]}: {result.get('total_facts', 0)} facts stored", "info")
        
        except asyncio.TimeoutError:
            self._log("Memory update timed out after 30 seconds", "warning")
        except Exception as e:
            self._log(f"Failed to update memory state: {type(e).__name__}: {e}", "error")
            import traceback
            self._log(f"State update traceback: {traceback.format_exc()}", "error")

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
                for f in facts_by_type["identity"][:self.valves.max_injected_memories]:
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
                for f in sorted(facts_by_type["preference"], key=lambda x: x.get("confidence", 0), reverse=True)[:5]:
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

    async def inlet(
        self,
        body: Dict[str, Any],
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __request__: Optional[Request] = None,
    ) -> Dict[str, Any]:
        """
        Inlet: Extract memories from user message and inject relevant memories into context
        """
        self._log("=== INLET START ===", "info")
        
        if not __user__ or not __user__.get("id"):
            self._log("No user ID, skipping memory processing", "info")
            return body
        
        user_id = __user__["id"]
        conversation_id = body.get("chat_id", "default")
        self._log(f"Processing user={user_id[:8]}... chat={conversation_id[:8] if conversation_id else 'default'}...", "info")
        
        try:
            # Initialize if needed
            if not self._initialized:
                self._log("First run - initializing graph...", "info")
                if self.valves.show_status and __event_emitter__:
                    await __event_emitter__({
                        "type": "status",
                        "data": {
                            "description": "🧠 Initializing LangGraph memory system...",
                            "done": False
                        }
                    })
                
                await self._initialize_graph()
                
                if self.valves.show_status and __event_emitter__:
                    await __event_emitter__({
                        "type": "status",
                        "data": {
                            "description": "✅ Memory system ready",
                            "done": True
                        }
                    })
            
            # Get user's memory state
            if self.valves.show_status and __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": "🔍 Loading your memories...",
                        "done": False
                    }
                })
            
            self._log("Calling _get_user_memory_state...", "info")
            memory_state = await self._get_user_memory_state(user_id, conversation_id)
            self._log(f"Got memory state with {memory_state.get('total_facts', 0)} facts", "info")
            
            # Always inject memories into context (that's the point of this filter)
            if memory_state:
                memory_context = self._format_memory_context(memory_state)
                
                if memory_context:
                    # Inject into system message
                    messages = body.get("messages", [])
                    if messages:
                        # Find or create system message
                        system_msg = None
                        for msg in messages:
                            if msg.get("role") == "system":
                                system_msg = msg
                                break
                        
                        if system_msg:
                            system_msg["content"] = f"{memory_context}\n\n{system_msg['content']}"
                        else:
                            messages.insert(0, {
                                "role": "system",
                                "content": memory_context
                            })
                        
                        body["messages"] = messages
                        
                        if self.valves.show_status and __event_emitter__:
                            total_facts = memory_state.get('total_facts', 0)
                            if total_facts > 0:
                                await __event_emitter__({
                                    "type": "status",
                                    "data": {
                                        "description": f"💭 Recalled {total_facts} memories about you",
                                        "done": True
                                    }
                                })
                            else:
                                await __event_emitter__({
                                    "type": "status",
                                    "data": {
                                        "description": "👋 Getting to know you...",
                                        "done": True
                                    }
                                })
            
            # Extract memories from conversation (that's the point of this filter)
            messages = body.get("messages", [])
            user_messages = [msg for msg in messages if msg.get("role") == "user"]
            self._log(f"Extraction check: {len(user_messages)} user messages, threshold={self.valves.extraction_threshold}", "info")
            
            # Check if we should extract (threshold met)
            if len(user_messages) >= self.valves.extraction_threshold:
                self._log("Threshold met! Triggering extraction...", "info")
                if self.valves.show_status and __event_emitter__:
                    await __event_emitter__({
                        "type": "status",
                        "data": {
                            "description": "🧩 Updating memory graph...",
                            "done": False
                        }
                    })
                
                # Get recent messages for context
                recent_messages = [
                    {"role": msg.get("role"), "content": msg.get("content")}
                    for msg in messages[-10:]
                ]
                
                # Update memory - await it to catch errors (extraction is important!)
                self._log("Starting memory update (awaiting completion)...", "info")
                try:
                    await self._update_user_memory_state(
                        user_id, 
                        conversation_id, 
                        recent_messages,
                        user=__user__,
                        request=__request__
                    )
                    self._log("Memory update completed successfully", "info")
                except Exception as update_err:
                    self._log(f"Memory update FAILED: {type(update_err).__name__}: {update_err}", "error")
                    import traceback
                    self._log(f"Update error traceback: {traceback.format_exc()}", "error")
                
                if self.valves.show_status and __event_emitter__:
                    await __event_emitter__({
                        "type": "status",
                        "data": {
                            "description": "✨ Memories updated",
                            "done": True
                        }
                    })
            else:
                self._log(f"Threshold NOT met: {len(user_messages)} < {self.valves.extraction_threshold}", "info")
            
            self._log("=== INLET COMPLETE ===", "info")
        except ConnectionError as e:
            self._log(f"PostgreSQL connection error: {e}", "error")
            if self.valves.show_status and __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": "⚠️ Cannot connect to memory database. Check PostgreSQL.",
                        "done": True
                    }
                })
        except ImportError as e:
            self._log(f"Missing dependencies: {e}", "error")
            if self.valves.show_status and __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": "⚠️ Missing dependencies. Install: langgraph, psycopg-binary",
                        "done": True
                    }
                })
        except Exception as e:
            self._log(f"Inlet processing error: {type(e).__name__}: {e}", "error")
            import traceback
            self._log(f"Inlet traceback: {traceback.format_exc()}", "error")
            if self.valves.show_status and __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": f"⚠️ Memory system error: {str(e)[:60]}",
                        "done": True
                    }
                })
        
        return body

    async def outlet(
        self,
        body: Dict[str, Any],
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __request__: Optional[Request] = None,
    ) -> Dict[str, Any]:
        """
        Outlet: Minimal processing, just logging
        """
        
        # Could add response analysis here if needed
        return body
                        