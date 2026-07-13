from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from datetime import datetime

class FormType(str, Enum):
    WEBSITE = "website"
    ONLINE = "online"

# ── Create Models ──

class WebsiteFormCreate(BaseModel):
    website_link: str
    alert_email: str
    tags: Optional[List[str]] = []

class OnlineFormCreate(BaseModel):
    form_image: str
    form_title: str
    tags: Optional[List[str]] = []

class FormUpdate(BaseModel):
    website_link: Optional[str] = None
    alert_email: Optional[str] = None
    form_image: Optional[str] = None
    form_title: Optional[str] = None
    tags: Optional[List[str]] = None

# ── Response Models ──

class FormResponse(BaseModel):
    """Base form response — used for list/get operations."""
    id: str
    company_id: str
    type: FormType
    tags: List[str] = []
    created_at: datetime
    updated_at: datetime

    # Website form fields
    website_link: Optional[str] = None
    alert_email: Optional[str] = None

    # Online form fields
    form_image: Optional[str] = None
    form_title: Optional[str] = None

class WebsiteFormCreateResponse(BaseModel):
    """Returned after creating a website form — includes keys + snippet for Security Info tab."""
    id: str
    company_id: str
    type: FormType
    website_link: str
    alert_email: str
    tags: List[str] = []
    created_at: datetime
    updated_at: datetime
    # Security Info (shown once on creation, retrievable later)
    api_key: str            # SDPK-272XXXXXXXXXXXXX (full key, shown once)
    public_key: str         # SDPK-272XXXXXXXXXXXXX (full key, shown once)
    snippet: str            # The <script> block to copy into their site

class OnlineFormCreateResponse(BaseModel):
    """Returned after creating an online form."""
    id: str
    company_id: str
    type: FormType
    form_image: str
    form_title: str
    tags: List[str] = []
    created_at: datetime
    updated_at: datetime
    form_url: str           # Shareable URL for this online form

class FormKeysResponse(BaseModel):
    """Retrievable later at swiftagents.org/forms/keys (Security Info tab)."""
    form_id: str
    api_key_prefix: str     # SDPK-272XXXXXXX... (masked for display)
    public_key_prefix: str  # SDPK-272XXXXXXX... (masked for display)
    snippet: str

# ── Widget Submission (from the embedded JS widget) ──

class WidgetSubmissionCreate(BaseModel):
    """Sent by widget.js when a form is submitted on the developer's website."""
    page_url: str               # e.g. "https://serendptai.com/contact-us"
    form_identifier: str        # CSS selector / form id / name / DOM index
    form_name: Optional[str] = None  # Human-readable: "Contact Form"
    data: Dict[str, Any]        # {name: "John Doe", email: "john@...", message: "..."}
    visitor_id: Optional[str] = None

# ── Submission Responses ──

class FormSubmissionCreate(BaseModel):
    """Legacy: direct submission (online forms / existing public endpoint)."""
    data: Dict[str, Any]
    visitor_id: Optional[str] = None

class FormSubmissionResponse(BaseModel):
    """Submission shown in the inbox — includes submitter info for display."""
    id: str
    form_id: str
    company_id: str
    data: Dict[str, Any]
    is_read: bool = False
    visitor_id: Optional[str] = None
    submitted_at: datetime
    # Page/form context (populated for website form submissions)
    page_url: Optional[str] = None
    form_identifier: Optional[str] = None
    form_name: Optional[str] = None
    # Auto-extracted from data for inbox list display
    submitter_name: Optional[str] = None      # "John Doe"
    submitter_preview: Optional[str] = None   # "Good day, I lost my sister in a flood and I'd..."
    replied_at: Optional[datetime] = None
    replies: List[Dict[str, Any]] = []

class FormReplyRequest(BaseModel):
    reply_text: str
    subject: Optional[str] = None


# ── Page & Form Hierarchy (for dashboard overview) ──

class FormGroupInfo(BaseModel):
    """A detected form within a page (e.g. 'Contact Form' on /contact-us)."""
    form_identifier: str
    form_name: str
    entries_count: int
    last_submission: Optional[datetime] = None

class PageInfo(BaseModel):
    """A detected page path within a website (e.g. /contact-us)."""
    page_path: str              # "/contact-us"
    forms: List[FormGroupInfo]
    total_entries: int

class WebsiteOverview(BaseModel):
    """Full overview of a website form's discovered pages and forms."""
    form_id: str
    website_link: str
    pages: List[PageInfo]
    total_entries: int

# ── 4-Level Delete Hierarchy Models ──

class WebsiteDeleteInfo(BaseModel):
    """Step 1: List of websites with form counts."""
    website: str
    form_count: int

class PageDeleteInfo(BaseModel):
    """Step 2: List of pages within a website with entry counts."""
    page_path: str
    entries_count: int

class FormDeleteInfo(BaseModel):
    """Step 3: List of forms within a page with metadata."""
    form_identifier: str
    form_name: str
    entries_count: int
    last_submission: Optional[datetime] = None

class EntryDeleteInfo(BaseModel):
    """Step 4: Individual entries within a form."""
    id: str
    submitter_name: Optional[str] = None
    submitter_preview: Optional[str] = None
    submitted_at: datetime

class BulkDeleteRequest(BaseModel):
    """Request body for bulk-deleting specific submissions."""
    submission_ids: List[str]

# ── Edit/Rename Models ──

class WebsiteLabelUpdate(BaseModel):
    old_website: str
    new_website: str

class PageLabelUpdate(BaseModel):
    website: str
    old_page: str
    new_page: str

class FormRenameRequest(BaseModel):
    """Rename a detected form (e.g. 'Form 1' → 'Contact Us Form')."""
    new_name: str

# ── Labels Response (existing, kept for backward compat) ──

class WebsiteLabel(BaseModel):
    website: str
    form_count: int

class PageLabel(BaseModel):
    website: str
    page: str
    entry_count: int

class LabelsResponse(BaseModel):
    websites: List[WebsiteLabel]
    pages: List[PageLabel]
