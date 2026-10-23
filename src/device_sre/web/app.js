'use strict';
(() => {
  const form = document.getElementById('device-form');
  const serialInput = document.getElementById('serial-numbers');
  const tokenInput = document.getElementById('connector-token');
  const submit = document.getElementById('fetch-metrics');
  const status = document.getElementById('request-status');
  const error = document.getElementById('request-error');
  const results = document.getElementById('results');
  const cards = document.getElementById('device-results');
  const empty = document.getElementById('empty-state');
  document.getElementById('mcp-url').textContent = window.location.origin + '/mcp';

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function label(value) {
    return String(value).replaceAll('_', ' ').replace(/\b\w/g, character => character.toUpperCase());
  }

  function number(value, suffix = '') {
    return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString(undefined, {maximumFractionDigits: 2}) + suffix : 'Unavailable';
  }

  function time(value) {
    if (!value) return 'Unavailable';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Unavailable' : date.toLocaleString(undefined, {timeZoneName: 'short'});
  }

  function badge(text, tone) { return element('span', text, 'badge ' + (tone || '')); }

  function metric(title, value) {
    const card = element('div', null, 'metric');
    card.append(element('div', title, 'metric-label'), element('div', value, 'metric-value'));
    return card;
  }



  function timing(title, value) {
    const row = element('div');
    row.append(element('span', title, 'timing-label'), element('span', value, 'timing-value'));
    return row;
  }

  function comparison(evaluation) {
    const section = element('section', null, 'comparison');
    section.append(element('h4', 'Comparison with previous observation'));
    const meanings = {
      baseline_created: 'Baseline created. Fetch again after the device reports a newer reading to compare performance.',
      no_new_telemetry: 'The device source timestamp has not changed. MST was requested again, but there is no newer telemetry to compare.',
      compared: 'Compared with the previous retained device reading.',
      out_of_order_telemetry: 'The returned device reading is older than the comparison baseline. A trend cannot be calculated.',
      source_timestamp_reused_with_changed_metrics: 'Metrics changed at the same source timestamp. Validate the collector before drawing a trend.',
      timestamp_basis_changed: 'The source timestamp basis changed. These observations are not directly comparable.',
      normalization_version_changed: 'The metric format changed. A compatible baseline is required.'
    };
    section.append(element('p', meanings[evaluation.baseline_status] || 'No comparable earlier observation is available.'));
    const deltas = Object.entries(evaluation.deltas || {});
    if (deltas.length) {
      const list = element('ul');
      for (const [key, delta] of deltas) {
        const unit = delta.unit === 'percentage_points' ? ' pp' : ' ' + delta.unit;
        const change = (delta.delta > 0 ? '+' : '') + number(delta.delta, unit);
        list.append(element('li', label(key) + ': ' + number(delta.previous) + ' → ' + number(delta.current) + ' (' + change + ')'));
      }
      section.append(list);
    }
    return section;
  }

  function deviceCard(device) {
    const card = element('article', null, 'device-card');
    const heading = element('div', null, 'device-heading');
    const identity = element('div');
    identity.append(element('h3', device.device_id));
    const snapshot = device.snapshot;
    const evaluation = device.evaluation || {};
    const context = snapshot?.device_context || {};
    const contextText = [context.manufacturer, context.model].filter(Boolean).join(' · ');
    if (contextText) identity.append(element('div', contextText, 'device-context'));
    heading.append(identity);
    const badges = element('div', null, 'badges');
    badges.append(badge(device.fetched_from_mst ? 'Fetched from MST' : snapshot ? 'Cached observation' : 'Fetch failed', device.fetched_from_mst ? 'good' : 'error'));
    if (snapshot) {
      badges.append(badge(evaluation.source_freshness === 'fresh' ? 'Fresh telemetry' : evaluation.source_freshness === 'stale' ? 'Stale telemetry' : 'Check source timing', evaluation.source_freshness === 'fresh' ? '' : 'warning'));
      badges.append(badge(snapshot.is_device_online === true ? 'Device reports online' : snapshot.is_device_online === false ? 'Device reports offline' : 'Online status unknown', snapshot.is_device_online === false ? 'warning' : ''));
    }
    heading.append(badges);
    card.append(heading);
    if (device.error_code) {
      const failure = element('div', null, 'device-error');
      failure.append(element('strong', label(device.error_code)), element('div', device.error_details?.message || 'The latest device fetch did not succeed.'));
      if (snapshot) failure.append(element('div', 'The metrics below are from an earlier cached observation.'));
      if (device.error_details?.next_steps?.length) {
        const list = element('ul');
        for (const step of device.error_details.next_steps) list.append(element('li', step));
        failure.append(list);
      }
      card.append(failure);
    }
    if (snapshot) {
      const metrics = snapshot.metrics || {};
      const grid = element('div', null, 'metrics-grid');
      grid.append(metric('CPU usage', number(metrics.cpu_used_percent, '%')));
      grid.append(metric('RAM used', number(metrics.ram_used_percent, '%')));
      grid.append(metric('Storage used', number(metrics.storage_used_percent, '%')));
      grid.append(metric('Active network', metrics.connection_mode === 0 ? 'Wi-Fi' : metrics.connection_mode === 1 ? 'Cellular' : 'Unknown'));
      card.append(grid);
      const timings = element('div', null, 'timings');
      timings.append(timing('Device source timestamp', time(snapshot.source_timestamp)), timing('Observation collected at', time(snapshot.observed_at)));
      if (evaluation.previous_observed_at) timings.append(timing('Previous observation', time(evaluation.previous_observed_at)));
      if (typeof evaluation.actual_interval_seconds === 'number') timings.append(timing('Observation interval', number(evaluation.actual_interval_seconds / 60, ' minutes')));
      card.append(timings, comparison(evaluation));
      const findings = evaluation.findings || [];
      const recommendations = element('section', null, 'recommendations');
      recommendations.append(element('h4', 'Performance review'));
      if (!findings.length) recommendations.append(element('p', 'No configured threshold findings for this observation. This does not establish overall device health.', 'field-help'));
      for (const finding of findings) {
        const row = element('div', null, 'finding ' + (finding.severity === 'warning' ? 'warning' : ''));
        row.append(element('strong', label(finding.code)), element('p', finding.recommendation));
        recommendations.append(row);
      }
      card.append(recommendations);
    }
    const details = element('details', null, 'raw-details');
    details.append(element('summary', 'View full metrics and response'), element('pre', JSON.stringify(device, null, 2)));
    card.append(details);
    return card;
  }

  async function fetchMetrics(serials, token, signal) {
    let id = 0;
    let protocol = '2025-03-26';
    let session;
    async function rpc(method, params, notification = false) {
      const headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream', 'Authorization': 'Bearer ' + token};
      if (method !== 'initialize') headers['MCP-Protocol-Version'] = protocol;
      if (session) headers['Mcp-Session-Id'] = session;
      const message = {jsonrpc: '2.0', method, params};
      if (!notification) message.id = ++id;
      const response = await fetch('/mcp', {method: 'POST', mode: 'same-origin', credentials: 'omit', redirect: 'error', headers, body: JSON.stringify(message), signal});
      if (response.status === 401) throw new Error('The MCP connector token was not accepted. Copy the dedicated connector token from your deployment settings and try again.');
      if (response.status === 403) throw new Error('This browser origin is not permitted. Open this page using the approved connector hostname.');
      if (!response.ok) throw new Error('The connector returned HTTP ' + response.status + '. Retry after the service has finished starting.');
      if (notification && response.status === 202) return null;
      if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('The service is not ready to return MCP data. Wait for startup, then try again.');
      const value = await response.json();
      if (value.error) throw new Error(value.error.message || 'The MCP request was rejected.');
      if (method === 'initialize') session = response.headers.get('Mcp-Session-Id');
      return value.result;
    }
    status.textContent = 'Authenticating with the connector…';
    const initialized = await rpc('initialize', {protocolVersion: protocol, capabilities: {}, clientInfo: {name: 'device-sre-browser', version: '1.0'}});
    protocol = initialized.protocolVersion;
    await rpc('notifications/initialized', {}, true);
    status.textContent = 'Fetching ' + serials.length + ' device' + (serials.length === 1 ? '' : 's') + ' through MST…';
    const value = await rpc('tools/call', {name: 'get_device_details', arguments: {serial_numbers: serials, refresh: true}});
    if (value.isError) throw new Error('The device request was rejected. Check your serial numbers and connector configuration.');
    let batch = value.structuredContent;
    if (!batch?.devices) {
      for (const item of value.content || []) {
        if (item.type !== 'text') continue;
        try { const parsed = JSON.parse(item.text); if (Array.isArray(parsed.devices)) { batch = parsed; break; } } catch { /* Ignore non-JSON content. */ }
      }
    }
    if (!Array.isArray(batch?.devices)) throw new Error('The connector returned an unexpected device response.');
    return batch;
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submit.disabled) return;
    error.hidden = true;
    serialInput.removeAttribute('aria-invalid');
    tokenInput.removeAttribute('aria-invalid');
    const token = tokenInput.value.trim();
    const entries = serialInput.value.trim().split(/[,\s]+/).filter(Boolean);
    const serials = [...new Set(entries)];
    if (!serials.length || entries.length > 1000 || serials.length > 100 || serials.some(value => !/^[A-Za-z0-9_.:-]{1,128}$/.test(value))) {
      error.textContent = 'Enter 1–100 distinct serial numbers using letters, digits, underscores, dots, colons or hyphens. Separate devices with commas or whitespace.';
      error.hidden = false; serialInput.setAttribute('aria-invalid', 'true'); serialInput.focus(); return;
    }
    if (token.length < 32 || !/^[\x21-\x7e]+$/.test(token)) {
      error.textContent = 'Paste the dedicated MCP connector token without the Bearer prefix. The token must contain at least 32 characters and no whitespace.';
      error.hidden = false; tokenInput.setAttribute('aria-invalid', 'true'); tokenInput.focus(); return;
    }
    submit.disabled = true;
    submit.textContent = 'Fetching metrics…';
    cards.replaceChildren(); results.hidden = true; empty.hidden = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 180000);
    try {
      const batch = await fetchMetrics(serials, token, controller.signal);
      for (const device of batch.devices) cards.append(deviceCard(device));
      document.getElementById('results-summary').textContent = batch.succeeded + ' succeeded · ' + batch.failed + ' failed · ' + batch.requested + ' requested';
      results.hidden = false;
      status.textContent = batch.failed ? 'Fetch complete with device errors. Review each result below.' : 'Live MST fetch complete. Device source times are shown below.';
    } catch (failure) {
      error.textContent = failure.name === 'AbortError' ? 'The request timed out. Some observations may have been collected; retry to request the latest readings.' : failure instanceof TypeError ? 'The connection was interrupted. Wait for the service to start, then retry.' : failure.message;
      error.hidden = false;
      status.textContent = 'The request did not complete.';
      empty.hidden = false;
    } finally {
      clearTimeout(timeout);
      submit.disabled = false;
      submit.textContent = 'Fetch device metrics';
    }
  });
})();
