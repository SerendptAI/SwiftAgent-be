from langchain_core.tools import tool
from typing import Dict, Any, Annotated
import json

from langgraph.prebuilt import InjectedState
from app.services.sibyl_memory import sibyl_memory

@tool
def set_working_state(key: str, state_data: dict, state: Annotated[dict, InjectedState]) -> str:
    """Set or update a key-value working state (HOT tier memory) for the agent and session."""
    company_id = state.get("company_id", "default")
    sibyl_memory.set_state(company_id, key, state_data)
    return f"Working state for '{key}' updated successfully."

@tool
def get_working_state(key: str, state: Annotated[dict, InjectedState]) -> str:
    """Retrieve working state (HOT tier memory) by key."""
    company_id = state.get("company_id", "default")
    data = sibyl_memory.get_state(company_id, key)
    return json.dumps(data) if data else f"No working state found for '{key}'."

@tool
def remember_entity(category: str, name: str, data: dict, state: Annotated[dict, InjectedState]) -> str:
    """Save an important entity, preference, decision, or user detail (WARM tier memory)."""
    company_id = state.get("company_id", "default")
    sibyl_memory.set_entity(company_id, category, name, data)
    return f"Entity '{category}/{name}' remembered successfully."

@tool
def recall_entity(category: str, name: str, state: Annotated[dict, InjectedState]) -> str:
    """Retrieve a specific saved entity (WARM tier memory) by category and name."""
    company_id = state.get("company_id", "default")
    entity = sibyl_memory.get_entity(company_id, category, name)
    return json.dumps(entity) if entity else f"No entity found for '{category}/{name}'."

@tool
def log_event(event_description: str, state: Annotated[dict, InjectedState]) -> str:
    """Log an important event or action into the permanent history (COLD tier memory)."""
    company_id = state.get("company_id", "default")
    sibyl_memory.write_event(company_id, event_description)
    return f"Event logged to COLD memory: {event_description}"

@tool
def search_memory(query: str, state: Annotated[dict, InjectedState]) -> str:
    """Perform full-text search across all stored memory entities (FTS5 search)."""
    company_id = state.get("company_id", "default")
    results = sibyl_memory.search_entities(company_id, query)
    return json.dumps(results) if results else "No matches found."

MEMORY_TOOLS = [
    set_working_state,
    get_working_state,
    remember_entity,
    recall_entity,
    log_event,
    search_memory,
]
