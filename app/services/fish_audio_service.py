"""
Fish.audio integration service — STT (speech-to-text) and TTS (text-to-speech).

Uses Fish.audio REST API:
- ASR: POST https://api.fish.audio/v1/asr  (multipart/form-data)
- TTS: POST https://api.fish.audio/v1/tts  (application/json)
"""
import logging
import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

FISH_API_BASE = "https://api.fish.audio"

_http_client = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            base_url=FISH_API_BASE,
            headers={"Authorization": f"Bearer {settings.FISH_AUDIO_API_KEY}"},
            timeout=30.0,
        )
    return _http_client


async def transcribe(audio_bytes: bytes, language: str = "auto", filename: str = "audio.wav", content_type: str = "audio/wav") -> str:
    """
    Transcribe audio to text via Fish.audio ASR.

    Args:
        audio_bytes: Raw audio data (wav/webm/ogg).
        language: Language hint, or "auto" for auto-detection.
        filename: Filename to send in the multipart request.
        content_type: MIME type of the audio data.

    Returns:
        Transcribed text string.
    """
    client = _get_client()
    files = {"audio": (filename, audio_bytes, content_type)}
    data = {}
    if language != "auto":
        data["language"] = language

    try:
        resp = await client.post("/v1/asr", files=files, data=data)
        resp.raise_for_status()
        result = resp.json()
        return result.get("text", "").strip()
    except httpx.HTTPStatusError as e:
        logger.error(f"Fish.audio ASR error {e.response.status_code}: {e.response.text}")
        raise
    except Exception as e:
        logger.exception("Fish.audio ASR request failed")
        raise


async def synthesize(text: str, voice_id: str = "") -> bytes:
    """
    Convert text to speech via Fish.audio TTS.

    Args:
        text: Text to synthesize.
        voice_id: Fish.audio voice model ID. Falls back to settings default.

    Returns:
        Audio bytes (mp3 format).
    """
    client = _get_client()
    ref_id = voice_id or settings.FISH_AUDIO_VOICE_ID

    payload = {
        "text": text,
        "reference_id": ref_id,
        "format": "mp3",
        "latency": "balanced",
    }

    try:
        resp = await client.post(
            "/v1/tts",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.content
    except httpx.HTTPStatusError as e:
        logger.error(f"Fish.audio TTS error {e.response.status_code}: {e.response.text}")
        raise
    except Exception as e:
        logger.exception("Fish.audio TTS request failed")
        raise
