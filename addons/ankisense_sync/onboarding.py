"""Non-model local presence and sync receipts for the desktop setup guide."""
from pathlib import Path

from .client import PluginClient

VERSION = "0.2.0"
SETUP_KEY = "ankisense_sync_setup_id"


def configuration(mw):
    try:
        return mw.addonManager.getConfig(__name__) or {}
    except Exception:
        return {}


def connection_path(mw):
    config = configuration(mw)
    if config.get("setup_id") and mw.pm.profile.get(SETUP_KEY) != config["setup_id"]:
        return config.get("connection_file", "")
    return mw.pm.profile.get("ankisense_sync_connection_file") or config.get("connection_file", "")


def send_event(path, client_id, kind="heartbeat", result=None):
    client = PluginClient.from_connection_file(path, client_id, timeout=3)
    return client.connection_event(kind, result or {}, plugin_version=VERSION)


def background_event(mw, path, client_id, kind="heartbeat", result=None, on_failure=None):
    def finished(future):
        try:
            future.result()
        except Exception:
            if on_failure:
                try:
                    on_failure()
                except RuntimeError:
                    pass  # The dialog may already be closed.
    mw.taskman.run_in_background(lambda: send_event(path, client_id, kind, result), finished)


def register(mw, get_client_id):
    if getattr(mw, "_ankisense_presence_timer", None) is not None:
        return
    from aqt import gui_hooks
    from aqt.qt import QTimer
    timer = QTimer(mw)
    timer.setInterval(25000)
    busy = False
    def heartbeat():
        nonlocal busy
        if busy or mw.col is None or not configuration(mw).get("setup_id"):
            return
        path = connection_path(mw)
        if not isinstance(path, str) or not Path(path).is_file():
            return
        busy = True
        identifier = get_client_id(mw)
        def finished(future):
            nonlocal busy
            busy = False
            try:
                future.result()
            except Exception:
                pass  # Offline Anki review remains available.
        mw.taskman.run_in_background(lambda: send_event(path, identifier), finished)
    timer.timeout.connect(heartbeat)
    timer.start()
    gui_hooks.profile_did_open.append(lambda: QTimer.singleShot(1000, heartbeat))
    QTimer.singleShot(1000, heartbeat)
    mw._ankisense_presence_timer = timer
