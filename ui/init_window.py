import os
import secrets
import threading

from PySide6.QtCore import Qt, Signal, QTimer, QSize, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QGridLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from alas_gyre.api.connection_mode import CONNECTION_MODE_AUTO, normalize_connection_mode
from alas_gyre.api.control_gateway import test_connection
from alas_gyre.api.overlay_launcher import RUNTIME_DIR_NAME, generate_portable_overlay_launchers
from alas_gyre.api.websocket_hijack import (
    ConfigDetectionError,
    NavigationError,
    NotPyWebIOError,
    WebSocketHandshakeError,
    WebUIUnavailableError,
)
from alas_gyre.core.config import ensure_api_token, save_config
from alas_gyre.core.paths import app_base_dir
from .message_dialog import ask_confirm
from .widgets import WindowButton
from .i18n import tr
from .window_behavior import install_title_bar_drag, schedule_frameless_stabilize


class InitSetupWindow(QDialog):
    test_result_signal = Signal(bool, str)

    def __init__(
        self,
        parent=None,
        config=None,
        config_path="",
    ):
        super().__init__(parent)
        self.config = config if config is not None else {}
        self.config_path = config_path
        self.runtime_output_dir = os.path.join(app_base_dir(), RUNTIME_DIR_NAME)
        self.runtime_generated = os.path.isdir(self.runtime_output_dir)
        self.current_step = 0

        self.setObjectName("initWindow")
        self.setFixedSize(680, 420)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.card = QFrame(self)
        self.card.setObjectName("initCard")
        self.card.setAttribute(Qt.WA_StyledBackground, True)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        self.topBg = QWidget(self.card)
        self.topBg.setObjectName("initTopBg")
        self.topBg.setAttribute(Qt.WA_StyledBackground, True)
        self.topBg.setFixedHeight(30)
        install_title_bar_drag(self, self.topBg)
        top_layout = QHBoxLayout(self.topBg)
        top_layout.setContentsMargins(20, 0, 8, 0)

        title = QLabel(tr("wizard_title"))
        title.setObjectName("initTitle")
        top_layout.addWidget(title)
        top_layout.addStretch()

        self.closeBtn = WindowButton("close", self.topBg)
        self.closeBtn.mousePressEvent = lambda event: self.reject() if event.button() == Qt.LeftButton else None
        top_layout.addWidget(self.closeBtn)
        card_layout.addWidget(self.topBg)

        self.bodyBg = QWidget(self.card)
        self.bodyBg.setObjectName("initBodyBg")
        self.bodyBg.setAttribute(Qt.WA_StyledBackground, True)
        body_layout = QVBoxLayout(self.bodyBg)
        body_layout.setContentsMargins(22, 16, 22, 16)
        body_layout.setSpacing(10)

        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(18)

        self.stepRail = QFrame(self.bodyBg)
        self.stepRail.setObjectName("initStepRail")
        self.stepRail.setFixedWidth(156)
        rail_layout = QVBoxLayout(self.stepRail)
        rail_layout.setContentsMargins(14, 14, 14, 14)
        rail_layout.setSpacing(8)

        self.stepProgressLabel = QLabel(self.stepRail)
        self.stepProgressLabel.setObjectName("initStepProgress")
        rail_layout.addWidget(self.stepProgressLabel)
        rail_layout.addSpacing(4)

        self.stepNavItems = []
        for number, key in (
            (1, "init_nav_runtime"),
            (2, "init_nav_start"),
            (3, "init_nav_test"),
        ):
            rail_layout.addWidget(self._build_step_nav_item(number, key))
        rail_layout.addStretch()
        content_layout.addWidget(self.stepRail)

        right_panel = QWidget(self.bodyBg)
        right_panel.setObjectName("initStepContent")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(9)

        self.stepTitleLabel = QLabel(right_panel)
        self.stepTitleLabel.setObjectName("initStepTitle")
        self.stepTitleLabel.setWordWrap(True)
        right_layout.addWidget(self.stepTitleLabel)

        self.stepDescLabel = QLabel(right_panel)
        self.stepDescLabel.setObjectName("initStepDesc")
        self.stepDescLabel.setWordWrap(True)
        right_layout.addWidget(self.stepDescLabel)

        self.stack = QStackedWidget(right_panel)
        self.stack.setObjectName("initStepStack")
        self.stack.addWidget(self._build_runtime_page())
        self.stack.addWidget(self._build_start_page())
        self.stack.addWidget(self._build_test_page())
        right_layout.addWidget(self.stack, stretch=1)
        content_layout.addWidget(right_panel, stretch=1)
        body_layout.addLayout(content_layout, stretch=1)

        self.statusLabel = QLabel("")
        self.statusLabel.setObjectName("initStatus")
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setFixedHeight(34)
        self.statusLabel.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        body_layout.addWidget(self.statusLabel)

        nav_layout = QHBoxLayout()
        nav_layout.addStretch()
        self.backBtn = QPushButton(tr("prev"))
        self.backBtn.setObjectName("cancelBtn")
        self.backBtn.setCursor(Qt.PointingHandCursor)
        self.backBtn.setFocusPolicy(Qt.NoFocus)
        self.backBtn.setFixedSize(84, 30)
        self.backBtn.clicked.connect(self._go_back)
        nav_layout.addWidget(self.backBtn)

        self.cancelBtn = QPushButton(tr("cancel"))
        self.cancelBtn.setObjectName("cancelBtn")
        self.cancelBtn.setCursor(Qt.PointingHandCursor)
        self.cancelBtn.setFocusPolicy(Qt.NoFocus)
        self.cancelBtn.setFixedSize(84, 30)
        self.cancelBtn.clicked.connect(self.reject)
        nav_layout.addWidget(self.cancelBtn)

        self.nextBtn = QPushButton(tr("next"))
        self.nextBtn.setObjectName("tokenBtn")
        self.nextBtn.setCursor(Qt.PointingHandCursor)
        self.nextBtn.setFocusPolicy(Qt.NoFocus)
        self.nextBtn.setFixedSize(92, 30)
        self.nextBtn.clicked.connect(self._go_next)
        nav_layout.addWidget(self.nextBtn)

        self.finishBtn = QPushButton(tr("finish"))
        self.finishBtn.setObjectName("saveBtn")
        self.finishBtn.setCursor(Qt.PointingHandCursor)
        self.finishBtn.setFocusPolicy(Qt.NoFocus)
        self.finishBtn.setFixedSize(120, 30)
        self.finishBtn.clicked.connect(self._finish_setup)
        nav_layout.addWidget(self.finishBtn)
        body_layout.addLayout(nav_layout)

        self.test_result_signal.connect(self._on_test_result)
        card_layout.addWidget(self.bodyBg)
        main_layout.addWidget(self.card)

        self._center_on_parent()
        self._set_step(0)
        self._force_layout()
        QTimer.singleShot(0, self._force_layout)

    def _build_step_nav_item(self, number, text_key):
        row = QFrame(self.stepRail)
        row.setObjectName("initStepNavItem")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 4, 0, 4)
        row_layout.setSpacing(8)

        badge = QLabel(str(number), row)
        badge.setObjectName("initStepBadge")
        badge.setFixedSize(22, 22)
        badge.setAlignment(Qt.AlignCenter)
        row_layout.addWidget(badge)

        label = QLabel(tr(text_key), row)
        label.setObjectName("initStepNavLabel")
        label.setWordWrap(True)
        row_layout.addWidget(label, stretch=1)

        self.stepNavItems.append((row, badge, label))
        return row

    def _build_runtime_page(self):
        page = QWidget(self.bodyBg)
        page.setObjectName("initStepPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        panel = QFrame(page)
        panel.setObjectName("initPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(14, 14, 14, 14)
        panel_layout.setSpacing(10)

        token_layout = QHBoxLayout()
        token_layout.setSpacing(10)
        token_label = QLabel("API Token")
        token_label.setObjectName("formLabel")
        token_label.setFixedWidth(82)
        self.tokenInput = QLineEdit(self.config.get("api_token", ""))
        self.tokenInput.setObjectName("settingsInput")
        self.tokenInput.setFixedHeight(30)
        self.tokenInput.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.tokenInput.setPlaceholderText(tr("token_auto_placeholder"))
        token_layout.addWidget(token_label)
        token_layout.addWidget(self.tokenInput, stretch=1)

        self.generateBtn = QPushButton(tr("regenerate_token"))
        self.generateBtn.setObjectName("tokenBtn")
        self.generateBtn.setCursor(Qt.PointingHandCursor)
        self.generateBtn.setFocusPolicy(Qt.NoFocus)
        self.generateBtn.setFixedSize(104, 30)
        self.generateBtn.clicked.connect(self._generate_token)
        token_layout.addWidget(self.generateBtn)
        panel_layout.addLayout(token_layout)

        mode_layout = QHBoxLayout()
        mode_layout.setSpacing(10)
        mode_label = QLabel(tr("connection_mode"))
        mode_label.setObjectName("formLabel")
        mode_label.setFixedWidth(82)
        self.connectionModeCombo = QComboBox()
        self.connectionModeCombo.setObjectName("settingsInput")
        self.connectionModeCombo.setFixedHeight(30)
        self.connectionModeCombo.addItem(tr("connection_mode_overlay"), "overlay")
        self.connectionModeCombo.addItem(tr("connection_mode_websocket"), "websocket")
        self.connectionModeCombo.addItem(tr("connection_mode_auto"), "auto")
        mode_index = self.connectionModeCombo.findData(normalize_connection_mode(self.config))
        self.connectionModeCombo.setCurrentIndex(
            mode_index if mode_index >= 0 else self.connectionModeCombo.findData(CONNECTION_MODE_AUTO)
        )
        mode_layout.addWidget(mode_label)
        mode_layout.addWidget(self.connectionModeCombo, stretch=1)
        panel_layout.addLayout(mode_layout)

        mode_hint = QLabel(tr("init_connection_mode_desc"))
        mode_hint.setObjectName("initSubtle")
        mode_hint.setWordWrap(True)
        panel_layout.addWidget(mode_hint)

        token_hint = QLabel(tr("token_auto_hint"))
        token_hint.setObjectName("initSubtle")
        token_hint.setWordWrap(True)
        panel_layout.addWidget(token_hint)

        runtime_btn_layout = QHBoxLayout()
        runtime_btn_layout.addStretch()
        self.openRuntimeDirBtn = QPushButton(tr("open_runtime_dir"))
        self.openRuntimeDirBtn.setObjectName("tokenBtn")
        self.openRuntimeDirBtn.setCursor(Qt.PointingHandCursor)
        self.openRuntimeDirBtn.setFocusPolicy(Qt.NoFocus)
        self.openRuntimeDirBtn.setFixedSize(96, 32)
        self.openRuntimeDirBtn.setEnabled(self.runtime_generated)
        self.openRuntimeDirBtn.clicked.connect(self._open_runtime_dir)
        runtime_btn_layout.addWidget(self.openRuntimeDirBtn)

        self.runtimeBtn = QPushButton(tr("generate_runtime"))
        self.runtimeBtn.setObjectName("runtimePrimaryBtn")
        self.runtimeBtn.setCursor(Qt.PointingHandCursor)
        self.runtimeBtn.setFocusPolicy(Qt.NoFocus)
        self.runtimeBtn.setFixedSize(176, 32)
        self.runtimeBtn.clicked.connect(self._generate_overlay_launcher)
        runtime_btn_layout.addWidget(self.runtimeBtn)
        panel_layout.addLayout(runtime_btn_layout)
        layout.addWidget(panel)

        hint = QLabel(tr("runtime_next_hint"), page)
        hint.setObjectName("initSubtle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()
        return page

    def _build_start_page(self):
        page = QWidget(self.bodyBg)
        page.setObjectName("initStepPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        panel = QFrame(page)
        panel.setObjectName("initPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(14, 14, 14, 14)
        panel_layout.setSpacing(10)
        for key in ("start_step_windows", "start_step_linux", "start_step_note"):
            label = QLabel(tr(key), panel)
            label.setObjectName("initHint")
            label.setWordWrap(True)
            panel_layout.addWidget(label)

        open_layout = QHBoxLayout()
        open_layout.addStretch()
        self.openRuntimeDirBtn2 = QPushButton(tr("open_runtime_dir"))
        self.openRuntimeDirBtn2.setObjectName("tokenBtn")
        self.openRuntimeDirBtn2.setCursor(Qt.PointingHandCursor)
        self.openRuntimeDirBtn2.setFocusPolicy(Qt.NoFocus)
        self.openRuntimeDirBtn2.setFixedSize(110, 32)
        self.openRuntimeDirBtn2.setEnabled(self.runtime_generated)
        self.openRuntimeDirBtn2.clicked.connect(self._open_runtime_dir)
        open_layout.addWidget(self.openRuntimeDirBtn2)
        panel_layout.addLayout(open_layout)
        layout.addWidget(panel)
        layout.addStretch()
        return page

    def _build_test_page(self):
        page = QWidget(self.bodyBg)
        page.setObjectName("initStepPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        test_panel = QFrame(page)
        test_panel.setObjectName("initPanel")
        test_layout = QVBoxLayout(test_panel)
        test_layout.setContentsMargins(14, 14, 14, 14)
        test_layout.setSpacing(10)

        test_hint = QLabel(tr("optional_connection_desc"), test_panel)
        test_hint.setObjectName("initSubtle")
        test_hint.setWordWrap(True)
        test_layout.addWidget(test_hint)

        connection_layout = QGridLayout()
        connection_layout.setHorizontalSpacing(10)
        connection_layout.setVerticalSpacing(10)
        ip_label = QLabel(tr("ip_address"))
        ip_label.setObjectName("formLabel")
        ip_label.setFixedWidth(62)
        self.ipInput = QLineEdit(self.config.get("ip", "127.0.0.1"))
        self.ipInput.setObjectName("settingsInput")
        self.ipInput.setFixedHeight(30)
        self.ipInput.setMinimumWidth(260)
        connection_layout.addWidget(ip_label, 0, 0)
        connection_layout.addWidget(self.ipInput, 0, 1, 1, 3)

        port_label = QLabel(tr("service_port"))
        port_label.setObjectName("formLabel")
        port_label.setFixedWidth(64)
        self.portInput = QLineEdit(str(self.config.get("port", "22267")))
        self.portInput.setObjectName("settingsInput")
        self.portInput.setFixedSize(86, 30)
        connection_layout.addWidget(port_label, 1, 0)
        connection_layout.addWidget(self.portInput, 1, 1)

        self.testBtn = QPushButton(tr("test_connection_optional"))
        self.testBtn.setObjectName("testBtn")
        self.testBtn.setCursor(Qt.PointingHandCursor)
        self.testBtn.setFocusPolicy(Qt.NoFocus)
        self.testBtn.setFixedSize(132, 30)
        self.testBtn.clicked.connect(self._run_connection_test)
        connection_layout.addWidget(self.testBtn, 1, 3)
        connection_layout.setColumnStretch(2, 1)
        test_layout.addLayout(connection_layout)
        layout.addWidget(test_panel)

        layout.addStretch()
        return page

    def showEvent(self, event):
        super().showEvent(event)
        self._force_layout()
        QTimer.singleShot(0, self._force_layout)
        schedule_frameless_stabilize(self, self.card, self.topBg, self.bodyBg)

    def _force_layout(self):
        for widget in (self, self.card, self.bodyBg, self.stack):
            layout = widget.layout()
            if layout is not None:
                layout.invalidate()
                layout.activate()
            widget.updateGeometry()
            widget.update()

    def _set_step(self, index):
        self.current_step = max(0, min(index, 2))
        self.stack.setCurrentIndex(self.current_step)
        self.stepProgressLabel.setText(tr("wizard_step_progress", current=self.current_step + 1, total=3))
        titles = [
            tr("init_step_runtime_title"),
            tr("init_step_start_title"),
            tr("init_step_test_title"),
        ]
        descs = [
            tr("init_step_runtime_desc"),
            tr("init_step_start_desc"),
            tr("init_step_test_desc"),
        ]
        self.stepTitleLabel.setText(titles[self.current_step])
        self.stepDescLabel.setText(descs[self.current_step])
        self.backBtn.setEnabled(self.current_step > 0)
        self.nextBtn.setVisible(self.current_step < 2)
        self.finishBtn.setVisible(self.current_step == 2)
        for idx, (row, badge, label) in enumerate(getattr(self, "stepNavItems", [])):
            active = idx == self.current_step
            done = idx < self.current_step
            badge.setText("✓" if done else str(idx + 1))
            for widget in (row, badge, label):
                widget.setProperty("active", active)
                widget.setProperty("done", done)
                widget.style().unpolish(widget)
                widget.style().polish(widget)
        self._refresh_runtime_buttons()
        self._force_layout()

    def _go_back(self):
        self._set_step(self.current_step - 1)

    def _go_next(self):
        self._set_step(self.current_step + 1)

    def _refresh_runtime_buttons(self):
        enabled = bool(self.runtime_generated and os.path.isdir(self.runtime_output_dir))
        if hasattr(self, "openRuntimeDirBtn"):
            self.openRuntimeDirBtn.setEnabled(enabled)
        if hasattr(self, "openRuntimeDirBtn2"):
            self.openRuntimeDirBtn2.setEnabled(enabled)

    def _center_on_parent(self):
        if self.parent():
            parent_geom = self.parent().geometry()
            x = parent_geom.x() + (parent_geom.width() - self.width()) // 2
            y = parent_geom.y() + (parent_geom.height() - self.height()) // 2
            self.move(x, y)

    def _sync_config_from_ui(self):
        self.config["ip"] = self.ipInput.text().strip() or "127.0.0.1"
        self.config["port"] = self.portInput.text().strip() or "22267"
        self.config["api_token"] = self.tokenInput.text().strip()
        self.config["connection_mode"] = self.connectionModeCombo.currentData() or CONNECTION_MODE_AUTO

    def _set_status(self, text, state="normal", tooltip=None):
        self.statusLabel.setText(text)
        self.statusLabel.setToolTip(tooltip if tooltip is not None else text)
        self.statusLabel.setProperty("state", state)
        self.statusLabel.style().unpolish(self.statusLabel)
        self.statusLabel.style().polish(self.statusLabel)

    def _short_display_path(self, path, max_len=44):
        text = os.path.abspath(str(path or "")).replace("/", "\\")
        if len(text) <= max_len:
            return text
        stripped = text.rstrip("\\")
        name = os.path.basename(stripped)
        parent = os.path.basename(os.path.dirname(stripped))
        short = f"...\\{parent}\\{name}" if parent else f"...\\{name}"
        if len(short) <= max_len:
            return short
        return "..." + text[-(max_len - 3):]

    def _open_runtime_dir(self):
        if not self.runtime_output_dir or not os.path.isdir(self.runtime_output_dir):
            self._set_status(tr("open_runtime_dir_failed"), "error")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.runtime_output_dir))

    def _ensure_token(self):
        self._sync_config_from_ui()
        token = ensure_api_token(self.config, self.config_path)
        self.tokenInput.setText(token)
        return token

    def _generate_token(self):
        self._sync_config_from_ui()
        token = secrets.token_urlsafe(32)
        self.config["api_token"] = token
        self.tokenInput.setText(token)
        try:
            save_config(self.config, self.config_path)
            self._set_status(tr("token_generated"), "success")
        except Exception as exc:
            self._set_status(tr("action_failed", error=str(exc)), "error")
        self.tokenInput.setFocus()

    def _generate_overlay_launcher(self):
        self.runtimeBtn.setEnabled(False)
        self.runtimeBtn.setText(tr("generating_runtime"))
        self._refresh_runtime_buttons()
        token = self._ensure_token()
        try:
            save_config(self.config, self.config_path)
        except Exception as exc:
            self._set_status(tr("action_failed", error=str(exc)), "error")
            self.runtimeBtn.setEnabled(True)
            self.runtimeBtn.setText(tr("generate_runtime"))
            self._refresh_runtime_buttons()
            return

        try:
            result = generate_portable_overlay_launchers(api_token=token)
            output_dir = result["output_dir"]
            self.runtime_output_dir = output_dir
            self.runtime_generated = True
            self._refresh_runtime_buttons()
            display_dir = self._short_display_path(output_dir)
            self._set_status(
                tr("overlay_launcher_success", dir=display_dir),
                "success",
                tooltip=output_dir,
            )
        except Exception as exc:
            self._set_status(tr("overlay_launcher_failed", error=str(exc)), "error")
            self._refresh_runtime_buttons()
        finally:
            self.runtimeBtn.setEnabled(True)
            self.runtimeBtn.setText(tr("generate_runtime"))

    def _finish_setup(self):
        self._sync_config_from_ui()
        if self.config.get("connection_mode") != "websocket" and not self.runtime_generated:
            if not ask_confirm(
                self,
                tr("runtime_missing_confirm_title"),
                tr("runtime_missing_confirm_desc"),
                tr("finish_anyway"),
                tr("cancel"),
            ):
                return
        self._ensure_token()
        self.config["setup_completed"] = True
        try:
            save_config(self.config, self.config_path)
            self.accept()
        except Exception as exc:
            self._set_status(tr("action_failed", error=str(exc)), "error")

    def _run_connection_test(self):
        self._ensure_token()
        if not self.config["ip"] or not self.config["port"].isdigit():
            self._on_test_result(False, tr("test_invalid"))
            return

        connection_mode = self.config.get("connection_mode", CONNECTION_MODE_AUTO)
        self.testBtn.setText("...")
        self.testBtn.setIcon(QIcon())
        self.testBtn.setEnabled(False)
        self.testBtn.setProperty("state", "testing")
        self.testBtn.style().unpolish(self.testBtn)
        self.testBtn.style().polish(self.testBtn)
        threading.Thread(target=self._test_api, args=(connection_mode,), daemon=True).start()

    def _test_api(self, connection_mode):
        success = False
        message = ""
        try:
            test_config = dict(self.config)
            test_config["connection_mode"] = connection_mode
            result = test_connection(test_config, timeout=3.0)
            success = result.ok
            if success and result.mode == "websocket":
                count = len(result.data.get("configs", []))
                message = tr("websocket_test_success", count=count)
                if result.degraded:
                    message = tr("websocket_degraded_notice") + "\n" + message
            elif success:
                message = tr("test_success")
            else:
                message = tr("test_failed")
        except WebUIUnavailableError:
            message = tr("websocket_webui_unavailable")
        except NotPyWebIOError:
            message = tr("websocket_not_pywebio")
        except WebSocketHandshakeError:
            message = tr("websocket_handshake_failed")
        except ConfigDetectionError:
            message = tr("websocket_config_detection_failed")
        except NavigationError:
            message = tr("websocket_navigation_failed")
        except Exception as exc:
            message = str(exc)
        self.test_result_signal.emit(success, message)

    def _create_icon(self, state):
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        if state == "success":
            pen = QPen(QColor("#42d392"), 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(5, 12, 10, 17)
            painter.drawLine(10, 17, 19, 7)
        else:
            pen = QPen(QColor("#ff5c5c"), 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(7, 7, 17, 17)
            painter.drawLine(17, 7, 7, 17)
        painter.end()
        return QIcon(pixmap)

    def _on_test_result(self, success, message=""):
        self.testBtn.setEnabled(True)
        self.testBtn.setText("")
        self.testBtn.setToolTip(message)
        self.testBtn.setIconSize(QSize(20, 20))
        self.testBtn.setIcon(self._create_icon("success" if success else "error"))
        self.testBtn.setProperty("state", "success" if success else "error")
        self.testBtn.style().unpolish(self.testBtn)
        self.testBtn.style().polish(self.testBtn)
        self._set_status(tr("test_success") if success else tr("test_failed_short"), "success" if success else "error")
        if message and not success:
            print(f"[InitSetup] Connection test failed: {message}")
        QTimer.singleShot(2000, self._reset_test_btn)

    def _reset_test_btn(self):
        self.testBtn.setIcon(QIcon())
        self.testBtn.setText(tr("test_connection_optional"))
        self.testBtn.setToolTip("")
        self.testBtn.setProperty("state", "normal")
        self.testBtn.style().unpolish(self.testBtn)
        self.testBtn.style().polish(self.testBtn)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
