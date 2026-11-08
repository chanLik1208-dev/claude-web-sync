# claude-web-sync

Save your **claude.ai** custom instructions, memory, Project instructions/knowledge and Styles to local files — Windows / macOS / Linux. Includes an MCP server so Claude Code can read them.

> Uses claude.ai's **undocumented internal endpoints** with your own logged-in session. They may change at any time; each item fails independently and raw JSON is always kept in `raw/`. Use only on your own account and mind the ToS.

## Recommended: Chrome extension (`extension/`)
Runs inside **your own, already-logged-in Chrome** — no automation, nothing to log in again, nothing to install on the machine except the extension. Works the same on any computer.

1. `chrome://extensions` → enable **Developer mode** → **Load unpacked** → pick the `extension/` folder.
2. Make sure that Chrome is signed in to the claude.ai account you want.
3. Click the extension icon → **立即同步**. After that it syncs every N minutes (default 60).
4. Files land in `Downloads/claude-web-sync/`. Turn off Chrome's "Ask where to save each file".

Files are only rewritten when content changes; the previous version goes to `history/<path>.<timestamp>`.

## Optional: Playwright CLI (`claude_web_sync.py`)
A headless-capable CLI that drives its own browser profile. Cloudflare may challenge automated browsers, in which case use the extension instead.
```
pip install -r requirements.txt
python claude_web_sync.py login | sync | discover | schedule --every 60 | unschedule
```

## Output
`memory.md`, `preferences.md`, `projects/<name>/{instructions.md,knowledge/}`, `styles/`, `raw/*.json`, `history/`.

## MCP server
```
claude mcp add claude-web-sync -s user -- python /path/to/mcp_server.py
```
Reads `<Downloads>/claude-web-sync` (or `$CLAUDE_SYNC_OUT`). Tools: `sync_now`, `get_status`, `read_memory`, `read_preferences`, `list_projects`, `read_project`, `search_synced`.

**`sync_now` asks your own Chrome to sync.** The MCP server opens a tiny HTTP port on `127.0.0.1:47821` (`CLAUDE_SYNC_PORT`); the extension long-polls it every 30 s, runs the sync in your logged-in browser when asked, and posts the result back. Chrome must be open with the extension loaded; set the same port in the extension popup if you change it.
Only requests with `Origin: chrome-extension://…`, an `X-CWS` header and a localhost `Host` are accepted (pin your extension ID with `CLAUDE_SYNC_EXT_ID`). If another Claude session already owns the port, `sync_now` says so and the read tools still work on the local copy.

## Security
The extension holds no credentials: it reuses the browser's own session. The CLI's profile (`~/.claude-web-sync/profile`) contains a session cookie — never commit or cloud-sync it.
