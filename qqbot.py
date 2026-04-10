"""
QQ Bot platform adapter for Hermes Agent.
QQ Open Platform WebSocket Gateway.

Env vars: QQBOT_APP_ID, QQBOT_CLIENT_SECRET
Requirements: pip install httpx websockets

config.yaml:
  platforms:
    qqbot:
      enabled: true
      extra:
        app_id: "xxx"
        client_secret: "xxx"
"""

import asyncio, json, logging, os, re, threading, time, uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter, MessageEvent, MessageType, SendResult, cache_image_from_url,
)

logger = logging.getLogger(__name__)

QQBOT_AUTH_URL = "https://bots.qq.com/app/getAppAccessToken"
QQBOT_GATEWAY_URL = "https://api.sgroup.qq.com/gateway"
QQBOT_API_BASE    = "https://api.sgroup.qq.com"

OP_DISPATCH=0; OP_HEARTBEAT=1; OP_IDENTIFY=2; OP_RESUME=6
OP_RECONNECT=7; OP_INVALID_SESSION=9; OP_HELLO=10; OP_HEARTBEAT_ACK=11

# Match OpenClaw behavior:
#   1<<30: GUILD_MESSAGES (AT_MESSAGE_CREATE)
#   1<<12: DIRECT_MESSAGE
#   1<<25: GROUP_AT + C2C
INTENTS_DEFAULT = (1<<30)|(1<<12)|(1<<25)
MAX_MESSAGE_LENGTH = 2000
HTTP_TIMEOUT = 15.0; SEND_TIMEOUT = 15.0
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
DEDUP_WINDOW_SECONDS = 300; DEDUP_MAX_SIZE = 2000
_MENTION_RE = re.compile(r"<@!?\d+>")

# ── msg_seq counter (per message-id/event-id deduplication) ────────────────────
_MSG_SEQ_BASE = 1_000_000
_msg_seq_map: Dict[str, int] = {}          # key → last used seq
_fallback_msg_seq = 0                      # global fallback counter
_msg_seq_lock = threading.Lock()            # type: ignore[attr-defined]


def _next_msg_seq(sequence_key: Optional[str] = None) -> int:
    """Return a monotonically increasing msg_seq for the given sequence key."""
    global _fallback_msg_seq
    with _msg_seq_lock:
        if not sequence_key:
            _fallback_msg_seq += 1
            return _MSG_SEQ_BASE + _fallback_msg_seq
        current = _msg_seq_map.get(sequence_key, 0) + 1
        _msg_seq_map[sequence_key] = current
        # Prune if map grows too large
        if len(_msg_seq_map) > 1000:
            for _ in range(500):
                _msg_seq_map.pop(next(iter(_msg_seq_map)), None)
        return _MSG_SEQ_BASE + current


def _resolve_msg_seq_key(msg_id: Optional[str], event_id: Optional[str]) -> Optional[str]:
    """Build the deduplication key for a passive message."""
    if msg_id:
        return f"msg:{msg_id}"
    if event_id:
        return f"event:{event_id}"
    return None


def check_qqbot_requirements(config: Optional[Any] = None) -> bool:
    """Return True if runtime deps and credentials are present.

    Credentials can come from environment variables *or* from the PlatformConfig
    extra dict – we accept either so the function stays consistent with other
    check_* helpers even when called without a config object.
    """
    if not HTTPX_AVAILABLE or not WEBSOCKETS_AVAILABLE:
        return False
    # Check env vars first (fast path)
    if os.getenv("QQBOT_APP_ID") and os.getenv("QQBOT_CLIENT_SECRET"):
        return True
    # Fall back to config.extra (allows YAML-only configuration)
    if config is not None:
        extra = getattr(config, "extra", None) or {}
        app_id = extra.get("app_id") or extra.get("appId")
        client_secret = extra.get("client_secret") or extra.get("clientSecret") or extra.get("appSecret")
        if app_id and client_secret:
            return True
    return False


class QQBotAdapter(BasePlatformAdapter):
    """QQ Open Platform chatbot adapter (WebSocket gateway + REST API)."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.QQBOT)
        extra = config.extra or {}
        # Accept both snake_case and camelCase keys for easier migration.
        self._app_id: str = (
            extra.get("app_id")
            or extra.get("appId")
            or os.getenv("QQBOT_APP_ID", "")
        )
        self._client_secret: str = (
            extra.get("client_secret")
            or extra.get("clientSecret")
            or extra.get("appSecret")
            or os.getenv("QQBOT_CLIENT_SECRET", "")
        )
        self._intents: int = int(extra.get("intents", INTENTS_DEFAULT))
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._ws: Any = None
        self._session_id: Optional[str] = None
        self._last_seq: Optional[int] = None
        self._heartbeat_interval: float = 40.0
        self._bot_user_id: Optional[str] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._http_client: Optional[Any] = None
        self._seen_messages: Dict[str, float] = {}
        # _reply_ctx stores per-chat context; the "msg_id" field now holds the
        # MOST RECENT msg_id for that chat (used as fallback when reply_to is not
        # explicitly provided).  Individual msg_ids are tracked in _seen_messages
        # for deduplication, so concurrent replies to different messages from the
        # same chat are safe as long as Hermes passes the correct reply_to.
        self._reply_ctx: Dict[str, Dict[str, Any]] = {}

    # -- Connection lifecycle --

    async def connect(self) -> bool:
        if not HTTPX_AVAILABLE:
            logger.warning("[%s] httpx not installed", self.name); return False
        if not WEBSOCKETS_AVAILABLE:
            logger.warning("[%s] websockets not installed", self.name); return False
        if not self._app_id or not self._client_secret:
            logger.warning("[%s] QQBOT_APP_ID/QQBOT_CLIENT_SECRET required", self.name); return False
        try:
            self._http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
            self._mark_connected()
            self._ws_task = asyncio.create_task(self._gateway_loop())
            logger.info("[%s] Started", self.name)
            return True
        except Exception as e:
            logger.error("[%s] Failed: %s", self.name, e)
            return False

    async def disconnect(self) -> None:
        # Cancel tasks first, then signal stop to avoid racing with _gateway_loop
        for t in (self._heartbeat_task, self._ws_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        self._heartbeat_task = self._ws_task = None
        self._running = False
        self._mark_disconnected()
        if self._ws:
            try: await self._ws.close()
            except Exception: pass
            self._ws = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        self._session_id = self._last_seq = None
        self._seen_messages.clear(); self._reply_ctx.clear()
        logger.info("[%s] Disconnected", self.name)

    # -- OAuth2 token --

    async def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token and self._token_expires_at > now + 60:
            return self._access_token
        resp = await self._http_client.post(
            QQBOT_AUTH_URL,
            json={"appId": self._app_id, "clientSecret": self._client_secret},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        d = resp.json()
        self._access_token = d["access_token"]
        self._token_expires_at = now + int(d.get("expires_in", 7200))
        return self._access_token

    def _auth_headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self._app_id,
            "Content-Type": "application/json",
        }

    # -- WebSocket gateway --

    async def _gateway_loop(self) -> None:
        idx = 0
        while self._running:
            try:
                await self._run_ws_session(); idx = 0
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running: return
                logger.warning("[%s] Session error: %s", self.name, e)
            if not self._running: return
            delay = RECONNECT_BACKOFF[min(idx, len(RECONNECT_BACKOFF)-1)]
            logger.info("[%s] Reconnecting in %ds", self.name, delay)
            await asyncio.sleep(delay); idx += 1

    async def _run_ws_session(self) -> None:
        token = await self._get_access_token()
        resp = await self._http_client.get(QQBOT_GATEWAY_URL, headers=self._auth_headers(token), timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        ws_url = resp.json().get("url", "")
        if not ws_url: raise RuntimeError("Empty WebSocket URL")
        async with websockets.connect(ws_url) as ws:
            self._ws = ws
            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
                try: await self._heartbeat_task
                except asyncio.CancelledError: pass
                self._heartbeat_task = None
            async for raw in ws:
                if not self._running: return
                try:
                    await self._handle_payload(json.loads(raw), ws)
                except asyncio.CancelledError: raise
                except Exception as e:
                    logger.error("[%s] Payload error: %s", self.name, e, exc_info=True)
        self._ws = None

    async def _handle_payload(self, p: Dict[str, Any], ws: Any) -> None:
        op = p.get("op"); seq = p.get("s")
        if seq is not None: self._last_seq = seq
        if op == OP_HELLO:
            d = p.get("d") or {}
            self._heartbeat_interval = d.get("heartbeat_interval", 40000) / 1000.0
            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            token = await self._get_access_token()
            if self._session_id and self._last_seq:
                await ws.send(json.dumps({"op": OP_RESUME, "d": {
                    "token": f"QQBot {token}", "session_id": self._session_id, "seq": self._last_seq}}))
            else:
                await ws.send(json.dumps({"op": OP_IDENTIFY, "d": {
                    "token": f"QQBot {token}", "intents": self._intents, "shard": [0, 1],
                    "properties": {"$os": "linux", "$browser": "hermes", "$device": "hermes"}}}))
        elif op == OP_RECONNECT:
            await ws.close()
        elif op == OP_INVALID_SESSION:
            self._session_id = self._last_seq = None
            # Cancel heartbeat before re-identifying to avoid racing sends
            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass
                self._heartbeat_task = None
            await asyncio.sleep(2)
            token = await self._get_access_token()
            await ws.send(json.dumps({"op": OP_IDENTIFY, "d": {
                "token": f"QQBot {token}", "intents": self._intents, "shard": [0, 1]}}))
        elif op == OP_DISPATCH:
            et = p.get("t"); ed = p.get("d") or {}; eid = p.get("id") or ""
            if et == "READY":
                self._session_id = ed.get("session_id")
                self._bot_user_id = str((ed.get("user") or {}).get("id", ""))
                logger.info("[%s] READY bot_id=%s", self.name, self._bot_user_id)
            elif et:
                await self._dispatch_event(et, ed, eid)

    async def _heartbeat_loop(self, ws: Any) -> None:
        try:
            while self._running:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._running: break
                try:
                    await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self._last_seq}))
                except Exception as e:
                    logger.warning("[%s] Heartbeat failed: %s", self.name, e); break
        except asyncio.CancelledError: pass

    # -- Inbound event dispatch --

    async def _dispatch_event(self, et: str, data: Dict[str, Any], eid: str) -> None:
        key = eid or data.get("id") or data.get("msg_id") or uuid.uuid4().hex
        if self._is_duplicate(key): return
        m = {"C2C_MESSAGE_CREATE": self._handle_c2c,
             "GROUP_AT_MESSAGE_CREATE": self._handle_group_at,
             "AT_MESSAGE_CREATE": self._handle_channel_at,
             "DIRECT_MESSAGE_CREATE": self._handle_direct}
        h = m.get(et)
        if h: await h(data)
        else: logger.debug("[%s] Unhandled event: %s", self.name, et)

    async def _handle_c2c(self, data: Dict[str, Any]) -> None:
        a = data.get("author") or {}
        sid = str(a.get("user_openid") or a.get("id") or "")
        content = self._clean_content(data.get("content", ""))
        msg_id = str(data.get("id", ""))
        mu, mt = await self._process_attachments(data)
        src = self.build_source(chat_id=sid, chat_type="dm", user_id=sid, user_name=a.get("username") or sid)
        self._reply_ctx[sid] = {"type": "c2c", "user_openid": sid, "msg_id": msg_id}
        await self.handle_message(MessageEvent(
            text=content, message_type=MessageType.PHOTO if mu else MessageType.TEXT,
            source=src, message_id=msg_id, media_urls=mu, media_types=mt,
            raw_message=data, reply_to_message_id=msg_id,
            timestamp=self._parse_timestamp(data.get("timestamp"))))

    async def _handle_group_at(self, data: Dict[str, Any]) -> None:
        goid = str(data.get("group_openid") or "")
        a = data.get("author") or {}
        sid = str(a.get("member_openid") or a.get("id") or "")
        content = self._clean_content(data.get("content", ""))
        msg_id = str(data.get("id", ""))
        mu, mt = await self._process_attachments(data)
        src = self.build_source(chat_id=goid, chat_name=goid, chat_type="group",
                                user_id=sid, user_name=a.get("username") or sid)
        self._reply_ctx[goid] = {"type": "group", "group_openid": goid, "msg_id": msg_id}
        await self.handle_message(MessageEvent(
            text=content, message_type=MessageType.PHOTO if mu else MessageType.TEXT,
            source=src, message_id=msg_id, reply_to_message_id=msg_id,
            media_urls=mu, media_types=mt, raw_message=data,
            timestamp=self._parse_timestamp(data.get("timestamp"))))

    async def _handle_channel_at(self, data: Dict[str, Any]) -> None:
        cid = str(data.get("channel_id") or ""); gid = str(data.get("guild_id") or "")
        a = data.get("author") or {}; sid = str(a.get("id") or "")
        content = self._clean_content(data.get("content", ""))
        msg_id = str(data.get("id", ""))
        mu, mt = await self._process_attachments(data)
        src = self.build_source(chat_id=cid, chat_name=data.get("channel_name") or cid,
                                chat_type="channel", user_id=sid, user_name=a.get("username") or sid)
        self._reply_ctx[cid] = {"type": "channel", "channel_id": cid, "guild_id": gid, "msg_id": msg_id}
        await self.handle_message(MessageEvent(
            text=content, message_type=MessageType.PHOTO if mu else MessageType.TEXT,
            source=src, message_id=msg_id, reply_to_message_id=msg_id,
            media_urls=mu, media_types=mt, raw_message=data,
            timestamp=self._parse_timestamp(data.get("timestamp"))))

    async def _handle_direct(self, data: Dict[str, Any]) -> None:
        gid = str(data.get("guild_id") or ""); cid = str(data.get("channel_id") or "")
        a = data.get("author") or {}; sid = str(a.get("id") or "")
        content = self._clean_content(data.get("content", ""))
        msg_id = str(data.get("id", ""))
        chat_id = gid or cid
        src = self.build_source(chat_id=chat_id, chat_type="dm",
                                user_id=sid, user_name=a.get("username") or sid)
        self._reply_ctx[chat_id] = {"type": "direct", "channel_id": cid, "guild_id": gid, "msg_id": msg_id}
        await self.handle_message(MessageEvent(
            text=content, message_type=MessageType.TEXT, source=src,
            message_id=msg_id, reply_to_message_id=msg_id, raw_message=data,
            timestamp=self._parse_timestamp(data.get("timestamp"))))

    # -- Outbound messaging --

    async def send(self, chat_id: str, content: str,
                   reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        metadata = metadata or {}
        ctx = metadata.get("reply_context") or self._reply_ctx.get(chat_id) or self._infer_context(chat_id)
        if not ctx:
            return SendResult(success=False,
                              error=f"No reply context for {chat_id!r}. Must receive a message first.")
        chunks = self.truncate_message(content, self.MAX_MESSAGE_LENGTH)
        result = SendResult(success=True)
        for idx, chunk in enumerate(chunks):
            # For the first chunk pass the caller's reply_to; subsequent chunks
            # have no reply reference so QQ will deduplicate by their own msg_seq.
            chunk_reply_to = reply_to if idx == 0 else None
            result = await self._send_chunk(ctx, chunk, reply_to=chunk_reply_to)
            if not result.success: return result
        return result

    async def _send_chunk(self, ctx: Dict[str, Any], content: str,
                          reply_to: Optional[str] = None) -> SendResult:
        try:
            msg_id = reply_to or ctx.get("msg_id")
            t = ctx.get("type", "")
            if t == "c2c":
                return await self._post_c2c(ctx["user_openid"], content, msg_id)
            elif t == "group":
                return await self._post_group(ctx["group_openid"], content, msg_id)
            elif t == "channel":
                return await self._post_channel(ctx["channel_id"], content, msg_id)
            elif t == "direct":
                return await self._post_direct(ctx["guild_id"], content, msg_id)
            return SendResult(success=False, error=f"Unknown type: {t!r}")
        except Exception as e:
            logger.error("[%s] send_chunk: %s", self.name, e, exc_info=True)
            return SendResult(success=False, error=str(e), retryable=True)

    async def _post_c2c(self, uid, content, msg_id):
        url = f"{QQBOT_API_BASE}/v2/users/{uid}/messages"
        event_id = f"hermes_{uuid.uuid4().hex[:8]}"
        p: Dict[str, Any] = {"content": content, "msg_type": 0}
        if msg_id:
            p["msg_id"] = msg_id
        else:
            p["event_id"] = event_id
        seq_key = _resolve_msg_seq_key(msg_id, event_id)
        return await self._send_with_msgseq_retry(url, p, seq_key)

    async def _post_group(self, goid, content, msg_id):
        url = f"{QQBOT_API_BASE}/v2/groups/{goid}/messages"
        event_id = f"hermes_{uuid.uuid4().hex[:8]}"
        p: Dict[str, Any] = {"content": content, "msg_type": 0}
        if msg_id:
            p["msg_id"] = msg_id
        else:
            p["event_id"] = event_id
        seq_key = _resolve_msg_seq_key(msg_id, event_id)
        return await self._send_with_msgseq_retry(url, p, seq_key)

    async def _post_channel(self, cid, content, msg_id):
        url = f"{QQBOT_API_BASE}/channels/{cid}/messages"
        p: Dict[str, Any] = {"content": content}
        event_id = f"hermes_{uuid.uuid4().hex[:8]}"
        if msg_id:
            p["msg_id"] = msg_id
        else:
            # Guild channel messages should always be replies; log a warning
            # but still attempt to send with a pseudo event_id.
            logger.warning(
                "[QQBot] Sending channel message without msg_id – QQ may reject passive replies"
            )
            p["event_id"] = event_id
        seq_key = _resolve_msg_seq_key(msg_id, event_id)
        return await self._send_with_msgseq_retry(url, p, seq_key)

    async def _post_direct(self, gid, content, msg_id):
        url = f"{QQBOT_API_BASE}/dms/{gid}/messages"
        p: Dict[str, Any] = {"content": content}
        event_id = f"hermes_{uuid.uuid4().hex[:8]}"
        if msg_id:
            p["msg_id"] = msg_id
        else:
            logger.warning(
                "[QQBot] Sending direct message without msg_id – QQ may reject passive replies"
            )
            p["event_id"] = event_id
        seq_key = _resolve_msg_seq_key(msg_id, event_id)
        return await self._send_with_msgseq_retry(url, p, seq_key)

    async def _send_with_msgseq_retry(
        self, url: str,
        payload: Dict[str, Any],
        seq_key: Optional[str],
        max_retries: int = 5,
    ) -> SendResult:
        """Send a passive message with automatic msg_seq retry on 40054005 (dedup) errors.

        Refreshes the access token on each retry to handle token expiry mid-loop.
        """
        last_error: SendResult = SendResult(success=False, error="unknown")
        for attempt in range(max_retries):
            # Always get a fresh token – the cached one may have expired since
            # the last iteration, especially on longer retry loops.
            token = await self._get_access_token()
            headers = self._auth_headers(token)
            p = dict(payload)
            p["msg_seq"] = _next_msg_seq(seq_key)
            result = await self._http_post(url, headers, p)
            if result.success:
                return result
            # Check if this is a msgseq deduplication error (code 40054005)
            raw = result.error or ""
            if "40054005" not in raw and "msgseq" not in raw.lower() and "去重" not in raw:
                # Non-retryable error – give up immediately
                return result
            last_error = result
            if attempt == max_retries - 1:
                return result
            logger.debug("[%s] msg_seq retry %d/%d: %s", self.name, attempt + 1, max_retries, raw[:80])
        return last_error

    async def _http_post(self, url, headers, payload) -> SendResult:
        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized")
        try:
            resp = await self._http_client.post(url, json=payload, headers=headers, timeout=SEND_TIMEOUT)
            if resp.status_code < 300:
                d = resp.json()
                return SendResult(success=True,
                                  message_id=str(d.get("id") or uuid.uuid4().hex[:12]),
                                  raw_response=d)
            body = resp.text
            logger.warning("[%s] HTTP %d %s: %s", self.name, resp.status_code, url, body[:200])
            return SendResult(success=False, error=f"HTTP {resp.status_code}: {body[:200]}",
                              retryable=resp.status_code >= 500)
        except httpx.TimeoutException:
            return SendResult(success=False, error="QQ Bot API timed out")
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_image(self, chat_id: str, image_url: str,
                         caption: Optional[str] = None,
                         reply_to: Optional[str] = None,
                         metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Send image via QQ rich-media API; falls back to URL text."""
        metadata = metadata or {}
        ctx = (metadata.get("reply_context") or self._reply_ctx.get(chat_id)
               or self._infer_context(chat_id))
        if ctx and ctx.get("type") in ("c2c", "group"):
            try:
                token = await self._get_access_token()
                headers = self._auth_headers(token)
                msg_id = reply_to or ctx.get("msg_id")
                result = await self._post_rich_media_image(headers, ctx, image_url, caption, msg_id)
                if result.success: return result
            except Exception as e:
                logger.warning("[%s] rich-media image failed: %s", self.name, e)
        text = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)

    async def _post_rich_media_image(self, headers, ctx, image_url, caption, msg_id) -> SendResult:
        t = ctx["type"]
        if t == "c2c":
            uid = ctx["user_openid"]
            media_url = f"{QQBOT_API_BASE}/v2/users/{uid}/rich_media"
            msg_url = f"{QQBOT_API_BASE}/v2/users/{uid}/messages"
        else:
            gid = ctx["group_openid"]
            media_url = f"{QQBOT_API_BASE}/v2/groups/{gid}/rich_media"
            msg_url = f"{QQBOT_API_BASE}/v2/groups/{gid}/messages"
        mr = await self._http_post(media_url, headers, {"file_type": 1, "url": image_url, "srv_send_msg": False})
        if not mr.success: return mr
        file_info = (mr.raw_response or {}).get("file_info", "")
        if not file_info: return SendResult(success=False, error="No file_info in rich_media response")
        event_id = f"hermes_{uuid.uuid4().hex[:8]}"
        seq_key = _resolve_msg_seq_key(msg_id, event_id)
        p: Dict[str, Any] = {"msg_type": 7, "media": {"file_info": file_info}}
        if caption: p["content"] = caption
        if msg_id: p["msg_id"] = msg_id
        else: p["event_id"] = event_id
        return await self._send_with_msgseq_retry(msg_url, p, seq_key)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict] = None) -> None:
        """Send a C2C input-notify (typing indicator) via QQ rich-media API.

        Only supported for C2C messages; silently ignored for other chat types.
        QQ requires the user to have messaged the bot recently (within a time window)
        before input-notify will be delivered.
        """
        metadata = metadata or {}
        ctx = metadata.get("reply_context") or self._reply_ctx.get(chat_id)
        if not ctx or ctx.get("type") != "c2c":
            return  # QQ input-notify only works for C2C; ignore silently
        try:
            uid = ctx.get("user_openid", "")
            msg_id = ctx.get("msg_id")
            event_id = f"hermes_{uuid.uuid4().hex[:8]}"
            p: Dict[str, Any] = {"msg_type": 6, "input_notify": {"input_type": 1, "input_second": 60}}
            if msg_id:
                p["msg_id"] = msg_id
            else:
                p["event_id"] = event_id
            seq_key = _resolve_msg_seq_key(msg_id, event_id)
            await self._send_with_msgseq_retry(
                f"{QQBOT_API_BASE}/v2/users/{uid}/messages",
                p,
                seq_key,
            )
        except Exception as e:
            logger.debug("[%s] send_typing ignored: %s", self.name, e)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        ctx = self._reply_ctx.get(chat_id) or {}
        t = ctx.get("type", "dm")
        return {
            "id": chat_id,
            "name": ctx.get("chat_name") or chat_id,
            "type": "direct" if t == "c2c" else t,
            "channel_id": ctx.get("channel_id"),
            "guild_id": ctx.get("guild_id"),
            "group_openid": ctx.get("group_openid"),
            "user_openid": ctx.get("user_openid"),
        }

    def get_self_user_id(self) -> Optional[str]:
        """Return the bot's own user ID once the READY event has been received."""
        return self._bot_user_id

    # -- Helpers --

    def _clean_content(self, s: str) -> str:
        return _MENTION_RE.sub("", s).strip()

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        if len(self._seen_messages) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW_SECONDS
            self._seen_messages = {k: v for k, v in self._seen_messages.items() if v > cutoff}
        if msg_id in self._seen_messages: return True
        self._seen_messages[msg_id] = now
        return False

    @staticmethod
    def _parse_timestamp(ts: Any) -> datetime:
        if not ts: return datetime.now(tz=timezone.utc)
        try:
            if isinstance(ts, (int, float)):
                val = float(ts)
                if val > 1e10: val /= 1000.0
                return datetime.fromtimestamp(val, tz=timezone.utc)
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            return datetime.now(tz=timezone.utc)

    @staticmethod
    def _extract_attachments(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        atts = list(data.get("attachments") or [])
        img = data.get("image")
        if img and isinstance(img, dict): atts = [img] + atts
        return atts

    async def _process_attachments(self, data: Dict[str, Any]) -> Tuple[List[str], List[str]]:
        mu: List[str] = []; mt: List[str] = []
        for att in self._extract_attachments(data):
            url = att.get("url") or att.get("image")
            if not url: continue
            ct = att.get("content_type", "")
            is_img = "image" in ct or any(
                url.lower().endswith(x) for x in (".jpg", ".jpeg", ".png", ".webp", ".gif"))
            if is_img:
                try:
                    local = await cache_image_from_url(url)
                    mu.append(local); mt.append("image/jpeg")
                except Exception as e:
                    logger.warning("[QQBot] Attachment cache failed %s: %s", url[:60], e)
        return mu, mt

    def _infer_context(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Guess chat type from chat_id format as last resort.

        Heuristics (not guaranteed):
          - Pure numeric  → guild channel (channel_id)
          - 32-char hex   → QQ group openid
          - 28+ alnum     → C2C user openid
        These are best-effort; replies may fail if the guess is wrong.
        """
        if not chat_id:
            return None
        if chat_id.isdigit():
            # Numeric IDs are guild channel_ids
            return {"type": "channel", "channel_id": chat_id, "guild_id": "", "msg_id": None}
        if len(chat_id) == 32 and chat_id.isalnum():
            # 32-char hex → group openid
            return {"type": "group", "group_openid": chat_id, "msg_id": None}
        if len(chat_id) >= 20 and chat_id.isalnum():
            # Long alnum → C2C user openid
            return {"type": "c2c", "user_openid": chat_id, "msg_id": None}
        return None
