from __future__ import annotations

import io
import os
import unittest
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

from PIL import Image

import app as app_module
from supabase_catalog import (
    SupabaseCatalog,
    SupabaseCatalogError,
    SupabaseSettings,
)


ADMIN_HEADERS = {"X-Admin-Password": "test-admin-password"}


def png_stream(color: tuple[int, int, int, int], size: int = 48) -> io.BytesIO:
    stream = io.BytesIO()
    Image.new("RGBA", (size, size), color).save(stream, format="PNG")
    stream.seek(0)
    return stream


class FakeSupabaseCatalog:
    configured = True

    def __init__(self) -> None:
        self.records = [
            {
                "id": "local-purple",
                "name": "Purple Drop",
                "source": "local",
                "local_filename": "purple_drop.png",
                "storage_path": None,
                "original_filename": "purple_drop.png",
                "tags": ["drop", "liquid"],
                "colors": ["purple", "white"],
                "width": 4500,
                "height": 4500,
                "is_active": True,
                "updated_at": "2026-08-18T00:00:00Z",
            },
            {
                "id": "local-guava",
                "name": "Guava",
                "source": "local",
                "local_filename": "guava.png",
                "storage_path": None,
                "original_filename": "guava.png",
                "tags": ["fruit", "guava"],
                "colors": ["pink", "red", "green"],
                "width": 4500,
                "height": 4500,
                "is_active": True,
                "updated_at": "2026-08-18T00:00:00Z",
            },
        ]
        self.storage: dict[str, bytes] = {}
        self.next_id = 1
        self.search_calls: list[dict[str, object]] = []
        self.list_calls = 0
        self.download_calls: list[str] = []
        self.search_error = False

    def require_configured(self) -> None:
        return None

    def list_icons(self, *, active_only: bool = True):
        self.list_calls += 1
        records = self.records
        if active_only:
            records = [record for record in records if record["is_active"]]
        return deepcopy(sorted(records, key=lambda record: record["name"]))

    def search_icons(self, query: str, *, limit: int = 48, offset: int = 0):
        self.search_calls.append(
            {"query": query, "limit": limit, "offset": offset}
        )
        if self.search_error:
            raise SupabaseCatalogError("Search is temporarily unavailable")
        records = [record for record in self.records if record["is_active"]]
        records.sort(key=lambda record: record["name"].casefold())
        if query:
            records = [
                record
                for record in records
                if app_module.icon_matches_query(record, query)
            ]
        return deepcopy(records[offset : offset + limit])

    def public_object_url(self, storage_path: str) -> str:
        object_path = quote(storage_path.lstrip("/"), safe="/")
        return (
            "https://project.example/storage/v1/object/public/"
            f"icon-assets/{object_path}"
        )

    def get_icon(self, icon_id: str, *, active_only: bool = True):
        for record in self.records:
            if record["id"] != icon_id:
                continue
            if active_only and not record["is_active"]:
                return None
            return deepcopy(record)
        return None

    def insert_icon(self, values):
        record = {
            "id": f"uploaded-{self.next_id}",
            "updated_at": "2026-08-18T00:00:00Z",
            **deepcopy(values),
        }
        self.next_id += 1
        self.records.append(record)
        return deepcopy(record)

    def update_icon(self, icon_id: str, values):
        for record in self.records:
            if record["id"] == icon_id:
                record.update(deepcopy(values))
                return deepcopy(record)
        return None

    def upload_png(self, storage_path: str, png_bytes: bytes) -> None:
        if storage_path in self.storage:
            raise SupabaseCatalogError("Storage object already exists")
        self.storage[storage_path] = png_bytes

    def download_png(self, storage_path: str) -> bytes:
        self.download_calls.append(storage_path)
        try:
            return self.storage[storage_path]
        except KeyError as error:
            raise SupabaseCatalogError("Storage object was not found") from error

    def remove_png(self, storage_path: str) -> None:
        if storage_path not in self.storage:
            raise SupabaseCatalogError("Storage object was not found")
        del self.storage[storage_path]


class LocalCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "SUPABASE_URL": "",
                "SUPABASE_SERVICE_ROLE_KEY": "",
                "SUPABASE_BUCKET": "",
            },
        )
        self.environment.start()
        app_module.app.config.update(TESTING=True)
        app_module.app.config.pop("SUPABASE_CATALOG_FACTORY", None)
        self.client = app_module.app.test_client()

    def tearDown(self) -> None:
        self.environment.stop()

    def test_app_starts_and_existing_icons_load(self) -> None:
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/health").get_json(), {"status": "ok"})

        response = self.client.get("/api/search")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Icon-Catalog"], "local")
        payload = response.get_json()
        self.assertEqual(payload["offset"], 0)
        self.assertEqual(payload["limit"], 48)
        self.assertFalse(payload["has_more"])
        self.assertEqual(len(payload["items"]), 11)

    def test_existing_processing_background_controls_and_resize_work(self) -> None:
        response = self.client.get(
            "/api/process/1?width=40&height=40&remove_bg=true"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "image/png")
        with Image.open(io.BytesIO(response.data)) as result:
            self.assertEqual(result.size, (40, 40))

        download = self.client.get("/api/process/1?size=40&download=true")
        self.assertEqual(download.status_code, 200)
        self.assertIn(
            "attachment", download.headers.get("Content-Disposition", "")
        )

        source = Image.new("RGBA", (32, 32), (255, 255, 255, 255))
        for x in range(10, 22):
            for y in range(10, 22):
                source.putpixel((x, y), (220, 20, 60, 255))
        source_bytes = io.BytesIO()
        source.save(source_bytes, format="PNG")
        source_bytes.seek(0)

        recolored = app_module.process_icon_image(
            source_bytes, width=32, height=32, background_color=(0, 255, 0)
        )
        with Image.open(recolored) as result:
            self.assertGreater(result.getpixel((0, 0))[1], 200)

        source_bytes.seek(0)
        transparent = app_module.process_icon_image(
            source_bytes, width=32, height=32, remove_background=True
        )
        with Image.open(transparent) as result:
            self.assertLess(result.getpixel((0, 0))[3], 20)

    def test_local_combination_search_uses_name_tags_and_colors(self) -> None:
        purple = self.client.get("/api/search?q=purple+drop").get_json()["items"]
        pink_fruit = self.client.get("/api/search?q=pink+fruit").get_json()["items"]
        self.assertEqual([icon["name"] for icon in purple], ["Purple Drop"])
        self.assertEqual([icon["name"] for icon in pink_fruit], ["Guava"])

    def test_local_pagination_limit_and_offset(self) -> None:
        response = self.client.get("/api/search?limit=3&offset=2")
        payload = response.get_json()
        self.assertEqual(payload["limit"], 3)
        self.assertEqual(payload["offset"], 2)
        self.assertEqual(len(payload["items"]), 3)
        self.assertTrue(payload["has_more"])


class SupabaseCatalogClientTests(unittest.TestCase):
    def test_search_icons_posts_parameters_to_catalog_rpc(self) -> None:
        catalog = SupabaseCatalog(
            SupabaseSettings(
                url="https://project.example",
                service_role_key="placeholder",
                bucket="icon-assets",
            )
        )
        with patch.object(catalog, "_request", return_value=b"[]") as request:
            self.assertEqual(
                catalog.search_icons("purple drop", limit=49, offset=96),
                [],
            )
        request.assert_called_once_with(
            "POST",
            "/rest/v1/rpc/search_icons_catalog",
            json_body={
                "search_query": "purple drop",
                "result_limit": 49,
                "result_offset": 96,
            },
            retry_transient=True,
        )

    def test_public_object_url_encodes_bucket_and_object_path(self) -> None:
        catalog = SupabaseCatalog(
            SupabaseSettings(
                url="https://project.example",
                service_role_key="placeholder",
                bucket="icon assets",
            )
        )
        self.assertEqual(
            catalog.public_object_url("uploads/pink icon #1.png"),
            "https://project.example/storage/v1/object/public/"
            "icon%20assets/uploads/pink%20icon%20%231.png",
        )


class DynamicCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeSupabaseCatalog()
        self.environment = patch.dict(
            os.environ, {"ADMIN_PASSWORD": "test-admin-password"}
        )
        self.environment.start()
        app_module.app.config.update(
            TESTING=True,
            SUPABASE_CATALOG_FACTORY=lambda: self.fake,
        )
        self.client = app_module.app.test_client()

    def tearDown(self) -> None:
        app_module.app.config.pop("SUPABASE_CATALOG_FACTORY", None)
        self.environment.stop()

    def upload_purple_icon(self):
        return self.client.post(
            "/api/admin/icons",
            headers=ADMIN_HEADERS,
            data={
                "name": "Rain Mark",
                "tags": "drop, weather",
                "file": (png_stream((128, 20, 200, 255)), "rain-mark.png"),
            },
            content_type="multipart/form-data",
        )

    def add_catalog_records(self, count: int) -> None:
        for index in range(count):
            self.fake.records.append(
                {
                    "id": f"catalog-{index:03d}",
                    "name": f"Catalog Icon {index:03d}",
                    "source": "local",
                    "local_filename": "guava.png",
                    "storage_path": None,
                    "original_filename": f"catalog_icon_{index:03d}.png",
                    "tags": ["catalog", f"item-{index:03d}"],
                    "colors": ["green"],
                    "width": 4500,
                    "height": 4500,
                    "is_active": True,
                    "updated_at": "2026-08-18T00:00:00Z",
                }
            )

    def test_default_gallery_pagination_uses_one_extra_rpc_row(self) -> None:
        self.add_catalog_records(58)
        response = self.client.get("/api/search")
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Icon-Catalog"], "supabase")
        self.assertEqual(payload["limit"], 48)
        self.assertEqual(payload["offset"], 0)
        self.assertEqual(len(payload["items"]), 48)
        self.assertTrue(payload["has_more"])
        self.assertEqual(
            self.fake.search_calls[-1],
            {"query": "", "limit": 49, "offset": 0},
        )

    def test_limit_handling_requests_limit_plus_one(self) -> None:
        response = self.client.get("/api/search?limit=1")
        payload = response.get_json()
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(len(payload["items"]), 1)
        self.assertTrue(payload["has_more"])
        self.assertEqual(self.fake.search_calls[-1]["limit"], 2)

        minimum = self.client.get("/api/search?limit=0").get_json()
        self.assertEqual(minimum["limit"], 1)
        self.assertEqual(self.fake.search_calls[-1]["limit"], 2)

    def test_offset_handling_is_forwarded_to_rpc(self) -> None:
        response = self.client.get("/api/search?limit=1&offset=1")
        payload = response.get_json()
        self.assertEqual(payload["offset"], 1)
        self.assertEqual(payload["items"][0]["id"], "local-purple")
        self.assertEqual(self.fake.search_calls[-1]["offset"], 1)

    def test_limit_is_clamped_to_public_maximum(self) -> None:
        response = self.client.get("/api/search?limit=10000&offset=-4")
        payload = response.get_json()
        self.assertEqual(payload["limit"], 100)
        self.assertEqual(payload["offset"], 0)
        self.assertEqual(self.fake.search_calls[-1]["limit"], 101)
        self.assertEqual(self.fake.search_calls[-1]["offset"], 0)

    def test_has_more_is_false_on_final_page(self) -> None:
        payload = self.client.get("/api/search?limit=48").get_json()
        self.assertEqual(len(payload["items"]), 2)
        self.assertFalse(payload["has_more"])

    def test_query_is_forwarded_and_multiword_search_is_and_style(self) -> None:
        matches = self.client.get("/api/search?q=purple+drop").get_json()[
            "items"
        ]
        self.assertEqual([icon["id"] for icon in matches], ["local-purple"])
        self.assertEqual(self.fake.search_calls[-1]["query"], "purple drop")

        no_match = self.client.get("/api/search?q=purple+fruit").get_json()[
            "items"
        ]
        self.assertEqual(no_match, [])

    def test_tag_parameter_remains_compatible(self) -> None:
        matches = self.client.get("/api/search?tag=pink+fruit").get_json()[
            "items"
        ]
        self.assertEqual([icon["id"] for icon in matches], ["local-guava"])
        self.assertEqual(self.fake.search_calls[-1]["query"], "pink fruit")

    def test_storage_thumbnail_uses_direct_public_url(self) -> None:
        self.fake.records.append(
            {
                "id": "storage-pink",
                "name": "Pink Storage Icon",
                "source": "storage",
                "local_filename": None,
                "storage_path": "uploads/pink icon.png",
                "original_filename": "pink icon.png",
                "tags": ["pink"],
                "colors": ["pink"],
                "width": 48,
                "height": 48,
                "is_active": True,
                "updated_at": "2026-08-18T00:00:00Z",
            }
        )
        matches = self.client.get("/api/search?q=storage").get_json()["items"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(
            matches[0]["thumbnail_url"],
            "https://project.example/storage/v1/object/public/"
            "icon-assets/uploads/pink%20icon.png",
        )
        self.assertNotIn("api/icons", matches[0]["thumbnail_url"])

    def test_local_thumbnail_retains_local_flask_url(self) -> None:
        matches = self.client.get("/api/search?q=guava").get_json()["items"]
        self.assertEqual(matches[0]["thumbnail_url"], "/icons/guava.png")

    def test_supabase_search_failure_gracefully_uses_local_catalog(self) -> None:
        self.fake.search_error = True
        response = self.client.get("/api/search?q=purple+drop&limit=2")
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Icon-Catalog"], "local-fallback")
        self.assertEqual([icon["name"] for icon in payload["items"]], ["Purple Drop"])
        self.assertFalse(payload["has_more"])

    def test_admin_listing_still_uses_complete_catalog(self) -> None:
        response = self.client.get("/api/admin/icons", headers=ADMIN_HEADERS)
        icons = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(icons, list)
        self.assertEqual(len(icons), 2)
        self.assertEqual(self.fake.list_calls, 1)
        self.assertEqual(self.fake.search_calls, [])

    def test_admin_routes_require_password(self) -> None:
        self.assertEqual(self.client.get("/api/admin/icons").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/api/admin/icons", headers={"X-Admin-Password": "wrong"}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/api/admin/check", headers=ADMIN_HEADERS).status_code,
            200,
        )

    def test_upload_detects_color_appears_and_processes_without_redeploy(self) -> None:
        response = self.upload_purple_icon()
        self.assertEqual(response.status_code, 201)
        icon = response.get_json()
        self.assertIn("purple", icon["colors"])
        self.assertTrue(self.fake.storage)

        matches = self.client.get("/api/search?q=purple+drop").get_json()[
            "items"
        ]
        self.assertIn(icon["id"], [match["id"] for match in matches])

        processed = self.client.get(
            f"/api/process/{icon['id']}?width=36&height=36"
        )
        self.assertEqual(processed.status_code, 200)
        storage_path = next(iter(self.fake.storage))
        self.assertIn(storage_path, self.fake.download_calls)
        with Image.open(io.BytesIO(processed.data)) as image:
            self.assertEqual(image.size, (36, 36))

    def test_edit_tags_changes_search_immediately(self) -> None:
        icon = self.upload_purple_icon().get_json()
        response = self.client.patch(
            f"/api/admin/icons/{icon['id']}",
            headers={**ADMIN_HEADERS, "Content-Type": "application/json"},
            json={"name": "Berry Rain", "tags": ["fruit", "berry"]},
        )
        self.assertEqual(response.status_code, 200)

        matches = self.client.get("/api/search?q=purple+fruit").get_json()[
            "items"
        ]
        self.assertEqual([match["id"] for match in matches], [icon["id"]])
        old_matches = self.client.get("/api/search?q=purple+weather").get_json()[
            "items"
        ]
        self.assertNotIn(icon["id"], [match["id"] for match in old_matches])

    def test_remove_uploaded_icon_deactivates_record_and_deletes_object(self) -> None:
        icon = self.upload_purple_icon().get_json()
        storage_path = next(
            record["storage_path"]
            for record in self.fake.records
            if record["id"] == icon["id"]
        )
        response = self.client.delete(
            f"/api/admin/icons/{icon['id']}", headers=ADMIN_HEADERS
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(storage_path, self.fake.storage)
        self.assertIsNone(self.fake.get_icon(icon["id"], active_only=True))

    def test_remove_local_icon_only_deactivates_metadata(self) -> None:
        local_file = Path(app_module.ICONS_DIR / "purple_drop.png")
        self.assertTrue(local_file.is_file())
        response = self.client.delete(
            "/api/admin/icons/local-purple", headers=ADMIN_HEADERS
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(local_file.is_file())
        self.assertIsNone(self.fake.get_icon("local-purple", active_only=True))

    def test_invalid_pngs_are_rejected_before_storage(self) -> None:
        corrupt = self.client.post(
            "/api/admin/icons",
            headers=ADMIN_HEADERS,
            data={
                "name": "Broken",
                "file": (io.BytesIO(b"not a png"), "broken.png"),
            },
            content_type="multipart/form-data",
        )
        wrong_extension = self.client.post(
            "/api/admin/icons",
            headers=ADMIN_HEADERS,
            data={
                "name": "Wrong",
                "file": (png_stream((255, 0, 0, 255)), "wrong.jpg"),
            },
            content_type="multipart/form-data",
        )
        oversized = self.client.post(
            "/api/admin/icons",
            headers=ADMIN_HEADERS,
            data={
                "name": "Too large",
                "file": (
                    io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * (5 * 1024 * 1024)),
                    "too-large.png",
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(corrupt.status_code, 400)
        self.assertEqual(wrong_extension.status_code, 400)
        self.assertEqual(oversized.status_code, 400)
        self.assertIn("5 MB", oversized.get_json()["error"])
        self.assertEqual(self.fake.storage, {})

    def test_frontend_contains_no_server_secret_values(self) -> None:
        template = (app_module.TEMPLATE_DIR / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", template)
        self.assertNotIn("ADMIN_PASSWORD", template)
        self.assertNotIn("test-admin-password", template)


if __name__ == "__main__":
    unittest.main()
