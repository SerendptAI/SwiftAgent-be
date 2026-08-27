from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class PromptVariable(BaseModel):
    """Definition of a variable that can be interpolated into a prompt."""
    name: str = Field(..., description="Variable name")
    description: str = Field("", description="What this variable is used for")
    default_value: Optional[str] = Field(None, description="Default value if not provided")
    required: bool = Field(False, description="Whether this variable is required")
    example: Optional[str] = Field(None, description="Example value for documentation")


class PromptTemplate(BaseModel):
    """A prompt template with variable interpolation support."""
    id: str = Field(..., description="Unique template ID")
    name: str = Field(..., description="Human-readable name")
    description: str = Field("", description="What this prompt is used for")
    category: str = Field(..., description="Prompt category")
    version: int = Field(default=1, description="Version number")
    template_text: str = Field(..., description="The prompt text with {variable} placeholders")
    variables: List[PromptVariable] = Field(default_factory=list)
    is_active: bool = Field(default=True)
    is_default: bool = Field(default=False, description="Default for new companies")
    company_id: Optional[str] = Field(None, description="Company ID. None = global default")
    is_global: bool = Field(default=True)
    created_by: Optional[str] = Field(None)
    created_by_name: Optional[str] = Field(None)
    change_reason: Optional[str] = Field(None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    tags: List[str] = Field(default_factory=list)


class PromptTemplateVersion(BaseModel):
    """A single version entry in the version history."""
    version: int
    template_text: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: Optional[str] = None
    created_by_name: Optional[str] = None
    change_reason: Optional[str] = None
    is_active: bool = True


class CompanyPromptOverride(BaseModel):
    """A company-specific prompt override."""
    id: str
    company_id: str
    template_id: str
    template_text: str
    variables_override: Dict[str, str] = Field(default_factory=dict)
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: Optional[str] = None
    change_reason: Optional[str] = None


class RenderedPrompt(BaseModel):
    """A fully rendered prompt with variables interpolated."""
    template_id: str
    version: int
    rendered_text: str
    variables_used: Dict[str, Any] = Field(default_factory=dict)
    was_interpolated: bool
    company_id: Optional[str] = None
    is_custom: bool = False


class PromptTemplateCreate(BaseModel):
    name: str
    description: str = ""
    category: str
    template_text: str
    variables: List[PromptVariable] = Field(default_factory=list)
    is_default: bool = False
    company_id: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    change_reason: Optional[str] = None


class PromptTemplateUpdate(BaseModel):
    template_text: Optional[str] = None
    variables: Optional[List[PromptVariable]] = None
    is_default: Optional[bool] = None
    change_reason: Optional[str] = None
    tags: Optional[List[str]] = None


class PromptTemplateVersionHistory(BaseModel):
    template_id: str
    versions: List[PromptTemplateVersion]
    total_versions: int


class PromptDiffResponse(BaseModel):
    version_a: int
    version_b: int
    template_text_a: str
    template_text_b: str
    added_lines: List[str] = Field(default_factory=list)
    removed_lines: List[str] = Field(default_factory=list)
    unchanged_lines: List[str] = Field(default_factory=list)


class PromptPreviewRequest(BaseModel):
    template_text: str
    variables: Dict[str, str]
    company_id: Optional[str] = None


class PromptPreviewResponse(BaseModel):
    rendered_text: str
    missing_variables: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
