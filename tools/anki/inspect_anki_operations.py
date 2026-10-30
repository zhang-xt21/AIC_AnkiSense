"""Inspect shipped bytecode locally when source is absent from the installer."""
import dis
import inspect
import io
import json
import os
from pathlib import Path

from aqt.operations import CollectionOp, QueryOp
from aqt.operations import on_op_finished
from aqt.taskman import TaskManager
from aqt.progress import ProgressManager

result = {}
output = io.StringIO()
dis.dis(on_op_finished, file=output)
result["on_op_finished"] = {"signature": str(inspect.signature(on_op_finished)), "bytecode": output.getvalue()}
for cls, names in [(QueryOp, ["__init__", "run_in_background", "_run"]),
                   (CollectionOp, ["__init__", "run_in_background", "_run"]),
                   (TaskManager, ["__init__", "run_in_background", "with_progress"]),
                   (ProgressManager, ["__init__", "start", "finish"])]:
    for name in names:
        method = getattr(cls, name, None)
        if method:
            output = io.StringIO()
            dis.dis(method, file=output)
            result[cls.__name__ + "." + name] = {"signature": str(inspect.signature(method)), "bytecode": output.getvalue()}
target = Path(os.environ["ANKISENSE_ANKI_TEST_ROOT"]) / "operation-behavior.json"
target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"result": str(target)}))
