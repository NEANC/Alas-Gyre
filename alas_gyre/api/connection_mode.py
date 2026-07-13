#!/usr/bin/env python3
# -_- coding: utf-8 -_-

CONNECTION_MODE_OVERLAY = "overlay"
CONNECTION_MODE_WEBSOCKET = "websocket"
CONNECTION_MODE_AUTO = "auto"
CONNECTION_MODES = {
    CONNECTION_MODE_OVERLAY,
    CONNECTION_MODE_WEBSOCKET,
    CONNECTION_MODE_AUTO,
}


def normalize_connection_mode(config):
    """规范化连接模式配置。"""
    mode = str((config or {}).get("connection_mode", CONNECTION_MODE_AUTO) or "").strip().lower()
    if mode not in CONNECTION_MODES:
        return CONNECTION_MODE_AUTO
    return mode


def should_try_overlay(mode):
    """判断当前模式是否应先尝试 Overlay 通道。"""
    return mode in {CONNECTION_MODE_OVERLAY, CONNECTION_MODE_AUTO}


def should_try_websocket_first(mode):
    """判断当前模式是否应直接使用 WebSocket 通道。"""
    return mode == CONNECTION_MODE_WEBSOCKET


def should_try_websocket_after_overlay_failure(mode):
    """判断 Overlay 失败后是否允许降级 WebSocket。"""
    return mode in {CONNECTION_MODE_OVERLAY, CONNECTION_MODE_AUTO}
