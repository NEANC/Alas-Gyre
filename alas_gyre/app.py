import json
import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QFont, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from alas_gyre.core.paths import (
    config_path,
    resource_path,
)
from alas_gyre.services.updater import cleanup_old_exe
from alas_gyre.core.version import set_current_version
from ui import AlasConsole
from ui.i18n import detect_system_language, set_language, tr


APP_USER_MODEL_ID = "AngeKatrina.AlasGyre"


def configure_windows_app_id():
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception as exc:
        print(f"[警告] 设置 Windows AppUserModelID 失败: {exc}")


def load_initial_language():
    lang = detect_system_language()
    try:
        cfg_p = config_path()
        if os.path.exists(cfg_p):
            with open(cfg_p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                configured_lang = cfg.get("lang")
                if configured_lang in ("zh", "en"):
                    lang = configured_lang
    except Exception as exc:
        print(f"[警告] 提前读取语言失败: {exc}")
    return lang


def create_tray(app, app_icon, window):
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None

    tray = QSystemTrayIcon(app_icon, app)
    tray.setToolTip("Alas-Gyre")
    menu = QMenu()
    tray_actions = {}

    show_action = QAction(tr("show_main"), menu)
    show_action.triggered.connect(window.card.restore_main_window)
    menu.addAction(show_action)
    tray_actions["show_main"] = show_action

    mini_action = QAction(tr("show_float"), menu)
    mini_action.triggered.connect(window.card.show_mini_window)
    menu.addAction(mini_action)
    tray_actions["show_float"] = mini_action

    home_action = QAction(tr("open_webui"), menu)
    home_action.triggered.connect(lambda: window.card._on_icon_click("home", window.card.homeIcon))
    menu.addAction(home_action)
    tray_actions["open_webui"] = home_action

    menu.addSeparator()

    settings_action = QAction(tr("settings_title"), menu)
    settings_action.triggered.connect(lambda: window.card._on_icon_click("settings", window.card.setIcon))
    menu.addAction(settings_action)
    tray_actions["settings"] = settings_action

    setup_action = QAction(tr("wizard"), menu)
    setup_action.triggered.connect(lambda: open_init_setup(window, app_icon))
    menu.addAction(setup_action)
    tray_actions["wizard"] = setup_action

    quit_action = QAction(tr("quit"), menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)
    tray_actions["quit"] = quit_action

    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: window.card.restore_main_window()
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick
        else None
    )
    app._alas_tray = tray
    app._alas_tray_actions = tray_actions
    return tray


def open_init_setup(window, app_icon):
    from ui.init_window import InitSetupWindow

    window.card.restore_main_window()
    dialog = InitSetupWindow(
        window,
        window.card.config,
        window.card.config_path,
    )
    if not app_icon.isNull():
        dialog.setWindowIcon(app_icon)
    if dialog.exec():
        window.card._save_config()
    dialog.deleteLater()


def main(current_version):
    set_current_version(current_version)
    cleanup_old_exe()
    set_language(load_initial_language())

    configure_windows_app_id()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # 退出时统一清理常驻 WebSocket 资源
    def _cleanup_ws():
        try:
            from alas_gyre.api.websocket_hijack import get_persistent_manager
            get_persistent_manager().stop_all()
        except Exception:
            pass

    app.aboutToQuit.connect(_cleanup_ws)
    app.setApplicationName("Alas-Gyre")
    app.setApplicationDisplayName("Alas-Gyre")
    app.setOrganizationName("Ange-Katrina")
    app.setFont(QFont("Microsoft YaHei", 9))

    icon_path = resource_path(os.path.join("ui", "assets", "alas.ico"))
    app_icon = QIcon(icon_path)
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    window = AlasConsole()
    from ui.theme import apply_theme

    apply_theme(app, window.card.config.get("theme", "dark"))

    if not app_icon.isNull():
        window.setWindowIcon(app_icon)

    tray = create_tray(app, app_icon, window)
    if tray is not None:
        tray.show()

    if not window.card.config.get("setup_completed", False):
        from ui.init_window import InitSetupWindow

        setup_dialog = InitSetupWindow(
            None,
            window.card.config,
            window.card.config_path,
        )
        if not app_icon.isNull():
            setup_dialog.setWindowIcon(app_icon)
        setup_dialog.exec()
        setup_dialog.deleteLater()

    window.show()
    QTimer.singleShot(1500, lambda: window.start_auto_update_check(current_version))

    sys.exit(app.exec())
