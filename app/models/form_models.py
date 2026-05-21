from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, HttpUrl
from datetime import datetime

class FormType(str, Enum):
    WEBSITE = "website"
    ONLINE = "online"

# Base Create/Update Models

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

# Response Models

class FormResponse(BaseModel):
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

# Submissions

class FormSubmissionCreate(BaseModel):
    data: Dict[str, Any]
    visitor_id: Optional[str] = None

class FormSubmissionResponse(BaseModel):
    id: str
    form_id: str
    company_id: str
    data: Dict[str, Any]
    is_read: bool = False
    visitor_id: Optional[str] = None
    submitted_at: datetime
