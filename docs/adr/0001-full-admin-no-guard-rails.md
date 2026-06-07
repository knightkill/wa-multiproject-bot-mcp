# Full admin scope, no guard rails (C1)

This MCP server is configured for one operator's Claude Desktop /
Claude Code use. It holds both `WP_ADMIN_TOKEN` and `WP_PROJECT_TOKEN`
and exposes every admin operation — including the destructive ones
(`delete_project`, `replace_allowlist`, `set_project_groups`,
`rotate_project_token`) — as MCP tools with no confirmation step.

We considered three milder postures: read-only admin (tools that only
list / observe), two-tier exposure where destructive tools require an
explicit `--allow-destructive` flag at server startup, and per-tool
confirmation parameters that force the agent to echo back current
state before mutating it. The operator explicitly chose C1 (full power, no
guard rails) after seeing all three, with the warning that "Claude,
clean up the allowlist" gone wrong could orphan every project's group
access in one `PUT /admin/api/allowlist` call.

Future readers will reasonably wonder why a single-operator tool exposes
delete operations without confirmation. The answer is: it's a
single-operator, local-only MCP server bound to one machine. The
threat model is "operator asks the agent to do something dumb," not
"third party gains access." The web UI at https://your-domain.example/admin
remains available as the careful path for destructive operations.
This ADR exists so that the next time someone is tempted to add
defensive ceremony, they remember the choice was deliberate and ask
the operator before changing it.
