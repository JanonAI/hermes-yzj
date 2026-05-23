"""YZJ (云之家) platform adapter plugin for Hermes."""
import json
import logging

logger = logging.getLogger(__name__)


def _apply_yaml_config(full_cfg: dict, platform_cfg: dict) -> dict:
    """Serialize the yzj: section into PlatformConfig.extra["yzj_config"]."""
    if not platform_cfg:
        return {}
    return {"yzj_config": json.dumps(platform_cfg)}


def _env_enablement_fn() -> "dict | None":
    """
    Seed PlatformConfig.extra from environment variables before the adapter
    is constructed. Called by the Hermes plugin loader.

    Supports two mutually exclusive single-account modes:
      App API mode:  YZJ_APP_ID + YZJ_APP_SECRET
      Legacy mode:   YZJ_SEND_MSG_URL

    Returns a dict to merge into PlatformConfig.extra, or None if YZJ is
    not configured via env vars (adapter will not start).
    """
    import os
    app_id = os.environ.get("YZJ_APP_ID", "").strip()
    app_secret = os.environ.get("YZJ_APP_SECRET", "").strip()
    send_msg_url = os.environ.get("YZJ_SEND_MSG_URL", "").strip()
    endpoint = os.environ.get("YZJ_ENDPOINT", "").strip()
    home_channel = os.environ.get("YZJ_HOME_CHANNEL", "").strip()

    if app_id and app_secret:
        cfg: dict = {"app_id": app_id, "app_secret": app_secret}
        if endpoint:
            cfg["endpoint"] = endpoint
        extra = {"yzj_config": json.dumps(cfg)}
        if home_channel:
            extra["home_channel"] = home_channel
        return extra

    if send_msg_url:
        cfg = {"send_msg_url": send_msg_url}
        if endpoint:
            cfg["endpoint"] = endpoint
        extra = {"yzj_config": json.dumps(cfg)}
        if home_channel:
            extra["home_channel"] = home_channel
        return extra

    return None


def _is_connected(config) -> bool:
    """Return True when YZJ is configured via env vars or config.yaml."""
    import os
    if os.environ.get("YZJ_APP_ID") and os.environ.get("YZJ_APP_SECRET"):
        return True
    if os.environ.get("YZJ_SEND_MSG_URL"):
        return True
    extra = getattr(config, "extra", {}) or {}
    raw = extra.get("yzj_config", "")
    if not raw:
        return False
    try:
        cfg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(cfg.get("app_id") or cfg.get("send_msg_url"))


def _setup_fn() -> None:
    """Interactive hermes gateway setup flow for the YZJ platform."""
    from hermes_cli.setup import (
        prompt,
        prompt_yes_no,
        print_info,
        print_warning,
        print_success,
        get_env_value,
        save_env_value,
    )
    import os

    print_info("─── 🤖 YZJ Robot (云之家) Setup ───")
    print_info("")
    print_info("Two authentication modes (choose one):")
    print_info("  App API mode  — YZJ_APP_ID + YZJ_APP_SECRET  (recommended for new bots)")
    print_info("  Legacy mode   — YZJ_SEND_MSG_URL with yzjtoken  (for existing webhook bots)")
    print_info("")

    existing_app_id = get_env_value("YZJ_APP_ID")
    existing_send_msg_url = get_env_value("YZJ_SEND_MSG_URL")
    already_configured = bool(existing_app_id or existing_send_msg_url)

    if already_configured:
        mode_desc = f"App API (app_id={existing_app_id})" if existing_app_id else f"Legacy (send_msg_url set)"
        print_success(f"YZJ is already configured ({mode_desc}).")
        if not prompt_yes_no("Reconfigure YZJ?", False):
            return

    use_app_api = prompt_yes_no("Use App API mode (YZJ_APP_ID + YZJ_APP_SECRET)?", True)

    if use_app_api:
        app_id = prompt("YZJ App ID", default=existing_app_id or "")
        if not app_id:
            print_warning("App ID is required — skipping YZJ setup.")
            return
        save_env_value("YZJ_APP_ID", app_id.strip())

        app_secret = prompt("YZJ App Secret", password=True)
        if not app_secret:
            print_warning("App Secret is required — skipping YZJ setup.")
            return
        save_env_value("YZJ_APP_SECRET", app_secret.strip())

        # Clear legacy var to avoid ambiguity
        if get_env_value("YZJ_SEND_MSG_URL"):
            save_env_value("YZJ_SEND_MSG_URL", "")
    else:
        send_msg_url = prompt(
            "YZJ Webhook URL (e.g. https://www.yunzhijia.com/gateway/robot/send?yzjtoken=xxx)",
            default=existing_send_msg_url or "",
        )
        if not send_msg_url:
            print_warning("Webhook URL is required — skipping YZJ setup.")
            return
        save_env_value("YZJ_SEND_MSG_URL", send_msg_url.strip())

        # Clear App API vars to avoid ambiguity
        if get_env_value("YZJ_APP_ID"):
            save_env_value("YZJ_APP_ID", "")
        if get_env_value("YZJ_APP_SECRET"):
            save_env_value("YZJ_APP_SECRET", "")

    endpoint = prompt(
        "Custom endpoint (leave blank for default https://yunzhijia.com)",
        default=get_env_value("YZJ_ENDPOINT") or "",
    )
    if endpoint:
        save_env_value("YZJ_ENDPOINT", endpoint.strip())

    home_channel = prompt(
        "Home channel for cron delivery, e.g. default@group:abc123 (optional)",
        default=get_env_value("YZJ_HOME_CHANNEL") or "",
    )
    if home_channel:
        save_env_value("YZJ_HOME_CHANNEL", home_channel.strip())

    print_info("")
    print_success("🤖 YZJ Robot configured!")


def register(ctx) -> None:
    logger.info("[yzj] register() called")
    try:
        from .adapter import YZJAdapter
    except Exception as exc:
        logger.error(f"[yzj] Failed to import YZJAdapter: {exc}", exc_info=True)
        raise

    def _factory(cfg):
        from gateway.config import Platform
        return YZJAdapter(cfg, Platform("hermes-yzj"))

    try:
        ctx.register_platform(
            "hermes-yzj",
            "YZJ Robot",
            _factory,
            lambda: _is_connected(None),
            apply_yaml_config_fn=_apply_yaml_config,
            env_enablement_fn=_env_enablement_fn,
            is_connected=_is_connected,
            setup_fn=_setup_fn,
            emoji="🤖",
            cron_deliver_env_var="YZJ_HOME_CHANNEL",
        )
        logger.info("[yzj] register_platform() succeeded")
    except Exception as exc:
        logger.error(f"[yzj] register_platform() failed: {exc}", exc_info=True)
        raise
