# Group-management tools are project-scope

The nine new group-lifecycle tools (`create_group`, `add_participants`,
`remove_participants`, `group_invite_code`, `revoke_invite`,
`set_group_setting`, `update_subject`, `update_description`,
`leave_group`) are exposed at **project scope** — they call the bot's
`/v1/groups/*` endpoints with `WP_PROJECT_TOKEN`, not `WP_ADMIN_TOKEN`,
and pass `groupJid` in the JSON body. This is permission parity with
`post_to_group`: the bot authorizes group management with the same
`requireProject` middleware and the same
(Project Group Access ∩ Allowlist) intersection it uses for posting, so
a tool that can post to a Group can also manage it, and the MCP layer
inherits that contract for free rather than re-deriving it.

`create_group` is the natural reason this lives at project scope rather
than admin scope: a freshly created Group has to be *bound to a Project
context* to be usable — the bot appends the new JID to both the global
Allowlist and the calling Project's Group Access so the same token can
immediately post to and administer it. An admin-scoped create would have
no Project to grant access to and would leave the new Group unreachable
by any Project Token. Keeping these tools project-scope also means they
work for a Claude session that holds only a Project Token, without
needing the admin credential in scope.

The destructive members of this set (`remove_participants`,
`leave_group`) carry the same no-guard-rails posture as the admin tools
documented in
[`0001-full-admin-no-guard-rails.md`](0001-full-admin-no-guard-rails.md):
no confirmation step, full power, personal-use threat model. The
tool docstrings carry the warnings (invite-link-first for
privacy-blocked adds, the 20-participant batch cap, that `create_group`
auto-grants Allowlist + Project access, that remove/leave are
destructive) so the agent surfaces them in conversation; the web UI at
https://your-domain.example/admin remains the careful path.
