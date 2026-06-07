"""FastMCP server exposing 21 tools that wrap the WhatsApp bot's HTTP API.

Project-scope tools use $WP_PROJECT_TOKEN; admin-scope tools use
$WP_ADMIN_TOKEN. Bot validation is authoritative — tool arguments are
typed for the agent's benefit, but rejection of bad input happens via
the bot's 400 response and surfaces here as a tool error.

Tools that mint new tokens (`create_project`, `rotate_project_token`)
return the plaintext token exactly once; the agent will see it in
chat. Save it to your password manager before the conversation ends.

DANGER: This server runs with C1 — full admin scope, no guard rails.
`delete_project`, `replace_allowlist`, `set_project_groups`, and
`rotate_project_token` are exposed as raw destructive operations. The
project-scope group tools are equally unguarded: `create_group` mints a
real WhatsApp group and self-grants access to it, `leave_group` disbands
the group for the bot, and `add_participants`/`remove_participants` add
and kick real people. See docs/adr/0001-full-admin-no-guard-rails.md for
rationale.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from wa_multiproject_bot_mcp.client import BotApiError, BotClient

mcp = FastMCP("wa-multiproject-bot")
_client: BotClient | None = None


def _bot() -> BotClient:
    global _client
    if _client is None:
        _client = BotClient()
    return _client


def _call(fn, *args, **kwargs) -> Any:
    """Run a BotClient call; translate BotApiError into a tool error string."""
    try:
        return fn(*args, **kwargs)
    except BotApiError as err:
        raise RuntimeError(str(err)) from err


# ── project-scope tools ─────────────────────────────────────────


@mcp.tool()
def health() -> dict[str, Any]:
    """Liveness check. Returns {ok, paired, projects}. Useful as a connection sanity check.

    `paired` indicates whether the bot's underlying WhatsApp account is connected;
    if false, posts will fail with a 503-equivalent error.
    """
    return _call(_bot().health)


@mcp.tool()
def post_to_group(
    group_jid: str,
    title: str,
    permalink: str | None = None,
    caption: str | None = None,
    media: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Post a Text card (and optional Album of up to 10 Media items) to a WhatsApp group.

    The group_jid must be in this MCP project's access list AND in the global allowlist;
    otherwise the bot returns 403. Use list_my_groups() first to discover valid JIDs.

    Each media item is {"url": "https://...", "type": "image/jpeg" | "video/mp4" | etc.}.
    Videos over 16 MB are transcoded server-side before sending. Omit `media` (or pass [])
    for a text-only Post.

    Args:
        group_jid: Target group, e.g. "1234567890-1612345678@g.us".
        title: Bolded title at the top of the Text card.
        permalink: Optional canonical URL appended to the Text card. Omit when the Post
                   has no associated link (e.g. ad-hoc image sharing).
        caption: Optional plain-text caption between title and permalink.
        media: Optional list of {url, type} dicts; max 10; mixed image/video allowed.

    Returns the bot's success envelope including the actual mediaCount sent.
    """
    body: dict[str, Any] = {"groupJid": group_jid, "title": title}
    if permalink is not None:
        body["permalink"] = permalink
    if caption is not None:
        body["caption"] = caption
    if media is not None:
        body["media"] = media
    return _call(_bot().post, body)


@mcp.tool()
def list_my_groups() -> dict[str, Any]:
    """List the groups *this MCP's project* can post to right now.

    Returns the intersection of the project's access list and the global allowlist,
    each item being {jid, subject}. `subject` may be null if the bot's live group
    cache hasn't fetched it yet.
    """
    return _call(_bot().list_my_groups)


@mcp.tool()
def create_group(
    subject: str,
    participants: list[str],
    announce: bool = False,
    description: str | None = None,
) -> dict[str, Any]:
    """Create a new WhatsApp group and auto-grant this project access to it.

    Unlike every other group tool, this needs no pre-existing permission: after the
    bot creates the group it appends the new JID to BOTH the global allowlist and this
    project's access list, so you can immediately post to and manage it.

    Adding participants by phone number often fails silently due to recipient privacy
    settings — WhatsApp returns a per-JID 403 and the person is NOT added. The robust
    pattern is to create the group, then share the returned invite link so people join
    themselves. The response carries `inviteLink` for exactly this; treat the invite link
    as the primary onboarding path and the participants list as best-effort.

    Args:
        subject: Group name, 1-100 chars (trimmed).
        participants: Phone numbers or JIDs to attempt to add immediately; max 20.
                      Pass [] to create an empty group and onboard via the invite link.
                      A phone number is digits only (7-15); a JID must end in
                      "@s.whatsapp.net" or "@lid".
        announce: If true, set the group to announcement mode (only admins can post).
        description: Optional group description, up to 512 chars.

    Returns {ok, groupJid, subject, inviteCode, inviteLink, participants:[{jid, status,
    added, note?}], settings:{announce}, warnings:[]}. inviteCode/inviteLink may be null
    if that step failed; post-creation step failures land in `warnings` rather than
    failing the whole call.
    """
    return _call(
        _bot().create_group,
        subject,
        participants,
        announce=announce,
        description=description,
    )


@mcp.tool()
def add_participants(group_jid: str, participants: list[str]) -> dict[str, Any]:
    """Add participants to an existing group this project manages.

    The group_jid must be in this project's access list AND the global allowlist, else
    the bot returns 403. Adds are best-effort and per-JID: WhatsApp privacy settings
    routinely block direct adds, in which case that JID comes back with status "403" and
    a note advising you to share the invite link instead (see group_invite_code). Prefer
    the invite-link flow for anyone who isn't reliably addable.

    Args:
        group_jid: Target group, e.g. "1234567890-1612345678@g.us".
        participants: Phone numbers or JIDs to add; 1-20 items. A phone number is digits
                      only (7-15); a JID must end in "@s.whatsapp.net" or "@lid".

    Returns {ok, results:[{jid, status, added, note?}]}. `added` is true only when
    status is "200"; non-200 entries carry a `note` explaining why.
    """
    return _call(_bot().add_participants, group_jid, participants)


@mcp.tool()
def remove_participants(group_jid: str, participants: list[str]) -> dict[str, Any]:
    """DESTRUCTIVE: remove participants from a group this project manages.

    Removed members are kicked immediately and must be re-added or re-invited to return.
    The group_jid must be in this project's access list AND the global allowlist, else
    the bot returns 403.

    Args:
        group_jid: Target group, e.g. "1234567890-1612345678@g.us".
        participants: Phone numbers or JIDs to remove; 1-20 items. A phone number is
                      digits only (7-15); a JID must end in "@s.whatsapp.net" or "@lid".

    Returns {ok, results:[{jid, status, added}]}.
    """
    return _call(_bot().remove_participants, group_jid, participants)


@mcp.tool()
def group_invite_code(group_jid: str) -> dict[str, Any]:
    """Fetch the current invite code and shareable link for a group this project manages.

    This is the recommended way to onboard people whom add_participants/create_group
    could not add directly (recipient privacy/blocked). Share the link and they join
    themselves. The group_jid must be in this project's access list AND the global
    allowlist, else the bot returns 403.

    Args:
        group_jid: Target group, e.g. "1234567890-1612345678@g.us".

    Returns {ok, code, link} where link is "https://chat.whatsapp.com/<code>".
    """
    return _call(_bot().group_invite_code, group_jid)


@mcp.tool()
def revoke_invite(group_jid: str) -> dict[str, Any]:
    """Revoke the current invite link and mint a fresh one for a group this project manages.

    The old link stops working immediately; anyone who hadn't joined yet must use the new
    link. Use this if a link leaked. The group_jid must be in this project's access list
    AND the global allowlist, else the bot returns 403.

    Args:
        group_jid: Target group, e.g. "1234567890-1612345678@g.us".

    Returns {ok, code, link} for the NEW invite, where link is
    "https://chat.whatsapp.com/<code>".
    """
    return _call(_bot().revoke_invite, group_jid)


@mcp.tool()
def set_group_setting(group_jid: str, setting: str) -> dict[str, Any]:
    """Change a group-wide setting for a group this project manages.

    The group_jid must be in this project's access list AND the global allowlist, else
    the bot returns 403.

    Args:
        group_jid: Target group, e.g. "1234567890-1612345678@g.us".
        setting: One of:
                 "announcement"     — only admins can send messages.
                 "not_announcement" — all members can send messages.
                 "locked"           — only admins can edit group info.
                 "unlocked"         — all members can edit group info.

    Returns {ok, setting}.
    """
    return _call(_bot().set_group_setting, group_jid, setting)


@mcp.tool()
def update_subject(group_jid: str, subject: str) -> dict[str, Any]:
    """Rename a group this project manages.

    The group_jid must be in this project's access list AND the global allowlist, else
    the bot returns 403.

    Args:
        group_jid: Target group, e.g. "1234567890-1612345678@g.us".
        subject: New group name, 1-100 chars (trimmed).

    Returns {ok}.
    """
    return _call(_bot().update_subject, group_jid, subject)


@mcp.tool()
def update_description(group_jid: str, description: str) -> dict[str, Any]:
    """Set or clear the description of a group this project manages.

    The group_jid must be in this project's access list AND the global allowlist, else
    the bot returns 403.

    Args:
        group_jid: Target group, e.g. "1234567890-1612345678@g.us".
        description: New description, up to 512 chars. Pass "" to clear it.

    Returns {ok}.
    """
    return _call(_bot().update_description, group_jid, description)


@mcp.tool()
def leave_group(group_jid: str) -> dict[str, Any]:
    """DESTRUCTIVE: leave a group and drop it from this project's access list.

    The paired phone leaves the group (effectively disbanding it for the bot's purposes);
    this is irreversible without a fresh invite. The JID is removed from THIS project's
    access list, but the GLOBAL allowlist is left unchanged — remove it via the admin UI
    if desired. The group_jid must be in this project's access list AND the global
    allowlist, else the bot returns 403.

    Args:
        group_jid: Target group, e.g. "1234567890-1612345678@g.us".

    Returns {ok, leftGroup, removedFromProject, note}.
    """
    return _call(_bot().leave_group, group_jid)


# ── admin-scope tools ───────────────────────────────────────────


@mcp.tool()
def list_projects() -> dict[str, Any]:
    """List all projects on the bot with their per-project group access.

    Returns {projects: [{id, name, createdAt, groupJids}]}. Tokens are never returned.
    """
    return _call(_bot().list_projects)


@mcp.tool()
def create_project(name: str, group_jids: list[str]) -> dict[str, Any]:
    """Create a new project. Returns the project's token PLAINTEXT exactly once.

    The token is never recoverable after this call. Save it immediately. The chat
    transcript will contain it in plaintext.

    Args:
        name: Unique project name (alphanumeric / dash / underscore is safe).
        group_jids: Initial set of allowed group JIDs. Each must be in the global allowlist;
                    otherwise the bot returns 400.
    """
    return _call(_bot().create_project, name, group_jids)


@mcp.tool()
def delete_project(project_id: int) -> dict[str, Any]:
    """DESTRUCTIVE: hard-delete a project and cascade-delete its group access rows.

    The project's token immediately stops working; any consumer Worker using it breaks.
    Message log history is preserved (rows snapshot project_name at write time).

    Args:
        project_id: Numeric id from list_projects().
    """
    return _call(_bot().delete_project, project_id)


@mcp.tool()
def rotate_project_token(project_id: int) -> dict[str, Any]:
    """DESTRUCTIVE: mint a new token for an existing project; old token stops working.

    Returns {token: "..."} in plaintext. Save it immediately. Any consumer Worker
    using the old token will start failing with 401 until you update its secret.

    Args:
        project_id: Numeric id from list_projects().
    """
    return _call(_bot().rotate_project_token, project_id)


@mcp.tool()
def set_project_groups(project_id: int, group_jids: list[str]) -> dict[str, Any]:
    """DESTRUCTIVE: REPLACE a project's allowed group JIDs (does not merge).

    Every JID must be in the global allowlist or the call returns 400. Pass the FULL
    desired list — anything not in the new list is removed from the project's access.

    Args:
        project_id: Numeric id from list_projects().
        group_jids: The complete new set of allowed group JIDs for this project.
    """
    return _call(_bot().set_project_groups, project_id, group_jids)


@mcp.tool()
def list_live_groups() -> dict[str, Any]:
    """List every WhatsApp group the paired phone is currently a member of.

    Each entry is {jid, subject, allowed} where `allowed` indicates whether the group
    is in the global allowlist. Backed by a 60s cache of sock.groupFetchAllParticipating.
    """
    return _call(_bot().list_live_groups)


@mcp.tool()
def get_allowlist() -> dict[str, Any]:
    """Return the current global allowlist as {jids: [...]}.

    The allowlist is the set of group JIDs the bot will accept as send targets at all.
    Any send to a JID not in this set returns 403.
    """
    return _call(_bot().get_allowlist)


@mcp.tool()
def replace_allowlist(jids: list[str]) -> dict[str, Any]:
    """DESTRUCTIVE: REPLACE the entire global allowlist atomically.

    This is a full overwrite, not a merge. Any JID currently in the allowlist but
    NOT in `jids` is removed, which means every project's intersection with the
    allowlist shrinks accordingly. Sends to removed groups start failing with 403.

    Pass the COMPLETE desired allowlist. Call get_allowlist() first to see the
    current state.

    Args:
        jids: The full intended allowlist.
    """
    return _call(_bot().replace_allowlist, jids)


@mcp.tool()
def list_messages(
    project: str | None = None,
    group: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """List recent Post attempts (successful and failed) from the bot's message_log.

    Newest first. Capped at 500 by the bot regardless of limit. Each row includes
    media_count (number of Album items sent), status ('sent' or 'failed'), and an
    optional error string for failed sends.

    Args:
        project: Optional project name filter (exact match).
        group: Optional group JID filter (exact match).
        limit: Optional row cap (1-500; default 100 server-side).
    """
    return _call(_bot().list_messages, project=project, group=group, limit=limit)


# ── entry point ────────────────────────────────────────────────


def main() -> None:
    """Stdio entry point invoked by `wa-multiproject-bot-mcp` or `python -m wa_multiproject_bot_mcp`."""
    mcp.run()


if __name__ == "__main__":
    main()
