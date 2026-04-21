"""WhatsApp channel implementation using Node.js bridge."""

import asyncio
import base64
import json
import mimetypes
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import WhatsAppConfig

# Directory for saving downloaded WhatsApp media
_MEDIA_DIR = Path.home() / ".nanobot" / "media" / "whatsapp"


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp channel that connects to a Node.js bridge.
    
    The bridge uses @whiskeysockets/baileys to handle the WhatsApp Web protocol.
    Communication between Python and Node.js is via WebSocket.
    """
    
    name = "whatsapp"
    
    def __init__(self, config: WhatsAppConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: WhatsAppConfig = config
        self._ws = None
        self._connected = False
    
    async def start(self) -> None:
        """Start the WhatsApp channel by connecting to the bridge."""
        import websockets
        
        bridge_url = self.config.bridge_url
        
        logger.info("Connecting to WhatsApp bridge at {}...", bridge_url)
        
        self._running = True
        
        while self._running:
            try:
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws
                    # Send auth token if configured
                    if self.config.bridge_token:
                        await ws.send(json.dumps({"type": "auth", "token": self.config.bridge_token}))
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")
                    
                    # Listen for messages
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error("Error handling bridge message: {}", e)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning("WhatsApp bridge connection error: {}", e)
                
                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)
    
    async def stop(self) -> None:
        """Stop the WhatsApp channel."""
        self._running = False
        self._connected = False
        
        if self._ws:
            await self._ws.close()
            self._ws = None
    
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WhatsApp."""
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return
        
        try:
            payload = {
                "type": "send",
                "to": msg.chat_id,
                "text": msg.content
            }
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.error("Error sending WhatsApp message: {}", e)
    
    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from bridge: {}", raw[:100])
            return
        
        msg_type = data.get("type")
        
        if msg_type == "message":
            # Incoming message from WhatsApp
            # Deprecated by whatsapp: old phone number style typically: <phone>@s.whatspp.net
            pn = data.get("pn", "")
            # New LID sytle typically:
            sender = data.get("sender", "")
            content = data.get("content", "")

            # Extract just the phone number or lid as chat_id
            user_id = pn if pn else sender
            sender_id = user_id.split("@")[0] if "@" in user_id else user_id
            logger.info("Sender {}", sender)

            # Handle voice transcription if it's a voice message
            if content == "[Voice Message]":
                logger.info("Voice message received from {}, but direct download from bridge is not yet supported.", sender_id)
                content = "[Voice Message: Transcription not available for WhatsApp yet]"

            # --- MIT-45: Save media attachment if present ---
            media_paths: list[str] = []
            media_payload = data.get("media")
            if media_payload and isinstance(media_payload, dict):
                saved = self._save_media(media_payload)
                if saved:
                    media_paths.append(saved)
                    logger.info("Saved WhatsApp media to {}", saved)

            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,  # Use full LID for replies
                content=content,
                media=media_paths or None,
                metadata={
                    "message_id": data.get("id"),
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False)
                }
            )
        
        elif msg_type == "status":
            # Connection status update
            status = data.get("status")
            logger.info("WhatsApp status: {}", status)
            
            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._connected = False
        
        elif msg_type == "qr":
            # QR code for authentication
            logger.info("Scan QR code in the bridge terminal to connect WhatsApp")
        
        elif msg_type == "error":
            logger.error("WhatsApp bridge error: {}", data.get('error'))

    # ------------------------------------------------------------------
    # Media helpers (MIT-45)
    # ------------------------------------------------------------------

    @staticmethod
    def _save_media(media: dict[str, Any]) -> str | None:
        """
        Decode base64 media from the bridge and save to disk.

        The bridge sends:
            { "data": "<base64>", "mimetype": "image/jpeg", "filename": "photo.jpg" }

        Returns the absolute path of the saved file, or None on failure.
        """
        b64_data = media.get("data")
        if not b64_data:
            return None

        try:
            raw = base64.b64decode(b64_data)
        except Exception as exc:
            logger.warning("Failed to decode base64 media: {}", exc)
            return None

        mimetype = media.get("mimetype", "application/octet-stream")
        filename = media.get("filename")

        # Derive a filename if the bridge didn't provide one
        if not filename:
            ext = mimetypes.guess_extension(mimetype) or ".bin"
            filename = f"{uuid.uuid4().hex[:12]}{ext}"

        # Ensure the filename is unique to avoid collisions
        safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"

        _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        dest = _MEDIA_DIR / safe_name

        try:
            dest.write_bytes(raw)
        except Exception as exc:
            logger.error("Failed to write media file {}: {}", dest, exc)
            return None

        return str(dest)
