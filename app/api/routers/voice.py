"""
Voice call WebSocket router — real-time audio streaming for voice conversations.

Protocol (JSON messages over WebSocket):
  Client → Server:
    {"type": "start", "session_id": "..."}     — begin call session
    {"type": "audio", "data": "<base64>"}      — audio chunk from mic
    {"type": "stop_audio"}                     — user finished speaking
    {"type": "end"}                            — hang up

  Server → Client:
    {"type": "status", "status": "..."}        — transcribing / thinking / ready
    {"type": "transcript", "text": "..."}      — what user said
    {"type": "reply_text", "text": "..."}      — agent text reply
    {"type": "audio", "data": "<base64>"}      — TTS audio response
    {"type": "error", "message": "..."}        — error occurred
"""
from datetime import datetime
import base64
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.core.database import db
from app.services import anthropic_agent_service, fish_audio_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Voice"])


@router.websocket("/{company_id}/call")
async def voice_call(websocket: WebSocket, company_id: str):
    """Handle a real-time voice call session over WebSocket."""
    await websocket.accept()

    # validate company exists
    company = await db.companies.find_one({"id": company_id})
    if not company:
        await websocket.send_json({"type": "error", "message": "Company not found"})
        await websocket.close()
        return

    session_id = None
    audio_buffer = bytearray()
    current_mime_type = None

    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")

            if msg_type == "start":
                # begin a new call session
                session_id = msg.get("session_id", "")
                current_mime_type = msg.get("mime_type")
                audio_buffer.clear()
                
                # record the call in database
                await db.calls.insert_one({
                    "company_id": company_id,
                    "session_id": session_id,
                    "timestamp": datetime.utcnow(),
                    "mime_type": current_mime_type
                })
                
                await websocket.send_json({"type": "status", "status": "ready"})
                logger.info(f"Voice call started: company={company_id}, session={session_id}, mime={current_mime_type}")

            elif msg_type == "start_audio":
                # client is starting a new utterance
                audio_buffer.clear()
                # update mime type if provided (e.g. from a restarted MediaRecorder)
                if "mime_type" in msg:
                    current_mime_type = msg["mime_type"]
                logger.debug(f"start_audio received: clearing buffer for session={session_id}, mime={current_mime_type}")

            elif msg_type == "audio":
                # accumulate audio chunks from the user's mic
                chunk_b64 = msg.get("data", "")
                if chunk_b64:
                    try:
                        chunk = base64.b64decode(chunk_b64)
                        audio_buffer.extend(chunk)
                    except Exception as e:
                        logger.error(f"Failed to decode audio chunk: {e}")

            elif msg_type == "stop_audio":
                # user finished speaking — process the audio
                if not audio_buffer:
                    await websocket.send_json({"type": "status", "status": "ready"})
                    continue

                if not session_id:
                    await websocket.send_json({"type": "error", "message": "Session not started"})
                    continue

                # STT — transcribe the audio
                await websocket.send_json({"type": "status", "status": "transcribing"})

                raw = bytes(audio_buffer)
                
                # Determine format: Prefer explicit mime_type, then magic bytes
                filename = "audio.webm"
                content_type = "audio/webm"

                if current_mime_type:
                    content_type = current_mime_type
                    ext = "webm"
                    if "ogg" in current_mime_type: ext = "ogg"
                    elif "mp4" in current_mime_type: ext = "mp4"
                    elif "wav" in current_mime_type: ext = "wav"
                    elif "mpeg" in current_mime_type: ext = "mp3"
                    elif "aac" in current_mime_type: ext = "aac"
                    filename = f"audio.{ext}"
                    logger.debug(f"Using provided mime_type: {content_type} -> {filename}")
                else:
                    # detect format from magic bytes
                    if raw.startswith(b'OggS'):
                        filename = "audio.ogg"
                        content_type = "audio/ogg"
                    elif raw.startswith(b'\x1a\x45\xdf\xa3'):
                        filename = "audio.webm"
                        content_type = "audio/webm"
                    elif raw.startswith(b'RIFF'):
                        filename = "audio.wav"
                        content_type = "audio/wav"
                    elif raw[4:8] == b'ftyp':
                        filename = "audio.mp4"
                        content_type = "audio/mp4"
                    elif raw.startswith((b'\xff\xf1', b'\xff\xf9')):
                        filename = "audio.aac"
                        content_type = "audio/aac"
                    else:
                        # fallback
                        filename = "audio.webm"
                        content_type = "audio/webm"
                    logger.debug(f"Detected format from magic bytes: {content_type} ({raw[:8].hex()})")

                try:
                    logger.info(f"Transcribing {len(raw)} bytes as {content_type} for session={session_id}")
                    transcript = await fish_audio_service.transcribe(
                        raw, 
                        filename=filename, 
                        content_type=content_type
                    )
                except Exception as e:
                    logger.error(f"STT failed: {e}")
                    await websocket.send_json({"type": "error", "message": "Could not transcribe audio"})
                    await websocket.send_json({"type": "status", "status": "ready"})
                    audio_buffer.clear()
                    continue

                audio_buffer.clear()

                if not transcript:
                    await websocket.send_json({"type": "status", "status": "ready"})
                    continue

                # send transcript back to client
                await websocket.send_json({"type": "transcript", "text": transcript})

                # agent — process through existing chat pipeline
                await websocket.send_json({"type": "status", "status": "thinking"})
                try:
                    result = await anthropic_agent_service.chat(company_id, session_id, transcript)
                    reply = result.get("reply", "I'm sorry, I couldn't generate a response.")
                except Exception as e:
                    logger.error(f"Agent chat failed: {e}")
                    reply = "I'm sorry, I'm having trouble right now. Please try again."

                # send text reply (for transcript/history display)
                await websocket.send_json({"type": "reply_text", "text": reply})

                # TTS — synthesize the reply to audio
                try:
                    audio_bytes = await fish_audio_service.synthesize(reply)
                    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
                    await websocket.send_json({"type": "audio", "data": audio_b64})
                except WebSocketDisconnect:
                    logger.info(f"Client disconnected during TTS send: session={session_id}")
                    return
                except Exception as e:
                    logger.error(f"TTS failed: {e}")
                    try:
                        await websocket.send_json({"type": "error", "message": "Could not synthesize audio"})
                    except Exception:
                        logger.info("Client disconnected before TTS error could be sent")
                        return

                try:
                    await websocket.send_json({"type": "status", "status": "ready"})
                except Exception:
                    logger.info("Client disconnected before ready status could be sent")
                    return

            elif msg_type == "end":
                # user hung up
                logger.info(f"Voice call ended: company={company_id}, session={session_id}")
                await websocket.send_json({"type": "status", "status": "ended"})
                await websocket.close()
                break

    except WebSocketDisconnect:
        logger.info(f"Voice call disconnected: company={company_id}, session={session_id}")
    except Exception as e:
        logger.exception(f"Voice call error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": "Internal server error"})
            await websocket.close()
        except Exception:
            pass
