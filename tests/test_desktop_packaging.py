"""The desktop bundle must contain runtime scripts and avoid live user data."""
from pathlib import Path
import ast
import sys
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def test_frozen_bundle_preserves_powerpoint_worker_and_frontend_modules(monkeypatch):
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda name: ([], [], [])
    hooks.collect_submodules = lambda name: []
    hooks.copy_metadata = lambda name: []
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    spec = (ROOT / "desktop/ankisense-backend.spec").read_text(encoding="utf-8-sig")
    namespace = {
        "SPECPATH": str(ROOT / "desktop"),
        "Analysis": lambda *args, **kwargs: type("Bundle", (), {"pure": [], "scripts": [], "binaries": [], "datas": kwargs["datas"]})(),
        "PYZ": lambda *args, **kwargs: None,
        "EXE": lambda *args, **kwargs: None,
        "COLLECT": lambda *args, **kwargs: None,
    }
    # Execute the actual spec without building: catches path/layout regressions.
    exec(compile(spec, "ankisense-backend.spec", "exec"), namespace)
    destinations = {target: Path(source) for source, target in namespace["datas"]}
    assert (destinations["ankisense/workers"] / "powerpoint-export.ps1").is_file()
    static = destinations["ankisense/static"]
    for name in ("automatic.js", "generation.js", "card-auto-review.js", "image-auto-review.js", "auto-illustration.js", "anki-setup.js", "model-settings.js"):
        assert (static / name).is_file()
    from ankisense.anki_setup import ADDON_FILES
    addon = [Path(source) for source, target in namespace["datas"] if target == "addons/ankisense_sync"]
    assert {file.name for file in addon} == set(ADDON_FILES)
    assert all(file.is_file() for file in addon)
    import json
    assert json.loads(next(file for file in addon if file.name == "config.json").read_text(encoding="utf-8")) == {"connection_file": ""}
    assert not any(source.name in {".env", "data", "model-settings.json"} for source in destinations.values())


def test_standalone_launcher_keeps_local_default_and_custom_test_port():
    tree = ast.parse((ROOT / "desktop/backend_launcher.py").read_text(encoding="utf-8-sig"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "parse_host_port")
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "backend_launcher.py", "exec"), namespace)
    parse = namespace["parse_host_port"]
    assert parse(["backend.exe"]) == ("127.0.0.1", 8765)
    assert parse(["backend.exe", "--host", "127.0.0.1", "--port", "18765"]) == ("127.0.0.1", 18765)


def test_desktop_and_backend_release_versions_match():
    import json
    from ankisense.version import APP_VERSION
    package = json.loads((ROOT / "desktop/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "desktop/package-lock.json").read_text(encoding="utf-8"))
    assert package["version"] == lock["version"] == lock["packages"][""]["version"] == APP_VERSION
