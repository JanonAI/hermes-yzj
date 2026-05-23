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
    if raw:
        try:
            cfg = json.loads(raw)
            return bool(cfg.get("app_id") or cfg.get("send_msg_url") or cfg.get("accounts"))
        except (json.JSONDecodeError, TypeError):
            pass
    # Fallback: read config.yaml directly (used by setup wizard which passes
    # a synthetic empty PlatformConfig with no extra data)
    try:
        from hermes_cli.config import read_raw_config
        yzj = (read_raw_config() or {}).get("yzj") or {}
        return bool(yzj.get("app_id") or yzj.get("send_msg_url") or yzj.get("accounts"))
    except Exception:
        return False


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
    from hermes_cli.config import read_raw_config, save_config

    print_info("─── 🤖 YZJ Robot (云之家) Setup ───")
    print_info("")
    print_info("支持两种凭据模式（二选一）：")
    print_info("  App API 模式  — app_id + app_secret  （推荐，新版应用机器人）")
    print_info("  Legacy 模式   — send_msg_url 含 yzjtoken  （旧版群机器人）")
    print_info("")

    config = read_raw_config() or {}
    yzj_cfg = config.get("yzj") or {}

    existing_app_id = yzj_cfg.get("app_id") or get_env_value("YZJ_APP_ID") or ""
    existing_send_msg_url = yzj_cfg.get("send_msg_url") or get_env_value("YZJ_SEND_MSG_URL") or ""
    already_configured = bool(existing_app_id or existing_send_msg_url or yzj_cfg.get("accounts"))

    if already_configured:
        if yzj_cfg.get("accounts"):
            mode_desc = f"多账户（{len(yzj_cfg['accounts'])} 个账户）"
        elif existing_app_id:
            mode_desc = f"App API (app_id={existing_app_id})"
        else:
            mode_desc = "Legacy (send_msg_url set)"
        print_success(f"YZJ 已配置（{mode_desc}）。")
        if not prompt_yes_no("重新配置？", False):
            _maybe_add_accounts()
            return

    use_app_api = prompt_yes_no("使用 App API 模式（app_id + app_secret）？", True)

    if use_app_api:
        app_id = prompt("YZJ App ID", default=existing_app_id or "")
        if not app_id:
            print_warning("App ID 为必填项，跳过配置。")
            return
        app_secret = prompt("YZJ App Secret", password=True)
        if not app_secret:
            print_warning("App Secret 为必填项，跳过配置。")
            return
        yzj_cfg.pop("send_msg_url", None)
        yzj_cfg["app_id"] = app_id.strip()
        yzj_cfg["app_secret"] = app_secret.strip()
    else:
        send_msg_url = prompt(
            "YZJ Webhook URL（含 ?yzjtoken=...）",
            default=existing_send_msg_url or "",
        )
        if not send_msg_url:
            print_warning("Webhook URL 为必填项，跳过配置。")
            return
        yzj_cfg.pop("app_id", None)
        yzj_cfg.pop("app_secret", None)
        yzj_cfg["send_msg_url"] = send_msg_url.strip()

    endpoint = prompt(
        "自定义接入端点（留空使用默认 https://yunzhijia.com）",
        default=yzj_cfg.get("endpoint") or get_env_value("YZJ_ENDPOINT") or "",
    )
    if endpoint:
        yzj_cfg["endpoint"] = endpoint.strip()
    else:
        yzj_cfg.pop("endpoint", None)

    home_channel = prompt(
        "cron 默认投递 channel，例如 default@group:abc123（可选）",
        default=get_env_value("YZJ_HOME_CHANNEL") or "",
    )

    config["yzj"] = yzj_cfg
    save_config(config)

    # Clear stale env vars so config.yaml takes effect cleanly
    for var in ("YZJ_APP_ID", "YZJ_APP_SECRET", "YZJ_SEND_MSG_URL", "YZJ_ENDPOINT"):
        if get_env_value(var):
            save_env_value(var, "")
    if home_channel:
        save_env_value("YZJ_HOME_CHANNEL", home_channel.strip())

    print_info("")
    print_success("🤖 YZJ Robot 主账户已写入 config.yaml！")

    _maybe_add_accounts()


def _maybe_add_accounts() -> None:
    """Offer to add/manage additional accounts in config.yaml yzj.accounts."""
    from hermes_cli.setup import (
        prompt,
        prompt_yes_no,
        print_info,
        print_warning,
        print_success,
    )
    from hermes_cli.config import read_raw_config, save_config

    print_info("")
    if not prompt_yes_no("配置额外账户（多账户模式）？", False):
        return

    config = read_raw_config() or {}
    yzj_cfg = config.get("yzj") or {}

    # Build accounts dict: start from existing or promote flat main account to "default"
    accounts = dict(yzj_cfg.get("accounts") or {})
    if not accounts:
        # Promote flat main account to accounts["default"]
        main: dict = {}
        if yzj_cfg.get("app_id"):
            main["app_id"] = yzj_cfg["app_id"]
            main["app_secret"] = yzj_cfg.get("app_secret", "")
        elif yzj_cfg.get("send_msg_url"):
            main["send_msg_url"] = yzj_cfg["send_msg_url"]
        if yzj_cfg.get("endpoint"):
            main["endpoint"] = yzj_cfg["endpoint"]
        if main:
            accounts["default"] = main

    print_info("")
    print_info("多账户模式：每个账户对应一个机器人，chat_id 格式为 {账户名}@group:{groupId}")
    if accounts:
        print_info(f"当前账户：{', '.join(accounts.keys())}")
    print_info("输入空名称结束添加。")

    while True:
        print_info("")
        name = prompt("账户名称（英文，例如 bot1，留空结束）", default="")
        if not name or not name.strip():
            break
        name = name.strip()

        use_app_api = prompt_yes_no(f"账户 [{name}] 使用 App API 模式？", True)
        if use_app_api:
            app_id = prompt(f"[{name}] App ID")
            if not app_id:
                print_warning(f"跳过账户 {name}（App ID 为空）")
                continue
            app_secret = prompt(f"[{name}] App Secret", password=True)
            if not app_secret:
                print_warning(f"跳过账户 {name}（App Secret 为空）")
                continue
            accounts[name] = {"app_id": app_id.strip(), "app_secret": app_secret.strip()}
        else:
            send_msg_url = prompt(f"[{name}] Webhook URL（含 ?yzjtoken=...）")
            if not send_msg_url:
                print_warning(f"跳过账户 {name}（URL 为空）")
                continue
            accounts[name] = {"send_msg_url": send_msg_url.strip()}

        print_success(f"账户 [{name}] 已添加。")

    if not accounts:
        return

    # Switch to multi-account format: remove flat keys, write accounts dict
    for k in ("app_id", "app_secret", "send_msg_url"):
        yzj_cfg.pop(k, None)
    yzj_cfg["accounts"] = accounts
    config["yzj"] = yzj_cfg
    save_config(config)
    print_info("")
    print_success(f"共 {len(accounts)} 个账户已写入 config.yaml：{', '.join(accounts.keys())}")
    print_info("多账户时 chat_id 格式：{账户名}@group:{groupId} 或 {账户名}@user:{openId}")



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
