"""Serve a completed disk-backed export and clean it up, including disconnects."""
from tempfile import TemporaryDirectory

from fastapi import HTTPException
from fastapi.responses import FileResponse


class ExportResponse(FileResponse):
    def __init__(self, path, temporary, **kwargs):
        super().__init__(path, **kwargs)
        self.temporary = temporary

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.temporary.cleanup()


def export_error(error):
    if getattr(error, "errno", None) == 28 or getattr(error, "winerror", None) == 112:
        return HTTPException(507, "临时磁盘空间不足，请释放空间后重新导出。")
    return HTTPException(500, "无法创建导出文件，请检查临时目录的空间和访问权限。")
