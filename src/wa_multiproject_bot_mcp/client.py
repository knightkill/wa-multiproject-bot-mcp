"""Thin httpx client wrapping the bot's REST API.

Holds two bearer tokens — admin and project — and routes each call to
the appropriate scope. Bot 4xx/5xx responses become BotApiError with
the bot's `error`/`detail` envelope passed through to the caller; the
MCP layer surfaces those as tool errors.

Configuration comes from environment variables only:
  WP_BASE_URL       e.g. https://your-domain.example
  WP_ADMIN_TOKEN    ADMIN_TOKEN Fly secret
  WP_PROJECT_TOKEN  per-project token for posting

A bot can run several WhatsApp numbers ("accounts"). The original one is
served at the root; each extra one under /a/<account>/ with its own
projects, so its project token comes from WP_PROJECT_TOKEN_<ACCOUNT>
(upper-cased, '-' as '_'). The admin token is shared.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class BotConfigError(RuntimeError):
    """A required environment variable is missing or malformed."""


class BotApiError(RuntimeError):
    """The bot returned a non-2xx status. status, error, detail surfaced for the caller."""

    def __init__(self, status: int, error: str, detail: str | None = None):
        self.status = status
        self.error = error
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"bot {status} {error}{suffix}")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise BotConfigError(
            f"{name} env var is required. Set it in your Claude MCP server config."
        )
    return value


class BotClient:
    """Synchronous httpx wrapper. MCP tools are synchronous functions."""

    def __init__(
        self,
        base_url: str | None = None,
        admin_token: str | None = None,
        project_token: str | None = None,
        timeout: float = 290.0,
        account: str | None = None,
    ) -> None:
        # 290s matches the Fly proxy idle_timeout=300 minus a safety
        # margin; the bot itself caps a Post at 270s.
        self.base_url = (base_url or _require_env("WP_BASE_URL")).rstrip("/")
        self.admin_token = admin_token or _require_env("WP_ADMIN_TOKEN")
        self.account = account
        self.prefix = f"/a/{account}" if account else ""
        if project_token:
            self.project_token = project_token
        elif account:
            # Optional until a project-scope call is made on this account.
            env = "WP_PROJECT_TOKEN_" + account.upper().replace("-", "_")
            self.project_token = os.environ.get(env, "").strip() or None
            self._project_token_env = env
        else:
            self.project_token = _require_env("WP_PROJECT_TOKEN")
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    # ── low-level dispatch ────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        scope: str,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        token = self.admin_token if scope in ("admin", "server") else self.project_token
        if token is None:
            raise BotConfigError(
                f"{self._project_token_env} env var is required for project-scope "
                f"calls on account {self.account!r}."
            )
        if scope == "server":
            url = f"{self.base_url}{path}"
        elif path == "/":
            url = f"{self.base_url}{self.prefix or '/'}"
        else:
            url = f"{self.base_url}{self.prefix}{path}"
        try:
            response = self._client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=json_body,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise BotApiError(0, "transport_error", str(exc)) from exc

        if response.status_code >= 400:
            error = "unknown_error"
            detail: str | None = None
            try:
                envelope = response.json()
                if isinstance(envelope, dict):
                    error = str(envelope.get("error") or error)
                    raw_detail = envelope.get("detail")
                    detail = str(raw_detail) if raw_detail is not None else None
            except ValueError:
                detail = response.text[:500] or None
            raise BotApiError(response.status_code, error, detail)

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise BotApiError(response.status_code, "invalid_json", str(exc)) from exc

    # ── project scope ─────────────────────────────────────────

    def health(self) -> Any:
        # Unauthenticated route; send the admin token so it works on an
        # account that has no project token configured.
        return self._request("GET", "/", scope="admin")

    def list_accounts(self) -> Any:
        return self._request("GET", "/accounts", scope="server")

    def inbound_media(self, message_id: int) -> tuple[bytes, str]:
        try:
            response = self._client.get(
                f"{self.base_url}{self.prefix}/admin/api/inbound/{int(message_id)}/media",
                headers={"Authorization": f"Bearer {self.admin_token}"},
            )
        except httpx.HTTPError as exc:
            raise BotApiError(0, "transport_error", str(exc)) from exc
        if response.status_code >= 400:
            try:
                body = response.json()
                raise BotApiError(response.status_code, body.get("error", "error"), body.get("detail"))
            except ValueError:
                raise BotApiError(response.status_code, response.text[:200]) from None
        return response.content, response.headers.get("content-type", "application/octet-stream")

    def qr_png(self) -> bytes:
        try:
            response = self._client.get(
                f"{self.base_url}{self.prefix}/qr",
                headers={"Authorization": f"Bearer {self.admin_token}"},
            )
        except httpx.HTTPError as exc:
            raise BotApiError(0, "transport_error", str(exc)) from exc
        if response.status_code >= 400:
            try:
                error = response.json().get("error", "error")
            except ValueError:
                error = response.text[:200]
            raise BotApiError(response.status_code, error)
        return response.content

    def post(self, body: dict[str, Any]) -> Any:
        return self._request("POST", "/v1/post", scope="project", json_body=body)

    def list_my_groups(self) -> Any:
        return self._request("GET", "/v1/groups", scope="project")

    def create_group(
        self,
        subject: str,
        participants: list[str],
        announce: bool | None = None,
        description: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {"subject": subject, "participants": participants}
        if announce is not None:
            body["announce"] = announce
        if description is not None:
            body["description"] = description
        return self._request(
            "POST", "/v1/groups/create", scope="project", json_body=body
        )

    def add_participants(self, group_jid: str, participants: list[str]) -> Any:
        return self._request(
            "POST",
            "/v1/groups/participants/add",
            scope="project",
            json_body={"groupJid": group_jid, "participants": participants},
        )

    def remove_participants(self, group_jid: str, participants: list[str]) -> Any:
        return self._request(
            "POST",
            "/v1/groups/participants/remove",
            scope="project",
            json_body={"groupJid": group_jid, "participants": participants},
        )

    def group_invite_code(self, group_jid: str) -> Any:
        return self._request(
            "POST",
            "/v1/groups/invite/code",
            scope="project",
            json_body={"groupJid": group_jid},
        )

    def revoke_invite(self, group_jid: str) -> Any:
        return self._request(
            "POST",
            "/v1/groups/invite/revoke",
            scope="project",
            json_body={"groupJid": group_jid},
        )

    def set_group_setting(self, group_jid: str, setting: str) -> Any:
        return self._request(
            "POST",
            "/v1/groups/setting",
            scope="project",
            json_body={"groupJid": group_jid, "setting": setting},
        )

    def update_subject(self, group_jid: str, subject: str) -> Any:
        return self._request(
            "POST",
            "/v1/groups/subject",
            scope="project",
            json_body={"groupJid": group_jid, "subject": subject},
        )

    def update_description(self, group_jid: str, description: str) -> Any:
        return self._request(
            "POST",
            "/v1/groups/description",
            scope="project",
            json_body={"groupJid": group_jid, "description": description},
        )

    def leave_group(self, group_jid: str) -> Any:
        return self._request(
            "POST",
            "/v1/groups/leave",
            scope="project",
            json_body={"groupJid": group_jid},
        )

    # ── project scope: 1:1 DMs (high ban-risk) ────────────────
    # Recipients must be on the DM allowlist (admin-managed). Caps and
    # jitter are enforced server-side and cannot be raised from here.

    def send_dm(self, to_jid: str, text: str) -> Any:
        return self._request(
            "POST", "/v1/dm", scope="project",
            json_body={"toJid": to_jid, "text": text},
        )

    def send_dm_batch(self, recipients: list[str], text: str) -> Any:
        return self._request(
            "POST", "/v1/dm/batch", scope="project",
            json_body={"recipients": recipients, "text": text},
        )

    def dm_status(self, batch_id: str | None = None) -> Any:
        params = {"batchId": batch_id} if batch_id else None
        return self._request("GET", "/v1/dm/status", scope="project", params=params)

    def dm_cancel(self, batch_id: str) -> Any:
        return self._request(
            "POST", "/v1/dm/cancel", scope="project",
            json_body={"batchId": batch_id},
        )

    # ── admin scope ───────────────────────────────────────────

    def list_projects(self) -> Any:
        return self._request("GET", "/admin/api/projects", scope="admin")

    def create_project(self, name: str, group_jids: list[str]) -> Any:
        return self._request(
            "POST",
            "/admin/api/projects",
            scope="admin",
            json_body={"name": name, "groupJids": group_jids},
        )

    def delete_project(self, project_id: int) -> Any:
        return self._request(
            "DELETE", f"/admin/api/projects/{project_id}", scope="admin"
        )

    def rotate_project_token(self, project_id: int) -> Any:
        return self._request(
            "POST",
            f"/admin/api/projects/{project_id}/rotate-token",
            scope="admin",
        )

    def set_project_groups(self, project_id: int, group_jids: list[str]) -> Any:
        return self._request(
            "PUT",
            f"/admin/api/projects/{project_id}/groups",
            scope="admin",
            json_body={"groupJids": group_jids},
        )

    def list_live_groups(self) -> Any:
        return self._request("GET", "/admin/api/groups", scope="admin")

    def get_allowlist(self) -> Any:
        return self._request("GET", "/admin/api/allowlist", scope="admin")

    def replace_allowlist(self, jids: list[str]) -> Any:
        return self._request(
            "PUT", "/admin/api/allowlist", scope="admin", json_body={"jids": jids}
        )

    # DM allowlist + resume are admin-scoped: resuming after a WhatsApp
    # restriction is a deliberate operator cool-off decision, withheld
    # from project tokens so an automated loop cannot clear the pause.

    def dm_resume(self) -> Any:
        return self._request("POST", "/admin/api/dm/resume", scope="admin")

    def find_contact(self, query: str | None = None, limit: int | None = None) -> Any:
        params: dict[str, Any] = {}
        if query:
            params["q"] = query
        if limit is not None:
            params["limit"] = str(limit)
        return self._request("GET", "/admin/api/contacts", scope="admin", params=params)

    def resync_contacts(self) -> Any:
        return self._request("POST", "/admin/api/contacts/resync", scope="admin")

    def get_dm_allowlist(self) -> Any:
        return self._request("GET", "/admin/api/dm-allowlist", scope="admin")

    def set_dm_allowlist(self, jids: list[str]) -> Any:
        return self._request(
            "PUT", "/admin/api/dm-allowlist", scope="admin", json_body={"jids": jids}
        )

    def list_messages(
        self,
        project: str | None = None,
        group: str | None = None,
        limit: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if project is not None:
            params["project"] = project
        if group is not None:
            params["group"] = group
        if limit is not None:
            params["limit"] = str(limit)
        return self._request(
            "GET", "/admin/api/messages", scope="admin", params=params or None
        )

    # Inbound capture is opt-in per chat: the bot only stores messages
    # arriving after a jid is added via set_readable_chats (no history
    # backfill for chats added later).

    def list_readable_chats(self) -> Any:
        return self._request("GET", "/admin/api/readable-chats", scope="admin")

    def set_readable_chats(self, chats: list[dict[str, Any]]) -> Any:
        return self._request(
            "PUT", "/admin/api/readable-chats", scope="admin", json_body={"chats": chats}
        )

    def list_inbound(
        self,
        jid: str | None = None,
        since: int | None = None,
        from_me: bool | None = None,
        limit: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if jid is not None:
            params["jid"] = jid
        if since is not None:
            params["since"] = str(since)
        if from_me is not None:
            params["fromMe"] = "true" if from_me else "false"
        if limit is not None:
            params["limit"] = str(limit)
        return self._request(
            "GET", "/admin/api/inbound", scope="admin", params=params or None
        )
