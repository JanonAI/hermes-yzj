"""YZJ (云之家) platform adapter plugin for Hermes."""
import json
import logging

logger = logging.getLogger(__name__)


def _apply_yaml_config(full_cfg: dict, platform_cfg: dict) -> dict:
    """Serialize the yzj: section into PlatformConfig.extra["yzj_config"]."""
    if not platform_cfg:
        return {}
    return {"yzj_config": json.dumps(platform_cfg)}


def register(ctx) -> None:
    logger.info("[yzj] register() called")
    try:
        from .adapter import YZJAdapter
    except Exception as exc:
        logger.error(f"[yzj] Failed to import YZJAdapter: {exc}", exc_info=True)
        raise

    def _factory(cfg):
        from gateway.config import Platform
        return YZJAdapter(cfg, Platform("yzj"))

    try:
        ctx.register_platform(
            "yzj",
            "YZJ Robot",
            _factory,
            lambda: True,
            apply_yaml_config_fn=_apply_yaml_config,
        )
        logger.info("[yzj] register_platform() succeeded")
    except Exception as exc:
        logger.error(f"[yzj] register_platform() failed: {exc}", exc_info=True)
        raise
