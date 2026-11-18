"""WSGI entry point for production servers (gunicorn, uWSGI, ...).

The app keeps short-lived state in process memory - resolved HLS playlists and
the TeraBox helper cache - so a request that builds a token must be served by
the same worker that later reads it. Run one worker with threads:

    gunicorn --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:$PORT wsgi:app

`python3 terabox_app.py` still works for local development.
"""

from terabox_app import app

__all__ = ["app"]
