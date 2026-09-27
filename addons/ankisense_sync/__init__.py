"""Anki entry point; transport helpers remain importable outside Anki."""
from importlib.util import find_spec


def _register():
    import aqt

    if aqt.mw is not None:
        from .ui import register

        register(aqt.mw)


if find_spec("aqt") is not None:
    import aqt

    if aqt.mw is not None:
        _register()
    else:
        aqt.gui_hooks.profile_did_open.append(_register)
