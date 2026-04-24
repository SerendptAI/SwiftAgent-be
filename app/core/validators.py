"""Validation utilities for common data types."""

import re
from typing import Optional


# RFC 5322 compliant email regex (simplified but effective)
_EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)


def validate_email(email: str) -> tuple[bool, Optional[str]]:
    """
    Validate an email address.
    
    Args:
        email: The email address to validate
        
    Returns:
        Tuple of (is_valid, error_message)
        - is_valid: True if valid, False otherwise
        - error_message: Description of the error if invalid, None if valid
    """
    if not email:
        return False, "Email address is required"
    
    email = email.strip()
    
    if len(email) > 254:
        return False, "Email address is too long (max 254 characters)"
    
    if not _EMAIL_REGEX.match(email):
        return False, "Invalid email address format"
    
    # Additional checks
    local_part, _, domain = email.rpartition('@')
    
    if len(local_part) > 64:
        return False, "Email local part is too long (max 64 characters)"
    
    if not domain or '.' not in domain:
        return False, "Email domain is invalid"
    
    # Check for consecutive dots
    if '..' in email:
        return False, "Email cannot contain consecutive dots"
    
    # Check for dot at start or end of local part
    if local_part.startswith('.') or local_part.endswith('.'):
        return False, "Email local part cannot start or end with a dot"
    
    return True, None


def sanitize_string(value: str, max_length: int = 1000) -> str:
    """
    Sanitize a string by stripping whitespace and limiting length.
    
    Args:
        value: The string to sanitize
        max_length: Maximum allowed length
        
    Returns:
        Sanitized string
    """
    if not isinstance(value, str):
        value = str(value)
    
    sanitized = value.strip()
    
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length]
    
    return sanitized


def validate_mongodb_id(value: str) -> bool:
    """
    Validate that a string could be a valid MongoDB ObjectId.
    
    Args:
        value: The string to validate
        
    Returns:
        True if valid, False otherwise
    """
    if not value:
        return False
    
    # ObjectId is 24 hex characters
    if len(value) != 24:
        return False
    
    try:
        int(value, 16)
        return True
    except ValueError:
        return False
