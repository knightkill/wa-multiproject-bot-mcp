# 1:1 DM tools — split-scope, server-throttled, no rate knobs

The seven DM tools (`send_dm`, `send_dm_batch`, `dm_status`, `dm_cancel`
at project scope; `get_dm_allowlist`, `set_dm_allowlist`, `dm_resume` at
admin scope) wrap the bot's existing `/v1/dm*` and `/admin/api/dm*`
endpoints. They were present in an earlier adapter, dropped at some
point, and restored here so the MCP surface matches the bot's actual
capability — the bridge has always had the DM endpoints.

**Scope split mirrors the bot, deliberately.** Sending and observing
(`send_dm`, `send_dm_batch`, `dm_status`, `dm_cancel`) are project-scope:
permission parity with `post_to_group`, usable by a session holding only
a project token. But `dm_resume` is **admin-scope on purpose**. The DM
queue auto-pauses when WhatsApp restricts the account; resuming is a human
cool-off decision. Withholding it from the project token means an
automated project loop cannot clear the pause and re-hammer an
already-restricted number — the single most important guard rail in the
DM path. `get_dm_allowlist`/`set_dm_allowlist` are admin-scope because the
allowlist is the operator's gate on who may be messaged at all.

**No rate knobs by design.** The tools expose no jitter/cap parameters.
Per-minute and per-day caps and randomized jitter live entirely in the
bot's `dmSender` and cannot be raised from a tool call — exposing a knob
would defeat the throttle that exists to keep the paired number alive.

**High ban-risk, surfaced in docstrings.** Unsolicited 1:1 DMs are the
fastest route to a flagged number, so the docstrings shout it:
recipients must be allowlisted (else 403), an invite-link group post is
the preferred distribution path, a 2xx from `send_dm` means accepted-for-
send and not guaranteed delivered, and `send_dm_batch` returns
non-allowlisted recipients in `skipped[]` rather than sending them. This
keeps the C1 "full power, no confirmation step" posture of
[`0001-full-admin-no-guard-rails.md`](0001-full-admin-no-guard-rails.md)
while making the danger legible to the agent.
