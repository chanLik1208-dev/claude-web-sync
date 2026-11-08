#!/usr/bin/env python3
"""claude-web-sync:把 claude.ai 網頁端的記憶 / 偏好 / Projects 指令與知識庫 / Styles 同步到本機。

跨平台 (Windows / macOS / Linux)。原理:用 Playwright 持久化瀏覽器 profile,
登入一次後,在 claude.ai 頁面內以 fetch 呼叫內部 API(自動帶 cookie、能過 Cloudflare)。

用法:
  pip install playwright          # 有裝 Chrome/Edge 就不用 playwright install
  python claude_web_sync.py login            # 開視窗登入一次
  python claude_web_sync.py sync             # 同步(預設無頭)
  python claude_web_sync.py discover         # 錄下頁面實際打的 API,端點失效時用
  python claude_web_sync.py schedule --every 60   # 安裝定時任務(分鐘)
  python claude_web_sync.py unschedule

注意:這些是 claude.ai 未公開的內部端點,可能隨時改版。每個端點都獨立容錯,
失敗只會跳過並報告,不會中斷其他項目;raw/ 內永遠保留原始 JSON。
"""
import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
DATA = Path(os.environ.get("CLAUDE_SYNC_HOME", HOME / ".claude-web-sync"))
PROFILE = DATA / "profile"
DEFAULT_OUT = Path(os.environ.get("CLAUDE_SYNC_OUT", HOME / "claude-web-sync-data"))
BASE = "https://claude.ai"
TASK_NAME = "ClaudeWebSync"

# 記憶端點不確定,依序嘗試(第一個 200 的採用)
MEMORY_CANDIDATES = [
    "/api/organizations/{org}/memory",
    "/api/organizations/{org}/memory/synthesis",
    "/api/memory",
]
STYLE_CANDIDATES = ["/api/organizations/{org}/list_styles", "/api/organizations/{org}/styles"]


# ───────────────────────── 檔案輸出(變更才寫 + 歷史) ─────────────────────────
class Writer:
    def __init__(self, root: Path):
        self.root = root
        self.stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.changed, self.same = [], 0

    def write(self, rel: str, text: str, history: bool = True):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if not text.endswith("\n"):
            text += "\n"
        if p.exists():
            old = p.read_text(encoding="utf-8", errors="replace")
            if old == text:
                self.same += 1
                return
            if history:
                h = self.root / "history" / (rel + "." + self.stamp)
                h.parent.mkdir(parents=True, exist_ok=True)
                h.write_text(old, encoding="utf-8", newline="\n")
        p.write_text(text, encoding="utf-8", newline="\n")  # newline="\n":避免 Windows 變 CRLF
        self.changed.append(rel)

    def write_json(self, rel: str, obj, history: bool = False):
        self.write(rel, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), history)


def slug(s: str, fallback="untitled") -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(s or "")).strip(" .")
    return (s[:80] or fallback)


# ───────────────────────── 瀏覽器 ─────────────────────────
def launch(pw, headless: bool):
    PROFILE.mkdir(parents=True, exist_ok=True)
    kw = dict(
        user_data_dir=str(PROFILE),
        headless=headless,
        viewport={"width": 1200, "height": 800},
        # 不要加額外旗標:--no-sandbox、--disable-blink-features=AutomationControlled
        # 都會讓 Chrome 跳「不受支援的命令列標幟」警告
        chromium_sandbox=True,  # Playwright 預設會加 --no-sandbox,Chrome 會跳不安全警告
        ignore_default_args=["--enable-automation"],  # 去掉「受自動測試軟體控制」橫幅
    )
    errs = []
    for ch in ("chrome", "msedge", None):  # 優先用系統瀏覽器,免下載 chromium
        try:
            return pw.chromium.launch_persistent_context(**kw, **({"channel": ch} if ch else {}))
        except Exception as e:  # noqa: BLE001
            errs.append(f"[{ch or 'chromium'}] {str(e).splitlines()[0]}")
    sys.exit("無法啟動瀏覽器。若有殘留的登入視窗請先關閉(同一個 profile 不能同時開兩次);"
             "沒有 Chrome 則執行 `playwright install chromium`\n" + "\n".join(errs))


class Api:
    def __init__(self, page):
        self.page = page

    def get(self, path: str):
        """回傳 (status, data)。data 為 JSON 物件,或解析失敗時的文字。"""
        r = self.page.evaluate(
            """async (u) => { try { const r = await fetch(u, {credentials:'include',
                headers:{'accept':'application/json'}}); return {s:r.status, t:await r.text()}; }
                catch(e){ return {s:0, t:String(e)}; } }""",
            path,
        )
        try:
            return r["s"], json.loads(r["t"])
        except Exception:  # noqa: BLE001
            return r["s"], r["t"]


def open_site(ctx):
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(BASE + "/settings/profile", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    return page


# ───────────────────────── login ─────────────────────────
def cmd_login(_a):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = launch(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE + "/login")
        print("請在彈出的視窗登入 claude.ai(最多等 10 分鐘)…")
        api = Api(page)
        for _ in range(300):
            page.wait_for_timeout(2000)
            try:
                if "claude.ai" in page.url and api.get("/api/organizations")[0] == 200:
                    print("登入成功,profile 已保存:", PROFILE)
                    break
            except Exception:  # noqa: BLE001  (換頁瞬間 evaluate 會丟錯)
                pass
        else:
            print("逾時,未偵測到登入")
        ctx.close()


# ───────────────────────── sync ─────────────────────────
def as_list(d, *keys):
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in keys:
            if isinstance(d.get(k), list):
                return d[k]
    return []


def find_strings(obj, pat, path=""):
    """遞迴找 key 符合 pat 的字串值 → [(path, value)]"""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if isinstance(v, str) and v.strip() and re.search(pat, k, re.I):
                out.append((p, v))
            else:
                out += find_strings(v, pat, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += find_strings(v, pat, f"{path}[{i}]")
    return out


def cmd_sync(a):
    from playwright.sync_api import sync_playwright

    out = Path(a.out).expanduser()
    w = Writer(out)
    problems = []

    with sync_playwright() as pw:
        ctx = launch(pw, headless=not a.headed)
        try:
            page = open_site(ctx)
            api = Api(page)

            st, orgs = api.get("/api/organizations")
            if st != 200 or not isinstance(orgs, list):
                sys.exit(f"未登入或被擋 (HTTP {st})。請先執行 login;若是 Cloudflare 擋,試 --headed。")
            w.write_json("raw/organizations.json", orgs)
            cands = [o for o in orgs if "chat" in (o.get("capabilities") or [])] or orgs
            org = next((o for o in orgs if o.get("uuid") == a.org), None) if a.org else cands[0]
            if not org:
                sys.exit("找不到指定的 org uuid")
            oid = org["uuid"]
            print(f"組織:{org.get('name')} ({oid})")

            # 帳號 / 個人偏好
            st, acc = api.get("/api/account")
            if st == 200:
                w.write_json("raw/account.json", acc)
                prefs = find_strings(acc, r"instruction|preference|about|profile|custom")
                if prefs:
                    w.write("preferences.md", "\n\n".join(f"## {k}\n\n{v}" for k, v in prefs))
            else:
                problems.append(f"account: HTTP {st}")

            # 記憶
            mem_done = False
            for tpl in MEMORY_CANDIDATES:
                st, mem = api.get(tpl.format(org=oid))
                if st == 200:
                    w.write_json("raw/memory.json", mem)
                    texts = find_strings(mem, r"^(memory|content|text|summary|synthesis)$")
                    body = "\n\n".join(v for _, v in texts) if texts else \
                        "```json\n" + json.dumps(mem, ensure_ascii=False, indent=2) + "\n```"
                    w.write("memory.md", body)
                    mem_done = True
                    break
            if not mem_done:
                problems.append("memory: 所有候選端點都失敗(執行 discover 找真實端點)")

            # Styles
            for tpl in STYLE_CANDIDATES:
                st, styles = api.get(tpl.format(org=oid))
                if st == 200:
                    w.write_json("raw/styles.json", styles)
                    for s in as_list(styles, "styles", "custom_styles", "data"):
                        if isinstance(s, dict) and s.get("type", "custom") == "custom":
                            body = s.get("prompt") or json.dumps(s, ensure_ascii=False, indent=2)
                            w.write(f"styles/{slug(s.get('name'))}.md", body)
                    break

            # Projects
            st, projs = api.get(f"/api/organizations/{oid}/projects?limit=200")
            if st != 200:
                st, projs = api.get(f"/api/organizations/{oid}/projects")
            if st == 200:
                plist = as_list(projs, "projects", "data")
                w.write_json("raw/projects.json", projs)
                for p in plist:
                    pid, name = p.get("uuid"), p.get("name")
                    base = f"projects/{slug(name)}-{str(pid)[:8]}"
                    head = f"# {name}\n\n{p.get('description') or ''}".rstrip()
                    w.write(f"{base}/instructions.md",
                            (p.get("prompt_template") or "").strip() or "(無自訂指令)")
                    w.write(f"{base}/README.md", head)
                    if a.no_knowledge:
                        continue
                    s2, docs = api.get(f"/api/organizations/{oid}/projects/{pid}/docs")
                    if s2 == 200:
                        for d in as_list(docs, "docs", "data"):
                            fn = slug(d.get("file_name") or d.get("uuid"))
                            w.write(f"{base}/knowledge/{fn}", d.get("content") or "")
                    else:
                        problems.append(f"project {name} docs: HTTP {s2}")
            else:
                problems.append(f"projects: HTTP {st}")
        finally:
            ctx.close()

    w.write("last_sync.txt", datetime.datetime.now().isoformat(timespec="seconds"), history=False)
    print(f"完成:{len(w.changed)} 個檔案有變更、{w.same} 個沒變 → {out}")
    for c in w.changed:
        print("  ~", c)
    for pr in problems:
        print("  !", pr)
    return 1 if problems and not w.changed and not w.same else 0


# ───────────────────────── discover ─────────────────────────
def cmd_discover(a):
    from playwright.sync_api import sync_playwright

    out = Path(a.out).expanduser() / "raw" / "discover"
    out.mkdir(parents=True, exist_ok=True)
    n = [0]

    def on_resp(r):
        try:
            if "/api/" in r.url and r.request.method == "GET" and \
                    "json" in (r.headers.get("content-type") or ""):
                name = slug(re.sub(r"^https?://[^/]+", "", r.url))[:120]
                (out / f"{name}.json").write_text(r.text(), encoding="utf-8")
                n[0] += 1
                print("  +", r.status, r.url)
        except Exception:  # noqa: BLE001
            pass

    with sync_playwright() as pw:
        ctx = launch(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", on_resp)
        for path in ("/settings/profile", "/settings/capabilities", "/projects", "/recents"):
            page.goto(BASE + path, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2500)
        input("可以手動點記憶『查看並編輯』、某個 Project 等,再按 Enter 結束… ")
        ctx.close()
    print(f"共錄到 {n[0]} 個 API 回應 → {out}")


# ───────────────────────── 排程 ─────────────────────────
def sync_cmdline(a):
    py = sys.executable
    if platform.system() == "Windows":  # pythonw 不會跳黑視窗
        pyw = Path(py).with_name("pythonw.exe")
        py = str(pyw) if pyw.exists() else py
    return [py, str(Path(__file__).resolve()), "sync", "--out", str(Path(a.out).expanduser())]


def cmd_schedule(a):
    cmd, mins, sysname = sync_cmdline(a), max(5, a.every), platform.system()
    if sysname == "Windows":
        tr = " ".join(f'"{c}"' for c in cmd)
        subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(mins),
                        "/TN", TASK_NAME, "/TR", tr], check=True)
    elif sysname == "Darwin":
        plist = HOME / "Library/LaunchAgents/com.claude-web-sync.plist"
        args = "".join(f"<string>{c}</string>" for c in cmd)
        plist.write_text(
            '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict>'
            f"<key>Label</key><string>com.claude-web-sync</string>"
            f"<key>ProgramArguments</key><array>{args}</array>"
            f"<key>StartInterval</key><integer>{mins * 60}</integer>"
            "<key>RunAtLoad</key><true/></dict></plist>", encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(plist)], stderr=subprocess.DEVNULL)
        subprocess.run(["launchctl", "load", str(plist)], check=True)
    else:
        line = f"*/{mins} * * * * " + " ".join(f"'{c}'" for c in cmd) + f"  # {TASK_NAME}"
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        cur = "\n".join(l for l in cur.splitlines() if TASK_NAME not in l)
        subprocess.run(["crontab", "-"], input=(cur + "\n" + line + "\n").lstrip("\n"), text=True, check=True)
    print(f"已安裝定時同步,每 {mins} 分鐘一次")


def cmd_unschedule(_a):
    sysname = platform.system()
    if sysname == "Windows":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
    elif sysname == "Darwin":
        plist = HOME / "Library/LaunchAgents/com.claude-web-sync.plist"
        subprocess.run(["launchctl", "unload", str(plist)])
        plist.unlink(missing_ok=True)
    else:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        new = "\n".join(l for l in cur.splitlines() if TASK_NAME not in l) + "\n"
        subprocess.run(["crontab", "-"], input=new, text=True)
    print("已移除定時同步")


def main():
    ap = argparse.ArgumentParser(description="同步 claude.ai 記憶與 prompt 到本機")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="輸出資料夾")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login").set_defaults(f=cmd_login)
    s = sub.add_parser("sync")
    s.add_argument("--headed", action="store_true", help="顯示瀏覽器(被 Cloudflare 擋時用)")
    s.add_argument("--org", help="指定組織 uuid")
    s.add_argument("--no-knowledge", action="store_true", help="不下載專案知識庫檔案")
    s.set_defaults(f=cmd_sync)
    sub.add_parser("discover").set_defaults(f=cmd_discover)
    sc = sub.add_parser("schedule")
    sc.add_argument("--every", type=int, default=60, help="分鐘(最少 5)")
    sc.set_defaults(f=cmd_schedule)
    sub.add_parser("unschedule").set_defaults(f=cmd_unschedule)
    a = ap.parse_args()
    sys.exit(a.f(a) or 0)


if __name__ == "__main__":
    main()
