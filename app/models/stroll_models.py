"""
Stroll feature models — dashboard navigation crawler & visual how-to guides.

Defines the data structures for:
- Interactive elements detected on pages
- Navigation graph (pages + transitions)
- Stroll versions (snapshots of the graph + screenshots)
- Diff logs (changes between versions)
- Configuration (per-company crawl settings)
- Query results (step-by-step visual guides)
"""

from collections import deque
from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from datetime import datetime


class BoundingBox(BaseModel):
    x: float
    y: float
    w: float
    h: float


class InteractiveElement(BaseModel):
    selector: str
    label: str                                  # raw text (innerText, aria-label, etc.)
    human_description: str = ""                 # LLM-enriched description (from vision pass)
    type: str = "nav"                           # "nav" (expands graph) | "action" (logged only)
    bbox: Optional[BoundingBox] = None


class PageNode(BaseModel):
    id: str                                     # stable page identifier
    url: str
    title: str
    page_summary: str = ""                      # LLM vision-generated summary
    elements: List[InteractiveElement] = []
    dom_hash: str = ""                          # for change detection between strolls


class Edge(BaseModel):
    from_page: str                              # page_id
    to_page: str                                # page_id
    via: InteractiveElement                     # the element that was clicked
    instruction: str = ""                       # pre-computed natural language step instruction


class NavGraph(BaseModel):
    nodes: Dict[str, PageNode] = {}             # page_id → PageNode
    edges: List[Edge] = []

    def shortest_path(self, start_id: str, end_id: str) -> Optional[List[str]]:
        """BFS shortest path returning list of page_ids from start to end."""
        if start_id == end_id:
            return [start_id]

        adj: Dict[str, List[str]] = {}
        for edge in self.edges:
            adj.setdefault(edge.from_page, []).append(edge.to_page)

        queue = deque([(start_id, [start_id])])
        visited = {start_id}

        while queue:
            current, path = queue.popleft()
            for neighbor in adj.get(current, []):
                if neighbor == end_id:
                    return path + [neighbor]
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))

        return None  # no path found

    def get_root_id(self) -> Optional[str]:
        """Return the root node (first node added, typically the dashboard home)."""
        if self.nodes:
            return next(iter(self.nodes))
        return None

    def edge_between(self, from_id: str, to_id: str) -> Optional[Edge]:
        """Find the edge connecting two pages."""
        for edge in self.edges:
            if edge.from_page == from_id and edge.to_page == to_id:
                return edge
        return None


class MovedFeature(BaseModel):
    label: str
    old_path: List[str]
    new_path: List[str]


class DiffLog(BaseModel):
    added: List[PageNode] = []
    removed: List[str] = []                     # page_ids
    moved: List[MovedFeature] = []
    unchanged_count: int = 0


class StrollVersion(BaseModel):
    id: str                                     # "stroll_v1", "stroll_v2", ...
    company_id: str
    timestamp: datetime
    graph: NavGraph
    screenshot_urls: Dict[str, str] = {}        # page_id → cloudinary URL
    diff: Optional[DiffLog] = None
    status: str = "success"                     # "success" | "failed" | "aborted"


class StrollCredentials(BaseModel):
    """Credentials for accessing authenticated dashboards."""
    login_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None              # encrypted at rest
    pre_auth_url: Optional[str] = None          # alternative: pre-authenticated URL/token


class StrollConfig(BaseModel):
    company_id: str
    dashboard_url: str
    schedule: str = "0 2 * * *"                 # cron expression (default: 2am daily)
    credentials: Optional[StrollCredentials] = None
    sandbox_mode: bool = True
    max_pages: int = 50
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class StrollConfigCreate(BaseModel):
    """Request body for creating/updating stroll configuration."""
    dashboard_url: str
    schedule: str = "0 2 * * *"
    credentials: Optional[StrollCredentials] = None
    sandbox_mode: bool = True
    max_pages: int = 50


class WidgetElement(BaseModel):
    selector: str
    label: str
    type: str
    href: str
    bbox: BoundingBox


class WidgetPageNode(BaseModel):
    url: str
    title: str
    screenshot_base64: str
    elements: List[WidgetElement]


class WidgetStrollReport(BaseModel):
    dashboard_url: str
    nodes: List[WidgetPageNode]


class NavigationStep(BaseModel):
    step: int
    page_title: str
    instruction: str
    screenshot_url: str
    highlight: Optional[BoundingBox] = None


class FindFeatureResult(BaseModel):
    path_summary: List[str]                     # ["Dashboard", "Settings", "Billing"]
    steps: List[NavigationStep]


class StrollJobStatus(BaseModel):
    company_id: str
    status: str                                 # "idle" | "running" | "completed" | "failed"
    last_run: Optional[datetime] = None
    last_version_id: Optional[str] = None
    error: Optional[str] = None
