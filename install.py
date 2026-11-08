#!/usr/bin/env python3
"""一鍵安裝(每台電腦跑一次):把 SessionStart hook 合併進 ~/.claude/settings.json,並註冊 MCP server。

  python install.py               # 安裝
  python install.py --uninstall   # 移除 hook 與 MCP 註冊
  python install.py --no-mcp      # 只裝 hook
  python install.py --settings-file PATH   # 指定設定檔(測試用)

hook 用「exec 形式」(command 只放 python 路徑、args 放腳本路徑,不經 shell),
所以 Windows 的 PowerShell / Git Bash、macOS 的 zsh 都不會被引號或反斜線搞壞。
重複執行是安全的(會更新舊條目,不會重複加)。已存在的其他設定一律保留。
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = (HERE / "session_hook.py").as_posix()
MCP = (HERE / "mcp_server.py").as_posix()
PY = Path(sys.executable).as_posix()
MARK = "session_hook.py"
MATCHER = "startup|resume|clear|compact"


def is_ours(h: dict) -> bool:
    return MARK in (h.get("command", "") + " ".join(h.get("args", []) or []))


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"{path} 不是合法 JSON({e}),為免弄壞它,已中止。請先修好或移走該檔。")
    if not isinstance(data, dict):
        sys.exit(f"{path} 的最外層不是物件,已中止。")
    return data


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def install_hook(path: Path) -> str:
    data = load(path)
    groups = data.setdefault("hooks", {}).setdefault("SessionStart", [])
    entry = {"type": "command", "command": PY, "args": [HOOK], "timeout": 10,
             "statusMessage": "載入 claude.ai 記憶…"}
    for g in groups:                       # 已有我們的條目 → 更新(路徑可能變了)
        for i, h in enumerate(g.get("hooks", [])):
            if is_ours(h):
                g["hooks"][i] = entry
                g["matcher"] = MATCHER
                save(path, data)
                return "已更新既有的 SessionStart hook"
    groups.append({"matcher": MATCHER, "hooks": [entry]})
    save(path, data)
    return "已新增 SessionStart hook"


def uninstall_hook(path: Path) -> str:
    if not path.exists():
        return "設定檔不存在,沒有要移除的"
    data = load(path)
    groups = data.get("hooks", {}).get("SessionStart", [])
    removed = 0
    for g in groups:
        before = len(g.get("hooks", []))
        g["hooks"] = [h for h in g.get("hooks", []) if not is_ours(h)]
        removed += before - len(g["hooks"])
    data.get("hooks", {})["SessionStart"] = [g for g in groups if g.get("hooks")]
    if not data.get("hooks", {}).get("SessionStart"):
        data.get("hooks", {}).pop("SessionStart", None)
    if "hooks" in data and not data["hooks"]:
        data.pop("hooks")
    if removed:
        save(path, data)
    return f"已移除 {removed} 個 hook"


def mcp(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["claude", "mcp", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def install_mcp() -> str:
    if shutil.which("claude") is None:
        return "找不到 claude 指令,略過 MCP 註冊(手動:claude mcp add claude-web-sync -s user -- python mcp_server.py)"
    mcp(["remove", "claude-web-sync", "-s", "user"])  # 不存在會失敗,忽略;存在則重註冊(路徑可能變了)
    r = mcp(["add", "claude-web-sync", "-s", "user", "--", PY, MCP])
    return "已註冊 MCP server" if r.returncode == 0 else f"MCP 註冊失敗:{(r.stderr or r.stdout).strip()[:200]}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-mcp", action="store_true")
    ap.add_argument("--settings-file", default=str(Path.home() / ".claude" / "settings.json"))
    a = ap.parse_args()
    path = Path(a.settings_file).expanduser()

    if a.uninstall:
        print("hook:", uninstall_hook(path))
        if not a.no_mcp and shutil.which("claude"):
            r = mcp(["remove", "claude-web-sync", "-s", "user"])
            print("MCP:", "已移除" if r.returncode == 0 else "沒有註冊或移除失敗")
        return
    print("hook:", install_hook(path), f"→ {path}")
    if not a.no_mcp:
        print("MCP:", install_mcp())
    print("\n接下來:1) 在 Chrome 載入 extension/ 並登入 claude.ai,按一次「立即同步」;2) 開新的 Claude Code session。")


if __name__ == "__main__":
    main()
