"""Explain controlled failure codes without returning upstream bodies or secrets."""


def error_details(code):
    messages = {
        "mst_credential_unavailable": (
            "credential",
            "No usable MST credential is available. Device collection cannot continue.",
            ["Set MST_TOKEN_FILE in .env and run ./run-local.sh configure-token in your terminal to save the approved MST token with hidden input, without the Bearer prefix.",
             "Alternatively unset MST_TOKEN_FILE and securely provide MST_BEARER_TOKEN in the server environment.",
             "Retry get_device_details with the requested serial_numbers and refresh=true."],
        ),
        "mst_http_401": (
            "mst_authentication", "MST rejected the credential (HTTP 401).",
            ["Refresh or replace the approved MST credential and retry. The separate MCP token does not authenticate to MST."],
        ),
        "mst_http_403": (
            "mst_authorization", "MST denied this request (HTTP 403).",
            ["Verify the token's analytics scope, accepted client and merchant/device access with the MST owner. Do not bypass authorization."],
        ),
        "mst_http_404": (
            "mst_http", "MST returned HTTP 404; the response does not establish whether this device exists.",
            ["Verify the deployed MST route and the requested hardware serial with the service owner."],
        ),
        "empty_device_details": (
            "mst_response", "MST returned no usable device metrics, including possibly HTTP 200 with null data.",
            ["Check analytics telemetry availability and MST forwarding for this serial. This response alone does not identify an absent device or the upstream failure cause."],
        ),
        "device_identity_mismatch": (
            "response_validation", "The returned device identity does not match the requested serial. The response was not cached.",
            ["Check the response device_id and MST forwarding. Never relabel another device's metrics as the requested device."],
        ),
        "app_device_identity_mismatch": (
            "response_validation", "The app inventory belongs to a different device. The response was not cached.",
            ["Check the app_data.device_id and device_details.device_id returned by MST for the requested serial."],
        ),
        "missing_source_timestamp": (
            "response_validation", "The response has no usable device source timestamp.",
            ["Check device_details.timestamp and last_updated. A Mongo _id date cannot replace a metric observation timestamp."],
        ),
        "no_cached_observation": (
            "cache", "No successful observation is cached for this serial. A cache-only call does not fetch MST.",
            ["Call get_device_details with this serial_numbers value and refresh=true after configuring the MST credential."],
        ),
        "observation_not_retained": (
            "cache", "One or both requested comparison observations are not retained for this serial.",
            ["Read get_device_history for this device and use two exact observed_at timestamps. A new device needs its own baseline and a later reading."],
        ),
        "replay_sample_missing": (
            "offline_replay", "No supplied response file matches this requested serial. No live MST request was made.",
            ["Supply a response file whose data.device_details.device_id matches this serial using --offline --file.",
             "To fetch a different device live, use scripts/check_mcp.py with an approved MST credential; replay mode uses supplied files only."],
        ),
    }
    if code in messages:
        stage, message, steps = messages[code]
    elif code == "mst_transport_error":
        stage, message, steps = (
            "mst_transport", "The MST request failed after bounded transport retries.",
            ["Check the server's network/VPN, DNS, TLS trust and MST connectivity, then retry."],
        )
    elif code in ("mst_invalid_json", "mst_response_too_large"):
        stage, message, steps = (
            "mst_response", "The MST response could not be safely parsed.",
            ["Check the deployed analytics response contract and gateway behavior with the service owner."],
        )
    else:
        stage, message, steps = (
            "collection", "Device collection failed; no new successful observation was stored.",
            ["Review error_code with the MST owner and retry after resolving the underlying failure."],
        )
    return {"stage": stage, "message": message, "next_steps": steps}
