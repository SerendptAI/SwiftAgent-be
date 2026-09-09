import os
import logging
from typing import Dict, Any, List
from sibyl_memory_client import MemoryClient

logger = logging.getLogger(__name__)

class SibylMemoryService:
    def __init__(self, base_dir: str = "/home/lambda/SwiftAgent-be/app/data/memory"):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)
        self.clients: Dict[str, MemoryClient] = {}

    def get_client(self, company_id: str) -> MemoryClient:
        """Get or initialize the MemoryClient for a specific company."""
        if company_id not in self.clients:
            db_path = os.path.join(self.base_dir, f"company_{company_id}.db")
            self.clients[company_id] = MemoryClient.local(db_path)
        return self.clients[company_id]

    def set_state(self, company_id: str, key: str, body: Dict[str, Any]) -> None:
        logger.info(f"[Sibyl] Setting HOT state for {company_id}: {key}")
        client = self.get_client(company_id)
        client.set_state(key, body)

    def get_state(self, company_id: str, key: str) -> Dict[str, Any]:
        client = self.get_client(company_id)
        return client.get_state(key)

    def set_entity(self, company_id: str, category: str, name: str, body: Dict[str, Any]) -> None:
        logger.info(f"[Sibyl] Setting WARM entity for {company_id}: {category}/{name}")
        client = self.get_client(company_id)
        client.set_entity(category, name, body)

    def get_entity(self, company_id: str, category: str, name: str) -> Dict[str, Any]:
        client = self.get_client(company_id)
        return client.get_entity(category, name)

    def write_event(self, company_id: str, event_description: str) -> None:
        logger.info(f"[Sibyl] Logging COLD event for {company_id}")
        client = self.get_client(company_id)
        client.write_event(acted=[event_description])

    def search_entities(self, company_id: str, query: str) -> List[Any]:
        logger.info(f"[Sibyl] Searching FTS5 for {company_id} with query: {query}")
        client = self.get_client(company_id)
        return client.search_entities(query)

sibyl_memory = SibylMemoryService()
