# wa-multiproject-bot-mcp

Local-only MCP adapter for the wa-multiproject-bot WhatsApp bot at `https://your-domain.example`. Exposes 28 tools over stdio so Claude Desktop / Claude Code can drive the bot directly — primary use case is "Claude, post this to a WhatsApp group."

Runs entirely locally; never deployed. The bot itself stays where it is on Fly.

## Caveats

- **Full admin scope.** This server holds both the bot's admin token and a per-project token. Every destructive operation (`delete_project`, `replace_allowlist`, `set_project_groups`, `rotate_project_token`) is exposed as a raw tool with no confirmation step. See [`docs/adr/0001-full-admin-no-guard-rails.md`](docs/adr/0001-full-admin-no-guard-rails.md) for the explicit rationale. The web UI at https://your-domain.example/admin remains the careful path for destructive work.
- **Tokens appear in chat.** `create_project` and `rotate_project_token` return plaintext tokens; the agent will surface them in your conversation. Save them to a password manager before the conversation ends.
- **Guard your config — it holds full admin power.** The env config carries your bot's `WP_ADMIN_TOKEN` and a project token, both bearer credentials; anyone who has it can do anything on your bot. Keep it out of version control and never share it.

## One-time setup

1. **Create the dedicated `mcp` project** on the bot via the admin UI at https://your-domain.example/admin:
   - Allowlist tab: tick every group you might want the MCP to post to (or just check that the groups you care about are already allowlisted).
   - Projects tab: "+ New project" → name `mcp` → tick the groups you want this MCP's project to access → Save. Copy the token from the one-time modal.

2. **Install `uv`** if you don't have it: `brew install uv`.

3. **Editable-install the package** so future source edits are picked up live without rebuilds:

   ```bash
   uv tool install --editable .
   ```

   This puts the binary at `~/.local/bin/wa-multiproject-bot-mcp`. Edits to `src/wa_multiproject_bot_mcp/*.py` are picked up the next time the MCP server spawns (no `uv tool install` rerun needed unless you change `pyproject.toml`).

   **Why not `uvx --from <path>`?** `uvx` caches the built package and won't rebuild on subsequent spawns unless you pass `--refresh`. Editable install gives you live source for ~zero spawn overhead.

## Claude Desktop registration

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) and add:

```json
{
  "mcpServers": {
    "wa-multiproject-bot": {
      "command": "~/.local/bin/wa-multiproject-bot-mcp",
      "env": {
        "WP_BASE_URL": "https://your-domain.example",
        "WP_ADMIN_TOKEN": "<paste your ADMIN_TOKEN here>",
        "WP_PROJECT_TOKEN": "<paste the mcp project token from step 1>"
      }
    }
  }
}
```

Restart Claude Desktop. The `wa-multiproject-bot` server should appear in the MCP indicator at the bottom-left of a new chat window.

## Claude Code registration

Easiest path is the CLI:

```bash
claude mcp add wa-multiproject-bot \
  --scope user \
  -e WP_BASE_URL=https://your-domain.example \
  -e WP_ADMIN_TOKEN=<paste> \
  -e WP_PROJECT_TOKEN=<paste> \
  -- ~/.local/bin/wa-multiproject-bot-mcp
```

`--scope user` makes it available in every Claude Code session, regardless of working directory. Verify with `claude mcp list`.

If you prefer JSON, write `~/.claude.json` directly with an `mcpServers.wa-multiproject-bot` entry mirroring the Desktop format above.

Restart any existing Claude Code session before the tools become available — mid-session registration isn't hot-loaded.

## Tools

| Tool | Wraps | Scope |
|---|---|---|
| `health` | `GET /` | project |
| `post_to_group` | `POST /v1/post` | project |
| `list_my_groups` | `GET /v1/groups` | project |
| `list_projects` | `GET /admin/api/projects` | admin |
| `create_project` | `POST /admin/api/projects` | admin |
| `delete_project` | `DELETE /admin/api/projects/{id}` | admin |
| `rotate_project_token` | `POST /admin/api/projects/{id}/rotate-token` | admin |
| `set_project_groups` | `PUT /admin/api/projects/{id}/groups` | admin |
| `list_live_groups` | `GET /admin/api/groups` | admin |
| `get_allowlist` | `GET /admin/api/allowlist` | admin |
| `replace_allowlist` | `PUT /admin/api/allowlist` | admin |
| `list_messages` | `GET /admin/api/messages` | admin |
| `create_group` | `POST /v1/groups/create` | project |
| `add_participants` | `POST /v1/groups/participants/add` | project |
| `remove_participants` | `POST /v1/groups/participants/remove` | project |
| `group_invite_code` | `POST /v1/groups/invite/code` | project |
| `revoke_invite` | `POST /v1/groups/invite/revoke` | project |
| `set_group_setting` | `POST /v1/groups/setting` | project |
| `update_subject` | `POST /v1/groups/subject` | project |
| `update_description` | `POST /v1/groups/description` | project |
| `leave_group` | `POST /v1/groups/leave` | project |
| `send_dm` | `POST /v1/dm` | project |
| `send_dm_batch` | `POST /v1/dm/batch` | project |
| `dm_status` | `GET /v1/dm/status` | project |
| `dm_cancel` | `POST /v1/dm/cancel` | project |
| `get_dm_allowlist` | `GET /admin/api/dm-allowlist` | admin |
| `set_dm_allowlist` | `PUT /admin/api/dm-allowlist` | admin |
| `dm_resume` | `POST /admin/api/dm/resume` | admin |

Validation is delegated to the bot — type hints in tool signatures are for the agent's benefit; bad input produces a 400 from the bot that surfaces as the tool's error text.

### Group management (project scope)

The nine group tools use `WP_PROJECT_TOKEN` (permission parity with `post_to_group`) — see [`docs/adr/0002-group-management-tools.md`](docs/adr/0002-group-management-tools.md):

- `create_group(subject, participants, announce, description)` — creates a group and **auto-grants** this project access to it (appends the new JID to the allowlist *and* the project's group access), returning the invite link.
- `add_participants(group_jid, participants)` — adds up to 20 people; returns a per-JID status so privacy-blocked adds (403/408) surface a note to share the invite link instead of failing the batch.
- `remove_participants(group_jid, participants)` — removes up to 20 people (destructive).
- `group_invite_code(group_jid)` — fetches the current invite code + `https://chat.whatsapp.com/<code>` link.
- `revoke_invite(group_jid)` — rotates the code, killing the old link.
- `set_group_setting(group_jid, setting)` — `announcement`/`not_announcement` (admins-only posting) or `locked`/`unlocked` (admins-only info edit).
- `update_subject(group_jid, subject)` — renames the group.
- `update_description(group_jid, description)` — sets/clears the description.
- `leave_group(group_jid)` — leaves the group and detaches it from this project; the global allowlist is left untouched (destructive).

### Direct messages (1:1 — HIGH ban-risk)

DMs are opt-in and deliberately constrained — the recommended distribution path is still an invite-link group post. Recipients must be on the DM allowlist; per-minute/per-day caps and randomized jitter are enforced server-side and **cannot be raised by the caller**. See [`docs/adr/0003-dm-tools.md`](docs/adr/0003-dm-tools.md).

- `send_dm(to_jid, text)` — one 1:1 DM (project scope). 403 if the recipient isn't allowlisted; a 2xx means accepted-for-send, not guaranteed delivered.
- `send_dm_batch(recipients, text)` — enqueues up to 50 DMs to drain in the background; non-allowlisted recipients come back in `skipped[]`. Returns a `batchId`.
- `dm_status(batch_id?)` — pause state, pending count, recent volume, and the caps (read-only); pass `batch_id` for that batch's counts.
- `dm_cancel(batch_id)` — marks still-pending rows in a batch as skipped.
- `get_dm_allowlist()` / `set_dm_allowlist(jids)` — read / **replace** the recipient allowlist (admin scope).
- `dm_resume()` — clears the auto-pause after a WhatsApp restriction (**admin scope on purpose** — a project token / automated loop must not be able to re-hammer a restricted account).

## Sanity check

After registering, ask Claude:

> Call `health` from the wa-multiproject-bot MCP server.

You should see `{"ok": true, "paired": true, "projects": N}` (N being however many projects you have on the bot).

## Updating

Editable install picks up source edits in `src/wa_multiproject_bot_mcp/*.py` immediately — the next time Claude spawns the MCP server (i.e. starting a fresh Claude Code session or restarting Claude Desktop), the new code is used. No `uv tool install` rerun needed.

After `pyproject.toml` dependency changes:

```bash
uv tool install --editable . --force
```

`--force` reinstalls the entry into the existing tool venv, picking up the dependency change.
