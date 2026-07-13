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
CONFIG_ACTION_KEYWORDS = {
    "start": ("启动", "啟動", "実行", "start"),
    "stop": ("停止", "中止", "stop"),
}
try:
    import websocket

    WebSocketTimeoutException = websocket.WebSocketTimeoutException
except Exception:
    WebSocketTimeoutException = TimeoutError
WEBSOCKET_TIMEOUT_ERRORS = (TimeoutError, WebSocketTimeoutException, ConnectionResetError)


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


@dataclass
class SchedulerButtonSet:
    """调度器启动和停止按钮缓存。"""

    start_callback_id: str = ""
    start_value: object = None
    stop_callback_id: str = ""
    stop_value: object = None


@dataclass
class PersistentConfigWorker:
    """常驻配置状态缓存。"""

    config: dict
    config_name: str
    buttons: SchedulerButtonSet = field(default_factory=SchedulerButtonSet)
    status: str = "error"
    last_error: str = ""

    def update_from_state(self, state):
        """从 PyWebIO 状态更新按钮和实例状态缓存。"""
        try:
            callback_id, value = find_config_action_callback(state, "start")
            self.buttons.start_callback_id = callback_id
            self.buttons.start_value = value
        except NavigationError:
            pass
        try:
            callback_id, value = find_config_action_callback(state, "stop")
            self.buttons.stop_callback_id = callback_id
            self.buttons.stop_value = value
        except NavigationError:
            pass
        status_data = extract_status_all(state, [self.config_name])
        self.status = status_data.get("statuses", {}).get(self.config_name, self.status)


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


def send_js_yield_event(ws, task_id, data=None):
    """向 PyWebIO 回传浏览器脚本求值结果。"""
    payload = {
        "event": "js_yield",
        "task_id": task_id,
        "data": data,
    }
    ws.send(json.dumps(payload, ensure_ascii=False))


def handle_pywebio_client_script(ws, message, local_storage=None):
    """处理 PyWebIO 要求浏览器执行的脚本。"""
    if message.command != "run_script":
        return False
    code = str(message.spec.get("code", "") or "")
    args = message.spec.get("args", {})
    if not isinstance(args, dict):
        args = {}
    if local_storage is None:
        local_storage = {}
    if "localStorage.setItem" in code:
        key = str(args.get("key", "") or "")
        if key:
            local_storage[key] = args.get("value")
            return True
    if not message.spec.get("eval"):
        return False
    data = None
    if "localStorage.getItem" in code:
        data = local_storage.get(str(args.get("key", "")))
    elif "document.visibilityState" in code:
        data = "visible"
    send_js_yield_event(ws, message.task_id, data)
    return True


def handle_pywebio_client_eval(ws, message, local_storage=None):
    """处理 PyWebIO 需要浏览器执行并回传的脚本。"""
    return handle_pywebio_client_script(ws, message, local_storage=local_storage)


def extract_instance_names(state):
    """从 ALAS 侧边栏输出中提取实例名称。"""
    names = []
    for output in state.outputs:
        scope = str(output.get("scope", "") if isinstance(output, dict) else "")
        if "pywebio-scope-alas-instance-" not in scope:
            continue
        for label in _extract_labels(output):
            if label and label not in names:
                names.append(label)
    if not names:
        return extract_config_names(state)
    return names


def extract_status_all(state, configs=None):
    """从 ALAS 页面输出中提取所有实例状态。"""
    configs = list(configs or extract_instance_names(state))
    statuses = {config_name: "error" for config_name in configs}
    status_text = ""
    for output in state.outputs:
        text = _searchable_text(output)
        scope = str(output.get("scope", "") if isinstance(output, dict) else "")
        if "header_status" in scope or "pywebio-scope-header_status" in text:
            for candidate in ("运行中", "空闲", "未连接", "错误", "更新中"):
                if candidate in text:
                    status_text = candidate
                    break
        if status_text:
            break
    if status_text:
        status = normalize_alas_status(status_text)
        statuses = {config_name: status for config_name in configs}
    else:
        statuses.update(_extract_instance_icon_statuses(state))
    return {
        "statuses": statuses,
        "tasks": {config_name: "" for config_name in configs},
    }


def _extract_instance_icon_statuses(state):
    """从 ALAS 侧边栏图标 class 提取实例运行状态。"""
    statuses = {}
    for output in state.outputs:
        scope = str(output.get("scope", "") if isinstance(output, dict) else "")
        if "pywebio-scope-alas-instance-" not in scope:
            continue
        labels = _extract_labels(output)
        if not labels:
            continue
        text = _searchable_text(output)
        if "icon-run" in text:
            status = "running"
        elif "icon-stop" in text or "icon-idle" in text:
            status = "idle"
        elif "icon-error" in text:
            status = "error"
        else:
            continue
        statuses[labels[0]] = status
    return statuses


def _extract_labels(value):
    """递归提取 PyWebIO 输出里的按钮标签。"""
    labels = []
    if isinstance(value, dict):
        buttons = value.get("buttons")
        if isinstance(buttons, list):
            for button in buttons:
                if isinstance(button, dict) and button.get("label"):
                    labels.append(str(button.get("label")))
        for item in value.values():
            labels.extend(_extract_labels(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            labels.extend(_extract_labels(item))
    return labels


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


def _iter_button_groups(value):
    """递归遍历 PyWebIO 输出里的按钮组。"""
    if isinstance(value, dict):
        buttons = value.get("buttons")
        callback_id = str(value.get("callback_id", "") or value.get("callback", "") or "")
        if isinstance(buttons, list):
            yield callback_id, buttons
        for item in value.values():
            yield from _iter_button_groups(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_button_groups(item)


def find_button_callback(state, label, scope_keyword=""):
    """按精确按钮标签查找回调 ID 与按钮值。"""
    expected = str(label or "")
    for output in state.outputs:
        scope = str(output.get("scope", "") if isinstance(output, dict) else "")
        if scope_keyword and scope_keyword not in scope:
            continue
        for group_callback_id, buttons in _iter_button_groups(output):
            for button in buttons:
                if not isinstance(button, dict):
                    continue
                if str(button.get("label", "") or "") != expected:
                    continue
                callback_id = str(button.get("callback_id", "") or button.get("callback", "") or group_callback_id)
                if callback_id:
                    return callback_id, button.get("value")
    raise NavigationError(f"button_callback_not_found:{expected}")


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
    """查找更新器动作回调 ID 与按钮值。"""
    keywords = UPDATE_ACTION_KEYWORDS.get(action, ())
    for keyword in keywords:
        try:
            return find_button_callback(state, keyword)
        except NavigationError:
            continue
    for output in state.outputs:
        text = _searchable_text(output)
        lower_text = text.lower()
        if any(keyword.lower() in lower_text for keyword in keywords):
            callback_id = str(output.get("callback_id", "") if isinstance(output, dict) else "")
            if callback_id:
                return callback_id, None
            callback_id = _search_callback_id(output) or _search_callback_id(text)
            if callback_id:
                return callback_id, None
    raise NavigationError(f"update_action_callback_not_found:{action}")


def find_config_action_callback(state, action):
    """查找配置启动或停止按钮回调 ID 与按钮值。"""
    keywords = CONFIG_ACTION_KEYWORDS.get(action, ())
    for keyword in keywords:
        try:
            return find_button_callback(state, keyword)
        except NavigationError:
            continue
    for output in state.outputs:
        text = _searchable_text(output)
        lower_text = text.lower()
        if not any(keyword.lower() in lower_text for keyword in keywords):
            continue
        callback_id = str(output.get("callback_id", "") if isinstance(output, dict) else "")
        if callback_id:
            return callback_id, None
        callback_id = _search_callback_id(output) or _search_callback_id(text)
        if callback_id:
            return callback_id, None
    raise NavigationError(f"config_action_callback_not_found:{action}")


def send_callback_event(ws, task_id, callback_id, data=None):
    """向 PyWebIO 发送回调事件。"""
    _ = task_id
    payload = {
        "event": "callback",
        "task_id": callback_id,
        "data": data,
    }
    ws.send(json.dumps(payload, ensure_ascii=False))


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


def collect_initial_state(ws, max_messages=1200, local_storage=None):
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
        handle_pywebio_client_script(ws, message, local_storage=local_storage)
    return state


def set_websocket_timeout(ws, timeout):
    """设置 WebSocket 接收超时，避免持续日志导致控制流程阻塞。"""
    try:
        ws.settimeout(timeout)
    except AttributeError:
        pass


def collect_state_until_buttons(ws, labels, max_messages=300, local_storage=None):
    """收集 PyWebIO 状态直到出现任一目标按钮。"""
    state = PyWebIOState()
    expected_labels = tuple(str(label or "") for label in labels)
    for _ in range(max_messages):
        try:
            payload = ws.recv()
        except WEBSOCKET_TIMEOUT_ERRORS:
            break
        if not payload:
            continue
        message = parse_pywebio_message(payload)
        state.apply_message(message)
        handle_pywebio_client_script(ws, message, local_storage=local_storage)
        for label in expected_labels:
            try:
                find_button_callback(state, label)
                return state
            except NavigationError:
                continue
    return state


def _click_button_if_present(ws, state, label, scope_keyword=""):
    """如果指定按钮存在则点击并返回是否已点击。"""
    try:
        callback_id, value = find_button_callback(state, label, scope_keyword=scope_keyword)
    except NavigationError:
        return False
    send_callback_event(ws, "", callback_id, value)
    return True


def prepare_alas_page(ws, config_name, local_storage):
    """初始化 ALAS PyWebIO 页面并选中目标配置。"""
    state = collect_initial_state(ws, local_storage=local_storage)
    instance_outputs = [
        output for output in state.outputs
        if isinstance(output, dict) and "pywebio-scope-alas-instance-" in str(output.get("scope", ""))
    ]
    if _click_button_if_present(ws, state, "简体中文"):
        state = collect_initial_state(ws, local_storage=local_storage)
        instance_outputs.extend(
            output for output in state.outputs
            if isinstance(output, dict) and "pywebio-scope-alas-instance-" in str(output.get("scope", ""))
        )
    for label in ("深色", "Dark", "黑暗"):
        if _click_button_if_present(ws, state, label):
            state = collect_initial_state(ws, local_storage=local_storage)
            instance_outputs.extend(
                output for output in state.outputs
                if isinstance(output, dict) and "pywebio-scope-alas-instance-" in str(output.get("scope", ""))
            )
            break
    if config_name:
        try:
            callback_id, value = find_button_callback(state, config_name, scope_keyword="alas-instance-")
            send_callback_event(ws, "", callback_id, value)
            labels = CONFIG_ACTION_KEYWORDS.get("stop", ()) + CONFIG_ACTION_KEYWORDS.get("start", ())
            next_state = collect_state_until_buttons(ws, labels, max_messages=300, local_storage=local_storage)
            if next_state.outputs:
                if instance_outputs:
                    next_state.outputs = instance_outputs + next_state.outputs
                state = next_state
        except NavigationError:
            pass
    return state


def post_update_action(config, action="check"):
    """通过 WebSocket 点击 ALAS 更新器动作。"""
    if action not in UPDATE_ACTION_KEYWORDS:
        raise WebSocketHijackError("unsupported_update_action")
    ws = open_pywebio_websocket(config)
    try:
        current_config = str(config.get("current_config", "") or "")
        local_storage = {"aside": current_config or None}
        state = prepare_alas_page(ws, current_config, local_storage)
        _click_button_if_present(ws, state, "更新器")
        state = collect_initial_state(ws, local_storage=local_storage)
        callback_id, value = find_update_action_callback(state, action)
        send_callback_event(ws, "", callback_id, value)
        return {"action": action, "status": "submitted"}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def open_pywebio_websocket(config, timeout=5.0):
    """打开 ALAS PyWebIO WebSocket。"""
    try:
        import websocket

        ws = websocket.create_connection(
            websocket_url(config),
            timeout=timeout,
            enable_multithread=True,
        )
        set_websocket_timeout(ws, timeout)
        return ws
    except Exception as exc:
        raise WebSocketHandshakeError(str(exc)) from exc


def probe_websocket(config, timeout=5.0):
    """验证 WebSocket 可连接且能识别多配置。"""
    home_error = None
    try:
        check_pywebio_home(config, timeout=timeout)
    except Exception as exc:
        home_error = exc
    try:
        ws = open_pywebio_websocket(config, timeout=timeout)
    except Exception:
        if home_error:
            raise home_error
        raise
    try:
        local_storage = {"aside": str(config.get("current_config", "") or "") or None}
        state = collect_initial_state(ws, local_storage=local_storage)
        configs = extract_instance_names(state)
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
    result = probe_websocket(config)
    state = result.get("state")
    configs = result.get("configs", [])
    return extract_status_all(state, configs)


def post_config_action(config, config_name, action):
    """通过 WebSocket 对指定配置执行启动或停止。"""
    if action not in {"start", "stop"}:
        raise WebSocketHijackError("unsupported_action")
    home_error = None
    try:
        check_pywebio_home(config)
    except Exception as exc:
        home_error = exc
    try:
        ws = open_pywebio_websocket(config)
    except Exception:
        if home_error:
            raise home_error
        raise
    try:
        identify_storage = {}
        state = prepare_alas_page(ws, "", identify_storage)
        configs = extract_instance_names(state)
        if config_name not in configs:
            raise ConfigDetectionError("config_not_found")
    finally:
        try:
            ws.close()
        except Exception:
            pass
    ws = open_pywebio_websocket(config)
    set_websocket_timeout(ws, 0.5)
    try:
        local_storage = dict(identify_storage)
        local_storage["aside"] = str(config_name or "")
        state = collect_state_until_buttons(
            ws,
            CONFIG_ACTION_KEYWORDS.get(action, ()),
            max_messages=1500,
            local_storage=local_storage,
        )
        callback_id, value = find_config_action_callback(state, action)
        send_callback_event(ws, "", callback_id, value)
        status = "running" if action == "start" else "idle"
        return {"config": config_name, "status": status}
    finally:
        try:
            ws.close()
        except Exception:
            pass
