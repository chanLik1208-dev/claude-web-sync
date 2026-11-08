#!/usr/bin/env python3
"""SessionStart hook:把已同步的 claude.ai 自訂指令 + 記憶塞進新 session 的開頭 context。

由 ~/.claude/settings.json 的 hooks.SessionStart 呼叫。只讀本機檔案、不連網、很快。
任何錯誤都靜默結束(exit 0、不輸出),絕不能因為這個 hook 擋住 session 啟動。
"""
import datetime
import json
import sys
from pathlib import Path

MAX_CHARS = 8000   # hook 注入的 context 有上限,保守一點
STALE_DAYS = 3


def read(out: Path, rel: str) -> str:
    p = out / rel
    return p.read_text(encoding="utf-8", errors="replace").strip() if p.is_file() else ""


def age_note(out: Path) -> str:
    raw = read(out, "last_sync.txt")
    if not raw:
        return "同步時間未知"
    try:
        t = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.astimezone()  # 視為本地時間
        days = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 86400
        s = f"{t.astimezone():%Y-%m-%d %H:%M} 同步"
        return s + (f"(已 {int(days)} 天沒更新,可呼叫 sync_now)" if days >= STALE_DAYS else "")
    except Exception:  # noqa: BLE001
        return f"最後同步:{raw}"


def build(out: Path) -> str:
    prefs, mem = read(out, "preferences.md"), read(out, "memory.md")
    # 沒內容或只有佔位訊息就不要吵 context
    if mem.startswith("(目前沒有記憶內容"):
        mem = ""
    if prefs.startswith("(沒有自訂指令"):
        prefs = ""
    projects = sorted(p.name for p in (out / "projects").iterdir() if p.is_dir()) if (out / "projects").is_dir() else []
    if not (prefs or mem or projects):
        return ""

    parts = [f"# 使用者在 claude.ai 網頁端的記憶與自訂指令({age_note(out)})",
             "以下是使用者自己 claude.ai 帳號裡的內容,由 claude-web-sync 同步到本機,作為背景參考;"
             "若與本次對話中使用者的明確指示衝突,以對話中的指示為準。"]
    if prefs:
        parts += ["## 自訂指令", prefs]
    if mem:
        parts += ["## 記憶", mem]
    if projects:
        parts += ["## 已同步的 Projects", "、".join(projects) + "(用 MCP 的 read_project 讀取)"]
    text = "\n\n".join(parts)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rstrip() + "\n\n…(內容過長已截斷;完整內容請用 MCP 的 read_memory / read_preferences)"
    return text


def main() -> None:
    try:
        sys.stdin.read()  # 吃掉 hook 輸入(JSON),避免對方寫入時 broken pipe
    except Exception:  # noqa: BLE001
        pass
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cws_common import resolve_out

    ctx = build(resolve_out())
    if not ctx:
        return
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}},
              sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)
