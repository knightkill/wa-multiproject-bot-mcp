"""FastMCP server exposing 31 tools that wrap the WhatsApp bot's HTTP API.

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

Every tool takes an optional `account`: the name of one of the bot's extra
WhatsApp numbers (see list_accounts). Omit it for the original number.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from wa_multiproject_bot_mcp.client import BotApiError, BotClient

mcp = FastMCP("wa-multiproject-bot")
_clients: dict[str | None, BotClient] = {}


def _bot(account: str | None = None) -> BotClient:
    """Client for one WhatsApp number. None is the bot's original number."""
    account = account or None
    if account not in _clients:
        _clients[account] = BotClient(account=account)
    return _clients[account]


def _call(fn, *args, **kwargs) -> Any:
    """Run a BotClient call; translate BotApiError into a tool error string."""
    try:
        return fn(*args, **kwargs)
    except BotApiError as err:
        raise RuntimeError(str(err)) from err


# ── project-scope tools ─────────────────────────────────────────


@mcp.tool()
def health(account: str | None = None) -> dict[str, Any]:
    """Liveness check. Returns {ok, paired, projects}. Useful as a connection sanity check.

    `paired` indicates whether the bot's underlying WhatsApp account is connected;
    if false, posts will fail with a 503-equivalent error.
    """
    return _call(_bot(account).health)


@mcp.tool()
def post_to_group(
    group_jid: str,
    title: str,
    permalink: str | None = None,
    caption: str | None = None,
    media: list[dict[str, str]] | None = None,
    account: str | None = None,
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
    return _call(_bot(account).post, body)


@mcp.tool()
def list_my_groups(account: str | None = None) -> dict[str, Any]:
    """List the groups *this MCP's project* can post to right now.

    Returns the intersection of the project's access list and the global allowlist,
    each item being {jid, subject}. `subject` may be null if the bot's live group
    cache hasn't fetched it yet.
    """
    return _call(_bot(account).list_my_groups)


@mcp.tool()
def create_group(
    subject: str,
    participants: list[str],
    announce: bool = False,
    description: str | None = None,
    account: str | None = None,
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
        _bot(account).create_group,
        subject,
        participants,
        announce=announce,
        description=description,
    )


@mcp.tool()
def add_participants(group_jid: str, participants: list[str], account: str | None = None) -> dict[str, Any]:
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
    return _call(_bot(account).add_participants, group_jid, participants)


@mcp.tool()
def remove_participants(group_jid: str, participants: list[str], account: str | None = None) -> dict[str, Any]:
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
    return _call(_bot(account).remove_participants, group_jid, participants)


@mcp.tool()
def group_invite_code(group_jid: str, account: str | None = None) -> dict[str, Any]:
    """Fetch the current invite code and shareable link for a group this project manages.

    This is the recommended way to onboard people whom add_participants/create_group
    could not add directly (recipient privacy/blocked). Share the link and they join
    themselves. The group_jid must be in this project's access list AND the global
    allowlist, else the bot returns 403.

    Args:
        group_jid: Target group, e.g. "1234567890-1612345678@g.us".

    Returns {ok, code, link} where link is "https://chat.whatsapp.com/<code>".
    """
    return _call(_bot(account).group_invite_code, group_jid)


@mcp.tool()
def revoke_invite(group_jid: str, account: str | None = None) -> dict[str, Any]:
    """Revoke the current invite link and mint a fresh one for a group this project manages.

    The old link stops working immediately; anyone who hadn't joined yet must use the new
    link. Use this if a link leaked. The group_jid must be in this project's access list
    AND the global allowlist, else the bot returns 403.

    Args:
        group_jid: Target group, e.g. "1234567890-1612345678@g.us".

    Returns {ok, code, link} for the NEW invite, where link is
    "https://chat.whatsapp.com/<code>".
    """
    return _call(_bot(account).revoke_invite, group_jid)


@mcp.tool()
def set_group_setting(group_jid: str, setting: str, account: str | None = None) -> dict[str, Any]:
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
    return _call(_bot(account).set_group_setting, group_jid, setting)


@mcp.tool()
def update_subject(group_jid: str, subject: str, account: str | None = None) -> dict[str, Any]:
    """Rename a group this project manages.

    The group_jid must be in this project's access list AND the global allowlist, else
    the bot returns 403.

    Args:
        group_jid: Target group, e.g. "1234567890-1612345678@g.us".
        subject: New group name, 1-100 chars (trimmed).

    Returns {ok}.
    """
    return _call(_bot(account).update_subject, group_jid, subject)


@mcp.tool()
def update_description(group_jid: str, description: str, account: str | None = None) -> dict[str, Any]:
    """Set or clear the description of a group this project manages.

    The group_jid must be in this project's access list AND the global allowlist, else
    the bot returns 403.

    Args:
        group_jid: Target group, e.g. "1234567890-1612345678@g.us".
        description: New description, up to 512 chars. Pass "" to clear it.

    Returns {ok}.
    """
    return _call(_bot(account).update_description, group_jid, description)


@mcp.tool()
def leave_group(group_jid: str, account: str | None = None) -> dict[str, Any]:
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
    return _call(_bot(account).leave_group, group_jid)


# ── project-scope DM tools (1:1 direct messages — HIGH BAN-RISK) ─


_DM_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
_DM_MEDIA_MAX_BYTES = 5 * 1024 * 1024


@mcp.tool()
def send_dm(
    to_jid: str,
    text: str | None = None,
    file_path: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Send ONE 1:1 direct message, optionally with an image or PDF. HIGH BAN-RISK — prefer an invite-link group post.

    Direct-messaging an unsolicited recipient is the fastest way to get the paired
    number flagged. The recipient must be on the DM allowlist (admin-managed via
    set_dm_allowlist), else the bot returns 403. Sends are throttled server-side
    (per-minute/per-day caps + randomized jitter) and the rate cannot be raised here.

    Args:
        to_jid: Recipient user JID, e.g. "6191234567@s.whatsapp.net".
        text: Message body (max 4096 chars); the caption when file_path is given.
        file_path: Optional local file to attach — .jpg/.jpeg/.png/.webp (sent as an
            image) or .pdf (sent as a document), max 5 MB. Read on this machine and
            uploaded inline; it is not hosted anywhere.

    At least one of text or file_path is required.

    Returns {ok, toJid, status}. A successful return means accepted for send, NOT
    guaranteed delivered — a WhatsApp restriction can surface a beat later.
    """
    if not text and not file_path:
        raise ValueError("text or file_path is required")
    media = None
    if file_path:
        path = Path(file_path).expanduser()
        mimetype = _DM_MEDIA_TYPES.get(path.suffix.lower())
        if mimetype is None:
            raise ValueError(f"unsupported file type {path.suffix!r}; use {sorted(_DM_MEDIA_TYPES)}")
        data = path.read_bytes()
        if len(data) > _DM_MEDIA_MAX_BYTES:
            raise ValueError(f"{path.name} is {len(data)} bytes; the limit is {_DM_MEDIA_MAX_BYTES}")
        media = {
            "data": base64.b64encode(data).decode("ascii"),
            "mimetype": mimetype,
            "filename": path.name,
        }
    return _call(_bot(account).send_dm, to_jid, text, media)


@mcp.tool()
def send_dm_batch(recipients: list[str], text: str, account: str | None = None) -> dict[str, Any]:
    """Enqueue up to 50 1:1 DMs to drain in the background. HIGH BAN-RISK.

    Nothing is sent synchronously: the queue drains with server-side caps + jitter the
    caller cannot raise. Recipients not on the DM allowlist are returned in skipped[]
    and never sent. Duplicate (batch, recipient) pairs are de-duplicated. Prefer an
    invite-link group post for distribution.

    Args:
        recipients: Up to 50 user JIDs (…@s.whatsapp.net or @lid).
        text: Message body (max 4096 chars).

    Returns {ok, batchId, enqueued, duplicates, skipped}. Keep batchId to poll
    dm_status(batch_id=…) or stop it with dm_cancel(batch_id=…).
    """
    return _call(_bot(account).send_dm_batch, recipients, text)


@mcp.tool()
def dm_status(batch_id: str | None = None, account: str | None = None) -> dict[str, Any]:
    """DM sender status: pause state, pending count, recent volume, caps (read-only).

    Pass batch_id to also get that batch's pending/sent/failed/skipped counts. Caps are
    server-side and cannot be changed via the API.
    """
    return _call(_bot(account).dm_status, batch_id)


@mcp.tool()
def dm_cancel(batch_id: str, account: str | None = None) -> dict[str, Any]:
    """Cancel a DM batch: mark every still-pending row skipped (already-sent rows untouched).

    Args:
        batch_id: The batchId returned by send_dm_batch.

    Returns {ok, cancelled}.
    """
    return _call(_bot(account).dm_cancel, batch_id)


# ── admin-scope tools ───────────────────────────────────────────


@mcp.tool()
def list_projects(account: str | None = None) -> dict[str, Any]:
    """List all projects on the bot with their per-project group access.

    Returns {projects: [{id, name, createdAt, groupJids}]}. Tokens are never returned.
    """
    return _call(_bot(account).list_projects)


@mcp.tool()
def create_project(name: str, group_jids: list[str], account: str | None = None) -> dict[str, Any]:
    """Create a new project. Returns the project's token PLAINTEXT exactly once.

    The token is never recoverable after this call. Save it immediately. The chat
    transcript will contain it in plaintext.

    Args:
        name: Unique project name (alphanumeric / dash / underscore is safe).
        group_jids: Initial set of allowed group JIDs. Each must be in the global allowlist;
                    otherwise the bot returns 400.
    """
    return _call(_bot(account).create_project, name, group_jids)


@mcp.tool()
def delete_project(project_id: int, account: str | None = None) -> dict[str, Any]:
    """DESTRUCTIVE: hard-delete a project and cascade-delete its group access rows.

    The project's token immediately stops working; any consumer Worker using it breaks.
    Message log history is preserved (rows snapshot project_name at write time).

    Args:
        project_id: Numeric id from list_projects().
    """
    return _call(_bot(account).delete_project, project_id)


@mcp.tool()
def rotate_project_token(project_id: int, account: str | None = None) -> dict[str, Any]:
    """DESTRUCTIVE: mint a new token for an existing project; old token stops working.

    Returns {token: "..."} in plaintext. Save it immediately. Any consumer Worker
    using the old token will start failing with 401 until you update its secret.

    Args:
        project_id: Numeric id from list_projects().
    """
    return _call(_bot(account).rotate_project_token, project_id)


@mcp.tool()
def set_project_groups(project_id: int, group_jids: list[str], account: str | None = None) -> dict[str, Any]:
    """DESTRUCTIVE: REPLACE a project's allowed group JIDs (does not merge).

    Every JID must be in the global allowlist or the call returns 400. Pass the FULL
    desired list — anything not in the new list is removed from the project's access.

    Args:
        project_id: Numeric id from list_projects().
        group_jids: The complete new set of allowed group JIDs for this project.
    """
    return _call(_bot(account).set_project_groups, project_id, group_jids)


@mcp.tool()
def list_live_groups(account: str | None = None) -> dict[str, Any]:
    """List every WhatsApp group the paired phone is currently a member of.

    Each entry is {jid, subject, allowed} where `allowed` indicates whether the group
    is in the global allowlist. Backed by a 60s cache of sock.groupFetchAllParticipating.
    """
    return _call(_bot(account).list_live_groups)


@mcp.tool()
def get_allowlist(account: str | None = None) -> dict[str, Any]:
    """Return the current global allowlist as {jids: [...]}.

    The allowlist is the set of group JIDs the bot will accept as send targets at all.
    Any send to a JID not in this set returns 403.
    """
    return _call(_bot(account).get_allowlist)


@mcp.tool()
def replace_allowlist(jids: list[str], account: str | None = None) -> dict[str, Any]:
    """DESTRUCTIVE: REPLACE the entire global allowlist atomically.

    This is a full overwrite, not a merge. Any JID currently in the allowlist but
    NOT in `jids` is removed, which means every project's intersection with the
    allowlist shrinks accordingly. Sends to removed groups start failing with 403.

    Pass the COMPLETE desired allowlist. Call get_allowlist() first to see the
    current state.

    Args:
        jids: The full intended allowlist.
    """
    return _call(_bot(account).replace_allowlist, jids)


@mcp.tool()
def list_messages(
    project: str | None = None,
    group: str | None = None,
    limit: int | None = None,
    account: str | None = None,
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
    return _call(_bot(account).list_messages, project=project, group=group, limit=limit)


# ── admin-scope inbound tools ────────────────────────────────────
# Capture is opt-in per chat and prospective only: the bot stores a
# message only if it arrives after the chat's jid is in the readable-
# chats set. Adding a jid today does NOT backfill its prior history.


@mcp.tool()
def list_readable_chats(account: str | None = None) -> dict[str, Any]:
    """List chats the bot is currently capturing inbound messages from.

    This is the opt-in gate for list_inbound: a chat's messages are only stored if its
    jid is in this set at the time the message arrives. Returns {chats: [{jid, kind,
    label, addedAt, subject}]}. `subject` is the live WhatsApp group name when available
    (groups only); null otherwise.
    """
    return _call(_bot(account).list_readable_chats)


@mcp.tool()
def set_readable_chats(chats: list[dict[str, Any]], account: str | None = None) -> dict[str, Any]:
    """DESTRUCTIVE: REPLACE the entire readable-chats set (not additive).

    Any jid currently readable but NOT in `chats` stops being captured. Turning capture
    on for a jid is NOT retroactive — only messages that arrive after this call are
    stored; there is no history backfill. Call list_readable_chats() first and pass the
    complete desired set (existing entries + any you're adding).

    Args:
        chats: Full desired set, each item {"jid": str, "kind": "group" | "person",
               "label": str (optional, 1-80 chars)}. kind must match the jid suffix
               (group ↔ "@g.us", person ↔ "@s.whatsapp.net" or "@lid"); duplicate jids
               are rejected with a 400.

    Returns {ok}.
    """
    return _call(_bot(account).set_readable_chats, chats)


@mcp.tool()
def list_inbound(
    jid: str | None = None,
    since: int | None = None,
    from_me: bool | None = None,
    limit: int | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """List recent inbound messages captured from readable chats (see list_readable_chats).

    Newest first by send time. Media messages carry media_type, media_mime and
    media_saved (1 = file already on the bot); fetch the file with get_media(id). A jid that was never added via set_readable_chats returns an empty
    list even if the bot is a member of that chat — capture must be explicitly opted in,
    and only messages received after opt-in are stored.

    Args:
        jid: Optional chat JID filter, e.g. "1234567890-1612345678@g.us".
        since: Optional cursor — pass the previous response's nextCursor to get only
               messages newer than that poll.
        from_me: Optional filter; true for messages the bot's own account sent, false
                 for messages received from others.
        limit: Optional row cap (1-500; default 100 server-side).

    Returns {messages: [{id, wa_id, chat_jid, sender_jid, sender_name, chat_name,
    from_me, timestamp, text, media_type, quoted_wa_id, media_mime, media_saved}],
    nextCursor}. sender_name / chat_name are contact names (see find_contact), null
    when unknown; chat_name is null for groups. nextCursor is the highest id in this batch
    (null if empty) — pass it as `since` on the next poll.
    """
    return _call(_bot(account).list_inbound, jid=jid, since=since, from_me=from_me, limit=limit)


# ── admin-scope contact tools ───────────────────────────────────


@mcp.tool()
def find_contact(query: str | None = None, limit: int | None = None, account: str | None = None) -> dict[str, Any]:
    """ADMIN: look up people by name or number in the bot's contact names.

    Names come from three sources kept side by side: the operator's WhatsApp address
    book, their Google Contacts (pushed hourly by an Apps Script), and the pushName the
    person set on their own profile. `name` is the one to show, picked in that order;
    `source` says which. Matching is a case-insensitive substring over every name,
    Google alias (other spellings from duplicate Google entries) and the JID digits.

    Use this to turn "DM Joshil" into a JID, then pass that JID explicitly to send_dm /
    add_participants. If several contacts match, ask the user which one — never guess:
    a wrong DM recipient is not undoable. A match does NOT mean the person is on the DM
    allowlist (see get_dm_allowlist).

    Args:
        query: Substring to match; omit to list contacts alphabetically.
        limit: Max rows (1-200; default 20).

    Returns {contacts: [{jid, lids, name, source, whatsappName, googleName,
    googleAliases, pushName}], counts: {total, whatsapp, google, pushname, lids}}.
    """
    return _call(_bot(account).find_contact, query, limit)


@mcp.tool()
def resync_contacts(account: str | None = None) -> dict[str, Any]:
    """ADMIN: re-pull the WhatsApp address-book names from the paired phone.

    New and edited contacts arrive on their own; use this once after first deploying
    contact support, or if find_contact is missing names the phone has. It re-reads the
    full address-book snapshot (no re-pairing). Google names are unaffected — those
    come from the Apps Script push.

    Returns {ok, before, after} contact counts.
    """
    return _call(_bot(account).resync_contacts)


# ── admin-scope DM tools (allowlist + resume) ───────────────────


@mcp.tool()
def get_dm_allowlist(account: str | None = None) -> dict[str, Any]:
    """ADMIN: get the DM (1:1) recipient allowlist. Returns {jids: [...]}.

    Only recipients on this list can receive DMs via send_dm / send_dm_batch.
    """
    return _call(_bot(account).get_dm_allowlist)


@mcp.tool()
def set_dm_allowlist(jids: list[str], account: str | None = None) -> dict[str, Any]:
    """ADMIN: REPLACE the entire DM (1:1) recipient allowlist (not additive).

    Args:
        jids: Full list of permitted user JIDs (…@s.whatsapp.net or @lid). Passing []
              clears the allowlist (blocks all DMs while allowlist enforcement is on).

    Returns {ok, count}.
    """
    return _call(_bot(account).set_dm_allowlist, jids)


@mcp.tool()
def dm_resume(account: str | None = None) -> dict[str, Any]:
    """ADMIN: resume DM sending after an auto-pause. Deliberate operator cool-off action.

    The DM queue auto-pauses when WhatsApp restricts the account. Resuming is admin-scoped
    on purpose so an automated project loop cannot clear the pause and re-hammer a
    restricted number. Only call once you've decided the account has cooled off.

    Returns {ok, resumed, status}.
    """
    return _call(_bot(account).dm_resume)


@mcp.tool()
def get_media(
    message_id: int,
    out_dir: str = "/tmp/wa-media",
    account: str | None = None,
) -> dict[str, Any]:
    """ADMIN: download the image / document / voice note of one captured message.

    `message_id` is the `id` field from list_inbound (not wa_id). Saves the file under
    `out_dir` and returns {ok, path, mime, bytes}; read the path to view it. Images
    and documents from live messages are saved by the bot on arrival; anything else
    is fetched from WhatsApp on request, which fails (410) once WhatsApp has expired
    it, and always for media captured before 2026-10-04.
    """
    import mimetypes
    import os

    data, mime = _call(_bot(account).inbound_media, message_id)
    mime = mime.split(";")[0].strip()
    ext = mimetypes.guess_extension(mime) or ".bin"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{account or 'default'}-{int(message_id)}{ext}")
    with open(path, "wb") as fh:
        fh.write(data)
    return {"ok": True, "path": path, "mime": mime, "bytes": len(data)}


# ── accounts (one bot, several WhatsApp numbers) ─────────────


@mcp.tool()
def list_accounts() -> dict[str, Any]:
    """ADMIN: list the WhatsApp numbers this bot runs and whether each is linked.

    Returns {accounts: [{name, prefix, paired, me}]}. `me` is the linked number's JID.
    Pass `name` as `account` to any other tool; the account at prefix "/" is the
    default and needs no `account` argument.
    """
    return _call(_bot().list_accounts)


@mcp.tool()
def get_qr(out_path: str, account: str | None = None) -> dict[str, Any]:
    """ADMIN: save the pairing QR for an unlinked number as a PNG at `out_path`.

    Whoever scans it takes control of that number on the bot, so hand it only to the
    number's owner. It expires in about 20 seconds; fetch it again if it does. Errors
    with 404 once the number is linked, 503 while the QR is not ready yet.

    Returns {ok, path, bytes}.
    """
    png = _call(_bot(account).qr_png)
    with open(out_path, "wb") as fh:
        fh.write(png)
    return {"ok": True, "path": out_path, "bytes": len(png)}


# ── entry point ────────────────────────────────────────────────


def main() -> None:
    """Stdio entry point invoked by `wa-multiproject-bot-mcp` or `python -m wa_multiproject_bot_mcp`."""
    mcp.run()


if __name__ == "__main__":
    main()
