#!/usr/bin/env python3
"""
Test all imports and initialization for LangGraph memory filter.

Run this script to verify all dependencies are correctly installed:
    python test_imports.py

Requirements:
    pip install "langgraph>=1.0.0" langgraph-checkpoint-postgres "psycopg[binary]" psycopg-pool
"""

import sys

def test_imports():
    """Test all required imports."""
    print("Testing imports...")
    
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        print("✅ PostgresSaver")
    except Exception as e:
        print(f"❌ PostgresSaver: {e}")
        return False
    
    try:
        from langgraph.graph import StateGraph, START, END
        print("✅ StateGraph, START, END")
    except Exception as e:
        print(f"❌ StateGraph: {e}")
        return False
    
    try:
        from typing import TypedDict, List, Annotated
        print("✅ typing (TypedDict, List, Annotated)")
    except Exception as e:
        print(f"❌ typing: {e}")
        return False
    
    try:
        from typing_extensions import Required
        print("✅ typing_extensions (Required)")
    except Exception as e:
        print(f"❌ typing_extensions: {e}")
        return False
    
    try:
        from langchain_core.messages import BaseMessage
        print("✅ langchain_core.messages")
    except Exception as e:
        print(f"❌ langchain_core.messages: {e}")
        return False
    
    try:
        from psycopg_pool import ConnectionPool
        print("✅ psycopg_pool (ConnectionPool)")
    except Exception as e:
        print(f"❌ psycopg_pool: {e}")
        print("   Install with: pip install psycopg-pool")
        return False
    
    try:
        import psycopg
        print(f"✅ psycopg (version {psycopg.__version__})")
    except Exception as e:
        print(f"❌ psycopg: {e}")
        print("   Install with: pip install 'psycopg[binary]'")
        return False
    
    return True


def test_postgres_saver_initialization():
    """Test PostgresSaver initialization (without actual connection)."""
    print("\nTesting PostgresSaver initialization...")
    
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        
        # Test that from_conn_string method exists and is callable
        assert hasattr(PostgresSaver, 'from_conn_string'), "from_conn_string method missing"
        assert callable(PostgresSaver.from_conn_string), "from_conn_string not callable"
        
        print("✅ PostgresSaver.from_conn_string exists and is callable")
        
        # Note: We can't test actual connection without PostgreSQL running
        # but we can verify the method signature
        import inspect
        sig = inspect.signature(PostgresSaver.from_conn_string)
        print(f"✅ from_conn_string signature: {sig}")
        
        return True
        
    except Exception as e:
        print(f"❌ PostgresSaver initialization test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_graph_definition():
    """Test defining a simple graph structure."""
    print("\nTesting graph definition...")
    
    try:
        from langgraph.graph import StateGraph, START, END
        from typing import TypedDict, List
        
        # Define a simple state (matching filter structure)
        class SimpleState(TypedDict):
            messages: List[str]
            user_id: str
        
        # Create workflow
        workflow = StateGraph(SimpleState)
        
        # Add a simple node
        def test_node(state: SimpleState) -> SimpleState:
            return state
        
        workflow.add_node("test", test_node)
        workflow.add_edge(START, "test")
        workflow.add_edge("test", END)
        
        print("✅ Graph definition successful")
        
        # Compile (without checkpointer - that requires PostgreSQL)
        graph = workflow.compile()
        print("✅ Graph compilation successful")
        
        return True
        
    except Exception as e:
        print(f"❌ Graph definition failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("LangGraph Memory Filter - Dependency Test")
    print("=" * 60)
    print("\nExpected installation command:")
    print('  pip install "langgraph>=1.0.0" langgraph-checkpoint-postgres "psycopg[binary]" psycopg-pool')
    print()
    
    success = True
    
    if not test_imports():
        success = False
    
    if not test_postgres_saver_initialization():
        success = False
    
    if not test_graph_definition():
        success = False
    
    print("\n" + "=" * 60)
    if success:
        print("✅ ALL TESTS PASSED")
        print("=" * 60)
        sys.exit(0)
    else:
        print("❌ SOME TESTS FAILED")
        print("\nTo install dependencies:")
        print('  pip install "langgraph>=1.0.0" langgraph-checkpoint-postgres "psycopg[binary]" psycopg-pool')
        print("=" * 60)
        sys.exit(1)
