from typing import List, Optional
from pydantic import BaseModel, EmailStr, field_validator
from datetime import datetime

class CompanyInfoCreate(BaseModel):
    name: str
    email_slug: Optional[str] = None
    website: Optional[str] = None
    industry: Optional[str] = None
    company_size: Optional[str] = None
    country: Optional[str] = None
    timezone: Optional[str] = None
    contact_email: str
    support_email: Optional[str] = None
    phone_number: Optional[str] = None

class CompanyInfoUpdate(BaseModel):
    name: Optional[str] = None
    website: Optional[str] = None
    industry: Optional[str] = None
    company_size: Optional[str] = None
    country: Optional[str] = None
    timezone: Optional[str] = None
    contact_email: Optional[str] = None
    support_email: Optional[str] = None
    phone_number: Optional[str] = None

class CompanySecurityUpdate(BaseModel):
    backup_email: Optional[str] = None
    access_code: Optional[str] = None

class MemberInviteCreate(BaseModel):
    email: EmailStr

class AcceptInviteRequest(BaseModel):
    token: str

class CompanyMember(BaseModel):
    user_id: str
    email: str
    role: str = "member"
    added_at: datetime

class PendingInvite(BaseModel):
    email: str
    token: str
    invited_at: datetime

class CompanyMemberResponse(BaseModel):
    email: str
    role: str
    status: str

class CompanyIdentityUpdate(BaseModel):
    description: Optional[str] = None
    customer_value: Optional[str] = None
    brand_tone: Optional[str] = None
    primary_language: str = "English"
    support_emails: Optional[List[EmailStr]] = None

    @field_validator("support_emails")
    @classmethod
    def normalize_support_emails(cls, v: Optional[List[EmailStr]]) -> Optional[List[EmailStr]]:
        if v is None:
            return None
        normalized: List[str] = []
        seen = set()
        for e in v:
            s = str(e).strip().lower()
            if not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            normalized.append(s)
        if len(normalized) == 0:
            return []
        if len(normalized) > 20:
            raise ValueError("support_emails cannot contain more than 20 emails")
        return normalized

class CompanyTypeUpdate(BaseModel):
    company_type: str

class AnswerBoundariesUpdate(BaseModel):
    enabled_sources: List[str] = []
    custom_info: List[str] = []

class VoiceSettingsUpdate(BaseModel):
    voice_style: str = "professional"

class CompanyResponse(BaseModel):
    id: str
    user_id: str
    name: str
    email_slug: Optional[str] = None
    email_address: Optional[str] = None
    logo_url: Optional[str] = None
    company_type: Optional[str] = None
    onboarding_step: int = 1
    setup_complete: bool = False
    # step 1
    website: Optional[str] = None
    industry: Optional[str] = None
    company_size: Optional[str] = None
    country: Optional[str] = None
    timezone: Optional[str] = None
    contact_email: Optional[str] = None
    support_email: Optional[str] = None
    support_emails: Optional[List[EmailStr]] = None
    phone_number: Optional[str] = None
    # step 2
    description: Optional[str] = None
    customer_value: Optional[str] = None
    brand_tone: Optional[str] = None
    primary_language: str = "English"
    # step 4
    enabled_sources: List[str] = []
    custom_info: List[str] = []
    # step 5
    # step 5
    voice_style: str = "professional"
    # billing and subscription
    subscription_tier: Optional[str] = None # basic, pro, enterprise
    subscription_status: str = "inactive" # active, past_due, canceled, unpaid
    billing_provider: Optional[str] = None # palmpay or polar
    subscription_id: Optional[str] = None
    customer_id: Optional[str] = None
    # members
    members: List[CompanyMember] = []
    pending_invites: List[PendingInvite] = []
    # timestamps
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

class CompanySummary(BaseModel):
    id: str
    name: str
    email_slug: Optional[str] = None
    email_address: Optional[str] = None
    logo_url: Optional[str] = None
    setup_complete: bool = False
    onboarding_step: int = 1

class LogoUpdate(BaseModel):
    logo_url: str
