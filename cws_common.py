"""claude-web-sync 共用:找同步資料夾。MCP server 與 SessionStart hook 都用這份。"""
import os
import sys
from pathlib import Path


def downloads_dir() -> Path:
    """真正的下載資料夾(Windows 使用者常把它搬到別的磁碟,不能寫死 ~/Downloads)。"""
    if sys.platform == "win32":
        try:
            import ctypes
            import uuid

            # FOLDERID_Downloads
            guid = (ctypes.c_byte * 16).from_buffer_copy(
                uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le)
            buf = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(buf)) == 0:
                return Path(buf.value)
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / "Downloads"


def resolve_out() -> Path:
    """CLAUDE_SYNC_OUT > <Downloads>/claude-web-sync(擴充功能預設) > ~/claude-web-sync-data(舊版 CLI)。"""
    env = os.environ.get("CLAUDE_SYNC_OUT")
    if env:
        return Path(env)
    ext = downloads_dir() / "claude-web-sync"
    return ext if ext.is_dir() else Path.home() / "claude-web-sync-data"
