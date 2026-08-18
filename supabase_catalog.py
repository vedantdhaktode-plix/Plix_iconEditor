from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ICON_COLUMNS = (
    "id,name,source,local_filename,storage_path,original_filename,"
    "tags,colors,width,height,is_active,created_at,updated_at"
)
TRANSIENT_STATUS_CODES = {502, 503, 504}


class SupabaseCatalogError(RuntimeError):
    """A safe, user-displayable Supabase request failure."""


@dataclass(frozen=True)
class SupabaseSettings:
    url: str
    service_role_key: str
    bucket: str

    @classmethod
    def from_environment(cls) -> "SupabaseSettings":
        return cls(
            url=os.environ.get("SUPABASE_URL", "").strip().rstrip("/"),
            service_role_key=os.environ.get(
                "SUPABASE_SERVICE_ROLE_KEY", ""
            ).strip(),
            bucket=os.environ.get("SUPABASE_BUCKET", "").strip(),
        )

    @property
    def configured(self) -> bool:
        return bool(self.url and self.service_role_key and self.bucket)


class SupabaseCatalog:
    """Minimal server-only client for the icons table and Storage bucket."""

    def __init__(
        self,
        settings: SupabaseSettings | None = None,
        *,
        timeout_seconds: float = 12.0,
    ) -> None:
        self.settings = settings or SupabaseSettings.from_environment()
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def require_configured(self) -> None:
        if not self.configured:
            raise SupabaseCatalogError(
                "Supabase is not configured on this server"
            )

    def _headers(self) -> dict[str, str]:
        self.require_configured()
        key = self.settings.service_role_key
        headers = {
            "Accept": "application/json",
            "apikey": key,
            "User-Agent": "plix-icon-editor/1.0",
        }

        # Legacy service-role keys are JWTs and are also sent as bearer tokens.
        # Current sb_secret keys must only be sent in the apikey header.
        if key.startswith("eyJ") and key.count(".") == 2:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    @staticmethod
    def _safe_error_detail(payload: bytes, status: int) -> str:
        try:
            parsed = json.loads(payload.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None

        if isinstance(parsed, dict):
            detail = parsed.get("message") or parsed.get("error")
            if isinstance(detail, str) and detail.strip():
                return f"Supabase returned HTTP {status}: {detail.strip()}"
        return f"Supabase returned HTTP {status}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        json_body: object | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
        prefer: str | None = None,
        expected_statuses: tuple[int, ...] = (200,),
        retry_transient: bool = False,
    ) -> bytes:
        self.require_configured()
        url = f"{self.settings.url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"

        headers = self._headers()
        if prefer:
            headers["Prefer"] = prefer

        body = raw_body
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        if extra_headers:
            headers.update(extra_headers)

        attempts = 3 if retry_transient else 1
        for attempt in range(attempts):
            request = Request(url, data=body, headers=headers, method=method)
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = response.read()
                    if response.status not in expected_statuses:
                        raise SupabaseCatalogError(
                            self._safe_error_detail(payload, response.status)
                        )
                    return payload
            except HTTPError as error:
                payload = error.read()
                if (
                    retry_transient
                    and error.code in TRANSIENT_STATUS_CODES
                    and attempt + 1 < attempts
                ):
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise SupabaseCatalogError(
                    self._safe_error_detail(payload, error.code)
                ) from error
            except (TimeoutError, URLError, OSError) as error:
                if retry_transient and attempt + 1 < attempts:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise SupabaseCatalogError(
                    "Supabase could not be reached from this server"
                ) from error

        raise SupabaseCatalogError("Supabase request failed")

    @staticmethod
    def _decode_rows(payload: bytes) -> list[dict[str, Any]]:
        try:
            rows = json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise SupabaseCatalogError(
                "Supabase returned an invalid catalog response"
            ) from error
        if not isinstance(rows, list) or not all(
            isinstance(row, dict) for row in rows
        ):
            raise SupabaseCatalogError(
                "Supabase returned an invalid catalog response"
            )
        return rows

    def list_icons(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        query = {"select": ICON_COLUMNS, "order": "name.asc"}
        if active_only:
            query["is_active"] = "eq.true"
        payload = self._request(
            "GET",
            "/rest/v1/icons",
            query=query,
            retry_transient=True,
        )
        return self._decode_rows(payload)

    def get_icon(
        self, icon_id: str, *, active_only: bool = True
    ) -> dict[str, Any] | None:
        query = {
            "select": ICON_COLUMNS,
            "id": f"eq.{icon_id}",
            "limit": "1",
        }
        if active_only:
            query["is_active"] = "eq.true"
        rows = self._decode_rows(
            self._request(
                "GET",
                "/rest/v1/icons",
                query=query,
                retry_transient=True,
            )
        )
        return rows[0] if rows else None

    def insert_icon(self, values: dict[str, Any]) -> dict[str, Any]:
        rows = self._decode_rows(
            self._request(
                "POST",
                "/rest/v1/icons",
                query={"select": ICON_COLUMNS},
                json_body=values,
                prefer="return=representation",
                expected_statuses=(200, 201),
            )
        )
        if not rows:
            raise SupabaseCatalogError(
                "Supabase did not return the new icon record"
            )
        return rows[0]

    def update_icon(
        self, icon_id: str, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        rows = self._decode_rows(
            self._request(
                "PATCH",
                "/rest/v1/icons",
                query={"id": f"eq.{icon_id}", "select": ICON_COLUMNS},
                json_body=values,
                prefer="return=representation",
                expected_statuses=(200,),
            )
        )
        return rows[0] if rows else None

    def upload_png(self, storage_path: str, png_bytes: bytes) -> None:
        bucket = quote(self.settings.bucket, safe="")
        object_path = quote(storage_path.lstrip("/"), safe="/")
        self._request(
            "POST",
            f"/storage/v1/object/{bucket}/{object_path}",
            raw_body=png_bytes,
            content_type="image/png",
            extra_headers={
                "cache-control": "max-age=3600",
                "x-upsert": "false",
            },
            expected_statuses=(200, 201),
        )

    def download_png(self, storage_path: str) -> bytes:
        bucket = quote(self.settings.bucket, safe="")
        object_path = quote(storage_path.lstrip("/"), safe="/")
        return self._request(
            "GET",
            f"/storage/v1/object/{bucket}/{object_path}",
            expected_statuses=(200,),
            retry_transient=True,
        )

    def remove_png(self, storage_path: str) -> None:
        bucket = quote(self.settings.bucket, safe="")
        self._request(
            "DELETE",
            f"/storage/v1/object/{bucket}",
            json_body={"prefixes": [storage_path.lstrip("/")]},
            expected_statuses=(200,),
        )
