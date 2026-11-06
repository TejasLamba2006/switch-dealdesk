"""Switch Agent Protocol client and local protocol-enforcing server harness.

Implements the exact Switch Agent Bridge HTTP/SSE wire surface:
- GET  /agents/{agent_id}/events?connection_id=...&rooms=...
- POST /agents/{agent_id}/connection/beat
- POST /agents/{agent_id}/ops/{op_name}
- POST /agents/{agent_id}/media
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class SwitchProtocolError(RuntimeError):
    """Raised when Switch rejects an operation (single-writer, role lease, addressing, or membership)."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status_code} [{code}]: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass
class Participant:
    transport_user_id: str
    name: str
    participant_type: str  # "human" | "agent"
    is_owner: bool = False


@dataclass
class RoomRole:
    name: str
    instructions: str
    exclusive: bool
    holders: dict[str, str] = field(default_factory=dict)  # agent_id -> connection_id


@dataclass
class RoomDocument:
    name: str
    description: str
    instructions: str
    content: str
    created_by: str  # transport_user_id of single-writer author
    updated_at: float = field(default_factory=time.time)


@dataclass
class RoomSchedule:
    schedule_id: str
    room_id: str
    agent_ids: list[str]
    prompt: str
    cron: str | None
    timezone: str
    created_by: str


@dataclass
class RoomState:
    room_id: str
    name: str
    channel_type: str  # "channel_public" | "channel_private"
    owner_user_id: str
    instructions: str
    participants: dict[str, Participant] = field(default_factory=dict)
    roles: dict[str, RoomRole] = field(default_factory=dict)
    docs: dict[str, RoomDocument] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    schedules: list[RoomSchedule] = field(default_factory=list)
    linked_rooms: list[dict[str, str]] = field(default_factory=list)


@dataclass
class AgentRegistration:
    agent_id: str
    name: str
    transport_user_id: str
    api_key: str
    owner_user_id: str
    addressing: str  # "anyone" | "owner-agents" | "owner"


@dataclass
class LiveConnection:
    connection_id: str
    agent_id: str
    rooms: set[str]
    last_beat: float
    events: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)


class SwitchProtocolState:
    """In-memory Switch Agent Bridge state enforcing real Switch room/agent invariants."""

    LEASE_TTL_SECONDS = 6.0

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.rooms: dict[str, RoomState] = {}
        self.agents: dict[str, AgentRegistration] = {}
        self.connections: dict[str, LiveConnection] = {}
        self.media: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def next_seq(self) -> int:
        with self.lock:
            self._seq += 1
            return self._seq

    def expire_stale_connections(self, now: float | None = None) -> None:
        ts = now if now is not None else time.time()
        with self.lock:
            expired = [
                cid
                for cid, conn in self.connections.items()
                if ts - conn.last_beat > self.LEASE_TTL_SECONDS
            ]
            for cid in expired:
                conn = self.connections.pop(cid)
                for room in self.rooms.values():
                    for role in room.roles.values():
                        if role.holders.get(conn.agent_id) == cid:
                            del role.holders[conn.agent_id]

    def can_address(self, sender: Participant, target_agent: AgentRegistration) -> bool:
        if target_agent.addressing == "anyone":
            return True
        if target_agent.addressing == "owner":
            return sender.participant_type == "human" and sender.is_owner
        if target_agent.addressing == "owner-agents":
            if sender.participant_type == "human" and sender.is_owner:
                return True
            if sender.participant_type == "agent":
                for reg in self.agents.values():
                    if reg.transport_user_id == sender.transport_user_id:
                        return reg.owner_user_id == target_agent.owner_user_id
            return False
        return False

    def post_room_message(
        self,
        *,
        room_id: str,
        sender_id: str,
        body: str,
        target_names: list[str] | None = None,
        attachment_id: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            room = self.rooms.get(room_id)
            if room is None:
                raise SwitchProtocolError(404, "ROOM_NOT_FOUND", f"Unknown room '{room_id}'.")
            sender = room.participants.get(sender_id)
            if sender is None:
                raise SwitchProtocolError(
                    403,
                    "NOT_IN_ROOM",
                    f"Sender '{sender_id}' is not a member of room '{room.name}'.",
                )

            delivered_to: list[str] = []
            dropped_by_addressing: list[str] = []
            for agent in self.agents.values():
                if agent.transport_user_id not in room.participants:
                    continue
                if agent.transport_user_id == sender_id:
                    continue
                mentioned = f"@{agent.name}" in body or (
                    target_names is not None and agent.name in target_names
                )
                if not mentioned:
                    continue
                if self.can_address(sender, agent):
                    delivered_to.append(agent.name)
                else:
                    dropped_by_addressing.append(agent.name)

            msg = {
                "seq": self.next_seq(),
                "event_id": f"$msg-{uuid.uuid4().hex[:10]}",
                "room_id": room_id,
                "room_name": room.name,
                "sender": sender_id,
                "body": body,
                "target_names": target_names or [],
                "attachment_id": attachment_id,
                "delivered_to": delivered_to,
                "dropped_by_addressing": dropped_by_addressing,
                "timestamp": time.time(),
            }
            room.messages.append(msg)

            for conn in self.connections.values():
                agent = self.agents[conn.agent_id]
                if room_id in conn.rooms and agent.name in delivered_to:
                    conn.events.put({"type": "message", "data": msg})
            return msg


class SwitchProtocolServer:
    """Threaded HTTP server exposing the Switch Agent Bridge endpoints."""

    def __init__(self, state: SwitchProtocolState) -> None:
        self.state = state
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._httpd.server_port
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> str:
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth_agent(self, agent_id: str) -> AgentRegistration:
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    raise SwitchProtocolError(401, "UNAUTHORIZED", "Missing Bearer token.")
                token = auth.removeprefix("Bearer ").strip()
                agent = state.agents.get(agent_id)
                if agent is None or agent.api_key != token:
                    raise SwitchProtocolError(403, "FORBIDDEN", "Invalid agent API key.")
                return agent

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                try:
                    if len(parts) == 3 and parts[0] == "agents" and parts[2] == "events":
                        agent = self._auth_agent(parts[1])
                        qs = urllib.parse.parse_qs(parsed.query)
                        cid = (qs.get("connection_id") or [f"conn-{uuid.uuid4().hex[:8]}"])[0]
                        rooms_raw = (qs.get("rooms") or [""])[0]
                        rooms = {r for r in rooms_raw.split(",") if r}
                        with state.lock:
                            for r_id in rooms:
                                room = state.rooms.get(r_id)
                                if room is None or agent.transport_user_id not in room.participants:
                                    raise SwitchProtocolError(
                                        403,
                                        "NOT_IN_ROOM",
                                        f"Agent '{agent.name}' is not a member of '{r_id}'.",
                                    )
                            conn = LiveConnection(
                                connection_id=cid,
                                agent_id=agent.agent_id,
                                rooms=rooms,
                                last_beat=time.time(),
                            )
                            state.connections[cid] = conn
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        init_evt = {
                            "type": "connection_state",
                            "connection_id": cid,
                            "rooms": sorted(rooms),
                        }
                        self.wfile.write(f"event: connection_state\ndata: {json.dumps(init_evt)}\n\n".encode())
                        self.wfile.flush()
                        while True:
                            try:
                                evt = conn.events.get(timeout=0.25)
                                self.wfile.write(
                                    f"event: {evt['type']}\ndata: {json.dumps(evt['data'])}\n\n".encode()
                                )
                                self.wfile.flush()
                            except queue.Empty:
                                if cid not in state.connections:
                                    break
                        return
                    self._send_json(404, {"error": {"code": "NOT_FOUND", "message": "Route not found."}})
                except SwitchProtocolError as exc:
                    self._send_json(exc.status_code, {"error": {"code": exc.code, "message": exc.message}})

            def do_POST(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                try:
                    state.expire_stale_connections()
                    if len(parts) == 4 and parts[0] == "agents" and parts[2] == "connection" and parts[3] == "beat":
                        agent = self._auth_agent(parts[1])
                        cid = body.get("connection_id", "")
                        rooms = set(body.get("rooms") or [])
                        with state.lock:
                            for r_id in rooms:
                                room = state.rooms.get(r_id)
                                if room is None or agent.transport_user_id not in room.participants:
                                    raise SwitchProtocolError(
                                        403,
                                        "NOT_IN_ROOM",
                                        f"Agent '{agent.name}' is not a member of '{r_id}'.",
                                    )
                            conn = state.connections.get(cid)
                            if conn is None:
                                conn = LiveConnection(
                                    connection_id=cid,
                                    agent_id=agent.agent_id,
                                    rooms=rooms,
                                    last_beat=time.time(),
                                )
                                state.connections[cid] = conn
                            else:
                                conn.last_beat = time.time()
                                if rooms:
                                    conn.rooms = rooms
                        self._send_json(200, {"ok": True, "connection_id": cid, "ttl_seconds": state.LEASE_TTL_SECONDS})
                        return

                    if len(parts) == 3 and parts[0] == "agents" and parts[2] == "media":
                        agent = self._auth_agent(parts[1])
                        media_id = f"mxc://switch.local/{uuid.uuid4().hex[:12]}"
                        with state.lock:
                            state.media[media_id] = {
                                "media_id": media_id,
                                "uploaded_by": agent.transport_user_id,
                                "filename": body.get("filename", "artifact.bin"),
                                "content_type": body.get("content_type", "application/octet-stream"),
                                "content": body.get("content", ""),
                            }
                        self._send_json(200, {"media_id": media_id, "filename": body.get("filename")})
                        return

                    if len(parts) == 4 and parts[0] == "agents" and parts[2] == "ops":
                        agent = self._auth_agent(parts[1])
                        op_name = parts[3]
                        result = self._handle_op(agent, op_name, body)
                        self._send_json(200, result)
                        return

                    self._send_json(404, {"error": {"code": "NOT_FOUND", "message": "Route not found."}})
                except SwitchProtocolError as exc:
                    self._send_json(exc.status_code, {"error": {"code": exc.code, "message": exc.message}})

            def _require_room_member(self, agent: AgentRegistration, room_id: str) -> RoomState:
                room = state.rooms.get(room_id)
                if room is None:
                    raise SwitchProtocolError(404, "ROOM_NOT_FOUND", f"Room '{room_id}' does not exist.")
                if agent.transport_user_id not in room.participants:
                    raise SwitchProtocolError(
                        403,
                        "NOT_IN_ROOM",
                        f"Agent '{agent.name}' is not a member of room '{room.name}'.",
                    )
                return room

            def _handle_op(self, agent: AgentRegistration, op_name: str, body: dict[str, Any]) -> dict[str, Any]:
                with state.lock:
                    if op_name == "load_internal_documents":
                        room = self._require_room_member(agent, body["room_id"])
                        return {
                            "documents": [
                                {
                                    "name": d.name,
                                    "description": d.description,
                                    "instructions": d.instructions,
                                    "content": d.content,
                                    "created_by": d.created_by,
                                }
                                for d in room.docs.values()
                            ]
                        }

                    if op_name == "create_room_document":
                        room = self._require_room_member(agent, body["room_id"])
                        doc_name = body["name"]
                        if doc_name in room.docs:
                            raise SwitchProtocolError(
                                409,
                                "DOCUMENT_EXISTS",
                                f"Room document '{doc_name}' already exists in '{room.name}'.",
                            )
                        doc = RoomDocument(
                            name=doc_name,
                            description=body.get("description", ""),
                            instructions=body.get("instructions", ""),
                            content=body.get("content", ""),
                            created_by=agent.transport_user_id,
                        )
                        room.docs[doc_name] = doc
                        return {"ok": True, "name": doc.name, "created_by": doc.created_by}

                    if op_name == "update_room_document":
                        room = self._require_room_member(agent, body["room_id"])
                        doc_name = body["name"]
                        doc = room.docs.get(doc_name)
                        if doc is None:
                            raise SwitchProtocolError(404, "DOCUMENT_NOT_FOUND", f"No document '{doc_name}'.")
                        if doc.created_by != agent.transport_user_id:
                            raise SwitchProtocolError(
                                403,
                                "SINGLE_WRITER_VIOLATION",
                                f"Document '{doc_name}' is single-writer owned by '{doc.created_by}'; "
                                f"agent '{agent.transport_user_id}' cannot overwrite it.",
                            )
                        doc.content = body["content"]
                        doc.updated_at = time.time()
                        return {"ok": True, "name": doc.name, "updated_at": doc.updated_at}

                    if op_name == "read_context":
                        room = self._require_room_member(agent, body["room_id"])
                        limit = int(body.get("limit", 50))
                        return {
                            "room_id": room.room_id,
                            "room_name": room.name,
                            "instructions": f"--- BEGIN ROOM INSTRUCTIONS ---\n{room.instructions.strip()}\n--- END ROOM INSTRUCTIONS ---",
                            "messages": list(room.messages[-limit:]),
                        }

                    if op_name == "list_participants":
                        room = self._require_room_member(agent, body["room_id"])
                        return {
                            "participants": [
                                {
                                    "transport_user_id": p.transport_user_id,
                                    "name": p.name,
                                    "type": p.participant_type,
                                    "is_owner": p.is_owner,
                                }
                                for p in room.participants.values()
                            ]
                        }

                    if op_name == "list_roles":
                        room = self._require_room_member(agent, body["room_id"])
                        return {
                            "roles": [
                                {
                                    "name": r.name,
                                    "instructions": r.instructions,
                                    "exclusive": r.exclusive,
                                    "holders": list(r.holders.keys()),
                                }
                                for r in room.roles.values()
                            ]
                        }

                    if op_name == "assume_role":
                        room = self._require_room_member(agent, body["room_id"])
                        role_name = body["role_name"]
                        cid = body.get("connection_id", "")
                        conn = state.connections.get(cid)
                        if conn is None or conn.agent_id != agent.agent_id or room.room_id not in conn.rooms:
                            raise SwitchProtocolError(
                                409,
                                "LIVE_CONNECTION_REQUIRED",
                                f"Agent '{agent.name}' must hold a live connection in '{room.name}' to assume '{role_name}'.",
                            )
                        role = room.roles.get(role_name)
                        if role is None:
                            raise SwitchProtocolError(404, "ROLE_NOT_FOUND", f"Role '{role_name}' not defined.")
                        if role.exclusive and role.holders and agent.agent_id not in role.holders:
                            holder_id = next(iter(role.holders.keys()))
                            raise SwitchProtocolError(
                                409,
                                "EXCLUSIVE_ROLE_HELD",
                                f"Exclusive role '{role_name}' is already leased by '{holder_id}'.",
                            )
                        role.holders[agent.agent_id] = cid
                        return {"ok": True, "role_name": role_name, "holder": agent.agent_id}

                    if op_name == "release_role":
                        room = self._require_room_member(agent, body["room_id"])
                        role_name = body["role_name"]
                        role = room.roles.get(role_name)
                        if role and agent.agent_id in role.holders:
                            del role.holders[agent.agent_id]
                        return {"ok": True, "role_name": role_name}

                    if op_name in ("post_message", "send_targeted_message"):
                        msg = state.post_room_message(
                            room_id=body["room_id"],
                            sender_id=agent.transport_user_id,
                            body=body["body"],
                            target_names=body.get("target_names"),
                            attachment_id=body.get("attachment_id"),
                        )
                        return {"ok": True, "message": msg}

                    if op_name == "create_schedule":
                        room = self._require_room_member(agent, body["room_id"])
                        sched = RoomSchedule(
                            schedule_id=f"sched-{uuid.uuid4().hex[:8]}",
                            room_id=room.room_id,
                            agent_ids=list(body.get("agent_ids") or [agent.agent_id]),
                            prompt=body["prompt"],
                            cron=body.get("cron"),
                            timezone=body.get("timezone", "UTC"),
                            created_by=agent.transport_user_id,
                        )
                        room.schedules.append(sched)
                        return {
                            "ok": True,
                            "schedule": {
                                "schedule_id": sched.schedule_id,
                                "room_id": sched.room_id,
                                "agent_ids": sched.agent_ids,
                                "prompt": sched.prompt,
                                "cron": sched.cron,
                                "timezone": sched.timezone,
                            },
                        }

                    if op_name == "list_schedules":
                        room = self._require_room_member(agent, body["room_id"])
                        return {
                            "schedules": [
                                {
                                    "schedule_id": s.schedule_id,
                                    "room_id": s.room_id,
                                    "agent_ids": s.agent_ids,
                                    "prompt": s.prompt,
                                    "cron": s.cron,
                                    "timezone": s.timezone,
                                }
                                for s in room.schedules
                            ]
                        }

                    raise SwitchProtocolError(400, "UNKNOWN_OP", f"Unsupported operation '{op_name}'.")

        return Handler


class SwitchAgentClient:
    """HTTP client speaking the Switch Agent Protocol for one registered agent."""

    def __init__(self, base_url: str, agent: AgentRegistration) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent = agent
        self.connection_id = f"conn-{agent.agent_id}-{uuid.uuid4().hex[:6]}"

    def _request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=raw,
            method=method,
            headers={
                "Authorization": f"Bearer {self.agent.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = json.loads(exc.read().decode("utf-8") or "{}")
            err = err_body.get("error") or {}
            raise SwitchProtocolError(
                exc.code,
                err.get("code", "HTTP_ERROR"),
                err.get("message", str(exc)),
            ) from exc

    def beat(self, rooms: list[str]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/agents/{self.agent.agent_id}/connection/beat",
            {"connection_id": self.connection_id, "rooms": rooms},
        )

    def op(self, op_name: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", f"/agents/{self.agent.agent_id}/ops/{op_name}", kwargs)

    def upload_media(self, filename: str, content_type: str, content: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/agents/{self.agent.agent_id}/media",
            {"filename": filename, "content_type": content_type, "content": content},
        )
