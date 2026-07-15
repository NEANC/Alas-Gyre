#!/usr/bin/env python3
# -_- coding: utf-8 -_-

from dataclasses import dataclass
from dataclasses import field
import json
import logging
import re
import threading
import time
import traceback

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
_SCHEDULER_BTN_STATUS_LABELS = (
    "停止",
    "中止",
    "stop",
    "启动",
    "啟動",
    "実行",
    "start",
)
try:
    import websocket

    WebSocketTimeoutException = websocket.WebSocketTimeoutException
except Exception:
    WebSocketTimeoutException = TimeoutError
WEBSOCKET_TIMEOUT_ERRORS = (TimeoutError, WebSocketTimeoutException, ConnectionResetError)

logger = logging.getLogger(__name__)


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
        elif message.command == "output_ctl":
            target_scope = str(message.spec.get("scope", "") if isinstance(message.spec, dict) else "")
            method = str(message.spec.get("method", "") if isinstance(message.spec, dict) else "").lower()
            if target_scope and method in ("replace", "append"):
                replacement_data = message.spec.get("data")
                if method == "replace":
                    self.outputs = [
                        o for o in self.outputs
                        if str(o.get("scope", "") if isinstance(o, dict) else "") != target_scope
                    ]
                if replacement_data is not None:
                    self.outputs.append({
                        "scope": message.spec.get("scope"),
                        "data": replacement_data,
                    })
            elif target_scope:
                self.outputs[:] = [
                    o for o in self.outputs
                    if str(o.get("scope", "") if isinstance(o, dict) else "") != target_scope
                ]
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
class ControlCommand:
    """WS 单会话控制命令。"""

    config_name: str
    action: str


class SingleSessionScheduler:
    """单 PyWebIO session 的 WS 状态扫描与控制调度器。"""

    def __init__(self, config):
        self.config = dict(config or {})
        self.ws = None
        self.stop_event = threading.Event()
        self.scanner_thread = None
        self.local_storage = {}
        self.configs = []
        self.statuses = {}
        self.tasks = {}
        self.buttons = {}
        self.last_seen_at = {}
        self.scan_errors = {}
        self.control_errors = {}
        self.transport_available = False
        self.last_transport_error = ""
        self.on_status_changed = None
        self._lock = threading.Lock()
        self._control_queue = []
        self.sidebar_state = PyWebIOState()

    def get_configs_snapshot(self):
        """返回配置列表缓存。"""
        with self._lock:
            return list(self.configs)

    def get_status_all(self):
        """返回状态缓存快照。"""
        with self._lock:
            return {
                "statuses": dict(self.statuses),
                "tasks": dict(self.tasks),
                "scan_errors": dict(self.scan_errors),
                "control_errors": dict(self.control_errors),
                "debug_buttons": {
                    config_name: {
                        "start_callback_id": btn.start_callback_id,
                        "stop_callback_id": btn.stop_callback_id,
                    }
                    for config_name, btn in self.buttons.items()
                },
            }

    def post_action(self, config_name, action):
        """提交控制意图；同一配置只保留最后一次未执行意图。"""
        if action not in {"start", "stop"}:
            raise WebSocketHijackError("unsupported_action")
        command = ControlCommand(str(config_name), str(action))
        with self._lock:
            self.control_errors.pop(str(config_name), None)
            self._control_queue = [
                item for item in self._control_queue
                if item.config_name != command.config_name
            ]
            self._control_queue.append(command)
        logger.info("WS control enqueued: config_name=%s action=%s", command.config_name, command.action)
        return {"submitted": True}

    def _update_configs_from_state(self, state):
        """从目标状态更新配置列表缓存。"""
        configs = extract_instance_names(state)
        if not configs:
            return False
        with self._lock:
            old_configs = list(self.configs)
            self.configs = list(configs)
            for config_name in self.configs:
                self.statuses.setdefault(config_name, "idle")
                self.tasks.setdefault(config_name, "")
                self.buttons.setdefault(config_name, SchedulerButtonSet())
        changed = old_configs != configs
        if changed:
            logger.info("WS scheduler configs updated: configs=%s", configs)
        return changed

    def _update_config_from_state(self, config_name, state):
        """从目标状态更新单个配置状态和按钮缓存。"""
        if not _has_status_evidence(state):
            with self._lock:
                self.scan_errors[str(config_name)] = "target_scope_not_found"
            return False
        status_data = extract_status_all(state, [str(config_name)])
        new_status = status_data.get("statuses", {}).get(str(config_name))
        if not new_status:
            return False
        try:
            start_callback_id, start_value = find_config_action_callback(state, "start")
        except NavigationError:
            start_callback_id = ""
            start_value = None
        try:
            stop_callback_id, stop_value = find_config_action_callback(state, "stop")
        except NavigationError:
            stop_callback_id = ""
            stop_value = None
        with self._lock:
            old_status = self.statuses.get(str(config_name))
            self.statuses[str(config_name)] = new_status
            self.tasks[str(config_name)] = ""
            self.last_seen_at[str(config_name)] = time.monotonic()
            self.scan_errors.pop(str(config_name), None)
            # 按钮缓存仅用于诊断/监控批量状态；控制执行在同一个 session 内
            # 重新收集 scheduler_btn scope 以确保 callback 有效性。
            buttons = self.buttons.setdefault(str(config_name), SchedulerButtonSet())
            if start_callback_id:
                buttons.start_callback_id = start_callback_id
                buttons.start_value = start_value
            if stop_callback_id:
                buttons.stop_callback_id = stop_callback_id
                buttons.stop_value = stop_value
        changed = old_status != new_status
        if changed and callable(self.on_status_changed):
            self.on_status_changed(str(config_name), new_status)
        return changed

    def start(self):
        """启动单会话扫描线程。"""
        with self._lock:
            if self.scanner_thread is not None and self.scanner_thread.is_alive():
                return self
            self.stop_event.clear()
            self.scanner_thread = threading.Thread(target=self._run_loop, daemon=True)
            self.scanner_thread.start()
        return self

    def stop(self):
        """停止扫描线程并关闭 WS。"""
        self.stop_event.set()
        ws = self.ws
        self.ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread = self.scanner_thread
        if thread is not None:
            thread.join(timeout=3)

    def _mark_transport_error(self, exc):
        """标记 WS 传输错误，保留业务状态但清空 callback。"""
        with self._lock:
            self.transport_available = False
            self.last_transport_error = str(exc)
            self.buttons.clear()
        logger.warning(
            "WS scheduler transport disconnected: error_type=%s error=%s",
            type(exc).__name__,
            exc,
        )

    def _connect(self):
        """创建唯一 WS 连接。"""
        self.ws = open_pywebio_websocket(self.config)
        set_websocket_timeout(self.ws, 2.0)
        with self._lock:
            self.transport_available = True
            self.last_transport_error = ""
        return self.ws

    def _run_loop(self):
        """单会话扫描主循环。"""
        while not self.stop_event.is_set():
            try:
                if self.ws is None:
                    self._connect()
                    self._bootstrap()
                self._process_one_control_command()
                self._scan_once()
            except WEBSOCKET_TIMEOUT_ERRORS:
                continue
            except Exception as exc:
                self._mark_transport_error(exc)
                try:
                    if self.ws is not None:
                        self.ws.close()
                except Exception:
                    pass
                self.ws = None
                self.stop_event.wait(1.0)

    def _bootstrap(self):
        """初始化页面并采集配置列表。"""
        self.local_storage = {"aside": str(self.config.get("current_config", "") or "") or None}
        state = collect_target_state(
            self.ws,
            scope_keywords=("alas-instance-",),
            max_messages=1200,
            local_storage=self.local_storage,
            stop_on_first_match=False,
        )
        self._click_bootstrap_button_if_present(state, "简体中文")
        for label in ("深色", "Dark", "黑暗"):
            if self._click_bootstrap_button_if_present(state, label):
                break
        self.sidebar_state = state
        return self._update_configs_from_state(state)

    def _click_bootstrap_button_if_present(self, state, label):
        """点击初始化阶段可选按钮。"""
        if not self.ws:
            return False
        clicked = _click_button_if_present(self.ws, state, label)
        if clicked:
            logger.info("WS scheduler bootstrap clicked: label=%s", label)
        return clicked

    def _scan_once(self):
        """执行一轮配置状态扫描。"""
        with self._lock:
            configs = list(self.configs)
        if not configs:
            self._bootstrap()
            with self._lock:
                configs = list(self.configs)
        for config_name in configs:
            if self.stop_event.is_set():
                return False
            self._process_one_control_command()
            logger.debug("WS scheduler scan config start: config_name=%s", config_name)
            navigate_to_config(self.ws, self.sidebar_state, config_name)
            state = collect_target_state(
                self.ws,
                scope_keywords=("header_status", "scheduler_btn"),
                max_messages=300,
                local_storage=self.local_storage,
                stop_on_first_match=False,
                stop_when_all_keywords_matched=True,
                post_match_drain_target_outputs=5,
            )
            self._update_config_from_state(config_name, state)
            logger.debug("WS scheduler scan config end: config_name=%s", config_name)
        return True

    def _process_one_control_command(self):
        """处理一个控制命令。"""
        with self._lock:
            if not self._control_queue:
                return False
            command = self._control_queue.pop(0)
        logger.info("WS control dequeued: config_name=%s action=%s", command.config_name, command.action)
        try:
            navigate_to_config(self.ws, self.sidebar_state, command.config_name)
            state = collect_target_state(
                self.ws,
                scope_keywords=("scheduler_btn",),
                max_messages=300,
                local_storage=self.local_storage,
            )
            callback_id, value = find_config_action_callback(state, command.action)
            logger.info(
                "WS control callback found: config_name=%s action=%s callback_id=%s",
                command.config_name,
                command.action,
                callback_id,
            )
            send_callback_event(self.ws, "", callback_id, value)
            self._update_config_from_state(command.config_name, state)
            with self._lock:
                self.control_errors.pop(command.config_name, None)
            logger.info("WS control submitted: config_name=%s action=%s", command.config_name, command.action)
            return True
        except Exception as exc:
            with self._lock:
                self.control_errors[command.config_name] = str(exc)
            logger.warning(
                "WS control failed: config_name=%s action=%s error_type=%s error=%s",
                command.config_name,
                command.action,
                type(exc).__name__,
                exc,
            )
            return False


@dataclass
class PersistentConfigWorker:
    """常驻配置状态缓存。

    已废弃：WS 模式已重构为 SingleSessionScheduler 单会话调度器，
    此类仅保留以兼容历史测试代码，生产路径不再使用。
    """

    config: dict
    config_name: str
    buttons: SchedulerButtonSet = field(default_factory=SchedulerButtonSet)
    status: str = "disconnected"
    last_error: str = ""
    ws: object = None
    stop_event: object = None
    reader_thread: object = None
    local_storage: dict = field(default_factory=dict)
    on_status_changed: object = None

    def __post_init__(self):
        """初始化 stop_event 和累积状态（避免 dataclass field 共享可变默认值）。"""
        if self.stop_event is None:
            self.stop_event = threading.Event()
        self._accumulated_state = PyWebIOState()

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
        if _has_status_evidence(state):
            status_data = extract_status_all(state, [self.config_name])
            new_status = status_data.get("statuses", {}).get(self.config_name, self.status)
            if new_status != self.status:
                self.status = new_status
                if callable(self.on_status_changed):
                    self.on_status_changed(self.config_name, self.status)

    def read_once(self):
        """从 WebSocket 接收一条消息，解析并累积更新状态缓存。"""
        payload = self.ws.recv()
        if not payload:
            return None
        message = parse_pywebio_message(payload)
        state = PyWebIOState()
        state.apply_message(message)
        handle_pywebio_client_script(self.ws, message, local_storage=self.local_storage)
        self._accumulated_state.apply_message(message)
        self.update_from_state(self._accumulated_state)
        return message

    def read_loop_step(self):
        """执行一次读取循环步骤，超时返回 True，异常标记传输断线并返回 False。"""
        try:
            self.read_once()
            return True
        except WEBSOCKET_TIMEOUT_ERRORS:
            return True
        except Exception as exc:
            self.last_error = traceback.format_exc()
            self.ws = None
            logger.warning(
                "WS reader disconnected: config_name=%s status=%s error_type=%s error=%s",
                self.config_name,
                self.status,
                type(exc).__name__,
                exc,
            )
            return False

    def start_background_reader(self):
        """打开 WebSocket 连接并启动后台读取线程。"""
        self.ws = open_pywebio_websocket(self.config)
        try:
            set_websocket_timeout(self.ws, 2.0)
            self.local_storage = {"aside": self.config_name or None}
            state = prepare_alas_page(self.ws, self.config_name, self.local_storage)
            self._accumulated_state = state
            self.update_from_state(self._accumulated_state)
            self.stop_event.clear()

            def _reader_loop():
                while not self.stop_event.is_set():
                    if not self.read_loop_step():
                        break
                try:
                    self.ws.close()
                except Exception:
                    pass

            self.reader_thread = threading.Thread(target=_reader_loop, daemon=True)
            self.reader_thread.start()
        except Exception:
            try:
                self.ws.close()
            except Exception:
                pass
            raise

    def _navigate_to_config_page(self):
        """点击侧边栏目标配置按钮，使页面显示该配置的调度器。"""
        try:
            callback_id, value = find_button_callback(
                self._accumulated_state, self.config_name, scope_keyword="alas-instance-"
            )
        except NavigationError:
            logger.warning("导航至配置页面失败: config_name=%s", self.config_name)
            return
        send_callback_event(self.ws, "", callback_id, value)
        nav_state = collect_initial_state(self.ws, max_messages=300, local_storage=self.local_storage)
        self._accumulated_state.outputs.extend(nav_state.outputs)
        self._accumulated_state.inputs.extend(nav_state.inputs)
        self._accumulated_state.scripts.extend(nav_state.scripts)
        self._accumulated_state.task_ids.update(nav_state.task_ids)
        self._accumulated_state.pin_names.update(nav_state.pin_names)
        self._accumulated_state.callback_ids.update(nav_state.callback_ids)
        if nav_state.session_id:
            self._accumulated_state.session_id = nav_state.session_id
        self.update_from_state(self._accumulated_state)

    def post_action(self, action):
        """使用已缓存的调度器按钮回调发送启动或停止动作。"""
        if action == "start":
            callback_id = self.buttons.start_callback_id
            value = self.buttons.start_value
        elif action == "stop":
            callback_id = self.buttons.stop_callback_id
            value = self.buttons.stop_value
        else:
            raise WebSocketHijackError("unsupported_action")
        if not callback_id:
            raise WebSocketHijackError("worker_not_ready")
        if self.ws is None:
            raise WebSocketHijackError("websocket_disconnected")
        send_callback_event(self.ws, "", callback_id, value)
        return {
            "config": self.config_name,
            "action": action,
            "status": self.status,
            "submitted": True,
        }


class WebSocketHijackManager:
    """WS 劫持连接生命周期管理器（单会话调度器）。"""

    _FINGERPRINT_KEY_FIELDS = ("ip", "port")

    def __init__(self):
        """初始化单会话 WS manager。"""
        self.config = {}
        self.scheduler = None
        self._lock = threading.Lock()
        self._config_fingerprint = None
        self._status_callback = None

    def set_status_callback(self, callback):
        """注册状态变更回调。"""
        self._status_callback = callback
        with self._lock:
            if self.scheduler is not None:
                self.scheduler.on_status_changed = callback

    @staticmethod
    def _make_config_fingerprint(config):
        """从配置关键字段计算指纹，用于判断配置是否变更。"""
        items = tuple(
            (k, str(config.get(k, "")))
            for k in WebSocketHijackManager._FINGERPRINT_KEY_FIELDS
        )
        return hash(items)

    def ensure_started(self, config):
        """确保单会话调度器已启动。"""
        self.config = config
        fingerprint = self._make_config_fingerprint(config)
        old_scheduler = None
        with self._lock:
            if fingerprint == self._config_fingerprint and self.scheduler is not None:
                self.scheduler.start()
                return self
            old_scheduler = self.scheduler
            self._config_fingerprint = fingerprint
            self.scheduler = SingleSessionScheduler(config)
            self.scheduler.on_status_changed = self._status_callback
            scheduler = self.scheduler
        if old_scheduler is not None:
            old_scheduler.stop()
        scheduler.start()
        return self

    def get_configs(self):
        """返回配置列表缓存。"""
        with self._lock:
            scheduler = self.scheduler
        if scheduler is None:
            return []
        return scheduler.get_configs_snapshot()

    def get_status_all(self):
        """返回全部状态缓存。"""
        with self._lock:
            scheduler = self.scheduler
        if scheduler is None:
            return {"statuses": {}, "tasks": {}}
        return scheduler.get_status_all()

    def post_action(self, config_name, action):
        """提交控制命令。"""
        with self._lock:
            scheduler = self.scheduler
        if scheduler is None:
            raise WebSocketHijackError("scheduler_not_started")
        return scheduler.post_action(config_name, action)

    def stop_all(self):
        """停止单会话调度器。"""
        with self._lock:
            scheduler = self.scheduler
            self.scheduler = None
        if scheduler is not None:
            scheduler.stop()


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
    if text in {"idle", "空闲", "闲置", "閒置", "inactive", "実行中止", "未运行", "stopped", "stop"}:
        return "idle"
    if text in {"running", "运行中", "run"}:
        return "running"
    if text in {"error", "出错", "错误", "发生错误", "發生錯誤", "エラー発生", "warning"}:
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
    logger.debug(
        "WS eval reply: task_id=%s data_type=%s code=%s",
        message.task_id,
        type(data).__name__,
        code[:80],
    )
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
    return names


def _has_status_evidence(state):
    """判断 PyWebIO 状态中是否包含可用于更新实例状态的证据。"""
    header_status_keywords = (
        "运行中",
        "空闲",
        "闲置",
        "閒置",
        "未连接",
        "错误",
        "发生错误",
        "發生錯誤",
        "更新中",
        "running",
        "idle",
        "inactive",
        "stopped",
        "error",
        "warning",
        "updating",
        "運行中",
        "錯誤",
        "実行中",
        "実行中止",
        "エラー発生",
        "エラー",
    )
    for output in state.outputs:
        text = _searchable_text(output)
        lower_text = text.lower()
        scope = str(output.get("scope", "") if isinstance(output, dict) else "")
        if "header_status" in scope and any(keyword in lower_text for keyword in header_status_keywords):
            return True
        if "scheduler_btn" in scope and any(label in lower_text for label in _SCHEDULER_BTN_STATUS_LABELS):
            return True
    return False


def extract_status_all(state, configs=None):
    """从 ALAS 页面输出中提取所有实例状态。"""
    configs = list(configs or extract_instance_names(state))
    if not configs:
        return {"statuses": {}, "tasks": {}}
    statuses = {config_name: "error" for config_name in configs}
    status_text = ""
    for output in reversed(state.outputs):
        text = _searchable_text(output)
        scope = str(output.get("scope", "") if isinstance(output, dict) else "")
        if "header_status" not in scope:
            continue
        for candidate in (
            "运行中",
            "空闲",
            "闲置",
            "閒置",
            "Inactive",
            "実行中止",
            "未连接",
            "发生错误",
            "發生錯誤",
            "エラー発生",
            "Warning",
            "错误",
            "更新中",
        ):
            if candidate in text:
                status_text = candidate
                break
        if status_text:
            break
    if status_text:
        status = normalize_alas_status(status_text)
        statuses[configs[0]] = status
    else:
        for output in reversed(state.outputs):
            scope = str(output.get("scope", "") if isinstance(output, dict) else "")
            if "scheduler_btn" not in scope:
                continue
            text = _searchable_text(output)
            lower_text = text.lower()
            if any(label in lower_text for label in ("停止", "中止", "stop")):
                statuses[configs[0]] = "running"
                break
            if any(label in lower_text for label in ("启动", "啟動", "実行", "start")):
                statuses[configs[0]] = "idle"
                break
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
    """按精确按钮标签查找回调 ID 与按钮索引。"""
    expected = str(label or "")
    for output in state.outputs:
        scope = str(output.get("scope", "") if isinstance(output, dict) else "")
        if scope_keyword and scope_keyword not in scope:
            continue
        for group_callback_id, buttons in _iter_button_groups(output):
            for index, button in enumerate(buttons):
                if not isinstance(button, dict):
                    continue
                if str(button.get("label", "") or "") != expected:
                    continue
                callback_id = str(button.get("callback_id", "") or button.get("callback", "") or group_callback_id)
                if callback_id:
                    return callback_id, index
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
    logger.info(
        "WS callback send: callback_id=%s data_type=%s",
        callback_id,
        type(data).__name__,
    )
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
    messages = 0
    timeout_reached = False
    for _ in range(max_messages):
        try:
            payload = ws.recv()
        except WEBSOCKET_TIMEOUT_ERRORS:
            timeout_reached = True
            break
        if not payload:
            continue
        messages += 1
        message = parse_pywebio_message(payload)
        state.apply_message(message)
        handle_pywebio_client_script(ws, message, local_storage=local_storage)
    logger.info(
        "WS collect summary: messages=%s outputs=%s inputs=%s scripts=%s pins=%s session_id=%s timeout=%s",
        messages,
        len(state.outputs),
        len(state.inputs),
        len(state.scripts),
        len(state.pin_names),
        bool(state.session_id),
        timeout_reached,
    )
    return state


def set_websocket_timeout(ws, timeout):
    """设置 WebSocket 接收超时，避免持续日志导致控制流程阻塞。"""
    try:
        ws.settimeout(timeout)
    except AttributeError:
        pass


def _get_websocket_timeout(ws, fallback=2.0):
    """读取当前 WebSocket 超时值，取不到时返回 fallback。"""
    try:
        return ws.gettimeout()
    except AttributeError:
        return fallback


def collect_target_state(ws, scope_keywords, max_messages=300, local_storage=None,
                        stop_on_first_match=True,
                        stop_when_all_keywords_matched=False,
                        post_match_drain_target_outputs=0):
    """按目标 scope 关键词收集 PyWebIO 消息，支持三种停止策略。

    stop_on_first_match=True（默认）：
        首次匹配任一目标 scope 立即返回。
    stop_when_all_keywords_matched=True：
        所有关键词各命中一次后，继续 drain post_match_drain_target_outputs 条
        目标 scope 输出，然后返回。drain 阶段使用 0.2s 短 timeout 避免阻塞。
    两者都为 False：
        在 max_messages 范围内持续收集所有匹配 scope，直到 timeout 或上限。
    优先级：stop_on_first_match > stop_when_all_keywords_matched > 无提前停止。
    """
    state = PyWebIOState()
    expected = tuple(str(item or "") for item in scope_keywords)
    matched_keywords = set()
    all_keywords_matched = False
    drain_remaining = 0
    saved_timeout = _get_websocket_timeout(ws, fallback=2.0)
    messages = 0
    for _ in range(max_messages):
        try:
            payload = ws.recv()
        except WEBSOCKET_TIMEOUT_ERRORS:
            break
        if not payload:
            continue
        messages += 1
        message = parse_pywebio_message(payload)
        handle_pywebio_client_script(ws, message, local_storage=local_storage)
        if message.command not in {"output", "output_ctl"}:
            continue
        scope = str(message.spec.get("scope", "") if isinstance(message.spec, dict) else "")
        matched_scope = next((kw for kw in expected if kw in scope), None)
        if matched_scope is None:
            continue
        state.apply_message(message)
        matched_keywords.add(matched_scope)
        logger.info(
            "WS target scope matched: messages=%s scope=%s keywords=%s outputs=%s",
            messages,
            scope,
            expected,
            len(state.outputs),
        )
        if stop_on_first_match:
            set_websocket_timeout(ws, saved_timeout)
            return state
        if stop_when_all_keywords_matched and not all_keywords_matched:
            if matched_keywords >= set(expected):
                logger.info(
                    "WS target scope all matched: messages=%s keywords=%s drain=%s",
                    messages,
                    expected,
                    post_match_drain_target_outputs,
                )
                all_keywords_matched = True
                drain_remaining = post_match_drain_target_outputs + 1
                set_websocket_timeout(ws, 0.2)
        if all_keywords_matched:
            drain_remaining -= 1
            if drain_remaining <= 0:
                logger.info(
                    "WS target scope drain done: messages=%s keywords=%s",
                    messages,
                    expected,
                )
                set_websocket_timeout(ws, saved_timeout)
                return state
    set_websocket_timeout(ws, saved_timeout)
    logger.info("WS target scope timeout: messages=%s keywords=%s", messages, expected)
    return state


def collect_state_until_buttons(ws, labels, max_messages=300, local_storage=None):
    """收集 PyWebIO 状态直到出现任一目标按钮。"""
    state = PyWebIOState()
    expected_labels = tuple(str(label or "") for label in labels)
    messages = 0
    matched_label = ""
    for _ in range(max_messages):
        try:
            payload = ws.recv()
        except WEBSOCKET_TIMEOUT_ERRORS:
            break
        if not payload:
            continue
        messages += 1
        message = parse_pywebio_message(payload)
        state.apply_message(message)
        handle_pywebio_client_script(ws, message, local_storage=local_storage)
        for label in expected_labels:
            try:
                find_button_callback(state, label)
                matched_label = label
                logger.info(
                    "WS collect buttons matched: messages=%s label=%s outputs=%s scripts=%s",
                    messages,
                    matched_label,
                    len(state.outputs),
                    len(state.scripts),
                )
                return state
            except NavigationError:
                continue
    logger.info(
        "WS collect buttons summary: messages=%s matched=%s labels=%s outputs=%s scripts=%s",
        messages,
        bool(matched_label),
        expected_labels,
        len(state.outputs),
        len(state.scripts),
    )
    return state


def _click_button_if_present(ws, state, label, scope_keyword=""):
    """如果指定按钮存在则点击并返回是否已点击。"""
    try:
        callback_id, value = find_button_callback(state, label, scope_keyword=scope_keyword)
    except NavigationError:
        return False
    send_callback_event(ws, "", callback_id, value)
    return True


def navigate_to_config(ws, state, config_name):
    """在当前 PyWebIO session 中点击侧边栏配置。"""
    callback_id, value = find_button_callback(state, config_name, scope_keyword="alas-instance-")
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
    """通过 WebSocket 点击 ALAS 更新器动作（本轮不调用）。"""
    _ = (config, action)
    raise WebSocketHijackError("update_action_out_of_scope")


_PERSISTENT_MANAGER = WebSocketHijackManager()


def get_persistent_manager():
    """获取模块级 WebSocketHijackManager 单例。"""
    return _PERSISTENT_MANAGER


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
        logger.info("WS connected: url=%s timeout=%s", websocket_url(config), timeout)
        return ws
    except Exception as exc:
        logger.warning(
            "WS connect failed: url=%s timeout=%s error_type=%s error=%s",
            websocket_url(config),
            timeout,
            type(exc).__name__,
            exc,
        )
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
    """通过单会话调度器获取 ALAS 多配置缓存。"""
    manager = get_persistent_manager().ensure_started(config)
    return manager.get_configs()


def get_status_all(config):
    """通过单会话调度器获取多配置状态。"""
    manager = get_persistent_manager().ensure_started(config)
    return manager.get_status_all()


def post_config_action(config, config_name, action):
    """通过单会话调度器提交配置启动或停止。"""
    manager = get_persistent_manager().ensure_started(config)
    return manager.post_action(config_name, action)
