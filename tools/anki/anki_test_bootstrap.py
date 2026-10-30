"""Guarded bootstrap for Anki's own Python; not an add-on or production entry."""
import functools
import json
import os
from pathlib import Path
import runpy
import site
import sys


def run(options):
    root = Path(options["anki_root"]).resolve()
    data = Path(options["data_directory"]).resolve()
    project = Path(options["work_directory"]).resolve()
    script = Path(options["script"]).resolve()
    protected = [root, root.parent / "AnkiData"]
    if os.getenv("APPDATA"):
        protected.append(Path(os.environ["APPDATA"]) / "Anki2")

    def within(path, parent):
        return path == parent or parent in path.parents

    if any(within(data, path.resolve()) for path in protected):
        raise RuntimeError("Test data directory must be outside the Anki installation and personal profiles.")
    data.mkdir(parents=True, exist_ok=True)
    os.environ["ANKISENSE_ANKI_TEST_ROOT"] = str(data)
    os.environ["ANKISENSE_ANKI_RUNTIME_ROOT"] = str(root)
    os.environ["ANKISENSE_ANKI_ALLOW_LOOPBACK"] = "1" if options.get("allow_loopback") else "0"
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.dont_write_bytecode = True
    sys.argv = [str(script), *options["arguments"]]
    sys.path[:0] = [str(script.parent), str(project)]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    dll_handle = os.add_dll_directory(str(root))

    def audit(event, args):
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
            address = args[0] if event == "socket.getaddrinfo" else args[1]
            host = address[0] if isinstance(address, tuple) else address
            if not options.get("allow_loopback") or host not in {"127.0.0.1", "::1"}:
                raise RuntimeError("Only explicitly enabled numeric loopback connections are allowed in this test runner.")
        if event in {"socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo"}:
            raise RuntimeError("DNS lookups are disabled in the isolated Anki test runner.")
        if event not in {"open", "sqlite3.connect"} or not args or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        candidate = Path(os.fsdecode(args[0])).resolve()
        if any(within(candidate, path.resolve()) for path in protected[1:]):
            raise RuntimeError("Personal Anki profiles are inaccessible in this test runner.")
        if event == "sqlite3.connect" and not within(candidate, data):
            raise RuntimeError("Test databases must stay inside ANKISENSE_ANKI_TEST_ROOT.")
        if event == "open" and within(candidate, root):
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            writable = (isinstance(mode, str) and any(char in mode for char in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writable:
                raise RuntimeError("The official Anki installation is read-only during tests.")

    sys.addaudithook(audit)
    # Anki's embedded launcher normally processes pywin32.pth. Reproduce that
    # package-path setup without installing anything into either interpreter.
    site.addsitedir(str(root / "app_packages"))
    from anki.collection import Collection
    original = Collection.__init__

    @functools.wraps(original)
    def guarded_collection(self, path, *args, **kwargs):
        if not within(Path(path).resolve(), data):
            raise RuntimeError("Collection path must stay inside ANKISENSE_ANKI_TEST_ROOT.")
        return original(self, path, *args, **kwargs)

    Collection.__init__ = guarded_collection
    runpy.run_path(str(script), run_name="__main__")
    # Keep the Windows DLL search handle alive throughout script execution.
    del dll_handle
