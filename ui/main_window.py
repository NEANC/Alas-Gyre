import os
import json
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QFrame,
    QVBoxLayout, QHBoxLayout, QScrollArea
)
from PySide6.QtCore import Qt, QTimer, Signal
import threading
import time

from alas_gyre.api.client import api_headers, api_request, gyre_api_url
from alas_gyre.api.control_gateway import get_configs as gateway_get_configs
from alas_gyre.api.control_gateway import get_status_all as gateway_get_status_all
from alas_gyre.api.control_gateway import post_action as gateway_post_action
from alas_gyre.api.connection_mode import CONNECTION_MODE_WEBSOCKET
from alas_gyre.api.connection_mode import normalize_connection_mode
from alas_gyre.api.websocket_hijack import get_persistent_manager
from alas_gyre.core.paths import (
    app_base_dir as app_base_dir, asset_path, config_path,
)
from alas_gyre.core.status import normalize_status
from .window_snap import snap_to_available_screen
from .i18n import get_language, tr
from .message_dialog import ask_confirm, show_info, show_warning
from .window_behavior import install_title_bar_drag, schedule_frameless_stabilize
from .widgets import (
    BottomIconButton,
    ConfigActionButton,
    ConfigDeleteButton,
    MarqueeLabel,
    StatusIndicator,
    WindowButton,
    build_bottom_icon,
    load_bottom_icon,
)



__all__ = [
    "AlasConsole",
    "BottomIconButton",
    "CardWidget",
    "ConfigActionButton",
    "ConfigDeleteButton",
    "MainConfigRow",
    "StatusIndicator",
    "WindowButton",
    "app_base_dir",
    "asset_path",
    "build_bottom_icon",
    "config_path",
    "get_status_text",
    "load_bottom_icon",
    "normalize_status",
]


try:
    from shiboken6 import isValid
except Exception:
    def isValid(widget):
        return widget is not None


def safe_emit_signal(signal, *args):
    """安全发射 Qt 信号，忽略信号源已删除的生命周期异常。"""
    try:
        signal.emit(*args)
        return True
    except RuntimeError as exc:
        if "Signal source has been deleted" in str(exc):
            return False
        raise


def control_connect_failed_message(config):
    """根据连接模式返回控制失败提示。"""
    if normalize_connection_mode(config) == CONNECTION_MODE_WEBSOCKET:
        return tr("control_websocket_failed")
    return tr("control_connect_failed")


MAIN_CARD_WIDTH = 294
MAIN_TITLE_HEIGHT = 30
MAIN_BOTTOM_HEIGHT = 40
MAIN_ROW_HEIGHT = 46
MAIN_ROW_SPACING = 2
MAIN_LIST_TOP_MARGIN = 8
MAIN_LIST_BOTTOM_MARGIN = 6
MAIN_VISIBLE_ROWS = 3
MAIN_LIST_HEIGHT = (
    MAIN_LIST_TOP_MARGIN
    + MAIN_LIST_BOTTOM_MARGIN
    + MAIN_VISIBLE_ROWS * MAIN_ROW_HEIGHT
    + max(MAIN_VISIBLE_ROWS - 1, 0) * MAIN_ROW_SPACING
    + 8
)
MAIN_CARD_HEIGHT = MAIN_TITLE_HEIGHT + MAIN_LIST_HEIGHT + MAIN_BOTTOM_HEIGHT


def get_status_text(status):
    return tr(normalize_status(status))


def build_config_status_lines(config_name, status, task="", show_task=False):
    """构建配置行的名称行和状态详情行。"""
    name = str(config_name or "")
    status_text = get_status_text(status)
    if show_task and task:
        return name, f"{status_text} · {task}"
    return name, status_text


class MainConfigRow(QWidget):
    btn_enable_signal = Signal(bool)

    def __init__(self, config_name, main_card, parent=None):
        super().__init__(parent)
        self.config_name = config_name
        self.main_card = main_card
        self.current_status = None
        self.current_task = ""
        self.setFixedHeight(46)
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 2, 6, 2)
        layout.setSpacing(10)

        self.statusIndicator = StatusIndicator()
        layout.addWidget(self.statusIndicator, alignment=Qt.AlignVCenter)

        vbox = QVBoxLayout()
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        self.nameLabel = MarqueeLabel()
        self.nameLabel.setObjectName("rowStatusLabel")
        self.nameLabel.setAlignment(Qt.AlignVCenter)

        self.statusLabel = MarqueeLabel()
        self.statusLabel.setObjectName("rowTaskLabel")
        self.statusLabel.setAlignment(Qt.AlignVCenter)
        font = self.statusLabel.font()
        font.setPointSize(font.pointSize() - 2)
        self.statusLabel.setFont(font)
        self.statusLabel.setStyleSheet("color: #888888;")

        vbox.addWidget(self.nameLabel)
        vbox.addWidget(self.statusLabel)

        layout.addLayout(vbox, stretch=1)

        self.deleteBtn = ConfigDeleteButton()
        self.deleteBtn.clicked.connect(self._on_delete_clicked)
        layout.addWidget(self.deleteBtn)

        self.toggleBtn = ConfigActionButton()
        self.toggleBtn.clicked.connect(self._on_toggle_clicked)
        self.btn_enable_signal.connect(self.toggleBtn.setEnabled)
        layout.addWidget(self.toggleBtn)

        self.update_status("idle", "")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.main_card.set_current_config(self.config_name)
            event.accept()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_label()

    def _refresh_label(self):
        name, detail = build_config_status_lines(
            self.config_name,
            self.current_status,
            self.current_task,
            self._should_show_task(),
        )
        self.nameLabel.set_marquee_text(name)
        self.statusLabel.set_marquee_text(detail)

    def _should_show_task(self):
        return bool(self.current_task and self.main_card.config.get("show_task_name", False))

    def apply_task_display_setting(self):
        self._refresh_label()

    def update_status(self, status, task=""):
        status = normalize_status(status)
        delete_enabled = status != "running" and len(self.main_card._configs) > 1
        if self.current_status == status and getattr(self, "current_task", "") == task:
            if self.deleteBtn.isEnabled() != delete_enabled:
                self.deleteBtn.setEnabled(delete_enabled)
            return

        self.current_status = status
        self.current_task = task
        self.statusIndicator.setStatus(self.current_status)
        self.toggleBtn.set_status(self.current_status)
        self.deleteBtn.setEnabled(delete_enabled)
        self._refresh_label()

    def _on_delete_clicked(self):
        if len(self.main_card._configs) <= 1:
            show_info(
                self,
                tr("delete_config_title"),
                tr("delete_config_last"),
            )
            return
        if self.current_status == "running":
            show_warning(
                self,
                tr("delete_config_title"),
                tr("delete_config_running", config=self.config_name),
            )
            return

        if ask_confirm(
            self,
            tr("delete_config_title"),
            tr("delete_config_confirm", config=self.config_name),
            tr("delete_config_action"),
            tr("cancel"),
            danger=True,
        ):
            self.main_card.delete_config(self.config_name)

    def _on_toggle_clicked(self):
        self.main_card.set_current_config(self.config_name)
        self.toggleBtn.setEnabled(False)
        action = "stop" if self.current_status == "running" else "start"

        def send_req():
            try:
                result = gateway_post_action(
                    self.main_card.config,
                    self.config_name,
                    action,
                )
                if result.ok:
                    if not result.data.get("submitted"):
                        status = normalize_status(result.data.get("status", "idle"))
                        safe_emit_signal(self.main_card.status_all_update_signal, {self.config_name: status}, {self.config_name: ""})
                        if self.main_card.current_config == self.config_name:
                            safe_emit_signal(self.main_card.status_update_signal, status, "")
                    if result.degraded:
                        self.main_card._notify_websocket_degraded_once()
                else:
                    safe_emit_signal(self.main_card.control_error_signal, action, control_connect_failed_message(self.main_card.config))
                time.sleep(0.5)
                self.main_card._start_poll_thread()
            except Exception as e:
                print(f"[Error] Failed to send control command: {e}")
                safe_emit_signal(
                    self.main_card.status_all_update_signal,
                    {self.config_name: "disconnected"},
                    {self.config_name: ""},
                )
                if self.main_card.current_config == self.config_name:
                    safe_emit_signal(self.main_card.status_update_signal, "disconnected", "")
                safe_emit_signal(self.main_card.control_error_signal, action, control_connect_failed_message(self.main_card.config))
            finally:
                safe_emit_signal(self.btn_enable_signal, True)

        threading.Thread(target=send_req, daemon=True).start()

class CardWidget(QFrame):
    """Main card"""
    status_update_signal = Signal(str, str)
    configs_update_signal = Signal(list)
    status_all_update_signal = Signal(dict, dict)
    config_delete_result_signal = Signal(bool, str, str, list, str)
    control_error_signal = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setFixedSize(MAIN_CARD_WIDTH, MAIN_CARD_HEIGHT)

        self.config = {
            "ip": "127.0.0.1",
            "port": "22267",
            "auto_start": False,
            "always_on_top": False,
            "api_token": "",
            "connection_mode": "auto",
            "mini_click_through": False,
            "show_task_name": False,
            "mini_opacity": 100,
            "lang": get_language(),
            "setup_completed": False,
        }

        self.config_path = config_path()
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded_config = json.load(f)
                self.config.update(loaded_config)
                if "setup_completed" not in loaded_config:
                    self.config["setup_completed"] = True
            except Exception as e:
                print(f"[Warning] Failed to read {self.config_path}: {e}")

        self._status = "idle" # idle, running, error, disconnected
        self._task = ""
        self._configs = ["alas"]
        self.current_config = self.config.get("current_config", "alas")
        self._configs[0] = self.current_config
        self._configs_fetching = False
        self._configs_last_fetch_at = 0.0
        self._configs_fetch_interval = 15.0
        self._polling_status = False
        self._poll_lock = threading.Lock()
        self._statuses = {}
        self._tasks = {}
        self.websocket_degraded_notified = False
        self.rows = {}

        self._config_idx = 0

        self._build_ui()

        self.status_update_signal.connect(self._update_status_ui)
        self.configs_update_signal.connect(self._on_configs_updated)
        self.status_all_update_signal.connect(self._on_status_all_updated)
        self.config_delete_result_signal.connect(self._on_config_delete_result)
        self.control_error_signal.connect(self._on_control_error)

        get_persistent_manager().set_status_callback(self._on_worker_status_changed)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._start_poll_thread)
        self.poll_timer.start(300)

        from PySide6.QtCore import QTimer as CoreQTimer
        CoreQTimer.singleShot(50, self._start_poll_thread)

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.windowCtrlBg = QWidget(self)
        self.windowCtrlBg.setObjectName("compactCtrlBg")
        self.windowCtrlBg.setAttribute(Qt.WA_StyledBackground, True)
        self.windowCtrlBg.setFixedHeight(30)
        install_title_bar_drag(self.window(), self.windowCtrlBg)
        ctrl_layout = QHBoxLayout(self.windowCtrlBg)
        ctrl_layout.setContentsMargins(20, 0, 8, 0)
        ctrl_layout.setSpacing(0)

        self.titleLabel = QLabel("Alas-Gyre", self.windowCtrlBg)
        self.titleLabel.setObjectName("settingsTitle")
        ctrl_layout.addWidget(self.titleLabel)
        ctrl_layout.addStretch()

        self.miniDot = WindowButton("minimize")
        self.miniDot.mousePressEvent = self._minimize_from_top
        self.closeDot = WindowButton("close")
        self.closeDot.mousePressEvent = self._close_from_top
        ctrl_layout.addWidget(self.miniDot, alignment=Qt.AlignVCenter)
        ctrl_layout.addWidget(self.closeDot, alignment=Qt.AlignVCenter)
        main_layout.addWidget(self.windowCtrlBg)

        self.configScroll = QScrollArea(self)
        self.configScroll.setObjectName("configScrollArea")
        self.configScroll.setAttribute(Qt.WA_StyledBackground, True)
        self.configScroll.setWidgetResizable(True)
        self.configScroll.setFrameShape(QFrame.NoFrame)
        self.configScroll.setFocusPolicy(Qt.NoFocus)
        self.configScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.configScroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self.configListBg = QWidget()
        self.configListBg.setObjectName("configListBg")
        self.configListBg.setAttribute(Qt.WA_StyledBackground, True)
        list_layout = QVBoxLayout(self.configListBg)
        list_layout.setContentsMargins(10, 8, 10, 6)
        list_layout.setSpacing(2)
        self.rows_layout = list_layout
        self.configScroll.setWidget(self.configListBg)
        self.configScroll.setFixedHeight(MAIN_LIST_HEIGHT)
        main_layout.addWidget(self.configScroll)

        self.bottomBg = QWidget(self)
        self.bottomBg.setObjectName("mainBottomBg")
        self.bottomBg.setAttribute(Qt.WA_StyledBackground, True)
        self.bottomBg.setFixedHeight(40)
        bot_layout = QHBoxLayout(self.bottomBg)
        bot_layout.setContentsMargins(24, 0, 24, 0)
        bot_layout.setSpacing(0)

        self.setIcon = BottomIconButton("settings")
        self.homeIcon = BottomIconButton("home")
        self.floatIcon = BottomIconButton("float")
        self.logIcon = BottomIconButton("log")

        self.setIcon.setToolTip(tr("settings_btn_tip"))
        self.homeIcon.setToolTip(tr("home_btn_tip"))
        self.floatIcon.setToolTip(tr("float_btn_tip"))
        self.logIcon.setToolTip(tr("log_btn_tip"))

        bot_layout.addWidget(self.setIcon)
        bot_layout.addStretch()
        bot_layout.addWidget(self.homeIcon)
        bot_layout.addStretch()
        bot_layout.addWidget(self.floatIcon)
        bot_layout.addStretch()
        bot_layout.addWidget(self.logIcon)

        self.setIcon.mousePressEvent = lambda e: self._on_icon_click("settings", self.setIcon)
        self.homeIcon.mousePressEvent = lambda e: self._on_icon_click("home", self.homeIcon)
        self.floatIcon.mousePressEvent = lambda e: self._on_icon_click("minimize", self.floatIcon)
        self.logIcon.mousePressEvent = lambda e: self._on_icon_click("log", self.logIcon)

        main_layout.addWidget(self.bottomBg)
        self._rebuild_rows()

    def retranslate_ui(self):
        self.setIcon.setToolTip(tr("settings_btn_tip"))
        self.homeIcon.setToolTip(tr("home_btn_tip"))
        self.floatIcon.setToolTip(tr("float_btn_tip"))
        self.logIcon.setToolTip(tr("log_btn_tip"))
        self._rebuild_rows()

    def _save_config(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"[Error] Failed to write {self.config_path}: {e}")
            return False

    def _notify_websocket_degraded_once(self):
        """提示用户已降级到 WebSocket 模式。"""
        if self.websocket_degraded_notified:
            return
        self.websocket_degraded_notified = True
        safe_emit_signal(self.control_error_signal, "degraded", tr("websocket_degraded_notice"))

    def apply_task_display_settings(self):
        for row in self.rows.values():
            row.apply_task_display_setting()

    def _sync_window_size(self, visible_count=None):
        _ = visible_count
        # The main window is deliberately not resized by the number of configs.
        # Config changes now only affect the scroll area's content. This keeps
        # the bottom menu visible after add/delete/status refresh operations.
        self.configScroll.setFixedHeight(MAIN_LIST_HEIGHT)
        self.setFixedSize(MAIN_CARD_WIDTH, MAIN_CARD_HEIGHT)
        self.updateGeometry()

        top_window = self.window()
        if top_window and top_window is not self:
            top_window.setMinimumSize(MAIN_CARD_WIDTH, MAIN_CARD_HEIGHT)
            top_window.setMaximumSize(MAIN_CARD_WIDTH, MAIN_CARD_HEIGHT)
            top_window.resize(MAIN_CARD_WIDTH, MAIN_CARD_HEIGHT)
            top_window.updateGeometry()

            # Re-apply after the current event pass. This protects the compact
            # frameless window from stale fixed-size state on Windows.
            QTimer.singleShot(
                0,
                lambda: isValid(top_window)
                and top_window.setFixedSize(MAIN_CARD_WIDTH, MAIN_CARD_HEIGHT),
            )

    def _rebuild_rows(self):
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.rows.clear()

        visible_configs = list(self._configs)
        if self.current_config and self.current_config not in visible_configs:
            visible_configs.insert(0, self.current_config)

        for config_name in visible_configs:
            row = MainConfigRow(config_name, self)
            self.rows_layout.addWidget(row)
            self.rows[config_name] = row
            if config_name in self._statuses:
                row.update_status(self._statuses[config_name], self._tasks.get(config_name, ""))
        self.rows_layout.addStretch()
        self._sync_window_size(len(visible_configs))

    def set_current_config(self, config_name):
        if not config_name or self.current_config == config_name:
            return
        self.current_config = config_name
        self.config["current_config"] = self.current_config
        self._save_config()
        if self.current_config not in self.rows:
            self._rebuild_rows()
        if hasattr(self, "log_dialog") and self.log_dialog.isVisible():
            self.log_dialog.set_config(self.current_config)

    def delete_config(self, config_name):
        threading.Thread(target=self._delete_config_task, args=(config_name,), daemon=True).start()

    def _delete_config_task(self, config_name):
        try:
            url = gyre_api_url(self.config, "configs")
            resp = api_request("DELETE",
                url,
                params={"config": config_name},
                headers=api_headers(self.config),
                timeout=5,
            )
            try:
                data = resp.json()
            except Exception:
                data = {}

            if resp.status_code == 200:
                configs = data.get("configs", [])
                default = data.get("default", "")
                self.config_delete_result_signal.emit(True, config_name, "", configs, default)
                return

            message = data.get("message") or data.get("error") or resp.text
            self.config_delete_result_signal.emit(False, config_name, message, [], "")
        except Exception as exc:
            self.config_delete_result_signal.emit(False, config_name, str(exc), [], "")

    def _on_config_delete_result(self, success, config_name, message, configs, default_config):
        if not success:
            show_warning(
                self,
                tr("delete_config_title"),
                tr("delete_config_failed", config=config_name, error=message),
            )
            return

        self._statuses.pop(config_name, None)
        self._tasks.pop(config_name, None)
        self._configs = [str(config) for config in configs if str(config)] or [
            config for config in self._configs if config != config_name
        ]
        if not self._configs:
            self._configs = ["alas"]

        if self.current_config == config_name:
            self.current_config = default_config if default_config in self._configs else self._configs[0]
            self.config["current_config"] = self.current_config
            self._save_config()

        self._rebuild_rows()
        if hasattr(self, "mini_dialog") and self.mini_dialog.isVisible():
            self.mini_dialog.rebuild_rows()
        if hasattr(self, "log_dialog") and self.log_dialog.isVisible():
            self.log_dialog.set_configs(self._configs, self.current_config)
            self.log_dialog.set_config(self.current_config)

        show_info(
            self,
            tr("delete_config_title"),
            tr("delete_config_success", config=config_name),
        )
        self._start_poll_thread()

    def format_control_http_error(self, resp):
        try:
            data = resp.json()
        except Exception:
            data = {}
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("message") or data.get("error") or "").strip()
        if not detail:
            detail = str(getattr(resp, "text", "") or "").strip()
        if detail:
            return tr("control_http_failed_with_detail", status=resp.status_code, error=detail[:300])
        return tr("control_http_failed", status=resp.status_code)

    def _on_control_error(self, action, message):
        parent = self.window()
        if hasattr(self, "mini_dialog") and self.mini_dialog.isVisible():
            parent = self.mini_dialog
        show_warning(
            parent or self,
            tr("control_failed_title"),
            message or tr("action_failed", error=action),
        )

    def _forward_drag_press(self, event):
        if self.window():
            self.window().mousePressEvent(event)

    def _forward_drag_move(self, event):
        if self.window():
            self.window().mouseMoveEvent(event)

    def _forward_drag_release(self, event):
        if self.window():
            self.window().mouseReleaseEvent(event)

    def _minimize_to_taskbar(self):
        if self.window():
            self.window().showMinimized()

    def _minimize_from_top(self, event):
        self._minimize_to_taskbar()
        event.accept()

    def _close_from_top(self, event):
        QApplication.quit()
        event.accept()

    def _update_status_ui(self, status, task=""):
        status = normalize_status(status)
        if self._status == status and getattr(self, "_task", "") == task:
            return
        self._status = status
        self._task = task
        if self.current_config in self.rows:
            self.rows[self.current_config].update_status(status, task)
        print(f"[Log] Status sync -> {status} ({self.current_config})")

    def _start_poll_thread(self):
        start_fetch = False
        start_poll = False
        with self._poll_lock:
            if not hasattr(self, "_configs_fetched") and not self._configs_fetching:
                self._configs_fetching = True
                self._configs_last_fetch_at = time.monotonic()
                start_fetch = True
            if not self._polling_status:
                self._polling_status = True
                start_poll = True

        if start_fetch:
            threading.Thread(target=self._fetch_configs_task, daemon=True).start()
        if start_poll:
            threading.Thread(target=self._poll_status_task_guarded, daemon=True).start()

    def _poll_status_task_guarded(self):
        try:
            self._poll_status_task()
        finally:
            with self._poll_lock:
                self._polling_status = False

    def _fetch_configs_task(self):
        try:
            if normalize_connection_mode(self.config) == CONNECTION_MODE_WEBSOCKET and hasattr(self, "_configs_fetched"):
                return
            result = gateway_get_configs(self.config)
            if result.ok:
                configs = result.data.get("configs", ["alas"])
                if isinstance(configs, list) and configs:
                    safe_emit_signal(self.configs_update_signal, configs)
                if result.degraded:
                    self._notify_websocket_degraded_once()
            else:
                safe_emit_signal(self.control_error_signal, "configs", tr("control_connect_failed"))
        except Exception:
            safe_emit_signal(self.control_error_signal, "configs", tr("control_connect_failed"))
        finally:
            with self._poll_lock:
                self._configs_fetching = False

    def _on_configs_updated(self, configs):
        self._configs_fetched = True
        new_configs = [str(config) for config in configs if str(config)]
        if not new_configs:
            new_configs = ["alas"]

        old_current_config = self.current_config
        if self.current_config not in new_configs:
            self.current_config = new_configs[0]
            self.config["current_config"] = self.current_config

        configs_changed = new_configs != self._configs
        current_changed = self.current_config != old_current_config
        self._configs = new_configs
        if not (configs_changed or current_changed):
            return

        self._rebuild_rows()

        old_status = self._status
        self._status = None
        self._task = ""
        self._update_status_ui(old_status or "idle", "")

        if hasattr(self, "mini_dialog") and self.mini_dialog.isVisible():
            self.mini_dialog.rebuild_rows()
        if hasattr(self, "log_dialog") and self.log_dialog.isVisible():
            self.log_dialog.set_configs(self._configs, self.current_config)

    def _on_status_all_updated(self, statuses, tasks):
        tasks = {
            str(config_name): str((tasks or {}).get(config_name, "") or "")
            for config_name in statuses
        }
        changed_statuses = {
            config_name: status
            for config_name, status in statuses.items()
            if self._statuses.get(config_name) != status or self._tasks.get(config_name) != tasks.get(config_name)
        }
        self._statuses.update(statuses)
        self._tasks.update(tasks)
        new_configs = [str(config_name) for config_name in statuses if str(config_name) not in self._configs]
        if new_configs:
            self._configs.extend(new_configs)
            self._configs.sort(key=str.lower)
            self._rebuild_rows()
        for config_name in changed_statuses.keys():
            if config_name in self.rows:
                self.rows[config_name].update_status(self._statuses[config_name], self._tasks.get(config_name, ""))

    def _on_worker_status_changed(self, config_name, status):
        """worker 状态变更实时回调（来自非 UI 线程，通过 signal 转到主线程）。"""
        safe_emit_signal(self.status_all_update_signal, {config_name: status}, {config_name: ""})
        if self.current_config == config_name:
            safe_emit_signal(self.status_update_signal, status, "")

    def _poll_status_task(self):
        if (
            hasattr(self, "_configs_fetched")
            and time.monotonic() - self._configs_last_fetch_at >= self._configs_fetch_interval
        ):
            delattr(self, "_configs_fetched")

        try:
            result = gateway_get_status_all(self.config)
            if result.ok:
                data = result.data
                statuses = {
                    str(config_name): normalize_status(status)
                    for config_name, status in data.get("statuses", {}).items()
                }
                tasks = {
                    str(config_name): str(task)
                    for config_name, task in data.get("tasks", {}).items()
                }
                safe_emit_signal(self.status_all_update_signal, statuses, tasks)
                current_status = statuses.get(self.current_config, "idle")
                current_task = tasks.get(self.current_config, "")
                safe_emit_signal(self.status_update_signal, current_status, current_task)
                if result.degraded:
                    self._notify_websocket_degraded_once()
            else:
                safe_emit_signal(self.status_update_signal, "disconnected", "")
        except Exception:
            safe_emit_signal(self.status_update_signal, "disconnected", "")

    def restore_main_window(self):
        if hasattr(self, "mini_dialog"):
            self.mini_dialog.hide()
        if self.window():
            self.window().showNormal()
            self.window().show()
            self.window().raise_()
            self.window().activateWindow()

    def show_mini_window(self):
        from .mini_window import MiniWindow
        if not hasattr(self, "mini_dialog"):
            self.mini_dialog = MiniWindow(self)

        if self.window():
            geom = self.window().geometry()
            self.mini_dialog.move(geom.x() + geom.width() // 2 - 100, geom.y() + geom.height() // 2 - 22)
            if self.config.get("mini_click_through", False):
                self.window().showMinimized()
            else:
                self.window().hide()
        self.mini_dialog.apply_window_settings()
        self.mini_dialog.show()

    def _on_icon_click(self, name, widget):
        print(f"[Log] Icon clicked -> {name}")
        if name == "close":
            QApplication.quit()
        elif name == "settings":
            from .settings_window import SettingsWindow
            dialog = SettingsWindow(self.window(), self.config, self._configs, self.current_config)
            if dialog.exec():
                try:
                    from .i18n import set_language
                    set_language(self.config.get("lang", "zh"))

                    with open(self.config_path, "w", encoding="utf-8") as f:
                        json.dump(self.config, f, indent=4, ensure_ascii=False)
                    print(f"[Log] Config successfully persisted to {self.config_path}")

                    self.retranslate_ui()

                    app = QApplication.instance()
                    tray_actions = getattr(app, "_alas_tray_actions", {})
                    if tray_actions:
                        action_texts = {
                            "show_main": tr("show_main"),
                            "show_float": tr("show_float"),
                            "open_webui": tr("open_webui"),
                            "settings": tr("settings_title"),
                            "wizard": tr("wizard"),
                            "quit": tr("quit"),
                        }
                        for action_name, action_text in action_texts.items():
                            action = tray_actions.get(action_name)
                            if action is not None:
                                action.setText(action_text)

                    from .theme import apply_theme
                    apply_theme(app, self.config.get("theme", "dark"))

                    if hasattr(self.window(), "apply_always_on_top"):
                        self.window().apply_always_on_top(self.config.get("always_on_top", False))
                    self.apply_task_display_settings()
                    if hasattr(self, "mini_dialog"):
                        self.mini_dialog.apply_window_settings()
                except Exception as e:
                    print(f"[Error] Failed to write {self.config_path}: {e}")
            dialog.deleteLater()
        elif name == "home":
            import webbrowser
            url = f"http://{self.config['ip']}:{self.config['port']}"
            print(f"[Log] Opening home page -> {url}")
            webbrowser.open(url)
        elif name == "log":
            from .log_window import LogWindow
            dialog = getattr(self, "log_dialog", None)
            if dialog is not None:
                try:
                    dialog.set_configs(self._configs, self.current_config)
                    dialog.set_config(self.current_config)
                    dialog.show()
                    dialog.activateWindow()
                    return
                except RuntimeError:
                    dialog = None
            if dialog is None:
                self.log_dialog = LogWindow(self.window(), self.config, self.current_config, self._configs)
                self.log_dialog.show()
        elif name == "minimize":
            self.show_mini_window()

class AlasConsole(QWidget):
    auto_update_result_signal = Signal(dict, int)

    def __init__(self):
        super().__init__()
        self.setObjectName("mainWindow")
        self.setWindowTitle("Alas-Gyre")
        self.setFixedSize(MAIN_CARD_WIDTH, MAIN_CARD_HEIGHT)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_StyledBackground, True)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.card = CardWidget(self)
        self.apply_always_on_top(self.card.config.get("always_on_top", False), show_after=False)
        main_layout.addWidget(self.card, alignment=Qt.AlignTop)
        self.card._sync_window_size()

        self._auto_update_check_id = 0
        self._update_prompt_shown = False
        self.auto_update_result_signal.connect(self._on_auto_update_result)
        self._center_on_screen()

    def start_auto_update_check(self, current_version):
        if self._update_prompt_shown:
            return
        self._auto_update_check_id += 1
        check_id = self._auto_update_check_id
        threading.Thread(
            target=self._auto_update_check_task,
            args=(current_version, check_id),
            daemon=True,
        ).start()

    def _auto_update_check_task(self, current_version, check_id):
        try:
            from alas_gyre.services.updater import check_for_updates
            result = check_for_updates(current_version)
        except Exception as exc:
            result = {"has_update": False, "error": str(exc)}
        try:
            if isValid(self):
                self.auto_update_result_signal.emit(result, check_id)
        except RuntimeError:
            pass

    def _on_auto_update_result(self, result, check_id):
        if not isValid(self):
            return
        if check_id != self._auto_update_check_id:
            return
        if not result.get("has_update"):
            if result.get("error"):
                print(f"[Update Check] Auto check failed: {result.get('error')}")
            return

        self._update_prompt_shown = True
        from .update_window import UpdatePromptWindow

        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

        self.update_dialog = UpdatePromptWindow(self, result)
        self.update_dialog.show()

    def apply_always_on_top(self, enabled, show_after=True):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(enabled))
        if show_after:
            self.show()

    def _center_on_screen(self):
        screen = QApplication.primaryScreen().availableGeometry()
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)

    def showEvent(self, event):
        super().showEvent(event)
        schedule_frameless_stabilize(self, self.card)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)

    def _snap_to_screen_edges(self):
        snap_to_available_screen(self, margins=(10, 10, 10, 10))
