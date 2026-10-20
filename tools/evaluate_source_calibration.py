"""Self-authored fixed policy cases plus optional real, local printed OCR.

Never loads .env or model settings and never sends cloud requests. The policy
fixtures are explicitly simulated and must not be reported as AI accuracy.
"""
import argparse
from io import BytesIO
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ankisense.source_calibration import assess, corroborate_changes
from ankisense.source_calibration_models import PageTranscription
from ankisense.ocr_calibration import analyze_page


def policy_cases():
    rows = json.loads((ROOT / "examples/source_calibration_cases.json").read_text(encoding="utf-8"))["cases"]
    results = []
    for row in rows:
        page = {"page": 1, "text": row["before"], "raw_text": row["before"], "status": "pending", "method": "ocr",
                "ocr_lines": [] if row.get("no_geometry") else [{"text": row["before"], "start": 0, "end": len(row["before"]), "bbox": [.1, .1, .9, .3], "score": .72}]}
        result = assess(page, PageTranscription(text=row["first"], uncertainties=row.get("uncertainties", [])), PageTranscription(text=row["second"]))
        local = not result["issues"] and corroborate_changes(b"synthetic", page, result,
                          recognize=lambda *args: {"text": row.get("local", row["first"])})
        automatic = local and not result["issues"]
        results.append({"id": row["id"], "auto_adopt": automatic, "expected_auto": row["expected_auto"],
                        "passed": automatic == row["expected_auto"], "issues": result["issues"]})
    return {"mode": "simulated_policy_cases", "real_model_accuracy": None, "results": results}


def distance(first, second):
    row = list(range(len(second) + 1))
    for i, a in enumerate(first, 1):
        new = [i]
        for j, b in enumerate(second, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (a != b)))
        row = new
    return row[-1]


def local_ocr(output):
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    from ankisense import ocr
    fonts = [Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")]
    font_path = next((p for p in fonts if p.is_file()), None)
    if font_path is None:
        return {"status": "unavailable", "reason": "缺少固定中文字体，未执行真实 OCR。"}
    original_engine = ocr._get_engine
    ocr._get_engine = lambda: original_engine(output / "ocr-models")
    samples = [
        ("clean_chinese", "栈遵循后进先出。\n最后入栈的元素最先出栈。", False),
        ("numbers_conditions", "队列遵循先进先出。\n容量为30，出队前需要判空。", False),
        ("degraded_scan", "空栈不能直接出栈。\n出栈前应检查是否为空。", True),
        ("paragraph", "栈的插入和删除都在栈顶进行。\n新元素放入栈顶，删除时取出当前栈顶元素。\n队列的插入在队尾进行，删除在队头进行。\n结构为空时不能删除元素，应先检查是否为空。", "paragraph"),
        ("small_print", "树用于表示层级结构。\n根结点没有双亲，每个非根结点有一个双亲。\n叶结点没有孩子。", "small"),
        ("critical_units", "时间为5毫秒。\n容量为128字节。\n电流为20毫安。", False),
        ("negation_conditions", "空栈不能执行出栈操作。\n队列未满时才能入队。\n非空队列才有可删除的队头元素。", False),
        ("formula", "循环队列下标更新：\nnext = (rear + 1) % capacity\n下标从0开始。", False),
        ("english", "A stack uses last-in, first-out order.\nCheck emptiness before removing an item.", "paragraph"),
        ("two_columns", "栈遵循后进先出。\n插入和删除在栈顶进行。\n空栈不能出栈。\n\n队列遵循先进先出。\n插入在队尾，删除在队头。\n空队列不能出队。", "columns"),
        ("table", "操作    条件    结果\n入栈    栈未满    元素放入栈顶\n出栈    栈非空    删除栈顶元素\n入队    队列未满    元素放入队尾", "table"),
        ("rotated_scan", "顺序表使用连续的存储空间。\n链表通过链接表示元素之间的顺序。\n插入和删除时应检查边界条件。", "rotate"),
    ]
    rows = []
    try:
        for identifier, truth, style in samples:
            height = max(400, 100 + len(truth.splitlines()) * 70)
            image = Image.new("RGB", (1200, height), "white")
            draw = ImageDraw.Draw(image)
            size = 20 if style == "small" else 30 if style in {"paragraph", "columns", "table"} else 46
            font = ImageFont.truetype(str(font_path), size)
            if style == "columns":
                left, right = truth.split("\n\n")
                draw.multiline_text((50, 65), left, fill="black", font=font, spacing=28)
                draw.multiline_text((650, 65), right, fill="black", font=font, spacing=28)
            else:
                draw.multiline_text((50, 65), truth, fill="black", font=font, spacing=28)
            if style is True:
                image = image.filter(ImageFilter.GaussianBlur(.7)).resize((600, height // 2)).resize((1200, height))
            if style == "rotate":
                image = image.rotate(6, fillcolor="white")
            path = output / f"{identifier}.png"
            image.save(path)
            data = BytesIO()
            image.save(data, format="PNG")
            recognized = ocr._recognize_image(data.getvalue())
            expected, actual = (re.sub(r"\s+", "", value) for value in (truth, recognized["text"]))
            errors = distance(expected, actual)
            screening = analyze_page({**recognized, "page": 1, "status": "pending", "method": "ocr"})
            rows.append({"id": identifier, "truth": truth, "recognized": recognized["text"],
                         "characters": len(expected), "errors": errors, "cer": errors / max(1, len(expected)),
                         "engine": recognized.get("engine"), "engine_version": recognized.get("engine_version"),
                         "models": recognized.get("models"), "image": str(path),
                         "screening_codes": sorted({i["code"] for i in screening["issues"]})})
    finally:
        ocr._get_engine = original_engine
    total = sum(r["characters"] for r in rows)
    return {"status": "completed", "mode": "real_local_ocr_on_synthetic_printed_images", "samples": rows,
            "characters": total, "errors": sum(r["errors"] for r in rows),
            "cer": sum(r["errors"] for r in rows) / max(1, total), "cloud_calibration_evaluated": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--local-ocr", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"policy": policy_cases(), "notice": "预设视觉读取只验证政策；真实 OCR 部分不代表云端校准或真实讲义准确率。"}
    if args.local_ocr:
        report["local_ocr"] = local_ocr(args.output)
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"policy_passed": sum(r["passed"] for r in report["policy"]["results"]),
                      "policy_total": len(report["policy"]["results"]), "local_ocr": report.get("local_ocr", {}).get("status"),
                      "report": str(args.output / "report.json")}, ensure_ascii=False))
    return 0 if all(r["passed"] for r in report["policy"]["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
