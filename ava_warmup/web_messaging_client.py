"""Genesys Cloud Web Messaging Guest API client."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, Optional

import websockets


class WebMessagingError(Exception):
    """Raised when Web Messaging connection or protocol handling fails."""


class WebMessagingClient:
    """Minimal Web Messaging client used by AVA Spec Warm Up attempts."""

    _UUID_PATTERN = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
        r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
    )

    def __init__(
        self,
        region: str,
        deployment_id: str,
        timeout: int = 90,
        origin: str = "https://apps.mypurecloud.com",
        debug_capture_frames: bool = False,
        debug_capture_frame_limit: int = 8,
    ):
        self.region = region
        self.deployment_id = deployment_id
        self.timeout = timeout
        self.origin = origin
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._token: Optional[str] = None
        self.conversation_id: Optional[str] = None
        self.participant_id: Optional[str] = None
        # First Genesys messageId we observed in any StructuredMessage frame.
        # The guest WebSocket protocol does not expose conversationId directly
        # (see GET /api/v2/conversations/messages/{messageId}/details for the
        # documented lookup path), so we keep this for the post-hoc REST call.
        self.message_id: Optional[str] = None
        self._conversation_id_candidates: list[str] = []
        # Map of UUID-shaped value -> JSON path of first occurrence in any
        # frame we received. Useful when Genesys never sends a frame containing
        # the literal "conversationId" key but does include UUIDs elsewhere.
        self._uuid_paths: dict[str, str] = {}
        self._debug_capture_frames = debug_capture_frames
        self._debug_capture_frame_limit = max(1, debug_capture_frame_limit)
        self._debug_frames: list[dict[str, Any]] = []

    @property
    def ws_url(self) -> str:
        return f"wss://webmessaging.{self.region}/v1?deploymentId={self.deployment_id}"

    async def connect(self) -> None:
        """Connect and configure a Web Messaging guest session."""

        try:
            self._ws = await websockets.connect(
                self.ws_url,
                additional_headers={"Origin": self.origin},
            )
        except TypeError:
            self._ws = await websockets.connect(
                self.ws_url,
                extra_headers={"Origin": self.origin},
            )
        except Exception as exc:
            raise WebMessagingError(
                "Failed to connect to Web Messaging API: "
                f"deployment_id={self.deployment_id}, region={self.region}. Error: {exc}"
            ) from exc

        self._token = str(uuid.uuid4())
        configure_message = {
            "action": "configureSession",
            "deploymentId": self.deployment_id,
            "token": self._token,
        }
        try:
            await self._ws.send(json.dumps(configure_message))
        except Exception as exc:
            raise WebMessagingError(
                "Failed to configure session: "
                f"deployment_id={self.deployment_id}, region={self.region}. Error: {exc}"
            ) from exc

        try:
            deadline = asyncio.get_event_loop().time() + self.timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                response = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                try:
                    data = json.loads(response)
                except json.JSONDecodeError:
                    continue
                self._update_conversation_metadata(data)
                self._capture_debug_frame(data, stage="connect")
                if data.get("type") == "SessionResponse" or self._is_session_ready_fallback(data):
                    break
        except asyncio.TimeoutError as exc:
            raise WebMessagingError(
                "Timed out waiting for session confirmation: "
                f"deployment_id={self.deployment_id}, region={self.region}"
            ) from exc
        except Exception as exc:
            raise WebMessagingError(
                "Error during session setup: "
                f"deployment_id={self.deployment_id}, region={self.region}. Error: {exc}"
            ) from exc

    def _is_session_ready_fallback(self, payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        msg_type = payload.get("type")
        if msg_type == "error":
            return False
        return msg_type in {"message", "response"}

    async def wait_for_welcome(self) -> str:
        if self._ws is None:
            raise WebMessagingError(
                f"Not connected: deployment_id={self.deployment_id}, region={self.region}"
            )
        try:
            return await self._receive_agent_message()
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Timed out waiting for welcome message after {self.timeout}s"
            ) from exc

    async def send_join(self) -> None:
        if self._ws is None:
            raise WebMessagingError(
                f"Not connected: deployment_id={self.deployment_id}, region={self.region}"
            )
        join_message = {
            "action": "onMessage",
            "token": self._token,
            "message": {
                "type": "Event",
                "events": [
                    {
                        "eventType": "Presence",
                        "presence": {"type": "Join"},
                    }
                ],
            },
        }
        try:
            await self._ws.send(json.dumps(join_message))
        except Exception as exc:
            raise WebMessagingError(
                f"Failed to send join event: deployment_id={self.deployment_id}, "
                f"region={self.region}. Error: {exc}"
            ) from exc

    async def send_message(self, text: str) -> None:
        if self._ws is None:
            raise WebMessagingError(
                f"Not connected: deployment_id={self.deployment_id}, region={self.region}"
            )
        message = {
            "action": "onMessage",
            "token": self._token,
            "message": {
                "type": "Text",
                "text": text,
            },
        }
        try:
            await self._ws.send(json.dumps(message))
        except Exception as exc:
            raise WebMessagingError(
                f"Failed to send message: deployment_id={self.deployment_id}, "
                f"region={self.region}. Error: {exc}"
            ) from exc

    async def receive_response(self) -> str:
        if self._ws is None:
            raise WebMessagingError(
                f"Not connected: deployment_id={self.deployment_id}, region={self.region}"
            )
        try:
            return await self._receive_agent_message()
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Timed out waiting for agent response after {self.timeout}s"
            ) from exc

    async def disconnect(self) -> None:
        # Keep diagnostic fields (conversation_id, participant_id, candidates,
        # debug_frames, _token) so callers can read them after disconnect —
        # build_result is invoked after the finally that calls us, and the
        # client instance is per-attempt anyway.
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            finally:
                self._ws = None

    async def _receive_agent_message(self) -> str:
        deadline = asyncio.get_event_loop().time() + self.timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            self._update_conversation_metadata(data)
            self._capture_debug_frame(data, stage="receive")
            msg_type = data.get("type", "")
            msg_class = data.get("class", "")
            body = data.get("body", {})

            if isinstance(body, dict):
                if body.get("direction", "") == "Inbound":
                    continue
                if body.get("type", "") == "Event":
                    continue

            if msg_type == "message" and msg_class == "StructuredMessage" and isinstance(body, dict):
                text = body.get("text", "")
                if text:
                    return text
            if msg_type == "message":
                if isinstance(body, str) and body:
                    return body
                if isinstance(body, dict) and body.get("text"):
                    return str(body["text"])
            if msg_type == "response":
                if isinstance(body, dict) and body.get("text"):
                    return str(body["text"])
                if isinstance(body, str) and body:
                    return body

    # Keys that, when seen anywhere in a server payload, identify the
    # Genesys conversation (a.k.a. interactionId). The Web Messaging guest
    # protocol uses several casings depending on the frame type.
    _CONVERSATION_ID_KEYS = (
        "conversationId",
        "conversation_id",
        "interactionId",
        "interaction_id",
    )
    _PARTICIPANT_ID_KEYS = (
        "participantId",
        "participant_id",
    )

    def _update_conversation_metadata(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return

        def _set_if_missing(attr_name: str, value: object) -> None:
            if getattr(self, attr_name) is not None:
                return
            if isinstance(value, str) and value.strip():
                normalized = value.strip()
                if attr_name == "conversation_id" and not self._is_likely_conversation_id(normalized):
                    return
                setattr(self, attr_name, normalized)

        all_paths: dict[str, str] = self._uuid_paths

        def _record_uuid(path: str, value: object) -> None:
            if not isinstance(value, str):
                return
            normalized = value.strip()
            if not normalized or not self._is_likely_conversation_id(normalized):
                return
            # Record first-seen path per unique UUID so candidates show context.
            if normalized not in all_paths:
                all_paths[normalized] = path
            self._add_conversation_id_candidate(normalized)

        def _walk(node: object, parent_key: Optional[str] = None, path: str = "$") -> None:
            if isinstance(node, dict):
                for cid_key in self._CONVERSATION_ID_KEYS:
                    if cid_key in node:
                        _set_if_missing("conversation_id", node.get(cid_key))
                        self._capture_conversation_id_candidate(
                            node.get(cid_key), is_explicit=True
                        )
                for pid_key in self._PARTICIPANT_ID_KEYS:
                    if pid_key in node:
                        _set_if_missing("participant_id", node.get(pid_key))
                conversation_obj = node.get("conversation")
                if isinstance(conversation_obj, dict):
                    _set_if_missing("conversation_id", conversation_obj.get("id"))
                    self._capture_conversation_id_candidate(
                        conversation_obj.get("id"), is_explicit=True
                    )
                participant_obj = node.get("participant")
                if isinstance(participant_obj, dict):
                    _set_if_missing("participant_id", participant_obj.get("id"))
                if parent_key == "conversation":
                    _set_if_missing("conversation_id", node.get("id"))
                if parent_key == "participant":
                    _set_if_missing("participant_id", node.get("id"))
                for key, value in node.items():
                    child_path = f"{path}.{key}"
                    if isinstance(value, str):
                        _record_uuid(child_path, value)
                    _walk(value, parent_key=key, path=child_path)
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    child_path = f"{path}[{index}]"
                    if isinstance(item, str):
                        _record_uuid(child_path, item)
                    _walk(item, parent_key=parent_key, path=child_path)

        _walk(payload)
        if self.conversation_id:
            self._add_conversation_id_candidate(self.conversation_id)
        self._capture_message_id_from_frame(payload)

    def _capture_message_id_from_frame(self, payload: object) -> None:
        """Record the Genesys messageId from a StructuredMessage frame.

        The Web Messaging guest API uses several shapes depending on the
        deployment/protocol version. We accept any of:

        - ``body.id``                — older / canonical shape
        - ``body.channel.messageId`` — newer shape (most common today)
        - ``body.messageId``         — occasional alternate

        Whichever value we capture goes to
        ``GET /api/v2/conversations/messages/{messageId}/details`` to
        translate into the real Genesys conversationId.
        """

        if self.message_id is not None or not isinstance(payload, dict):
            return
        if payload.get("type") not in ("message", "response"):
            return
        body = payload.get("body")
        if not isinstance(body, dict):
            return

        candidates: list[object] = [body.get("id"), body.get("messageId")]
        channel = body.get("channel")
        if isinstance(channel, dict):
            candidates.append(channel.get("messageId"))

        for raw_candidate in candidates:
            if not isinstance(raw_candidate, str):
                continue
            normalized = raw_candidate.strip()
            if normalized and self._is_likely_conversation_id(normalized):
                self.message_id = normalized
                return

    def _capture_conversation_id_candidate(self, value: object, is_explicit: bool = False) -> None:
        if not isinstance(value, str) or not is_explicit:
            return
        normalized = value.strip()
        if not normalized:
            return
        self._add_conversation_id_candidate(normalized)
        if self.conversation_id is None and self._is_likely_conversation_id(normalized):
            self.conversation_id = normalized

    def _add_conversation_id_candidate(self, value: str) -> None:
        if value not in self._conversation_id_candidates:
            self._conversation_id_candidates.append(value)

    def _is_likely_conversation_id(self, value: str) -> bool:
        return bool(self._UUID_PATTERN.match(value))

    def _capture_debug_frame(self, payload: object, stage: str) -> None:
        if (
            not self._debug_capture_frames
            or len(self._debug_frames) >= self._debug_capture_frame_limit
            or not isinstance(payload, dict)
        ):
            return
        body = payload.get("body")
        self._debug_frames.append(
            {
                "stage": stage,
                "type": payload.get("type"),
                "class": payload.get("class"),
                "top_level_keys": sorted(payload.keys()),
                "body_type": body.get("type") if isinstance(body, dict) else None,
                "body_direction": body.get("direction") if isinstance(body, dict) else None,
                "conversation_id": self.conversation_id,
                "participant_id": self.participant_id,
                "conversation_id_candidates": list(self._conversation_id_candidates),
            }
        )

    def get_debug_frames(self) -> list[dict[str, Any]]:
        return [dict(frame) for frame in self._debug_frames]

    def get_conversation_id_candidates(self) -> list[str]:
        return list(self._conversation_id_candidates)

    def get_uuid_paths(self) -> dict[str, str]:
        """Return every UUID-shaped string we saw in any received frame.

        Maps the UUID value to the JSON path of its first occurrence (e.g.
        ``$.body.channel.from.id``). Useful when Genesys' Web Messaging guest
        protocol does not include an explicit ``conversationId`` key in the
        responses we receive — the user can inspect candidates to identify
        the interaction.
        """

        return dict(self._uuid_paths)
