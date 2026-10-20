"""Read native slide text and render presentations through local office software."""
import os
from pathlib import Path
import posixpath
import shutil
import subprocess
import zipfile
from xml.etree import ElementTree as ET

NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def slide_text(path):
    try:
        with zipfile.ZipFile(path) as archive:
            if sum(i.file_size for i in archive.infolist()) > 150 * 1024 * 1024:
                raise ValueError("PPTX 解压内容超过限制，请拆分讲义。")
            presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
            rels = ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
            mapping = {r.attrib["Id"]: r.attrib["Target"] for r in rels if r.attrib.get("TargetMode") != "External"}
            result = []
            for slide in presentation.findall("p:sldIdLst/p:sldId", NS):
                target = mapping[slide.attrib[f"{{{NS['r']}}}id"]]
                name = posixpath.normpath(posixpath.join("ppt", target)) if not target.startswith("/") else target.lstrip("/")
                if not name.startswith("ppt/slides/") or not name.endswith(".xml"):
                    raise ValueError("PPTX 幻灯片索引无效。")
                root = ET.fromstring(archive.read(name))
                paragraphs = ["".join(t.text or "" for t in p.findall(".//a:t", NS)) for p in root.findall(".//a:p", NS)]
                result.append("\n".join(p for p in paragraphs if p.strip()))
            if not result or len(result) > 500:
                raise ValueError("讲义须包含 1–500 页幻灯片。")
            return result
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise ValueError("PPTX 文件无法读取，请检查文件是否完整。") from None


def convert_to_pdf(source: Path, directory: Path):
    output = directory / (source.stem + ".pdf")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    office = shutil.which("soffice") or shutil.which("libreoffice")
    try:
        if office:
            profile = (directory / "office-profile").resolve().as_uri()
            process = subprocess.run([office, f"-env:UserInstallation={profile}", "--headless", "--convert-to", "pdf", "--outdir", str(directory), str(source)],
                                     capture_output=True, timeout=180, creationflags=flags)
        elif os.name == "nt":
            script = Path(__file__).parent / "workers" / "powerpoint-export.ps1"
            process = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Source", str(source.resolve()), "-Destination", str(output.resolve())],
                                     capture_output=True, timeout=180, creationflags=flags)
        else:
            raise ValueError("此电脑缺少 PowerPoint 或 LibreOffice。请先将讲义另存为 PDF 后导入。")
    except subprocess.TimeoutExpired:
        raise ValueError("讲义转换超时，请将该文件另存为 PDF 后导入。") from None
    if process.returncode or not output.is_file() or not output.read_bytes().startswith(b"%PDF-"):
        raise ValueError("讲义转换失败，请确认本机已安装 PowerPoint 或 LibreOffice；也可直接导入 PDF。")
    if output.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("转换后的讲义超过 100 MB，请拆分文件。")
    return output.read_bytes()
