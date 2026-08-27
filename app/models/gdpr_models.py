"""GDPR request models."""

from pydantic import BaseModel, Field


class ExportRequest(BaseModel):
    include_conversations: bool = True
    include_knowledge: bool = True


class DeletionConfirmRequest(BaseModel):
    confirmation: str = Field(description="Must be the company id to confirm permanent deletion.")
