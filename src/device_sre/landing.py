"""Self-contained browser client. Credentials are entered in the browser, never embedded."""
import base64
import hashlib
from importlib.resources import files

assets = files("device_sre").joinpath("web")
style = assets.joinpath("app.css").read_text(encoding="utf-8")
script = assets.joinpath("app.js").read_text(encoding="utf-8")
HOME_HTML = (assets.joinpath("index.html").read_text(encoding="utf-8")
             .replace("<!-- STYLES -->", f"<style>{style}</style>")
             .replace("<!-- SCRIPT -->", f"<script>{script}</script>"))

def _hash(value):
    return base64.b64encode(hashlib.sha256(value.encode()).digest()).decode()

HOME_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; connect-src 'self'; "
        f"style-src 'sha256-{_hash(style)}'; "
        f"script-src 'sha256-{_hash(script)}'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}
