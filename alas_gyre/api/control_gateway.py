#!/usr/bin/env python3
# -_- coding: utf-8 -_-

from dataclasses import dataclass
from dataclasses import field

from alas_gyre.api.client import api_headers
from alas_gyre.api.client import api_request
from alas_gyre.api.client import gyre_api_url
from alas_gyre.api.connection_mode import CONNECTION_MODE_WEBSOCKET
from alas_gyre.api.connection_mode import normalize_connection_mode
from alas_gyre.api.connection_mode import should_try_overlay
from alas_gyre.api.connection_mode import should_try_websocket_after_overlay_failure
from alas_gyre.api.connection_mode import should_try_websocket_first
from alas_gyre.api import websocket_hijack
from alas_gyre.api.websocket_hijack import probe_websocket


@dataclass
class ControlResult:
    """远端控制调用结果。"""

    ok: bool
    mode: str
    degraded: bool = False
    message: str = ""
    data: dict = field(default_factory=dict)


def should_fallback_to_websocket(mode, status_code=None, error=None):
    """判断 Overlay 失败后是否应降级 WebSocket。"""
    if not should_try_websocket_after_overlay_failure(mode):
        return False
    if error:
        return True
    return status_code in {0, 404, 408, 500, 502, 503, 504}


def test_overlay_connection(config, timeout=2.0):
    """测试 Overlay 连接。"""
    resp = api_request(
        "GET",
        gyre_api_url(config, "health"),
        headers=api_headers(config),
        timeout=timeout,
    )
    return resp


def test_websocket_connection(config, timeout=5.0, degraded=False):
    """测试 WebSocket 劫持连接。"""
    result = probe_websocket(config, timeout=timeout)
    return ControlResult(
        ok=True,
        mode=CONNECTION_MODE_WEBSOCKET,
        degraded=degraded,
        message="websocket_ok",
        data={"configs": result.get("configs", [])},
    )


def test_connection(config, timeout=3.0):
    """按连接模式测试连接。"""
    mode = normalize_connection_mode(config)
    if should_try_websocket_first(mode):
        return test_websocket_connection(config, timeout=timeout)

    overlay_error = None
    overlay_status = None
    if should_try_overlay(mode):
        try:
            resp = test_overlay_connection(config, timeout=timeout)
            overlay_status = resp.status_code
            if resp.status_code == 200:
                return ControlResult(ok=True, mode="overlay", message="overlay_ok")
        except Exception as exc:
            overlay_error = exc

    if should_fallback_to_websocket(mode, overlay_status, overlay_error):
        return test_websocket_connection(config, timeout=timeout, degraded=True)

    return ControlResult(ok=False, mode=mode, message="connect_failed")


def get_configs(config):
    """按连接模式获取配置列表。"""
    mode = normalize_connection_mode(config)
    if should_try_websocket_first(mode):
        manager = websocket_hijack.get_persistent_manager().ensure_started(config)
        return ControlResult(ok=True, mode="websocket", data={"configs": manager.get_configs()})
    try:
        resp = api_request("GET", gyre_api_url(config, "configs"), headers=api_headers(config), timeout=2.0)
        if resp.status_code == 200:
            return ControlResult(ok=True, mode="overlay", data=resp.json())
        overlay_status = resp.status_code
        overlay_error = None
    except Exception as exc:
        overlay_status = None
        overlay_error = exc
    if should_fallback_to_websocket(mode, overlay_status, overlay_error):
        configs = websocket_hijack.get_configs(config)
        return ControlResult(ok=True, mode="websocket", degraded=True, data={"configs": configs})
    return ControlResult(ok=False, mode=mode, message="connect_failed")


def get_status_all(config):
    """按连接模式获取全部状态。"""
    mode = normalize_connection_mode(config)
    if should_try_websocket_first(mode):
        manager = websocket_hijack.get_persistent_manager().ensure_started(config)
        return ControlResult(ok=True, mode="websocket", data=manager.get_status_all())
    try:
        resp = api_request("GET", gyre_api_url(config, "status_all"), headers=api_headers(config), timeout=1.5)
        if resp.status_code == 200:
            return ControlResult(ok=True, mode="overlay", data=resp.json())
        overlay_status = resp.status_code
        overlay_error = None
    except Exception as exc:
        overlay_status = None
        overlay_error = exc
    if should_fallback_to_websocket(mode, overlay_status, overlay_error):
        manager = websocket_hijack.get_persistent_manager().ensure_started(config)
        return ControlResult(ok=True, mode="websocket", degraded=True, data=manager.get_status_all())
    return ControlResult(ok=False, mode=mode, message="connect_failed")


def post_action(config, config_name, action):
    """按连接模式启动或停止配置。"""
    mode = normalize_connection_mode(config)
    if should_try_websocket_first(mode):
        manager = websocket_hijack.get_persistent_manager().ensure_started(config)
        return ControlResult(ok=True, mode="websocket", data=manager.post_action(config_name, action))
    try:
        resp = api_request(
            "POST",
            gyre_api_url(config, action),
            params={"config": config_name},
            headers=api_headers(config),
            timeout=3,
        )
        if resp.status_code == 200:
            return ControlResult(ok=True, mode="overlay", data=resp.json())
        overlay_status = resp.status_code
        overlay_error = None
    except Exception as exc:
        overlay_status = None
        overlay_error = exc
    if should_fallback_to_websocket(mode, overlay_status, overlay_error):
        manager = websocket_hijack.get_persistent_manager().ensure_started(config)
        return ControlResult(ok=True, mode="websocket", degraded=True, data=manager.post_action(config_name, action))
    return ControlResult(ok=False, mode=mode, message="connect_failed")
