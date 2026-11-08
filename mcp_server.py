#!/usr/bin/env python3
"""claude-web-sync MCP server (stdio)。

讓 Claude Code 等 MCP client 直接讀取/觸發 claude.ai 網頁端記憶與 prompt 的本機同步。
讀取類工具只讀本機已同步檔案;sync_now 另開子行程跑 CLI(Playwright 不能放進 asyncio 迴圈)。
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

HERE = Path(__file__).resolve().parent
def _downloads_dir() -> Path:
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


def _default_out() -> Path:
    # 擴充功能預設寫到 <Downloads>/claude-web-sync;舊版 CLI 寫到 ~/claude-web-sync-data
    ext = _downloads_dir() / "claude-web-sync"
    return ext if ext.is_dir() else Path.home() / "claude-web-sync-data"


OUT = Path(os.environ.get("CLAUDE_SYNC_OUT") or _default_out())
CLI = HERE / "claude_web_sync.py"

mcp = FastMCP("claude-web-sync")


def _read(rel: str) -> str:
    p = OUT / rel
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def _safe(rel: str) -> Path:
    p = (OUT / rel).resolve()
    if OUT.resolve() not in p.parents and p != OUT.resolve():
        raise ValueError("路徑超出同步資料夾")
    return p


@mcp.tool()
async def sync_now(no_knowledge: bool = False, headed: bool = False) -> str:
    """立即從 claude.ai 同步記憶 / 偏好 / Projects / Styles 到本機,回傳變更摘要。
    需先在終端機執行過一次 `python claude_web_sync.py login`。"""
    args = [sys.executable, str(CLI), "--out", str(OUT), "sync"]
    if no_knowledge:
        args.append("--no-knowledge")
    if headed:
        args.append("--headed")
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        return "同步逾時(300 秒)"
    return out.decode("utf-8", errors="replace").strip() + f"\n(exit {proc.returncode})"


@mcp.tool()
def get_status() -> str:
    """最後同步時間與已同步項目概況。"""
    last = _read("last_sync.txt").strip() or "尚未同步"
    projects = len(list((OUT / "projects").glob("*/instructions.md"))) if (OUT / "projects").is_dir() else 0
    return (f"輸出資料夾:{OUT}\n最後同步:{last}\n"
            f"memory.md:{'有' if _read('memory.md') else '無'}\n"
            f"preferences.md:{'有' if _read('preferences.md') else '無'}\n"
            f"Projects:{projects}")


@mcp.tool()
def read_memory() -> str:
    """讀取已同步的 claude.ai 記憶(memory.md)。"""
    return _read("memory.md") or "(沒有記憶檔,請先 sync_now,或用 discover 校準端點)"


@mcp.tool()
def read_preferences() -> str:
    """讀取已同步的個人偏好 / 自訂指令(preferences.md)。"""
    return _read("preferences.md") or "(沒有偏好檔)"


@mcp.tool()
def list_projects() -> list[str]:
    """列出已同步的 Projects(資料夾名稱)。"""
    d = OUT / "projects"
    return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []


@mcp.tool()
def read_project(name: str, include_knowledge: bool = False) -> str:
    """讀取某個 Project 的指令;include_knowledge=True 時連知識庫檔案一起回傳。
    name 用 list_projects 回傳的資料夾名稱(可只給前綴)。"""
    d = OUT / "projects"
    hits = [p for p in d.iterdir() if p.is_dir() and p.name.startswith(name)] if d.is_dir() else []
    if len(hits) != 1:
        return f"找不到唯一符合的 Project:{[h.name for h in hits] or name}"
    p = hits[0]
    parts = [f"# {p.name}", (p / "README.md").read_text(encoding="utf-8") if (p / "README.md").exists() else "",
             "## 指令", (p / "instructions.md").read_text(encoding="utf-8")]
    if include_knowledge and (p / "knowledge").is_dir():
        for f in sorted((p / "knowledge").iterdir()):
            parts += [f"## 知識庫:{f.name}", f.read_text(encoding="utf-8", errors="replace")]
    return "\n\n".join(parts)


@mcp.tool()
def search_synced(query: str, max_hits: int = 30) -> list[str]:
    """在所有已同步的文字檔(排除 raw/history)中做不分大小寫的關鍵字搜尋。"""
    q, hits = query.lower(), []
    for f in OUT.rglob("*"):
        rel = f.relative_to(OUT).parts
        if not f.is_file() or rel[0] in ("raw", "history") or f.suffix.lower() not in (".md", ".txt", ""):
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if q in line.lower():
                hits.append(f"{'/'.join(rel)}:{i}: {line.strip()[:200]}")
                if len(hits) >= max_hits:
                    return hits
    return hits


if __name__ == "__main__":
    mcp.run()
