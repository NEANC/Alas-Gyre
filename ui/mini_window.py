from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QGraphicsDropShadowEffect, QFrame
from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
import sys
from alas_gyre.api.control_gateway import post_action as gateway_post_action
from alas_gyre.core.status import normalize_status
from .widgets import StatusIndicator, ConfigActionButton, MarqueeLabel
from .window_snap import snap_to_available_screen
from .i18n import tr
from .main_window import control_connect_failed_message, safe_emit_signal
from .window_behavior import schedule_frameless_stabilize

class MiniActionButton(ConfigActionButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(40, 24)

    def paintEvent(self, event):
        super(ConfigActionButton, self).paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self._status == "running":
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#ff4d4f"))
            painter.drawRect(17, 8, 8, 8)
        else:
            path = QPainterPath()
            path.moveTo(16, 7)
            path.lineTo(16, 17)
            path.lineTo(26, 12)
            path.closeSubpath()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#28e06f"))
            painter.drawPath(path)
        painter.end()

class MiniConfigRow(QWidget):
    btn_enable_signal = Signal(bool)

    def __init__(self, config_name, main_card, parent=None):
        super().__init__(parent)
        self.config_name = config_name
        self.main_card = main_card

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 5, 8, 5)
        layout.setSpacing(8)

        self.statusIndicator = StatusIndicator()
        self.statusIndicator.setFixedSize(20, 20)
        layout.addWidget(self.statusIndicator)

        textLayout = QVBoxLayout()
        textLayout.setContentsMargins(0, 0, 0, 0)
        textLayout.setSpacing(0)

        self.statusLabel = MarqueeLabel()
        self.statusLabel.setObjectName("miniStatusLabel")

        self.taskLabel = MarqueeLabel()
        self.taskLabel.setObjectName("miniTaskLabel")
        font = self.taskLabel.font()
        font.setPointSize(font.pointSize() - 2)
        self.taskLabel.setFont(font)
        self.taskLabel.setStyleSheet("color: #888888;")
        self.taskLabel.hide()

        textLayout.addWidget(self.statusLabel)
        textLayout.addWidget(self.taskLabel)

        layout.addLayout(textLayout, stretch=1)

        self.toggleBtn = MiniActionButton()
        self.toggleBtn.clicked.connect(self._on_toggle_clicked)
        self.btn_enable_signal.connect(self.toggleBtn.setEnabled)

        layout.addWidget(self.toggleBtn)
        self.current_status = "idle"
        self.toggleBtn.set_status("idle")
        self._status_text = tr("idle")
        self._task_text = ""
        self._refresh_status_label()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_status_label()

    def _refresh_status_label(self):
        full_text = f"{self.config_name}: {self._status_text}"
        self.statusLabel.set_marquee_text(full_text)
        if self._should_show_task():
            self.taskLabel.set_marquee_text(self._task_text)
        else:
            self.taskLabel.set_marquee_text("")

    def _set_status_text(self, status_text):
        self._status_text = status_text
        self._refresh_status_label()

    def _should_show_task(self):
        return bool(self._task_text and self.main_card.config.get("show_task_name", False))

    def apply_task_display_setting(self):
        self.taskLabel.setVisible(self._should_show_task())
        self._refresh_status_label()

    def update_status(self, status, task=""):
        status = normalize_status(status)
        self.current_status = status
        self.statusIndicator.setStatus(status)
        self.toggleBtn.set_status(status)
        self._status_text = tr(status)
        self._task_text = task
        self.taskLabel.setVisible(self._should_show_task())
        self._refresh_status_label()

    def _on_toggle_clicked(self):
        self.toggleBtn.setEnabled(False)
        action = "stop" if self.current_status == "running" else "start"
        import threading
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
                    elif not result.data.get("transport_available", True):
                        # 命令已入队但 WS 传输未就绪——给用户可感知反馈
                        safe_emit_signal(self.main_card.status_all_update_signal, {self.config_name: "queued"}, {self.config_name: ""})
                        if self.main_card.current_config == self.config_name:
                            safe_emit_signal(self.main_card.status_update_signal, "queued", "")
                    if result.degraded:
                        self.main_card._notify_websocket_degraded_once()
                else:
                    safe_emit_signal(self.main_card.control_error_signal, action, control_connect_failed_message(self.main_card.config))
            except Exception as exc:
                print(f"[错误] 悬浮窗控制命令失败: {exc}")
                safe_emit_signal(self.main_card.status_all_update_signal, {self.config_name: "disconnected"}, {self.config_name: ""})
                if self.main_card.current_config == self.config_name:
                    safe_emit_signal(self.main_card.status_update_signal, "disconnected", "")
                safe_emit_signal(self.main_card.control_error_signal, action, control_connect_failed_message(self.main_card.config))
            finally:
                safe_emit_signal(self.btn_enable_signal, True)
        threading.Thread(target=send_req, daemon=True).start()

class MiniWindow(QWidget):
    """
    极简悬浮多行看板
    """
    def __init__(self, main_card):
        super().__init__()
        self.main_card = main_card
        self.setObjectName("miniWindow")
        self._click_through_enabled = None

        # 悬浮无边框
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)

        self.bg = QFrame(self)
        self.bg.setObjectName("miniBg")
        self.bg.setAttribute(Qt.WA_StyledBackground, True)
        self.bg_layout = QVBoxLayout(self.bg)
        self.bg_layout.setContentsMargins(5, 5, 5, 5)
        self.bg_layout.setSpacing(2)

        # 还原按钮栏 (顶部横条)
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 8, 0)
        top_bar.setSpacing(0)
        top_bar.addStretch()
        self.restoreBtn = QPushButton("⛶ " + tr("show_main"))
        self.restoreBtn.setObjectName("miniRestoreBtn")
        self.restoreBtn.setCursor(Qt.PointingHandCursor)
        self.restoreBtn.setFocusPolicy(Qt.NoFocus)
        self.restoreBtn.clicked.connect(self._on_restore_clicked)
        top_bar.addWidget(self.restoreBtn)
        self.bg_layout.addLayout(top_bar)

        # 分割线
        line = QFrame()
        line.setObjectName("miniDivider")
        line.setFrameShape(QFrame.HLine)
        line.setFixedHeight(1)
        self.bg_layout.addWidget(line)

        # 动态创建行
        self.rows = {}
        for c in self.main_card._configs:
            row = MiniConfigRow(c, self.main_card)
            self.bg_layout.addWidget(row)
            self.rows[c] = row

        main_layout.addWidget(self.bg)

        # 阴影
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(15)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 100))
        self.bg.setGraphicsEffect(shadow)

        self.setFixedWidth(220)

        # 绑定批量状态更新信号
        self.main_card.status_all_update_signal.connect(self._on_status_all_updated)
        self.apply_window_settings()

    def rebuild_rows(self):
        # 清理旧的行
        for row in self.rows.values():
            self.bg_layout.removeWidget(row)
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self.rows.clear()

        # 重新创建
        for c in self.main_card._configs:
            row = MiniConfigRow(c, self.main_card)
            self.bg_layout.addWidget(row)
            self.rows[c] = row

    def _on_restore_clicked(self):
        self.main_card.restore_main_window()

    def _on_status_all_updated(self, statuses: dict, tasks: dict):
        for c, status in statuses.items():
            if c in self.rows:
                self.rows[c].update_status(status, tasks.get(c, ""))

    def showEvent(self, event):
        super().showEvent(event)
        self.apply_window_settings()
        schedule_frameless_stabilize(self, self.bg, stable_input_region=False)

    def apply_window_settings(self):
        self.restoreBtn.setText("⛶ " + tr("show_main"))
        self.rebuild_rows()
        for c, status in self.main_card._statuses.items():
            if c in self.rows:
                self.rows[c].update_status(status, self.main_card._tasks.get(c, ""))

        opacity = self._normalize_opacity(self.main_card.config.get("mini_opacity", 100))
        self.setWindowOpacity(opacity / 100.0)

        enabled = bool(self.main_card.config.get("mini_click_through", False))
        self._click_through_enabled = enabled
        self._disable_global_click_through()

    def _normalize_opacity(self, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 100
        return max(35, min(value, 100))

    def _disable_global_click_through(self):
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        transparent_flag = getattr(Qt, "WindowTransparentForInput", None)
        if transparent_flag is None and hasattr(Qt, "WindowType"):
            transparent_flag = getattr(Qt.WindowType, "WindowTransparentForInput", None)
        if transparent_flag is not None:
            was_visible = self.isVisible()
            self.setWindowFlag(transparent_flag, False)
            if was_visible:
                self.show()
        self._apply_native_click_through(False)

    def _apply_native_click_through(self, enabled):
        if sys.platform != "win32":
            return

        try:
            import ctypes
            hwnd = int(self.winId())
            gwl_exstyle = -20
            ws_ex_transparent = 0x00000020
            ws_ex_layered = 0x00080000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, gwl_exstyle)
            if enabled:
                style |= ws_ex_layered | ws_ex_transparent
            else:
                style &= ~ws_ex_transparent
            ctypes.windll.user32.SetWindowLongW(hwnd, gwl_exstyle, style)
        except Exception as exc:
            print(f"[警告] 应用悬浮窗鼠标穿透失败: {exc}")

    def nativeEvent(self, event_type, message):
        if (
            sys.platform == "win32"
            and self._click_through_enabled
        ):
            try:
                from ctypes import wintypes
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == 0x0084:  # WM_NCHITTEST
                    point = self._point_from_lparam(msg.lParam)
                    local_pos = self.mapFromGlobal(point)
                    if not self._is_interactive_point(local_pos):
                        return True, -1  # HTTRANSPARENT
            except Exception as e:
                print(f"[警告] 悬浮窗鼠标穿透 nativeEvent 处理失败: {e}")
        return super().nativeEvent(event_type, message)

    def _point_from_lparam(self, lparam):
        x = lparam & 0xFFFF
        y = (lparam >> 16) & 0xFFFF
        if x & 0x8000:
            x -= 0x10000
        if y & 0x8000:
            y -= 0x10000
        return QPoint(x, y)

    def _is_interactive_point(self, local_pos):
        widget = self.childAt(local_pos)
        while widget is not None:
            if isinstance(widget, QPushButton):
                return True
            widget = widget.parentWidget()
        return False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if hasattr(self, "_drag_offset") and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._snap_to_screen_edges()
            if hasattr(self, "_drag_offset"):
                del self._drag_offset
            event.accept()

    def _snap_to_screen_edges(self):
        snap_to_available_screen(self, margins=(10, 10, 10, 10))
