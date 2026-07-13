#!/usr/bin/env python3
# -_- coding: utf-8 -_-

from dataclasses import dataclass
from dataclasses import field
import json
import re

import requests

PYWEBIO_WS_PATH = "/"
DEFAULT_APP_NAME = "index"
CONFIG_PIN_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)_[A-Za-z0-9]+_")
IGNORED_PIN_PREFIXES = {
    "General",
}
NAVIGATION_KEYWORDS = {
    "remote": ("remote", "远程控制"),
    "updater": ("updater", "更新器"),
}
UPDATE_ACTION_KEYWORDS = {
    "check": ("检查更新", "check update"),
    "apply": ("进行更新", "update now", "apply update"),
    "cancel": ("取消更新", "cancel update"),
}
try:
    import websocket

    WebSocketTimeoutException = websocket.WebSocketTimeoutException
except Exception:
    WebSocketTimeoutException = TimeoutError
WEBSOCKET_TIMEOUT_ERRORS = (TimeoutError, WebSocketTimeoutException)


class WebSocketHijackError(Exception):
    """WebSocket 劫持通道基础错误。"""


class WebUIUnavailableError(WebSocketHijackError):
    """ALAS WebUI 不可访问。"""


class NotPyWebIOError(WebSocketHijackError):
    """目标页面不是可识别的 PyWebIO 页面。"""


class WebSocketHandshakeError(WebSocketHijackError):
    """WebSocket 握手失败。"""


class NavigationError(WebSocketHijackError):
    """主动导航失败。"""


class ConfigDetectionError(WebSocketHijackError):
    """多配置识别失败。"""


@dataclass
class PyWebIOMessage:
    """PyWebIO 服务端消息。"""

    command: str
    spec: dict = field(default_factory=dict)
    task_id: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class PyWebIOState:
    """WebSocket 劫持会话状态。"""

    session_id: str = ""
    task_ids: set = field(default_factory=set)
    pin_names: set = field(default_factory=set)
    callback_ids: dict = field(default_factory=dict)
    scripts: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    inputs: list = field(default_factory=list)

    def apply_message(self, message):
        """把 PyWebIO 消息合并到状态容器。"""
        if message.task_id:
            self.task_ids.add(message.task_id)
        if message.command == "set_session_id":
            self.session_id = str(message.spec or "")
        if message.command == "pin_onchange":
            name = str(message.spec.get("name", "") or "")
            callback_id = str(message.spec.get("callback_id", "") or "")
            if name:
                self.pin_names.add(name)
            if name and callback_id:
                self.callback_ids[name] = callback_id
        elif message.command == "run_script":
            self.scripts.append(message.spec)
        elif message.command == "output":
            self.outputs.append(message.spec)
        elif message.command == "input":
            self.inputs.append(message.spec)


def parse_pywebio_message(payload):
    """解析 PyWebIO JSON 消息。"""
    data = json.loads(payload)
    spec = data.get("spec")
    if not isinstance(spec, dict):
        spec = {} if spec is None else {"value": spec}
    return PyWebIOMessage(
        command=str(data.get("command", "") or ""),
        spec=spec,
        task_id=str(data.get("task_id", "") or ""),
        raw=data,
    )


def extract_config_names(state):
    """从 PyWebIO 状态中识别 ALAS 多配置名称。"""
    prefixes = set()
    for pin_name in state.pin_names:
        match = CONFIG_PIN_PATTERN.match(pin_name)
        if not match:
            continue
        prefix = match.group(1)
        if prefix in IGNORED_PIN_PREFIXES:
            continue
        prefixes.add(prefix)
    configs = sorted(prefixes, key=str.lower)
    if not configs:
        raise ConfigDetectionError("config_detection_failed")
    return configs


def normalize_alas_status(value):
    """把 ALAS WebUI 状态文本映射为 Alas-Gyre 状态。"""
    text = str(value or "").strip().lower()
    if not text:
        return "error"
    if text in {"idle", "空闲", "未运行", "stopped", "stop"}:
        return "idle"
    if text in {"running", "运行中", "run"}:
        return "running"
    if text in {"error", "出错", "错误"}:
        return "error"
    if text in {"update", "updating", "更新中"}:
        return "update"
    if text in {"disconnected", "未连接"}:
        return "disconnected"
    return "error"


def _search_callback_id(text):
    """从文本中提取 PyWebIO 回调 ID。"""
    match = re.search(r"CB-[A-Za-z0-9_-]+", str(text or ""))
    return match.group(0) if match else ""


def _searchable_text(value):
    """把 PyWebIO 输出结构转换为可搜索文本。"""
    if isinstance(value, dict):
        return " ".join(_searchable_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_searchable_text(item) for item in value)
    return str(value or "")


def find_navigation_callback(state, target):
    """查找导航到目标页面的回调 ID。"""
    keywords = NAVIGATION_KEYWORDS.get(target, ())
    for script in state.scripts:
        code = str(script.get("code", "") if isinstance(script, dict) else script)
        lower_code = code.lower()
        for keyword in keywords:
            if keyword.lower() not in lower_code:
                continue
            callback_match = re.search(
                rf"{re.escape(keyword)}.*?(CB-[A-Za-z0-9_-]+)",
                code,
                re.IGNORECASE,
            )
            if callback_match:
                return callback_match.group(1)
            callback_id = _search_callback_id(code)
            if callback_id:
                return callback_id
    raise NavigationError(f"navigation_callback_not_found:{target}")


def find_update_action_callback(state, action):
    """查找更新器动作回调 ID。"""
    keywords = UPDATE_ACTION_KEYWORDS.get(action, ())
    for output in state.outputs:
        text = _searchable_text(output)
        lower_text = text.lower()
        if any(keyword.lower() in lower_text for keyword in keywords):
            callback_id = str(output.get("callback_id", "") if isinstance(output, dict) else "")
            if callback_id:
                return callback_id
            callback_id = _search_callback_id(output) or _search_callback_id(text)
            if callback_id:
                return callback_id
    raise NavigationError(f"update_action_callback_not_found:{action}")


def send_callback_event(ws, task_id, callback_id, data=None):
    """向 PyWebIO 发送回调事件。"""
    payload = {
        "event": "callback",
        "task_id": task_id,
        "data": {"callback_id": callback_id, "data": data},
    }
    ws.send(json.dumps(payload))


def webui_base_url(config):
    """构造 ALAS WebUI 基础地址。"""
    ip = str(config.get("ip", "127.0.0.1") or "127.0.0.1").strip()
    port = str(config.get("port", "22267") or "22267").strip()
    return f"http://{ip}:{port}"


def websocket_url(config):
    """构造 ALAS PyWebIO WebSocket 地址。"""
    ip = str(config.get("ip", "127.0.0.1") or "127.0.0.1").strip()
    port = str(config.get("port", "22267") or "22267").strip()
    return f"ws://{ip}:{port}/?app={DEFAULT_APP_NAME}&session=NEW"


def check_pywebio_home(config, timeout=3.0):
    """检查 ALAS WebUI 首页是否像 PyWebIO。"""
    try:
        resp = requests.get(webui_base_url(config), timeout=timeout)
    except Exception as exc:
        raise WebUIUnavailableError(str(exc)) from exc
    if resp.status_code != 200:
        raise WebUIUnavailableError(f"HTTP {resp.status_code}")
    html = resp.text or ""
    markers = ("pywebio_static", "WebIO.startWebIOClient", "pywebio")
    if not any(marker in html for marker in markers):
        raise NotPyWebIOError("not_pywebio")
    return True


def collect_initial_state(ws, max_messages=1200):
    """从 WebSocket 收集初始 PyWebIO 状态。"""
    state = PyWebIOState()
    for _ in range(max_messages):
        try:
            payload = ws.recv()
        except WEBSOCKET_TIMEOUT_ERRORS:
            break
        if not payload:
            continue
        message = parse_pywebio_message(payload)
        state.apply_message(message)
        try:
            extract_config_names(state)
            return state
        except ConfigDetectionError:
            continue
    return state


def open_pywebio_websocket(config, timeout=5.0):
    """打开 ALAS PyWebIO WebSocket。"""
    try:
        import websocket

        ws = websocket.create_connection(
            websocket_url(config),
            timeout=timeout,
            enable_multithread=True,
        )
        return ws
    except Exception as exc:
        raise WebSocketHandshakeError(str(exc)) from exc


def probe_websocket(config, timeout=5.0):
    """验证 WebSocket 可连接且能识别多配置。"""
    check_pywebio_home(config, timeout=timeout)
    ws = open_pywebio_websocket(config, timeout=timeout)
    try:
        state = collect_initial_state(ws)
        configs = extract_config_names(state)
        return {"ok": True, "configs": configs, "state": state}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def get_configs(config):
    """通过 WebSocket 识别 ALAS 多配置。"""
    result = probe_websocket(config)
    return result.get("configs", [])


def get_status_all(config):
    """通过 WebSocket 获取多配置状态。"""
    get_configs(config)
    raise WebSocketHijackError("websocket_status_not_implemented")


def post_config_action(config, config_name, action):
    """通过 WebSocket 对指定配置执行启动或停止。"""
    configs = get_configs(config)
    if config_name not in configs:
        raise ConfigDetectionError("config_not_found")
    if action not in {"start", "stop"}:
        raise WebSocketHijackError("unsupported_action")
    raise WebSocketHijackError("websocket_action_not_implemented")
