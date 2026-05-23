"""
YZJ (云之家) Platform Adapter for Hermes Gateway
=================================================

入站：WebSocket 长连接，支持两种凭据：
  - app_id + app_secret  →  accessToken  →  wss://.../websocket?accessToken=...
  - send_msg_url（含 yzjtoken）  →  wss://.../websocket?yzjtoken=...

出站：优先使用 App API（/gateway/xtinterface/message/send），
      若未配置 app_id/app_secret 则降级为 send_msg_url（旧版机器人 Webhook）。

单账户 · App API 模式（config.yaml）:
  yzj:
    app_id: your_app_id
    app_secret: your_app_secret
    endpoint: https://yunzhijia.com   # 可选，默认值
    timeout: 10                        # 可选，默认值（秒）

单账户 · send_msg_url 模式（旧版机器人，入站+出站均支持）:
  yzj:
    send_msg_url: https://www.yunzhijia.com/gateway/robot/send?yzjtoken=xxx

多账户混合配置（config.yaml）:
  yzj:
    endpoint: https://yunzhijia.com
    timeout: 10
    accounts:
      new_bot:
        app_id: app_id_1
        app_secret: secret_1
      legacy_bot:
        send_msg_url: https://www.yunzhijia.com/gateway/robot/send?yzjtoken=xxx

多账户时，chat_id 格式为 "{account}@group:{groupId}" 或 "{account}@user:{openId}"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp

from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_YZJ_ENDPOINT = "https://yunzhijia.com"
DEFAULT_TIMEOUT = 10
MAX_MESSAGE_LENGTH = 20480
TOKEN_REFRESH_MARGIN_SECONDS = 60

# ---------------------------------------------------------------------------
# YAML config helper
# ---------------------------------------------------------------------------


def apply_yaml_config(yaml_cfg: dict, platform_cfg: PlatformConfig) -> Optional[dict]:
    """
    Serialize the yzj: config section into PlatformConfig.extra as JSON,
    so the adapter can access the full structured config including accounts.
    """
    if not yaml_cfg:
        return None
    return {"yzj_config": json.dumps(yaml_cfg)}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_endpoint(endpoint: Optional[str]) -> str:
    raw = (endpoint or DEFAULT_YZJ_ENDPOINT).strip().rstrip("/")
    parsed = urlparse(raw)
    if not parsed.scheme:
        raw = "https://" + raw
        parsed = urlparse(raw)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def _resolve_url(endpoint: str, path: str) -> str:
    base = _normalize_endpoint(endpoint).rstrip("/") + "/"
    return urljoin(base, path.lstrip("/"))


def _extract_yzjtoken(send_msg_url: str) -> Optional[str]:
    """Extract yzjtoken from a send_msg_url query string."""
    try:
        from urllib.parse import parse_qs
        qs = parse_qs(urlparse(send_msg_url).query)
        token = (qs.get("yzjtoken") or [""])[0].strip()
        return token or None
    except Exception:
        return None


def _parse_chat_id(chat_id: str) -> tuple[str, Optional[str], Optional[str]]:
    """Parse chat_id into (account_name, group_id, to_open_id)."""
    if "@" in chat_id:
        account_name, rest = chat_id.split("@", 1)
    else:
        account_name, rest = "", chat_id
    if rest.startswith("group:"):
        return account_name, rest[6:], None
    if rest.startswith("user:"):
        return account_name, None, rest[5:]
    return account_name, None, rest


# ---------------------------------------------------------------------------
# YZJ WebSocket payload classification  (ported from openclaw-yzj)
# ---------------------------------------------------------------------------

def _normalize_business_message(record: dict) -> Optional[dict]:
    """Validate and normalise a flat business-message dict.
    Returns a clean dict or None if required fields are missing.
    """
    if not isinstance(record.get("robotId"), str): return None
    if not isinstance(record.get("robotName"), str): return None
    if not isinstance(record.get("operatorOpenid"), str): return None
    if not isinstance(record.get("operatorName"), str): return None
    if not isinstance(record.get("msgId"), str): return None
    if not isinstance(record.get("content"), str): return None
    if not isinstance(record.get("type"), int): return None
    if not isinstance(record.get("time"), (int, float)): return None

    msg: dict = {
        "type":           record["type"],
        "robotId":        record["robotId"],
        "robotName":      record["robotName"],
        "operatorOpenid": record["operatorOpenid"],
        "operatorName":   record["operatorName"],
        "time":           record["time"],
        "msgId":          record["msgId"],
        "content":        record["content"],
        "groupType":      record.get("groupType") if isinstance(record.get("groupType"), int) else 0,
    }
    group_id = record.get("groupId", "")
    if isinstance(group_id, str) and group_id.strip():
        msg["groupId"] = group_id.strip()
    return msg


def _normalize_business_envelope(record: dict) -> Optional[dict]:
    """Unwrap messages nested in an envelope (cmd=directPush / type=robotMessage)."""
    envelope_type = str(record.get("type") or "").strip().lower()
    cmd = str(record.get("cmd") or "").strip().lower()
    if envelope_type not in ("robotmessage",) and cmd not in ("robotmessage", "directpush"):
        return None
    inner = record.get("msg")
    if not isinstance(inner, dict):
        return None
    return _normalize_business_message(inner)


def _build_directpush_ack(record: dict) -> Optional[str]:
    """Build ACK payload if the frame requires one (level=1, needAck=True)."""
    cmd = str(record.get("cmd") or "").strip().lower()
    if cmd != "directpush":
        return None
    if record.get("needAck") is not True:
        return None
    if record.get("level") != 1:
        return None
    seq = record.get("seq")
    if not isinstance(seq, int):
        return None
    biz_type = str(record.get("type") or "directPush")
    return json.dumps({"cmd": "directPush", "type": "ack", "bizType": biz_type, "level": 1, "endSeqId": seq})


def _classify_payload(record: dict) -> tuple[str, Optional[dict], Optional[str]]:
    """Classify a parsed WebSocket frame.

    Returns (kind, message, ack_str) where kind is:
      "dispatch"  — a valid business message; message is the normalised dict
      "control"   — a control/ack frame; message is None
      "invalid"   — unrecognised; message is None
    """
    ack = _build_directpush_ack(record)

    # Try flat business message first
    msg = _normalize_business_message(record)
    if msg:
        return "dispatch", msg, ack

    # Try envelope (nested in record["msg"])
    msg = _normalize_business_envelope(record)
    if msg:
        return "dispatch", msg, ack

    # Control frames
    cmd = str(record.get("cmd") or "").strip().lower()
    outer_type = str(record.get("type") or "").strip().lower()

    if cmd == "ping" or outer_type == "ping":
        return "control", None, json.dumps({"cmd": "pong"})

    if cmd in ("auth", "pong", "ack") or outer_type in ("pong", "ack", "close", "msgchg"):
        return "control", None, ack

    if cmd or outer_type:
        return "control", None, ack

    return "invalid", None, None


# ---------------------------------------------------------------------------
# YZJ conversation resolver  (ported from openclaw-yzj)
# ---------------------------------------------------------------------------

def _is_private_robot_group(group_id: str) -> bool:
    return group_id.upper().startswith("BOT-")


def _is_direct_conversation(group_type: Optional[int], group_id: str) -> bool:
    if group_type == 1 or group_type == 3:
        return True
    if group_type == 2 or group_type == 4:
        return False
    return _is_private_robot_group(group_id)


def _resolve_conversation(account_name: str, msg: dict) -> tuple[str, str]:
    """Return (chat_id, chat_type) for a validated YZJ message.

    chat_id format: "{account}@user:{openid}"  or  "{account}@group:{groupId}"
    """
    operator_openid = msg.get("operatorOpenid", "").strip() or "unknown"
    robot_id = msg.get("robotId", "").strip() or "unknown"
    raw_group_id = msg.get("groupId", "").strip() or ""
    group_type = msg.get("groupType")

    if _is_direct_conversation(group_type, raw_group_id):
        return f"{account_name}@user:{operator_openid}", "dm"

    group_target = raw_group_id or robot_id
    return f"{account_name}@group:{group_target}", "group"


# ---------------------------------------------------------------------------
# Per-account config
# ---------------------------------------------------------------------------


@dataclass
class _AccountConfig:
    name: str
    app_id: str
    app_secret: str
    endpoint: str
    timeout: float
    send_msg_url: str   # outbound fallback when app_id/app_secret not set


def _parse_accounts(yaml_cfg: dict) -> List[_AccountConfig]:
    """
    Parse the yzj: YAML section into a list of _AccountConfig.
    Supports both single-account (flat keys) and multi-account (accounts: dict) layouts.
    """
    global_endpoint = _normalize_endpoint(yaml_cfg.get("endpoint"))
    global_timeout = float(yaml_cfg.get("timeout", DEFAULT_TIMEOUT))
    global_send_msg_url = str(yaml_cfg.get("send_msg_url") or "").strip()

    accounts_dict: dict = yaml_cfg.get("accounts", {})

    if accounts_dict:
        result = []
        for name, acct in accounts_dict.items():
            if not isinstance(acct, dict):
                continue
            app_id = str(acct.get("app_id", "")).strip()
            app_secret = str(acct.get("app_secret", "")).strip()
            send_msg_url = str(acct.get("send_msg_url") or global_send_msg_url).strip()

            if not app_id and not send_msg_url:
                logger.warning(f"[yzj] Account '{name}' missing app_id/app_secret and send_msg_url, skipping.")
                continue

            result.append(_AccountConfig(
                name=name,
                app_id=app_id,
                app_secret=app_secret,
                endpoint=_normalize_endpoint(acct.get("endpoint") or global_endpoint),
                timeout=float(acct.get("timeout", global_timeout)),
                send_msg_url=send_msg_url,
            ))
        return result

    # Single-account flat layout
    app_id = str(yaml_cfg.get("app_id", "")).strip()
    app_secret = str(yaml_cfg.get("app_secret", "")).strip()
    if app_id or global_send_msg_url:
        return [_AccountConfig(
            name="default",
            app_id=app_id,
            app_secret=app_secret,
            endpoint=global_endpoint,
            timeout=global_timeout,
            send_msg_url=global_send_msg_url,
        )]
    return []


# ---------------------------------------------------------------------------
# Access token provider
# ---------------------------------------------------------------------------


class _AccessTokenProvider:
    def __init__(self, cfg: _AccountConfig, session: aiohttp.ClientSession) -> None:
        self._cfg = cfg
        self._session = session
        self._cached_token: str = ""
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        async with self._lock:
            if self._cached_token and time.monotonic() < self._expires_at:
                return self._cached_token
            token, expires_in = await self._fetch_token()
            self._cached_token = token
            margin = max(TOKEN_REFRESH_MARGIN_SECONDS, int(expires_in) // 10)
            self._expires_at = time.monotonic() + max(60.0, expires_in - margin)
            return self._cached_token

    async def _fetch_token(self) -> tuple[str, float]:
        url = _resolve_url(self._cfg.endpoint, "/api/oauth2_v12/auth/getAppAccessToken")
        payload = {
            "appId": self._cfg.app_id,
            "secret": self._cfg.app_secret,
            "timestamp": int(time.time() * 1000),
        }
        timeout = aiohttp.ClientTimeout(total=self._cfg.timeout)
        async with self._session.post(url, json=payload, timeout=timeout) as resp:
            text = await resp.text()
            if not resp.ok:
                raise RuntimeError(f"getAccessToken HTTP {resp.status}: {text}")
            data = json.loads(text)
            if not data.get("success"):
                code = data.get("errorCode", resp.status)
                msg = data.get("error", "unknown error")
                raise RuntimeError(f"getAccessToken failed: {code} {msg}")
            access_token = (data.get("data") or {}).get("accessToken", "").strip()
            if not access_token:
                raise RuntimeError("getAccessToken response missing accessToken")
            expire_in = float((data.get("data") or {}).get("expireIn") or 3600)
            return access_token, expire_in


# ---------------------------------------------------------------------------
# Per-account session (token provider + WebSocket loop)
# ---------------------------------------------------------------------------


class _AccountSession:
    def __init__(self, cfg: _AccountConfig, session: aiohttp.ClientSession,
                 on_message: Any) -> None:
        self.cfg = cfg
        self._session = session
        self._on_message = on_message  # async callable(account_name, payload)
        self.token_provider = _AccessTokenProvider(cfg, session) if cfg.app_id and cfg.app_secret else None
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_conn: Optional[aiohttp.ClientWebSocketResponse] = None
        self._dedup_seen: dict[str, float] = {}
        self._dedup_ttl: float = 60.0

    async def start(self) -> bool:
        """Start WebSocket loop: use access token (app API) or yzjtoken (send_msg_url)."""
        if self.token_provider:
            try:
                await self.token_provider.get_access_token()
            except Exception as exc:
                logger.error(f"[yzj/{self.cfg.name}] Failed to obtain access token: {exc}")
                return False
            self._ws_task = asyncio.create_task(self._websocket_loop())
            logger.info(f"[yzj/{self.cfg.name}] Account session started (websocket/accessToken).")
        elif self.cfg.send_msg_url:
            yzjtoken = _extract_yzjtoken(self.cfg.send_msg_url)
            if not yzjtoken:
                logger.error(
                    f"[yzj/{self.cfg.name}] send_msg_url has no yzjtoken parameter, cannot connect."
                )
                return False
            self._ws_task = asyncio.create_task(self._websocket_loop())
            logger.info(f"[yzj/{self.cfg.name}] Account session started (websocket/yzjtoken).")
        else:
            logger.error(f"[yzj/{self.cfg.name}] No credentials configured, skipping.")
            return False
        return True

    def _build_ws_url(self, access_token: Optional[str]) -> str:
        """Build the wss:// URL: accessToken if available, else yzjtoken from send_msg_url."""
        if access_token:
            host = urlparse(_normalize_endpoint(self.cfg.endpoint)).netloc
            return f"wss://{host}/xuntong/websocket?accessToken={access_token}"
        yzjtoken = _extract_yzjtoken(self.cfg.send_msg_url)
        parsed = urlparse(self.cfg.send_msg_url)
        return f"wss://{parsed.netloc}/xuntong/websocket?yzjtoken={yzjtoken}"

    async def stop(self) -> None:
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await asyncio.wait_for(self._ws_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        if self._ws_conn and not self._ws_conn.closed:
            await self._ws_conn.close()

    async def _websocket_loop(self) -> None:
        backoff = 1.0
        max_backoff = 60.0

        while True:
            try:
                access_token = await self.token_provider.get_access_token() if self.token_provider else None
                ws_url = self._build_ws_url(access_token)
                logger.info(f"[yzj/{self.cfg.name}] Connecting WebSocket...")

                async with self._session.ws_connect(
                    ws_url,
                    heartbeat=30.0,
                    timeout=aiohttp.ClientWSTimeout(ws_close=self.cfg.timeout),
                ) as ws:
                    self._ws_conn = ws
                    backoff = 1.0
                    logger.info(f"[yzj/{self.cfg.name}] WebSocket connected.")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._on_raw_message(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await self._on_raw_message(msg.data.decode("utf-8", errors="replace"))
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"[yzj/{self.cfg.name}] WebSocket error: {ws.exception()}")
                            break
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                            logger.info(f"[yzj/{self.cfg.name}] WebSocket closed by server.")
                            break

            except asyncio.CancelledError:
                logger.info(f"[yzj/{self.cfg.name}] WebSocket loop cancelled.")
                return
            except Exception as exc:
                logger.warning(
                    f"[yzj/{self.cfg.name}] WebSocket error: {exc}. Reconnecting in {backoff}s..."
                )

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def _on_raw_message(self, raw: str) -> None:
        raw = raw.strip()
        logger.debug("[yzj/%s] raw frame: %s", self.cfg.name, raw)
        if not raw or raw.lower() in ("ping", "pong"):
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("[yzj/%s] frame is not JSON, ignoring", self.cfg.name)
            return
        if not isinstance(payload, dict):
            return

        kind, msg, ack = _classify_payload(payload)

        # Send ACK if required
        if ack and self._ws_conn and not self._ws_conn.closed:
            try:
                await self._ws_conn.send_str(ack)
            except Exception:
                pass

        if kind == "dispatch" and msg:
            await self._on_message(self.cfg.name, msg)
        elif kind == "invalid":
            logger.debug("[yzj/%s] unrecognised frame keys=%s", self.cfg.name, list(payload.keys()))

    def is_duplicate(self, msg_id: str) -> bool:
        now = time.monotonic()
        expired = [k for k, t in self._dedup_seen.items() if now - t > self._dedup_ttl]
        for k in expired:
            del self._dedup_seen[k]
        if msg_id in self._dedup_seen:
            return True
        self._dedup_seen[msg_id] = now
        return False


# ---------------------------------------------------------------------------
# Main adapter
# ---------------------------------------------------------------------------


class YZJAdapter(BasePlatformAdapter):
    """
    YZJ (云之家) platform adapter for Hermes Gateway.

    Reads all configuration from config.yaml (yzj: section).
    Supports multiple accounts, each with its own WebSocket connection.
    Outbound falls back to send_msg_url when app_id/app_secret is not configured.
    """

    def __init__(self, config: PlatformConfig, platform: Platform) -> None:
        super().__init__(config, platform)

        extra: dict = config.extra or {}
        raw_cfg_json: str = extra.get("yzj_config", "")
        if raw_cfg_json:
            logger.info("[yzj] __init__: loading config from PlatformConfig.extra")
            try:
                yaml_cfg: dict = json.loads(raw_cfg_json)
            except json.JSONDecodeError:
                logger.error("[yzj] __init__: failed to parse yzj_config JSON")
                yaml_cfg = {}
        else:
            logger.warning("[yzj] __init__: yzj_config not in extra, falling back to load_config()")
            logger.debug("[yzj] __init__: extra keys = %s", list(extra.keys()))
            try:
                from hermes_cli.config import load_config
                hermes_cfg = load_config()
                yaml_cfg = hermes_cfg.get("yzj") or {}
                logger.info("[yzj] __init__: loaded yzj section from config, keys=%s", list(yaml_cfg.keys()))
            except Exception as exc:
                logger.error("[yzj] __init__: load_config() failed: %s", exc)
                yaml_cfg = {}

        self._account_configs: List[_AccountConfig] = _parse_accounts(yaml_cfg)
        logger.info("[yzj] __init__: parsed %d account(s): %s",
                    len(self._account_configs),
                    [a.name for a in self._account_configs])
        self._session: Optional[aiohttp.ClientSession] = None
        self._account_sessions: Dict[str, _AccountSession] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        logger.info("[yzj] connect() called, %d account config(s)", len(self._account_configs))
        if not self._account_configs:
            logger.error(
                "[yzj] No accounts configured. Add yzj: app_id/app_secret (or send_msg_url) "
                "to config.yaml."
            )
            return False

        self._session = aiohttp.ClientSession()

        any_ok = False
        for cfg in self._account_configs:
            logger.info("[yzj] Starting account '%s' (send_msg_url=%s, app_id=%s)",
                        cfg.name, bool(cfg.send_msg_url), bool(cfg.app_id))
            acct_session = _AccountSession(cfg, self._session, self._on_account_message)
            ok = await acct_session.start()
            if ok:
                self._account_sessions[cfg.name] = acct_session
                any_ok = True
            else:
                logger.error(f"[yzj] Account '{cfg.name}' failed to start, skipping.")

        if not any_ok:
            await self._session.close()
            return False

        self._mark_connected()
        logger.info(
            f"[yzj] Connected with {len(self._account_sessions)} account(s): "
            f"{list(self._account_sessions.keys())}"
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        for acct_session in self._account_sessions.values():
            await acct_session.stop()
        self._account_sessions.clear()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("[yzj] Disconnected.")

    def _default_session(self) -> Optional[_AccountSession]:
        if not self._account_sessions:
            return None
        return next(iter(self._account_sessions.values()))

    def _resolve_session(self, account_name: str) -> Optional[_AccountSession]:
        if account_name and account_name in self._account_sessions:
            return self._account_sessions[account_name]
        return self._default_session()

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def _is_valid_inbound(self, msg: dict) -> bool:
        for field_name in ("robotId", "robotName", "operatorOpenid", "operatorName", "msgId", "content"):
            if not isinstance(msg.get(field_name), str):
                return False
        if not isinstance(msg.get("type"), int):
            return False
        if not isinstance(msg.get("time"), (int, float)):
            return False
        if not isinstance(msg.get("groupType"), int):
            return False
        return bool(msg.get("content", "").strip())

    async def _on_account_message(self, account_name: str, payload: dict) -> None:
        """Called by _AccountSession for each validated business message."""
        msg_id: str = payload.get("msgId", "")
        acct_session = self._account_sessions.get(account_name)
        if acct_session and acct_session.is_duplicate(msg_id):
            logger.debug(f"[yzj/{account_name}] Duplicate message ignored: {msg_id}")
            return

        content: str = payload.get("content", "").strip()
        if not content:
            return

        chat_id, chat_type = _resolve_conversation(account_name, payload)
        sender_id: str = payload.get("operatorOpenid", "").strip()
        sender_name: str = payload.get("operatorName", "").strip()
        group_id: str = payload.get("groupId", "").strip()

        source = SessionSource(
            platform=self.platform,
            chat_id=chat_id,
            user_id=sender_id,
            user_name=sender_name,
            chat_type=chat_type,
        )

        event = MessageEvent(
            text=content,
            message_type=MessageType.COMMAND if content.startswith("/") else MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=msg_id,
            channel_context=json.dumps({
                "CurrentMessageId": msg_id,
                "SenderId": sender_id,
                "SenderName": sender_name,
                "GroupId": group_id,
                "RawBody": content,
                "YZJAccount": account_name,
            }),
        )

        logger.info(
            f"[yzj/{account_name}] Inbound {chat_type} from {sender_name}({sender_id}): {content[:80]}"
        )
        await self.handle_message(event)

    # ------------------------------------------------------------------
    # Outbound: send text
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not content.strip():
            return SendResult(success=True, message_id="")

        if len(content) > MAX_MESSAGE_LENGTH:
            chunks = [
                content[i : i + MAX_MESSAGE_LENGTH]
                for i in range(0, len(content), MAX_MESSAGE_LENGTH)
            ]
            last_result = SendResult(success=True, message_id="")
            continuation_ids: list[str] = []
            for chunk in chunks:
                r = await self._send_text(chat_id, chunk, reply_to)
                if not r.success:
                    return r
                continuation_ids.append(r.message_id or "")
                last_result = r
            return SendResult(
                success=True,
                message_id=last_result.message_id,
                continuation_message_ids=tuple(continuation_ids[1:]),
            )

        return await self._send_text(chat_id, content, reply_to)

    async def _send_text(self, chat_id: str, text: str, reply_to: Optional[str]) -> SendResult:
        account_name, group_id, to_open_id = _parse_chat_id(chat_id)
        if not group_id and not to_open_id:
            return SendResult(success=False, error="group_id or to_open_id required")

        acct = self._resolve_session(account_name)
        if not acct:
            return SendResult(success=False, error="No active YZJ account session")

        # App API path (app_id + app_secret)
        if acct.token_provider:
            return await self._send_app_text(acct, group_id, to_open_id, text, reply_to)

        # Legacy webhook fallback (send_msg_url)
        if acct.cfg.send_msg_url:
            return await self._send_legacy_text(acct, text)

        return SendResult(success=False, error="No outbound transport configured for this account")

    async def _send_app_text(
        self,
        acct: _AccountSession,
        group_id: Optional[str],
        to_open_id: Optional[str],
        text: str,
        reply_to: Optional[str],
    ) -> SendResult:
        access_token = await acct.token_provider.get_access_token()
        url = _resolve_url(acct.cfg.endpoint, "/gateway/xtinterface/message/send")

        body: dict = {"msgType": 2, "clientMsgId": str(uuid.uuid4()), "content": text}
        if group_id:
            body["groupId"] = group_id
        if to_open_id:
            body["toOpenId"] = to_open_id
        if reply_to:
            body["param"] = {
                "replyMsgId": reply_to,
                "replyRootMsgId": reply_to,
                "replySummary": "",
                "replyPersonName": "",
                "notifyTo": [to_open_id] if to_open_id else [],
                **({"replyOpenId": to_open_id} if to_open_id else {}),
            }

        timeout = aiohttp.ClientTimeout(total=acct.cfg.timeout)
        try:
            async with self._session.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=timeout,
            ) as resp:
                resp_text = await resp.text()
                if not resp.ok:
                    return SendResult(
                        success=False,
                        error=f"HTTP {resp.status}: {resp_text}",
                        retryable=resp.status >= 500,
                    )
                try:
                    data = json.loads(resp_text)
                except json.JSONDecodeError:
                    data = {}
                if data.get("success") is False:
                    err = str(data.get("error") or data.get("errorCode") or "send failed")
                    return SendResult(success=False, error=err)
                msg_id = str(
                    data.get("msgId")
                    or (data.get("data") or {}).get("msgId")
                    or (data.get("data") or {}).get("id")
                    or ""
                )
                return SendResult(success=True, message_id=msg_id)
        except aiohttp.ClientError as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def _send_legacy_text(self, acct: _AccountSession, text: str) -> SendResult:
        """Send via legacy robot send_msg_url (旧版机器人 Webhook)."""
        body = {"msgtype": 2, "content": text}
        timeout = aiohttp.ClientTimeout(total=acct.cfg.timeout)
        try:
            async with self._session.post(
                acct.cfg.send_msg_url, json=body, timeout=timeout
            ) as resp:
                if not resp.ok:
                    resp_text = await resp.text()
                    return SendResult(
                        success=False,
                        error=f"HTTP {resp.status}: {resp_text}",
                        retryable=resp.status >= 500,
                    )
                return SendResult(success=True, message_id="")
        except aiohttp.ClientError as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    # ------------------------------------------------------------------
    # Outbound: send image
    # ------------------------------------------------------------------

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        account_name, group_id, to_open_id = _parse_chat_id(chat_id)
        acct = self._resolve_session(account_name)
        if not acct:
            return SendResult(success=False, error="No active YZJ account session")

        # Legacy mode: App API not available, send URL as text
        if not acct.token_provider:
            fallback = f"{caption}\n{image_url}" if caption else image_url
            if acct.cfg.send_msg_url:
                return await self._send_legacy_text(acct, fallback)
            return SendResult(success=False, error="No outbound transport configured for this account")

        try:
            file_id = await self._upload_media(acct, image_url)
        except Exception as exc:
            logger.warning(f"[yzj/{acct.cfg.name}] Image upload failed ({exc}), falling back to URL text.")
            fallback = f"{caption}\n{image_url}" if caption else image_url
            return await self._send_app_text(acct, group_id, to_open_id, fallback, reply_to)

        access_token = await acct.token_provider.get_access_token()
        url = _resolve_url(acct.cfg.endpoint, "/gateway/xtinterface/message/send")

        body: dict = {"msgType": 23, "clientMsgId": str(uuid.uuid4())}
        if group_id:
            body["groupId"] = group_id
        if to_open_id:
            body["toOpenId"] = to_open_id
        body["param"] = {"fileId": file_id, "fileName": "image.jpg", "fileType": "img"}
        if caption:
            body["content"] = caption

        timeout = aiohttp.ClientTimeout(total=acct.cfg.timeout)
        try:
            async with self._session.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=timeout,
            ) as resp:
                resp_text = await resp.text()
                if not resp.ok:
                    return SendResult(success=False, error=f"HTTP {resp.status}: {resp_text}")
                data = json.loads(resp_text) if resp_text.strip() else {}
                msg_id = str(
                    data.get("msgId") or (data.get("data") or {}).get("msgId") or ""
                )
                return SendResult(success=True, message_id=msg_id)
        except aiohttp.ClientError as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def _upload_media(self, acct: _AccountSession, url_or_path: str) -> str:
        access_token = await acct.token_provider.get_access_token()
        upload_url = _resolve_url(acct.cfg.endpoint, "/gateway/docrest/doc/file/uploadfileOpen")
        headers = {"Authorization": f"Bearer {access_token}"}
        timeout = aiohttp.ClientTimeout(total=max(acct.cfg.timeout * 3, 30))

        form = aiohttp.FormData()
        if url_or_path.startswith(("http://", "https://")):
            async with self._session.get(url_or_path, timeout=aiohttp.ClientTimeout(total=30)) as img_resp:
                img_resp.raise_for_status()
                content_type = img_resp.headers.get("Content-Type", "application/octet-stream")
                file_bytes = await img_resp.read()
            form.add_field("file", file_bytes, content_type=content_type, filename="upload")
        else:
            import aiofiles
            async with aiofiles.open(url_or_path, "rb") as f:
                file_bytes = await f.read()
            form.add_field("file", file_bytes, filename=os.path.basename(url_or_path))

        async with self._session.post(upload_url, data=form, headers=headers, timeout=timeout) as resp:
            resp_text = await resp.text()
            resp.raise_for_status()
            data = json.loads(resp_text)
            file_id = str(
                (data.get("data") or {}).get("fileId")
                or (data.get("data") or {}).get("id")
                or data.get("fileId")
                or ""
            )
            if not file_id:
                raise RuntimeError(f"uploadfileOpen returned no fileId: {resp_text}")
            return file_id

    # ------------------------------------------------------------------
    # Typing indicator (no-op: YZJ has no typing API)
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> dict:
        """Return basic chat metadata for a YZJ chat_id."""
        _, group_id, to_open_id = _parse_chat_id(chat_id)
        if group_id:
            return {"name": group_id, "type": "group"}
        if to_open_id:
            return {"name": to_open_id, "type": "dm"}
        return {"name": chat_id, "type": "dm"}

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        pass


def register(ctx) -> None:
    """Entry point called by the Hermes plugin loader."""
    ctx.register_platform(YZJAdapter)
