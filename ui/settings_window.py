from PySide6.QtWidgets import (
    QComboBox, QDialog, QVBoxLayout, QHBoxLayout, QWidget, QLabel, QLineEdit, QCheckBox,
    QPushButton, QFrame, QSlider, QGridLayout, QStackedWidget
)
from PySide6.QtCore import Qt, Signal, QTimer, QSize
from PySide6.QtGui import QColor, QPixmap, QPainter, QPen, QIcon
import secrets
import threading

from alas_gyre.api.connection_mode import CONNECTION_MODE_AUTO, normalize_connection_mode
from alas_gyre.api.control_gateway import test_connection
from alas_gyre.api.runtime_update import DEFAULT_RUNTIME_UPDATE_PORT, update_remote_runtime
from alas_gyre.api.websocket_hijack import (
    ConfigDetectionError,
    NavigationError,
    NotPyWebIOError,
    WebSocketHandshakeError,
    WebUIUnavailableError,
)
from alas_gyre.services.updater import check_for_updates, do_update
from alas_gyre.core.version import get_current_version
from alas_gyre.core.paths import config_path
from .widgets import WindowButton
from .i18n import get_language, tr
from .window_behavior import install_title_bar_drag, schedule_frameless_stabilize

try:
    from shiboken6 import isValid
except Exception:
    def isValid(widget):
        return widget is not None

class CheckBox(QCheckBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.NoFocus)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.isChecked():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#d9fff0"), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        y = (self.height() - 14) // 2
        painter.drawLine(4, y + 8, 7, y + 11)
        painter.drawLine(7, y + 11, 13, y + 4)
        painter.end()

class SettingsWindow(QDialog):
    test_result_signal = Signal(bool, str)
    update_result_signal = Signal(dict, int)
    update_progress_signal = Signal(int)
    update_finish_signal = Signal(bool, str)
    runtime_update_result_signal = Signal(bool, str)

    def __init__(self, parent=None, config=None, configs=None, current_config="alas"):
        super().__init__(parent)
        self.config = config if config is not None else {}
        self.setObjectName("settingsWindow")
        self.setFixedSize(720, 520)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # 背景容器卡片
        self.card = QFrame(self)
        self.card.setObjectName("settingsCard")
        self.card.setAttribute(Qt.WA_StyledBackground, True)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        # ====== 顶栏 ======
        self.topBg = QWidget(self.card)
        self.topBg.setObjectName("settingsTopBg")
        self.topBg.setAttribute(Qt.WA_StyledBackground, True)
        self.topBg.setFixedHeight(30)
        install_title_bar_drag(self, self.topBg)
        top_layout = QHBoxLayout(self.topBg)
        top_layout.setContentsMargins(20, 0, 8, 0)

        title = QLabel(tr("settings_title"))
        title.setObjectName("settingsTitle")
        top_layout.addWidget(title)
        top_layout.addStretch()

        self.closeBtn = WindowButton("close", self.topBg)
        self.closeBtn.mousePressEvent = lambda event: self.reject() if event.button() == Qt.LeftButton else None
        top_layout.addWidget(self.closeBtn)

        card_layout.addWidget(self.topBg)

        # ====== 表单区域 ======
        self.formBg = QWidget(self.card)
        self.formBg.setObjectName("settingsFormBg")
        self.formBg.setAttribute(Qt.WA_StyledBackground, True)
        form_layout = QVBoxLayout(self.formBg)
        form_layout.setContentsMargins(24, 16, 24, 16)
        form_layout.setSpacing(10)

        self.autoStartCheck = CheckBox(tr("auto_start"))
        self.autoStartCheck.setCursor(Qt.PointingHandCursor)
        self.autoStartCheck.setChecked(self.config.get("auto_start", False))

        self.alwaysOnTopCheck = CheckBox(tr("always_on_top"))
        self.alwaysOnTopCheck.setCursor(Qt.PointingHandCursor)
        self.alwaysOnTopCheck.setChecked(self.config.get("always_on_top", False))

        self.miniClickThroughCheck = CheckBox(tr("click_through"))
        self.miniClickThroughCheck.setCursor(Qt.PointingHandCursor)
        self.miniClickThroughCheck.setChecked(self.config.get("mini_click_through", False))

        self.showTaskNameCheck = CheckBox(tr("show_task_name"))
        self.showTaskNameCheck.setCursor(Qt.PointingHandCursor)
        self.showTaskNameCheck.setChecked(self.config.get("show_task_name", False))

        self.lightThemeCheck = CheckBox(tr("light_mode"))
        self.lightThemeCheck.setCursor(Qt.PointingHandCursor)
        self.lightThemeCheck.setChecked(self.config.get("theme", "dark") == "light")

        self.englishLangCheck = CheckBox(tr("english_mode"))
        self.englishLangCheck.setCursor(Qt.PointingHandCursor)
        self.englishLangCheck.setChecked(self.config.get("lang", get_language()) == "en")

        preference_grid = QGridLayout()
        preference_grid.setContentsMargins(0, 0, 0, 0)
        preference_grid.setHorizontalSpacing(22)
        preference_grid.setVerticalSpacing(8)
        preference_grid.addWidget(self.autoStartCheck, 0, 0)
        preference_grid.addWidget(self.alwaysOnTopCheck, 0, 1)
        preference_grid.addWidget(self.miniClickThroughCheck, 1, 0)
        preference_grid.addWidget(self.lightThemeCheck, 1, 1)
        preference_grid.addWidget(self.showTaskNameCheck, 2, 0)
        preference_grid.addWidget(self.englishLangCheck, 2, 1)

        opacity_layout = QHBoxLayout()
        opacity_layout.setSpacing(10)
        opacity_label = QLabel(tr("float_opacity"))
        opacity_label.setObjectName("formLabel")
        opacity_label.setFixedWidth(104)
        self.miniOpacitySlider = QSlider(Qt.Horizontal)
        self.miniOpacitySlider.setObjectName("settingsSlider")
        self.miniOpacitySlider.setRange(35, 100)
        self.miniOpacitySlider.setSingleStep(5)
        self.miniOpacitySlider.setPageStep(10)
        self.miniOpacitySlider.setValue(self._normalize_opacity(self.config.get("mini_opacity", 100)))
        self.miniOpacityValue = QLabel(f"{self.miniOpacitySlider.value()}%")
        self.miniOpacityValue.setObjectName("sliderValueLabel")
        self.miniOpacityValue.setFixedWidth(42)
        self.miniOpacityValue.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.miniOpacitySlider.valueChanged.connect(
            lambda value: self.miniOpacityValue.setText(f"{value}%")
        )
        opacity_layout.addWidget(opacity_label)
        opacity_layout.addWidget(self.miniOpacitySlider, stretch=1)
        opacity_layout.addWidget(self.miniOpacityValue)

        mode_layout = QHBoxLayout()
        mode_layout.setSpacing(10)
        mode_label = QLabel(tr("connection_mode"))
        mode_label.setObjectName("formLabel")
        mode_label.setFixedWidth(104)
        self.connectionModeCombo = QComboBox()
        self.connectionModeCombo.setObjectName("settingsInput")
        self.connectionModeCombo.setFixedHeight(30)
        self.connectionModeCombo.addItem(tr("connection_mode_overlay"), "overlay")
        self.connectionModeCombo.addItem(tr("connection_mode_websocket"), "websocket")
        self.connectionModeCombo.addItem(tr("connection_mode_auto"), "auto")
        current_mode = normalize_connection_mode(self.config)
        index = self.connectionModeCombo.findData(current_mode)
        self.connectionModeCombo.setCurrentIndex(index if index >= 0 else self.connectionModeCombo.findData(CONNECTION_MODE_AUTO))
        mode_hint = QLabel(tr("connection_mode_hint"))
        mode_hint.setStyleSheet("color: #8f96a3; font-size: 12px; font-family: 'Microsoft YaHei', 'Segoe UI';")
        mode_hint.setWordWrap(True)
        mode_layout.addWidget(mode_label)
        mode_layout.addWidget(self.connectionModeCombo)
        mode_layout.addWidget(mode_hint, stretch=1)

        # IP 布局
        ip_layout = QHBoxLayout()
        ip_layout.setSpacing(10)
        ip_label = QLabel(tr("ip_address"))
        ip_label.setObjectName("formLabel")
        ip_label.setFixedWidth(104)
        self.ipInput = QLineEdit()
        self.ipInput.setObjectName("settingsInput")
        self.ipInput.setFixedHeight(30)
        self.ipInput.setText(self.config.get("ip", "127.0.0.1"))
        ip_layout.addWidget(ip_label)
        ip_layout.addWidget(self.ipInput, stretch=1)

        # 端口与测试按钮布局
        port_layout = QHBoxLayout()
        port_layout.setSpacing(10)
        
        port_label = QLabel(tr("service_port"))
        port_label.setObjectName("formLabel")
        port_label.setFixedWidth(104)
        self.portInput = QLineEdit()
        self.portInput.setObjectName("settingsInput")
        self.portInput.setFixedSize(96, 30)
        self.portInput.setText(str(self.config.get("port", "22267")))
        port_layout.addWidget(port_label)
        port_layout.addWidget(self.portInput)
        port_layout.addStretch()

        self.testBtn = QPushButton(tr("test_connection"))
        self.testBtn.setObjectName("testBtn")
        self.testBtn.setCursor(Qt.PointingHandCursor)
        self.testBtn.setFocusPolicy(Qt.NoFocus)
        self.testBtn.setFixedSize(116, 30)
        self.testBtn.clicked.connect(self._run_connection_test)
        port_layout.addWidget(self.testBtn)

        runtime_port_layout = QHBoxLayout()
        runtime_port_layout.setSpacing(10)

        runtime_port_label = QLabel(tr("runtime_update_port"))
        runtime_port_label.setObjectName("formLabel")
        runtime_port_label.setFixedWidth(104)
        self.runtimePortInput = QLineEdit()
        self.runtimePortInput.setObjectName("settingsInput")
        self.runtimePortInput.setFixedSize(96, 30)
        self.runtimePortInput.setText(str(self.config.get("runtime_update_port", DEFAULT_RUNTIME_UPDATE_PORT)))
        runtime_port_hint = QLabel(tr("runtime_update_port_hint"))
        runtime_port_hint.setStyleSheet("color: #8f96a3; font-size: 12px; font-family: 'Microsoft YaHei', 'Segoe UI';")
        runtime_port_layout.addWidget(runtime_port_label)
        runtime_port_layout.addWidget(self.runtimePortInput)
        runtime_port_layout.addWidget(runtime_port_hint, stretch=1)

        token_layout = QHBoxLayout()
        token_layout.setSpacing(10)
        token_label = QLabel("API Token")
        token_label.setObjectName("formLabel")
        token_label.setFixedWidth(104)
        self.tokenInput = QLineEdit()
        self.tokenInput.setObjectName("settingsInput")
        self.tokenInput.setFixedHeight(30)
        self.tokenInput.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.tokenInput.setText(self.config.get("api_token", ""))
        token_layout.addWidget(token_label)
        token_layout.addWidget(self.tokenInput, stretch=1)

        self.tokenGenerateBtn = QPushButton(tr("generate"))
        self.tokenGenerateBtn.setObjectName("tokenBtn")
        self.tokenGenerateBtn.setCursor(Qt.PointingHandCursor)
        self.tokenGenerateBtn.setFocusPolicy(Qt.NoFocus)
        self.tokenGenerateBtn.setFixedSize(82, 30)
        self.tokenGenerateBtn.clicked.connect(self._generate_token)
        token_layout.addWidget(self.tokenGenerateBtn)

        wizard_layout = QHBoxLayout()
        wizard_layout.setSpacing(10)

        wizard_label = QLabel(tr("wizard"))
        wizard_label.setObjectName("formLabel")
        wizard_label.setFixedWidth(104)

        self.wizardBtn = QPushButton(tr("open_wizard"))
        self.wizardBtn.setObjectName("updateBtn")
        self.wizardBtn.setCursor(Qt.PointingHandCursor)
        self.wizardBtn.setFocusPolicy(Qt.NoFocus)
        self.wizardBtn.setFixedSize(116, 30)
        self.wizardBtn.setStyleSheet("""
            QPushButton#updateBtn {
                background-color: transparent;
                border: 1px solid #454852;
                border-radius: 4px;
                color: #a6abb4;
            }
            QPushButton#updateBtn:hover {
                background-color: #454852;
                color: #ffffff;
            }
        """)
        self.wizardBtn.clicked.connect(self._open_init_setup)

        wizard_layout.addWidget(wizard_label)
        wizard_layout.addStretch()
        wizard_layout.addWidget(self.wizardBtn)

        # 版本更新布局
        update_layout = QHBoxLayout()
        update_layout.setSpacing(10)
        
        update_label = QLabel(tr("version_update"))
        update_label.setObjectName("formLabel")
        update_label.setFixedWidth(104)
        
        self.versionLabel = QLabel(f"{tr('current_version')} {get_current_version()}")
        self.versionLabel.setStyleSheet("color: #a6abb4; font-size: 13px; font-family: 'Microsoft YaHei', 'Segoe UI';")
        
        self.updateBtn = QPushButton(tr("check_update"))
        self.updateBtn.setObjectName("updateBtn")
        self.updateBtn.setCursor(Qt.PointingHandCursor)
        self.updateBtn.setFocusPolicy(Qt.NoFocus)
        self.updateBtn.setFixedSize(116, 30)
        self.updateBtn.setStyleSheet("""
            QPushButton#updateBtn {
                background-color: transparent;
                border: 1px solid #454852;
                border-radius: 4px;
                color: #a6abb4;
            }
            QPushButton#updateBtn:hover {
                background-color: #454852;
                color: #ffffff;
            }
        """)
        self.updateBtn.clicked.connect(self._check_for_updates)
        
        update_layout.addWidget(update_label)
        update_layout.addWidget(self.versionLabel)
        update_layout.addStretch()
        update_layout.addWidget(self.updateBtn)

        runtime_update_layout = QHBoxLayout()
        runtime_update_layout.setSpacing(10)

        runtime_update_label = QLabel(tr("runtime_update"))
        runtime_update_label.setObjectName("formLabel")
        runtime_update_label.setFixedWidth(104)

        self.runtimeUpdateHint = QLabel(tr("runtime_update_hint"))
        self.runtimeUpdateHint.setStyleSheet("color: #8f96a3; font-size: 12px; font-family: 'Microsoft YaHei', 'Segoe UI';")
        self.runtimeUpdateHint.setWordWrap(True)

        self.runtimeUpdateBtn = QPushButton(tr("update_runtime"))
        self.runtimeUpdateBtn.setObjectName("updateBtn")
        self.runtimeUpdateBtn.setCursor(Qt.PointingHandCursor)
        self.runtimeUpdateBtn.setFocusPolicy(Qt.NoFocus)
        self.runtimeUpdateBtn.setFixedSize(116, 30)
        self.runtimeUpdateBtn.setStyleSheet(self.updateBtn.styleSheet())
        self.runtimeUpdateBtn.clicked.connect(self._update_runtime)

        runtime_update_layout.addWidget(runtime_update_label)
        runtime_update_layout.addWidget(self.runtimeUpdateHint, stretch=1)
        runtime_update_layout.addWidget(self.runtimeUpdateBtn)

        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(16)

        sidebar = QFrame(self.formBg)
        sidebar.setObjectName("settingsSidebar")
        sidebar.setFixedWidth(146)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(10, 10, 10, 10)
        sidebar_layout.setSpacing(8)

        self.settingsNavButtons = []
        for index, (key, tip_key) in enumerate((
            ("settings_nav_behavior", "settings_behavior_desc"),
            ("settings_nav_connection", "settings_connection_desc"),
            ("settings_nav_maintenance", "settings_maintenance_desc"),
        )):
            button = QPushButton(tr(key), sidebar)
            button.setObjectName("settingsNavButton")
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.NoFocus)
            button.setFixedHeight(34)
            button.setToolTip(tr(tip_key))
            button.clicked.connect(lambda checked=False, page=index: self._set_settings_page(page))
            sidebar_layout.addWidget(button)
            self.settingsNavButtons.append(button)
        sidebar_layout.addStretch()
        content_layout.addWidget(sidebar)

        self.settingsStack = QStackedWidget(self.formBg)
        self.settingsStack.setObjectName("settingsStack")

        behavior_page = QWidget(self.settingsStack)
        behavior_page.setObjectName("settingsPage")
        behavior_layout = QVBoxLayout(behavior_page)
        behavior_layout.setContentsMargins(0, 0, 0, 0)
        behavior_layout.setSpacing(10)
        behavior_layout.addWidget(self._page_title(tr("settings_section_behavior"), tr("settings_behavior_desc")))
        behavior_panel = QFrame(behavior_page)
        behavior_panel.setObjectName("settingsPanel")
        behavior_panel_layout = QVBoxLayout(behavior_panel)
        behavior_panel_layout.setContentsMargins(14, 14, 14, 14)
        behavior_panel_layout.setSpacing(12)
        behavior_panel_layout.addLayout(preference_grid)
        behavior_panel_layout.addLayout(opacity_layout)
        behavior_layout.addWidget(behavior_panel)
        behavior_layout.addStretch()
        self.settingsStack.addWidget(behavior_page)

        connection_page = QWidget(self.settingsStack)
        connection_page.setObjectName("settingsPage")
        connection_layout = QVBoxLayout(connection_page)
        connection_layout.setContentsMargins(0, 0, 0, 0)
        connection_layout.setSpacing(10)
        connection_layout.addWidget(self._page_title(tr("settings_section_connection"), tr("settings_connection_desc")))
        connection_panel = QFrame(connection_page)
        connection_panel.setObjectName("settingsPanel")
        connection_panel_layout = QVBoxLayout(connection_panel)
        connection_panel_layout.setContentsMargins(14, 14, 14, 14)
        connection_panel_layout.setSpacing(12)
        connection_panel_layout.addLayout(mode_layout)
        connection_panel_layout.addLayout(ip_layout)
        connection_panel_layout.addLayout(port_layout)
        connection_panel_layout.addLayout(runtime_port_layout)
        connection_panel_layout.addLayout(token_layout)
        connection_layout.addWidget(connection_panel)
        connection_layout.addStretch()
        self.settingsStack.addWidget(connection_page)

        maintenance_page = QWidget(self.settingsStack)
        maintenance_page.setObjectName("settingsPage")
        maintenance_layout = QVBoxLayout(maintenance_page)
        maintenance_layout.setContentsMargins(0, 0, 0, 0)
        maintenance_layout.setSpacing(10)
        maintenance_layout.addWidget(self._page_title(tr("settings_section_maintenance"), tr("settings_maintenance_desc")))
        maintenance_panel = QFrame(maintenance_page)
        maintenance_panel.setObjectName("settingsPanel")
        maintenance_panel_layout = QVBoxLayout(maintenance_panel)
        maintenance_panel_layout.setContentsMargins(14, 12, 14, 12)
        maintenance_panel_layout.setSpacing(10)
        maintenance_panel_layout.addLayout(wizard_layout)
        maintenance_panel_layout.addLayout(update_layout)
        maintenance_panel_layout.addLayout(runtime_update_layout)
        maintenance_layout.addWidget(maintenance_panel)
        maintenance_layout.addStretch()
        self.settingsStack.addWidget(maintenance_page)

        content_layout.addWidget(self.settingsStack, stretch=1)
        form_layout.addLayout(content_layout, stretch=1)
        self._set_settings_page(0)

        # 绑定测试结果信号
        self.test_result_signal.connect(self._on_test_result)
        self.update_result_signal.connect(self._handle_update_check_result)
        self.update_progress_signal.connect(self._on_download_progress)
        self.update_finish_signal.connect(self._on_update_finish)
        self.runtime_update_result_signal.connect(self._on_runtime_updated)

        # 底部按钮
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 8, 0, 0)
        btn_layout.addStretch()

        self.cancelBtn = QPushButton(tr("cancel"))
        self.cancelBtn.setObjectName("cancelBtn")
        self.cancelBtn.setCursor(Qt.PointingHandCursor)
        self.cancelBtn.setFocusPolicy(Qt.NoFocus)
        self.cancelBtn.setFixedSize(76, 30)
        self.cancelBtn.clicked.connect(self.reject)

        self.saveBtn = QPushButton(tr("save"))
        self.saveBtn.setObjectName("saveBtn")
        self.saveBtn.setCursor(Qt.PointingHandCursor)
        self.saveBtn.setFocusPolicy(Qt.NoFocus)
        self.saveBtn.setFixedSize(76, 30)
        self.saveBtn.clicked.connect(self.accept)

        btn_layout.addWidget(self.cancelBtn)
        btn_layout.addWidget(self.saveBtn)

        form_layout.addLayout(btn_layout)

        card_layout.addWidget(self.formBg)
        main_layout.addWidget(self.card)

        # 阴影效果

        self._center_on_screen()
        self._force_layout()
        QTimer.singleShot(0, self._force_layout)
        QTimer.singleShot(100, self._check_for_updates)

    def showEvent(self, event):
        super().showEvent(event)
        self._force_layout()
        QTimer.singleShot(0, self._force_layout)
        schedule_frameless_stabilize(self, self.card, self.topBg, self.formBg)

    def _force_layout(self):
        widgets = [self, self.card, self.topBg, self.formBg]
        if hasattr(self, "settingsStack"):
            widgets.append(self.settingsStack)
        for widget in widgets:
            layout = widget.layout()
            if layout is not None:
                layout.invalidate()
                layout.activate()
            widget.updateGeometry()
            widget.update()

    def _page_title(self, title, desc):
        box = QWidget()
        box.setObjectName("settingsPageHeader")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        title_label = QLabel(title)
        title_label.setObjectName("settingsPageTitle")
        desc_label = QLabel(desc)
        desc_label.setObjectName("settingsPageDesc")
        desc_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(desc_label)
        return box

    def _set_settings_page(self, index):
        if not hasattr(self, "settingsStack"):
            return
        self.settingsStack.setCurrentIndex(index)
        for i, button in enumerate(getattr(self, "settingsNavButtons", [])):
            button.setProperty("active", i == index)
            button.style().unpolish(button)
            button.style().polish(button)

    def _section_label(self, text):
        label = QLabel(text)
        label.setObjectName("settingsSectionLabel")
        label.setFixedHeight(18)
        return label

    def _center_on_screen(self):
        if self.parent():
            parent_geom = self.parent().geometry()
            x = parent_geom.x() + (parent_geom.width() - self.width()) // 2
            y = parent_geom.y() + (parent_geom.height() - self.height()) // 2
            self.move(x, y)

    def _check_for_updates(self):
        if not isValid(self):
            return
        self.updateBtn.setEnabled(False)
        self.updateBtn.setText(tr("checking"))
        self.updateBtn.setToolTip("")
        self._checking_active = True
        self._update_check_id = getattr(self, "_update_check_id", 0) + 1
        check_id = self._update_check_id
        QTimer.singleShot(60000, lambda: self._on_update_timeout(check_id))
        threading.Thread(target=self._update_task, args=(check_id,), daemon=True).start()

    def _update_task(self, check_id):
        try:
            result = check_for_updates(get_current_version())
        except Exception as exc:
            result = {"has_update": False, "error": str(exc)}
        try:
            if isValid(self):
                self.update_result_signal.emit(result, check_id)
        except RuntimeError:
            pass

    def _handle_update_check_result(self, result, check_id):
        if not isValid(self):
            return
        if not getattr(self, "_checking_active", False):
            return
        if check_id != getattr(self, "_update_check_id", None):
            return
        self._checking_active = False
        if result.get("has_update"):
            self.updateBtn.setText(tr("download_update"))
            if result.get("version"):
                self.updateBtn.setToolTip(result["version"])
            self.updateBtn.setStyleSheet("""
                QPushButton#updateBtn {
                    background-color: #28e06f;
                    border: none;
                    border-radius: 4px;
                    color: #1a1b26;
                    font-weight: bold;
                }
                QPushButton#updateBtn:hover {
                    background-color: #42d392;
                }
            """)
            self.updateBtn.setEnabled(True)
            try:
                self.updateBtn.clicked.disconnect()
            except Exception:
                pass
            
            self.updateBtn.clicked.connect(
                lambda: self._start_download(
                    result.get("url", ""),
                    result.get("sha256_url", ""),
                    result.get("asset_name", ""),
                )
            )
        elif "error" in result:
            self.updateBtn.setText(tr("check_failed"))
            self.updateBtn.setToolTip(result.get("error", ""))
            self.updateBtn.setEnabled(True)
            QTimer.singleShot(3000, self._reset_update_btn)
        else:
            self.updateBtn.setText(tr("new_version"))
            self.updateBtn.setToolTip(result.get("version", ""))
            QTimer.singleShot(3000, self._reset_update_btn)

    def _start_download(self, download_url, sha256_url="", asset_name=""):
        if not isValid(self):
            return
        self.updateBtn.setEnabled(False)
        self.updateBtn.setText("0%")
        threading.Thread(
            target=do_update,
            args=(download_url, self._emit_update_progress, self._emit_update_finish),
            kwargs={"sha256_url": sha256_url, "asset_name": asset_name},
            daemon=True,
        ).start()

    def _emit_update_progress(self, percentage):
        try:
            if isValid(self):
                self.update_progress_signal.emit(percentage)
        except RuntimeError:
            pass

    def _emit_update_finish(self, success, message):
        try:
            if isValid(self):
                self.update_finish_signal.emit(success, message)
        except RuntimeError:
            pass

    def _on_download_progress(self, percentage):
        if not isValid(self):
            return
        self.updateBtn.setText(f"{percentage}%")

    def _on_update_finish(self, success, message):
        if not isValid(self):
            return
        self.updateBtn.setText(tr("restart") if success else tr("check_failed"))
        self.updateBtn.setToolTip(message or "")
        if not success:
            self.updateBtn.setEnabled(True)
            QTimer.singleShot(3000, self._reset_update_btn)

    def _on_update_timeout(self, check_id=None):
        if not isValid(self):
            return
        if getattr(self, "_checking_active", False):
            if check_id is not None and check_id != getattr(self, "_update_check_id", None):
                return
            self._checking_active = False
            self.updateBtn.setText(tr("timeout"))
            self.updateBtn.setToolTip("")
            self.updateBtn.setEnabled(True)
            QTimer.singleShot(2000, self._reset_update_btn)

    def _reset_update_btn(self):
        if not isValid(self):
            return
        self.updateBtn.setText(tr("check_update"))
        self.updateBtn.setToolTip("")
        self.updateBtn.setEnabled(True)
        self.updateBtn.setStyleSheet("""
            QPushButton#updateBtn {
                background-color: transparent;
                border: 1px solid #454852;
                border-radius: 4px;
                color: #a6abb4;
            }
            QPushButton#updateBtn:hover {
                background-color: #454852;
                color: #ffffff;
            }
        """)
        try:
            self.updateBtn.clicked.disconnect()
        except Exception:
            pass
        self.updateBtn.clicked.connect(self._check_for_updates)

    def _update_runtime(self):
        if not isValid(self):
            return
        self._sync_config_from_ui()
        self.runtimeUpdateBtn.setEnabled(False)
        self.runtimeUpdateBtn.setText(tr("updating"))
        threading.Thread(target=self._update_runtime_task, daemon=True).start()

    def _update_runtime_task(self):
        success = False
        message = ""
        try:
            result = update_remote_runtime(self.config, get_current_version())
            success = bool(result.get("success"))
            code = result.get("message", "")
            if success:
                if code == "latest":
                    message = tr("runtime_update_latest")
                else:
                    updated = result.get("updated") or []
                    if result.get("restart_required"):
                        message = tr("runtime_update_success_restart", count=len(updated))
                    else:
                        message = tr("runtime_update_success", count=len(updated))
            elif code == "unauthorized" or code == "missing_token":
                message = tr("runtime_update_unauthorized")
            elif code == "connect_failed":
                message = tr("runtime_update_connect_failed")
            elif code == "unsupported":
                message = tr("runtime_update_unsupported")
            else:
                detail = result.get("detail") or code or tr("check_failed")
                message = tr("runtime_update_failed", error=detail)
        except Exception as exc:
            message = tr("runtime_update_failed", error=str(exc))
        self._emit_runtime_update_result(success, message)

    def _emit_runtime_update_result(self, success, message):
        try:
            if isValid(self):
                self.runtime_update_result_signal.emit(success, message)
        except RuntimeError:
            pass

    def _on_runtime_updated(self, success, message):
        if not isValid(self):
            return
        self.runtimeUpdateBtn.setEnabled(True)
        self.runtimeUpdateBtn.setText(tr("update_runtime"))
        self.runtimeUpdateBtn.setToolTip(message or "")
        self.runtimeUpdateHint.setText(message)

    def _sync_config_from_ui(self):
        self.config["auto_start"] = self.autoStartCheck.isChecked()
        self.config["always_on_top"] = self.alwaysOnTopCheck.isChecked()
        self.config["theme"] = "light" if self.lightThemeCheck.isChecked() else "dark"
        self.config["lang"] = "en" if self.englishLangCheck.isChecked() else "zh"
        self.config["ip"] = self.ipInput.text()
        self.config["port"] = self.portInput.text()
        if hasattr(self, "connectionModeCombo"):
            self.config["connection_mode"] = self.connectionModeCombo.currentData() or CONNECTION_MODE_AUTO
        runtime_port = self.runtimePortInput.text().strip()
        self.config["runtime_update_port"] = runtime_port if runtime_port.isdigit() else DEFAULT_RUNTIME_UPDATE_PORT
        self.config["api_token"] = self.tokenInput.text().strip()
        self.config["mini_click_through"] = self.miniClickThroughCheck.isChecked()
        self.config["show_task_name"] = self.showTaskNameCheck.isChecked()
        self.config["mini_opacity"] = self._normalize_opacity(self.miniOpacitySlider.value())
        if "api_port" in self.config:
            del self.config["api_port"]

    def _refresh_fields_from_config(self):
        self.ipInput.setText(self.config.get("ip", "127.0.0.1"))
        self.portInput.setText(str(self.config.get("port", "22267")))
        self.runtimePortInput.setText(str(self.config.get("runtime_update_port", DEFAULT_RUNTIME_UPDATE_PORT)))
        self.tokenInput.setText(self.config.get("api_token", ""))
        if hasattr(self, "connectionModeCombo"):
            mode_index = self.connectionModeCombo.findData(normalize_connection_mode(self.config))
            self.connectionModeCombo.setCurrentIndex(
                mode_index if mode_index >= 0 else self.connectionModeCombo.findData(CONNECTION_MODE_AUTO)
            )

    def _open_init_setup(self):
        self._sync_config_from_ui()
        from .init_window import InitSetupWindow

        dialog = InitSetupWindow(
            self,
            self.config,
            config_path(),
        )
        if dialog.exec():
            self._refresh_fields_from_config()
        dialog.deleteLater()

    def accept(self):
        self._sync_config_from_ui()
        print(f"[Settings] lang={self.config['lang']}, auto_start={self.config['auto_start']}, always_on_top={self.config['always_on_top']}, theme={self.config['theme']}, ip={self.config['ip']}, port={self.config['port']}")
        super().accept()

    def _normalize_opacity(self, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 100
        return max(35, min(value, 100))

    def _generate_token(self):
        self.tokenInput.setText(secrets.token_urlsafe(32))
        self.tokenInput.setFocus()

    def _run_connection_test(self):
        if not isValid(self):
            return
        ip = self.ipInput.text().strip()
        port_str = self.portInput.text().strip()
        connection_mode = self.connectionModeCombo.currentData() or CONNECTION_MODE_AUTO
        
        if not ip or not port_str.isdigit():
            self._on_test_result(False, tr("test_invalid"))
            return
            
        self.testBtn.setText("...")
        self.testBtn.setIcon(QIcon())
        self.testBtn.setEnabled(False)
        self.testBtn.setProperty("state", "testing")
        self.testBtn.style().unpolish(self.testBtn)
        self.testBtn.style().polish(self.testBtn)
        
        threading.Thread(
            target=self._test_api,
            args=(ip, port_str, self.tokenInput.text().strip(), connection_mode),
            daemon=True,
        ).start()

    def _test_api(self, ip, port, token, connection_mode):
        success = False
        message = ""
        try:
            test_config = {
                "ip": ip,
                "port": port,
                "api_token": token,
                "connection_mode": connection_mode,
            }
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
        self._emit_test_result(success, message)

    def _emit_test_result(self, success, message):
        try:
            if isValid(self):
                self.test_result_signal.emit(success, message)
        except RuntimeError:
            pass

    def _create_icon(self, state):
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.transparent)
        p = QPixmap(24, 24)
        pixmap.fill(Qt.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.Antialiasing)
        
        if state == "success":
            pen = QPen(QColor("#42d392"), 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            p.setPen(pen)
            p.drawLine(5, 12, 10, 17)
            p.drawLine(10, 17, 19, 7)
        elif state == "error":
            pen = QPen(QColor("#ff5c5c"), 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            p.setPen(pen)
            p.drawLine(7, 7, 17, 17)
            p.drawLine(17, 7, 7, 17)
            
        p.end()
        return QIcon(pixmap)

    def _on_test_result(self, success, message=""):
        if not isValid(self):
            return
        self.testBtn.setEnabled(True)
        self.testBtn.setText("")
        self.testBtn.setToolTip(message)
        self.testBtn.setIconSize(QSize(20, 20))
        if success:
            self.testBtn.setIcon(self._create_icon("success"))
            self.testBtn.setProperty("state", "success")
        else:
            self.testBtn.setIcon(self._create_icon("error"))
            self.testBtn.setProperty("state", "error")
            
        self.testBtn.style().unpolish(self.testBtn)
        self.testBtn.style().polish(self.testBtn)
        
        QTimer.singleShot(2000, self._reset_test_btn)

    def _reset_test_btn(self):
        if not isValid(self):
            return
        self.testBtn.setIcon(QIcon())
        self.testBtn.setText(tr("test_connection"))
        self.testBtn.setToolTip("")
        self.testBtn.setProperty("state", "normal")
        self.testBtn.style().unpolish(self.testBtn)
        self.testBtn.style().polish(self.testBtn)
