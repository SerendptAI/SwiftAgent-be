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
import base64
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.core.database import db
from app.services import agent_service, fish_audio_service

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
    webm_init_segment = None  # stores WebM header from the first audio chunk

    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")

            if msg_type == "start":
                # begin a new call session
                session_id = msg.get("session_id", "")
                audio_buffer.clear()
                await websocket.send_json({"type": "status", "status": "ready"})
                logger.info(f"Voice call started: company={company_id}, session={session_id}")

            elif msg_type == "audio":
                # accumulate audio chunks from the user's mic
                chunk_b64 = msg.get("data", "")
                if chunk_b64:
                    chunk = base64.b64decode(chunk_b64)
                    # capture the WebM init segment from the very first chunk
                    # (contains EBML header + Segment/Track info needed for a valid file)
                    if webm_init_segment is None and chunk[:4] == b'\x1a\x45\xdf\xa3':
                        webm_init_segment = bytes(chunk)
                    audio_buffer.extend(chunk)

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

                # build a valid WebM file: if the buffer doesn't start with
                # the EBML magic bytes, prepend the saved init segment
                raw = bytes(audio_buffer)
                if webm_init_segment and raw[:4] != b'\x1a\x45\xdf\xa3':
                    raw = webm_init_segment + raw

                try:
                    transcript = await fish_audio_service.transcribe(raw)
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
                    result = await agent_service.chat(company_id, session_id, transcript)
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
                except Exception as e:
                    logger.error(f"TTS failed: {e}")
                    await websocket.send_json({"type": "error", "message": "Could not synthesize audio"})

                await websocket.send_json({"type": "status", "status": "ready"})

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
