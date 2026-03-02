"""
Service for extracting text from uploaded documents (PDF, DOCX, TXT).
"""

import io
import fitz  # PyMuPDF
from docx import Document


def extract_text(filename: str, content: bytes) -> str:
    """
    Extract text from a file based on its extension.
    Raises ValueError if the file type is unsupported or extraction fails.
    """
    ext = filename.lower().split('.')[-1]
    
    if ext == 'pdf':
        return extract_text_from_pdf(content)
    elif ext == 'docx':
        return extract_text_from_docx(content)
    elif ext in ['txt', 'csv']:
        return extract_text_from_txt(content)
    else:
        raise ValueError(f"Unsupported file format: {ext}")


def extract_text_from_pdf(content: bytes) -> str:
    """Extract text from a PDF file."""
    text_blocks = []
    try:
        # open the pdf from bytes
        doc = fitz.open(stream=content, filetype="pdf")
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            text_blocks.append(page.get_text())
        return "\n".join(text_blocks)
    except Exception as e:
        raise ValueError(f"Failed to extract text from PDF: {str(e)}")


def extract_text_from_docx(content: bytes) -> str:
    """Extract text from a DOCX file."""
    try:
        # open docx from bytes
        doc = Document(io.BytesIO(content))
        paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
        return "\n".join(paragraphs)
    except Exception as e:
        raise ValueError(f"Failed to extract text from DOCX: {str(e)}")


def extract_text_from_txt(content: bytes) -> str:
    """Extract text from a plain text or CSV file."""
    try:
        return content.decode('utf-8')
    except UnicodeDecodeError:
        try:
            # fallback to latin-1 if utf-8 fails
            return content.decode('latin-1')
        except Exception as e:
            raise ValueError(f"Failed to decode text file: {str(e)}")
