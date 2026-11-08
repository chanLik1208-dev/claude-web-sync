#!/usr/bin/env python3
"""claude-web-sync MCP server (stdio)。

讓 Claude Code 等 MCP client 讀取 claude.ai 網頁端記憶 / 偏好 / Projects 的本機副本,
並能叫「你自己的 Chrome 擴充功能」立刻同步(sync_now)。

橋接方式:本程式在 127.0.0.1 開一個小 HTTP 埠;擴充功能每 30 秒 long-poll 一次,
有人呼叫 sync_now 就把請求交給它,它在你已登入的瀏覽器裡抓完再把結果回傳。
安全:只接受 Origin 為 chrome-extension://(可用 CLAUDE_SYNC_EXT_ID 釘死 ID)、
帶 X-CWS 標頭、Host 為 127.0.0.1/localhost 的請求(擋一般網頁與 DNS rebinding)。
"""
import asyncio
import json
import os
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp.server.fastmcp import FastMCP

PORT = int(os.environ.get("CLAUDE_SYNC_PORT", "47821"))
EXT_ID = os.environ.get("CLAUDE_SYNC_EXT_ID", "")
POLL_HOLD = 20  # 秒:擴充功能的 long-poll 最多等多久


def _downloads_dir() -> Path:
    """真正的下載資料夾(Windows 使用者常把它搬到別的磁碟,不能寫死 ~/Downloads)。"""
    if sys.platform == "win32":
        try:
            import ctypes
            import uuid as _uuid

            # FOLDERID_Downloads
            guid = (ctypes.c_byte * 16).from_buffer_copy(
                _uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le)
            buf = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(buf)) == 0:
                return Path(buf.value)
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / "Downloads"


def _default_out() -> Path:
    ext = _downloads_dir() / "claude-web-sync"
    return ext if ext.is_dir() else Path.home() / "claude-web-sync-data"


OUT = Path(os.environ.get("CLAUDE_SYNC_OUT") or _default_out())


# ───────────────────────── 橋接:MCP ⇄ 擴充功能 ─────────────────────────
class Bridge:
    def __init__(self, port: int, ext_id: str = ""):
        self.port, self.ext_id = port, ext_id
        self.cv = threading.Condition()
        self.pending = None   # {"id", "force"}:還沒被擴充功能取走
        self.inflight = None  # 已取走、等結果的 id
        self.results = {}
        self.last_seen = 0.0
        self.server = None

    # --- HTTP 端 ---
    def start(self) -> bool:
        bridge = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 不能往 stdout 寫(那是 MCP 的通道)
                pass

            def _origin_ok(self) -> bool:
                o = self.headers.get("Origin", "")
                if not o.startswith("chrome-extension://"):
                    return False
                if bridge.ext_id and o != f"chrome-extension://{bridge.ext_id}":
                    return False
                return self.headers.get("Host", "") in (f"127.0.0.1:{bridge.port}", f"localhost:{bridge.port}")

            def _send(self, code, obj=None):
                body = b"" if obj is None else json.dumps(obj).encode()
                self.send_response(code)
                o = self.headers.get("Origin", "")
                if o.startswith("chrome-extension://"):
                    self.send_header("Access-Control-Allow-Origin", o)
                    self.send_header("Access-Control-Allow-Headers", "X-CWS, Content-Type")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _guard(self) -> bool:
                if not (self._origin_ok() and self.headers.get("X-CWS") == "1"):
                    self._send(403, {"error": "forbidden"})
                    return False
                return True

            def do_OPTIONS(self):
                self._send(204) if self._origin_ok() else self._send(403, {"error": "forbidden"})

            def do_GET(self):
                if self.path != "/poll" or not self._guard():
                    return self._send(404, {}) if self.path != "/poll" else None
                bridge.last_seen = time.time()
                with bridge.cv:
                    bridge.cv.wait_for(lambda: bridge.pending is not None, timeout=POLL_HOLD)
                    req, bridge.pending = bridge.pending, None
                    if req:
                        bridge.inflight = req["id"]
                self._send(200, req or {})

            def do_POST(self):
                if self.path != "/result" or not self._guard():
                    return self._send(404, {}) if self.path != "/result" else None
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    data = json.loads(self.rfile.read(min(n, 8_000_000)) or b"{}")
                except Exception:  # noqa: BLE001
                    return self._send(400, {"error": "bad json"})
                with bridge.cv:
                    bridge.results[str(data.get("id"))] = data.get("status") or {}
                    bridge.inflight = None
                    bridge.cv.notify_all()
                self._send(200, {"ok": True})

        class Srv(ThreadingHTTPServer):
            # Windows 的 SO_REUSEADDR 允許多個程式聽同一個埠(會互搶、也可能被攔截),要改成獨佔;
            # POSIX 的 SO_REUSEADDR 只是略過 TIME_WAIT,保留以便快速重啟。
            allow_reuse_address = sys.platform != "win32"

            def server_bind(self):
                if sys.platform == "win32":
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        try:
            self.server = Srv(("127.0.0.1", self.port), H)
        except OSError:
            self.server = None  # 埠被別的 Claude session 佔用
            return False
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return True

    # --- MCP 端 ---
    def request(self, force: bool, timeout: float = 70):
        """交給擴充功能並等結果。回傳 (status 或 None, 失敗原因)。"""
        rid = uuid.uuid4().hex
        with self.cv:
            if self.pending is not None or self.inflight is not None:
                return None, "已有一個同步請求在進行中,請稍後再試"
            self.pending = {"id": rid, "force": force}
            self.cv.notify_all()
            ok = self.cv.wait_for(lambda: rid in self.results, timeout=timeout)
            if not ok:
                if self.pending and self.pending["id"] == rid:
                    self.pending = None  # 根本沒人來取
                    return None, "擴充功能沒有回應(Chrome 沒開、或擴充功能沒載入/沒重新載入新版?)"
                return None, "擴充功能已接手但逾時未回報"
            return self.results.pop(rid), ""


bridge = Bridge(PORT, EXT_ID)
mcp = FastMCP("claude-web-sync")


def _read(rel: str) -> str:
    p = OUT / rel
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def _fmt_status(s: dict) -> str:
    lines = [f"{'成功' if s.get('ok') else '失敗'}:{s.get('msg', '')}"]
    if s.get("email"):
        lines.append(f"帳號:{s['email']}")
    if s.get("mode"):
        lines.append(f"讀取方式:{s['mode']}")
    if s.get("changed"):
        lines.append("變更:" + "、".join(s["changed"][:15]) + (" …" if len(s["changed"]) > 15 else ""))
    if s.get("problems"):
        lines.append("問題:" + "；".join(s["problems"]))
    return "\n".join(lines)


@mcp.tool()
async def sync_now(force: bool = False) -> str:
    """叫你自己的 Chrome 擴充功能立刻從 claude.ai 同步(記憶/偏好/Projects/Styles)並回報結果。
    force=True 會忽略「內容沒變就不寫」直接全部重寫。需要 Chrome 開著且已載入擴充功能。"""
    if bridge.server is None:
        return ("橋接埠沒有啟動(多半被另一個 Claude session 佔用)。"
                "可直接用 read_memory 等工具讀取目前本機副本。")
    status, why = await asyncio.to_thread(bridge.request, force)
    return _fmt_status(status) if status is not None else f"同步沒有完成:{why}"


@mcp.tool()
def get_status() -> str:
    """最後同步時間、已同步項目概況,以及擴充功能橋接是否在線。"""
    last = _read("last_sync.txt").strip() or "尚未同步"
    projects = len(list((OUT / "projects").glob("*/instructions.md"))) if (OUT / "projects").is_dir() else 0
    if bridge.server is None:
        link = "橋接未啟動(埠被佔用)"
    elif bridge.last_seen:
        link = f"擴充功能 {int(time.time() - bridge.last_seen)} 秒前連線過"
    else:
        link = "擴充功能尚未連線"
    return (f"輸出資料夾:{OUT}\n最後同步:{last}\n橋接:{link}(埠 {PORT})\n"
            f"memory.md:{'有' if _read('memory.md') else '無'}\n"
            f"preferences.md:{'有' if _read('preferences.md') else '無'}\n"
            f"Projects:{projects}")


@mcp.tool()
def read_memory() -> str:
    """讀取已同步的 claude.ai 記憶(memory.md)。"""
    return _read("memory.md") or "(沒有記憶檔,請先 sync_now)"


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
    bridge.start()
    mcp.run()
