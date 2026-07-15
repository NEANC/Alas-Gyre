from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QWidget, QLabel, QFrame, QTextEdit,
    QSizeGrip, QComboBox, QPushButton, QStackedWidget
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor, QFontMetrics
import threading
import html
import hashlib

from alas_gyre.api.client import api_headers, api_request, gyre_api_url
from alas_gyre.api.connection_mode import CONNECTION_MODE_WEBSOCKET, normalize_connection_mode
from .error_screenshot_window import ErrorScreenshotPanel
from .widgets import WindowButton
from .i18n import tr
from .window_behavior import (
    clamp_window_to_available_screen,
    install_title_bar_drag,
    schedule_frameless_stabilize,
)

LOG_LEVEL_STYLES = {
    "CRITICAL": {"fg": "#ff6b6b", "bg": "#2b171a", "bar": "#ff5c5c"},
    "ERROR": {"fg": "#ff8585", "bg": "#26171a", "bar": "#ff5c5c"},
    "WARNING": {"fg": "#f6c177", "bg": "#272116", "bar": "#d8a545"},
    "INFO": {"fg": "#9cdcfe", "bg": "transparent", "bar": "#315b7a"},
    "DEBUG": {"fg": "#8b949e", "bg": "transparent", "bar": "#3b4450"},
    "SUCCESS": {"fg": "#42d392", "bg": "transparent", "bar": "#42d392"},
}

LOG_LEVEL_STYLES_LIGHT = {
    "CRITICAL": {"fg": "#b91c1c", "bg": "#fee2e2", "bar": "#ef4444"},
    "ERROR": {"fg": "#9f1239", "bg": "#ffe4e6", "bar": "#f43f5e"},
    "WARNING": {"fg": "#b45309", "bg": "#fef3c7", "bar": "#d97706"},
    "INFO": {"fg": "#1d4ed8", "bg": "transparent", "bar": "#3b82f6"},
    "DEBUG": {"fg": "#475569", "bg": "transparent", "bar": "#64748b"},
    "SUCCESS": {"fg": "#047857", "bg": "transparent", "bar": "#10b981"},
}

LOG_FETCH_LINES = 500

class LogWindow(QDialog):
    # 用信号在主线程更新 UI
    log_update_signal = Signal(str)

    def __init__(self, parent=None, config=None, current_config="alas", configs=None):
        super().__init__(parent)
        self.config = config if config is not None else {}
        self.current_config = current_config
        self.configs = [str(item) for item in (configs or [current_config]) if str(item)]
        if self.current_config not in self.configs:
            self.configs.insert(0, self.current_config)
        self.setObjectName("logWindow")
        self.resize(680, 460)
        self.setMinimumSize(560, 400)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # 卡片背景
        self.card = QFrame(self)
        self.card.setObjectName("logCard")
        self.card.setAttribute(Qt.WA_StyledBackground, True)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        # ====== 顶栏 ======
        self.topBg = QWidget(self.card)
        self.topBg.setObjectName("logTopBg")
        self.topBg.setAttribute(Qt.WA_StyledBackground, True)
        self.topBg.setFixedHeight(30)
        install_title_bar_drag(self, self.topBg)
        top_layout = QHBoxLayout(self.topBg)
        top_layout.setContentsMargins(20, 0, 8, 0)

        self.titleLabel = QLabel(tr("log_title", config=self.current_config))
        self.titleLabel.setObjectName("logTitle")
        top_layout.addWidget(self.titleLabel)

        self.configCombo = QComboBox(self.topBg)
        self.configCombo.setObjectName("logConfigCombo")
        self.configCombo.setFixedHeight(24)
        self.configCombo.setFocusPolicy(Qt.NoFocus)
        self.configCombo.setCursor(Qt.PointingHandCursor)
        self.configCombo.addItems(self.configs)
        self.configCombo.setCurrentText(self.current_config)
        self.configCombo.currentIndexChanged.connect(self._on_config_selected)
        self.configCombo.setEditable(False)
        top_layout.addSpacing(10)
        top_layout.addWidget(self.configCombo)
        self._update_config_combo_width()
        top_layout.addSpacing(8)

        self.logTabBtn = QPushButton(tr("log_view"), self.topBg)
        self.logTabBtn.setObjectName("logViewTab")
        self.logTabBtn.setCursor(Qt.PointingHandCursor)
        self.logTabBtn.setFocusPolicy(Qt.NoFocus)
        self.logTabBtn.setCheckable(True)
        self.logTabBtn.clicked.connect(lambda: self._set_active_view(0))
        top_layout.addWidget(self.logTabBtn)

        self.screenshotTabBtn = QPushButton(tr("screenshot_view"), self.topBg)
        self.screenshotTabBtn.setObjectName("logViewTab")
        self.screenshotTabBtn.setCursor(Qt.PointingHandCursor)
        self.screenshotTabBtn.setFocusPolicy(Qt.NoFocus)
        self.screenshotTabBtn.setCheckable(True)
        self.screenshotTabBtn.clicked.connect(lambda: self._set_active_view(1))
        top_layout.addWidget(self.screenshotTabBtn)

        top_layout.addStretch()

        self.closeBtn = WindowButton("close", self.topBg)
        self.closeBtn.mousePressEvent = lambda event: self.reject() if event.button() == Qt.LeftButton else None
        top_layout.addWidget(self.closeBtn)

        card_layout.addWidget(self.topBg)

        # ====== 内容区 ======
        self.stack = QStackedWidget(self.card)
        self.stack.setObjectName("logContentStack")

        self.logText = QTextEdit(self.card)
        self.logText.setObjectName("logTextPanel")
        self.logText.setReadOnly(True)
        self.stack.addWidget(self.logText)

        self.screenshotPanel = ErrorScreenshotPanel(self.card, self.config, auto_fetch=False)
        self.stack.addWidget(self.screenshotPanel)
        self._screenshot_loaded_once = False

        card_layout.addWidget(self.stack, stretch=1)
        main_layout.addWidget(self.card)

        # 阴影效果

        self.sizeGrip = QSizeGrip(self.card)
        self.sizeGrip.setFixedSize(18, 18)
        self.sizeGrip.setStyleSheet("background: transparent;")
        self.sizeGrip.raise_()

        self._center_on_screen()

        # 信号绑定
        self.log_update_signal.connect(self._on_log_updated)

        # 初始抓取并启动定时器
        self._last_log_digest = ""
        self._fetching_log = False
        self.logText.document().setMaximumBlockCount(LOG_FETCH_LINES + 80)
        self._set_active_view(0)
        self._fetch_log()
        
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._fetch_log)
        self.poll_timer.start(2000)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "sizeGrip"):
            self.sizeGrip.move(
                self.card.width() - self.sizeGrip.width() - 3,
                self.card.height() - self.sizeGrip.height() - 3,
            )

    def _center_on_screen(self):
        if self.parent():
            parent_geom = self.parent().geometry()
            x = parent_geom.x() + (parent_geom.width() - self.width()) // 2
            y = parent_geom.y() + (parent_geom.height() - self.height()) // 2
            self.move(x, y)
        clamp_window_to_available_screen(self, self.parent())

    def mousePressEvent(self, event):
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)

    def _fetch_log(self):
        if normalize_connection_mode(self.config) == CONNECTION_MODE_WEBSOCKET:
            if hasattr(self, "poll_timer"):
                self.poll_timer.stop()
            return
        if self._fetching_log:
            return
        self._fetching_log = True
        threading.Thread(target=self._fetch_log_thread, daemon=True).start()

    def _fetch_log_thread(self):
        try:
            url = gyre_api_url(self.config, "log")
            resp = api_request(
                "GET",
                url,
                params={"config": self.current_config, "lines": LOG_FETCH_LINES},
                headers=api_headers(self.config),
                timeout=1.5,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("error"):
                    print(f"[LogWindow] Log API error: {data.get('error')}")
                    log_text = tr("log_fetch_failed")
                elif data.get("exists") is False:
                    log_text = f"[{self.current_config}] 暂无可用日志"
                else:
                    log_text = data.get("log", "")
                    if data.get("source") == "file" and data.get("file"):
                        log_text = f"[{self.current_config}] 已载入日志文件: {data.get('file')}\n{log_text}"
                self.log_update_signal.emit(log_text)
            else:
                try:
                    data = resp.json()
                    message = data.get("error") or data.get("message") or resp.text
                except Exception:
                    message = resp.text
                print(f"[LogWindow] Log fetch failed HTTP {resp.status_code}: {message}")
                self.log_update_signal.emit(tr("log_http_failed", status=resp.status_code))
        except Exception as e:
            print(f"[LogWindow] Log connection failed: {e}")
            self.log_update_signal.emit(tr("log_connect_failed"))
        finally:
            self._fetching_log = False

    def set_config(self, config_name):
        self.current_config = config_name
        self.titleLabel.setText(tr("log_title", config=self.current_config))
        if self.current_config not in self.configs:
            self.configs.insert(0, self.current_config)
            self.set_configs(self.configs, self.current_config)
        else:
            self.configCombo.blockSignals(True)
            self.configCombo.setCurrentText(self.current_config)
            self.configCombo.blockSignals(False)
        self._last_log_digest = ""
        self.logText.clear()
        self._fetch_log()

    def set_configs(self, configs, current_config=None):
        cleaned = [str(item) for item in configs if str(item)]
        if not cleaned:
            cleaned = [self.current_config]
        self.configs = cleaned
        if current_config is not None:
            self.current_config = current_config
        if self.current_config not in self.configs:
            self.configs.insert(0, self.current_config)

        self.configCombo.blockSignals(True)
        self.configCombo.clear()
        self.configCombo.addItems(self.configs)
        self.configCombo.setCurrentText(self.current_config)
        self.configCombo.blockSignals(False)
        self._update_config_combo_width()

    def _update_config_combo_width(self):
        metrics = QFontMetrics(self.configCombo.font())
        text_width = max(metrics.horizontalAdvance(config) for config in self.configs)
        width = min(max(text_width + 42, 150), 260)
        self.configCombo.setFixedWidth(width)
        self.configCombo.view().setMinimumWidth(width)

    def _on_config_selected(self, index):
        if 0 <= index < len(self.configs):
            self.set_config(self.configs[index])

    def _set_active_view(self, index):
        self.stack.setCurrentIndex(index)
        self.logTabBtn.setChecked(index == 0)
        self.screenshotTabBtn.setChecked(index == 1)
        for button in (self.logTabBtn, self.screenshotTabBtn):
            button.setProperty("active", button.isChecked())
            button.style().unpolish(button)
            button.style().polish(button)

        if index == 1 and not self._screenshot_loaded_once:
            self._screenshot_loaded_once = True
            self.screenshotPanel.fetch_groups()

    def _log_to_html(self, text):
        theme = self.config.get("theme", "dark")
        styles = LOG_LEVEL_STYLES_LIGHT if theme == "light" else LOG_LEVEL_STYLES
        default_fg = "#334155" if theme == "light" else "#cfd3dc"
        divider_fg = "#94a3b8" if theme == "light" else "#5f6773"

        rows = []
        for line in text.splitlines():
            escaped = html.escape(line) or " "
            style = None
            level_name = None

            for level, level_style in styles.items():
                if f"| {level}" in line or line.startswith(level) or f" {level} " in line:
                    style = level_style
                    level_name = level
                    break

            lower_line = line.lower()
            if style is None and ("traceback" in lower_line or "exception" in lower_line):
                style = styles["ERROR"]
                level_name = "ERROR"

            if style is None:
                if set(line.strip()) and len(set(line.strip())) <= 3:
                    rows.append(
                        f'<div style="color:{divider_fg}; padding:1px 0;">'
                        f"{escaped}</div>"
                    )
                else:
                    rows.append(
                        f'<div style="color:{default_fg}; padding:1px 0 1px 8px; '
                        'border-left:2px solid transparent;">'
                        f"{escaped}</div>"
                    )
                continue

            badge = ""
            rendered = escaped
            if level_name:
                badge = (
                    f'<span style="color:{style["fg"]}; font-weight:600;">'
                    f"{level_name}</span>"
                )
                rendered = rendered.replace(level_name, badge, 1)

            rows.append(
                f'<div style="color:{default_fg}; background-color:{style["bg"]}; '
                f'border-left:2px solid {style["bar"]}; '
                'padding:2px 8px; margin:1px 0; border-radius:3px;">'
                f"{rendered}</div>"
            )
        body = "".join(rows)
        return (
            '<html><body style="white-space:pre-wrap; margin:0; '
            "font-family:'Microsoft YaHei UI','Microsoft YaHei',Consolas,Courier New,monospace; font-size:12px; "
            f'line-height:1.45; color:{default_fg};">'
            f"{body}</body></html>"
        )

    def _on_log_updated(self, text):
        if not text or not self.isVisible():
            return

        digest = hashlib.blake2s(text.encode("utf-8", errors="ignore"), digest_size=12).hexdigest()
        if digest == self._last_log_digest:
            return

        self._last_log_digest = digest

        scrollbar = self.logText.verticalScrollBar()
        is_at_bottom = scrollbar.value() >= scrollbar.maximum() - 5

        self.logText.setHtml(self._log_to_html(text))

        if is_at_bottom:
            self.logText.moveCursor(QTextCursor.End)

    def reject(self):
        self.poll_timer.stop()
        self.logText.clear()
        self.screenshotPanel.clear()
        self._last_log_digest = ""
        super().reject()

    def showEvent(self, event):
        super().showEvent(event)
        clamp_window_to_available_screen(self, self.parent())
        schedule_frameless_stabilize(self, self.card, self.topBg, self.stack)
        QTimer.singleShot(0, lambda: clamp_window_to_available_screen(self, self.parent()))
        if not self.poll_timer.isActive():
            self.poll_timer.start(2000)
        self._fetch_log()
        if self.stack.currentIndex() == 1 and not self._screenshot_loaded_once:
            self._screenshot_loaded_once = True
            self.screenshotPanel.fetch_groups()
