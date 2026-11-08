# claude-web-sync

Sync your **claude.ai** web memory, preferences, Project instructions/knowledge and Styles to local files — Windows / macOS / Linux. Includes an MCP server so Claude Code can read and trigger the sync.

> Uses claude.ai's **undocumented internal endpoints** through a logged-in Playwright browser profile. They may change at any time; each item fails independently and raw JSON is always kept in `raw/`. Use only on your own account and mind the ToS.

## Setup
```
pip install -r requirements.txt     # uses installed Chrome/Edge; else: playwright install chromium
python claude_web_sync.py login     # sign in once in the popup window
python claude_web_sync.py sync      # -> ~/claude-web-sync-data
python claude_web_sync.py schedule --every 60   # schtasks / launchd / cron
```
If an endpoint stops working: `python claude_web_sync.py discover` records the API calls the site actually makes into `raw/discover/`.

## Output
`memory.md`, `preferences.md`, `projects/<name>/{instructions.md,knowledge/}`, `styles/`, `raw/*.json`, `history/` (previous versions, written only on change).

## MCP server
```
claude mcp add claude-web-sync -- python /path/to/mcp_server.py
```
Tools: `sync_now`, `get_status`, `read_memory`, `read_preferences`, `list_projects`, `read_project`, `search_synced`.

## Security
Your session cookie lives in `~/.claude-web-sync/profile` (outside this repo). Never commit or cloud-sync it.
