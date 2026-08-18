# Plix Icon Editor

A Flask and Pillow icon customizer with a Supabase-backed catalog. Bundled
icons remain in `icons/`, while teammate uploads are stored in Supabase Storage
and processed in memory by the same background-removal and recoloring pipeline.

## Server configuration

Set these variables in Render. Keep all four server-side and never expose them
in browser code:

- `SUPABASE_URL` — the project API URL
- `SUPABASE_SERVICE_ROLE_KEY` — a service-role or server secret key
- `SUPABASE_BUCKET` — the Storage bucket name (`icon-assets` in production)
- `ADMIN_PASSWORD` — the password teammates use to open **Manage Icons**

The app expects `public.icons` to contain the catalog metadata used by the
configured Supabase project. If Supabase is not configured or a catalog read
temporarily fails, the public gallery gracefully falls back to scanning the
repository's local PNG files. Administrative changes still require Supabase so
that Render's ephemeral filesystem is never used for uploaded assets.

## Local development

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000`. Without Supabase variables, local icons, search,
preview, background processing, resize, and PNG download remain available.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The test suite uses an in-memory Supabase double. It covers local compatibility,
upload validation, automatic color tagging, combination search, metadata edits,
both removal modes, and processing a Storage-backed PNG without writing it to
the local filesystem.
