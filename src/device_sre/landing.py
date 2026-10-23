"""Public connection instructions; device tools remain behind bearer authentication."""
import base64
import hashlib


_STYLE = """
:root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #17243b; background: #f5f7fb; }
* { box-sizing: border-box; }
body { margin: 0; }
main { max-width: 1060px; margin: 0 auto; padding: 44px 28px 36px; }
header { display: flex; align-items: center; justify-content: space-between; gap: 18px; margin-bottom: 70px; }
.brand { display: flex; align-items: center; gap: 12px; font-weight: 650; font-size: 15px; }
.mark { background: #203e7c; color: white; padding: 9px 11px; border-radius: 10px; font-size: 13px; letter-spacing: .04em; }
.status { display: inline-flex; align-items: center; gap: 8px; color: #176346; background: #e8f5ed; border: 1px solid #cae7d6; border-radius: 30px; padding: 8px 12px; font-size: 13px; white-space: nowrap; }
.dot { height: 7px; width: 7px; background: #269360; border-radius: 50%; }
.layout { display: grid; grid-template-columns: 1fr 1fr; gap: 48px; align-items: start; }
.eyebrow { color: #49618b; text-transform: uppercase; letter-spacing: .12em; font-size: 11px; font-weight: 700; margin: 4px 0 16px; }
h1 { font-size: clamp(32px, 4vw, 46px); line-height: 1.12; letter-spacing: -.035em; margin: 0 0 22px; }
.intro { font-size: 17px; line-height: 1.7; color: #526178; margin: 0 0 28px; }
.health { display: inline-block; border: 1px solid #d6deea; background: white; padding: 11px 16px; border-radius: 9px; color: #203e7c; font-size: 14px; font-weight: 600; text-decoration: none; }
.health:hover { background: #edf2fa; }
.card { background: white; border: 1px solid #dfe5ef; border-radius: 16px; padding: 28px; box-shadow: 0 12px 36px #17243b08; }
h2 { font-size: 19px; margin: 0 0 23px; letter-spacing: -.015em; }
.label { display: block; color: #617087; font-size: 12px; font-weight: 600; margin-bottom: 9px; }
.endpoint { background: #f3f6fb; border: 1px solid #e4e9f2; padding: 13px; border-radius: 8px; overflow-wrap: anywhere; line-height: 1.65; font-size: 12px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
button { cursor: pointer; background: #203e7c; color: white; border: 0; border-radius: 8px; padding: 11px 16px; margin: 12px 0 23px; font-size: 13px; font-weight: 600; }
button:hover { background: #152d60; }
button:focus-visible, a:focus-visible { outline: 3px solid #769eea; outline-offset: 3px; }
.copy-status { margin-left: 10px; color: #526178; font-size: 12px; }
.row { display: flex; gap: 18px; justify-content: space-between; padding: 13px 0; border-top: 1px solid #edf0f5; font-size: 13px; }
.row span:first-child { color: #617087; }
.row span:last-child { font-weight: 600; }
.auth { font-size: 12px; overflow-wrap: anywhere; background: #f3f6fb; border-radius: 8px; padding: 13px; line-height: 1.6; margin-top: 8px; }
.help { font-size: 13px; line-height: 1.7; color: #617087; margin: 17px 0 0; }
.steps { margin-top: 45px; padding-top: 27px; border-top: 1px solid #dfe5ef; }
.steps h2 { font-size: 16px; margin-bottom: 13px; }
ol { padding-left: 19px; margin: 0; color: #526178; font-size: 14px; line-height: 1.8; }
footer { color: #617087; font-size: 12px; line-height: 1.8; margin-top: 45px; padding-top: 22px; border-top: 1px solid #dfe5ef; }
@media (max-width: 760px) { main { padding: 25px 20px; } header { margin-bottom: 42px; } .layout { grid-template-columns: 1fr; gap: 28px; } .card { padding: 23px; } .brand { font-size: 13px; } .status { font-size: 11px; } }
"""

_SCRIPT = """
const endpoint = window.location.origin + '/mcp';
document.getElementById('mcp-url').textContent = endpoint;
document.getElementById('copy-url').addEventListener('click', async () => {
  const status = document.getElementById('copy-status');
  try {
    await navigator.clipboard.writeText(endpoint);
    status.textContent = 'Copied';
  } catch {
    status.textContent = 'Select and copy the endpoint above.';
  }
});
"""

HOME_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Device Performance SRE | MCP Connector</title>
  <style>{_STYLE}</style>
</head>
<body>
<main>
  <header>
    <div class="brand"><span class="mark">SRE</span> Device Performance</div>
    <span class="status"><span class="dot" aria-hidden="true"></span> Server online</span>
  </header>
  <div class="layout">
    <section aria-labelledby="page-title">
      <p class="eyebrow">Device analytics connector</p>
      <h1 id="page-title">Your connector<br>is running.</h1>
      <p class="intro">Connect your SRE agent to retrieve device metrics, compare observations and review performance summaries.</p>
      <a class="health" href="/healthz">Open health check &rarr;</a>
      <div class="steps">
        <h2>Connect your agent</h2>
        <ol>
          <li>Add an MCP connector in AgenticOrg or your MCP client.</li>
          <li>Use the endpoint shown here with Streamable HTTP.</li>
          <li>Configure the bearer header with your dedicated MCP token.</li>
          <li>Pass one serial number or a list when requesting device details.</li>
        </ol>
      </div>
    </section>
    <section class="card" aria-labelledby="connection-title">
      <h2 id="connection-title">Connection settings</h2>
      <span class="label">MCP endpoint</span>
      <div class="endpoint"><code id="mcp-url">/mcp</code></div>
      <button id="copy-url" type="button">Copy endpoint</button>
      <span class="copy-status" id="copy-status" role="status" aria-live="polite"></span>
      <div class="row"><span>Transport</span><span>Streamable HTTP</span></div>
      <div class="row"><span>Authentication</span><span>Bearer token</span></div>
      <div class="auth"><code>Authorization: Bearer &lt;your MCP token&gt;</code></div>
      <p class="help">Use the dedicated connector token from your secure deployment settings. Device metrics are available through an authenticated MCP client.</p>
    </section>
  </div>
  <footer>This page confirms that the HTTP server is running. MST access is checked when device details are requested.<br>Opening <code>/mcp</code> directly without its bearer token returns <code>401 Unauthorized</code>.</footer>
</main>
<script>{_SCRIPT}</script>
</body>
</html>
"""


def _hash(value):
    return base64.b64encode(hashlib.sha256(value.encode()).digest()).decode()


HOME_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; "
        f"style-src 'sha256-{_hash(_STYLE)}'; "
        f"script-src 'sha256-{_hash(_SCRIPT)}'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}
