import logging
import json
from pydantic import BaseModel, Field
from typing import Optional
from google import genai
from google.genai import types

from app.core.config import settings

logger = logging.getLogger(__name__)

class CompanyScrapeResult(BaseModel):
    industry: Optional[str] = Field(None, description="The industry or sector of the company.")
    company_size: Optional[str] = Field(None, description="The estimated size of the company (e.g. '1-10', '11-50', '51-200', '201+').")
    description: Optional[str] = Field(None, description="A brief description of what the company does.")
    customer_value: Optional[str] = Field(None, description="The core value proposition or benefit the company provides to its customers.")
    brand_tone: Optional[str] = Field(None, description="The tone of the brand (e.g. Professional, Friendly, Playful, Authoritative).")
    primary_language: Optional[str] = Field("English", description="The primary language of the website.")
    contact_email: Optional[str] = Field(None, description="The main contact email address found on the site.")
    support_email: Optional[str] = Field(None, description="The support email address found on the site, if any.")
    phone_number: Optional[str] = Field(None, description="The contact phone number found on the site.")

async def extract_company_info(website_text: str) -> dict:
    """
    Uses Gemini to extract structured company information from scraped website text.
    Returns a dictionary corresponding to CompanyScrapeResult.
    """
    if not website_text or len(website_text.strip()) == 0:
        return CompanyScrapeResult().model_dump()

    prompt = (
        "You are an expert data extractor. I am providing you with the text scraped from a company's website. "
        "Your goal is to extract the following information and return it strictly in valid JSON format. "
        "If a piece of information is not found, use null.\n\n"
        "Fields to extract:\n"
        "- industry: The industry or sector.\n"
        "- company_size: Estimated size (e.g., '1-10', '11-50', '51-200', '201+').\n"
        "- description: A brief summary of what the company does.\n"
        "- customer_value: The core value proposition for customers.\n"
        "- brand_tone: The tone of the brand's writing (e.g., Professional, Friendly).\n"
        "- primary_language: The primary language of the site text (default 'English').\n"
        "- contact_email: The main contact email.\n"
        "- support_email: The support email (if distinct).\n"
        "- phone_number: The contact phone number.\n\n"
        f"Website Text:\n---\n{website_text[:12000]}\n---\n\n"
        "Return ONLY a JSON object matching the requested fields. Do not include markdown code blocks or any other text."
    )

    try:
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        response = await client.aio.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[prompt],
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
                response_schema=CompanyScrapeResult,
            )
        )
        
        reply = ""
        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.text:
                    reply += part.text
        
        if not reply:
            logger.warning("Empty response from Gemini when parsing website text.")
            return CompanyScrapeResult().model_dump()

        try:
            data = json.loads(reply.strip())
            return CompanyScrapeResult(**data).model_dump()
        except json.JSONDecodeError:
            # Sometimes LLMs wrap JSON in markdown blocks despite instructions
            if "```json" in reply:
                reply = reply.split("```json")[1].split("```")[0]
            elif "```" in reply:
                reply = reply.split("```")[1].split("```")[0]
            
            data = json.loads(reply.strip())
            return CompanyScrapeResult(**data).model_dump()
            
    except Exception as e:
        logger.error(f"Failed to extract company info using Gemini: {e}")
        return CompanyScrapeResult().model_dump()
