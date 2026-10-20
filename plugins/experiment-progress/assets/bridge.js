/* MCP Apps bridge for Codex: JSON-RPC over postMessage with the host (ui/initialize, tools/call, notifications).
   Shared by the track card and the observatory; the observatory's fetch layer is answered through EP_BRIDGE. */
(() => {
  if (window.EPHost) return;
  let next = 0;
  const pending = new Map(), handlers = {};
  const send = message => parent.postMessage({jsonrpc: '2.0', ...message}, '*');
  const rpc = (method, params) => new Promise((resolve, reject) => {
    const id = ++next;
    pending.set(id, {resolve, reject});
    send({id, method, params});
    setTimeout(() => { if (pending.delete(id)) reject(Error(method + ' timed out')); }, 20000);
  });
  addEventListener('message', event => {
    if (event.source !== parent) return;
    const m = event.data;
    if (!m || m.jsonrpc !== '2.0') return;
    if (m.id != null && !m.method) {
      const p = pending.get(m.id);
      if (p) { pending.delete(m.id); m.error ? p.reject(Error(m.error.message || 'host error')) : p.resolve(m.result); }
      return;
    }
    for (const f of handlers[m.method] || []) { try { f(m.params || {}); } catch (err) { console.error(err); } }
    if (m.id != null) send({id: m.id, result: {}});
  });
  const host = {
    context: {},
    on: (method, f) => (handlers[method] ||= []).push(f),
    rpc,
    notify: (method, params) => send({method, params}),
    resize: height => send({method: 'ui/notifications/size-changed', params: {height: Math.ceil(height)}}),
    displayMode: mode => rpc('ui/request-display-mode', {mode}),
  };
  const apply = () => {
    const c = host.context, root = document.documentElement;
    if (c.theme) root.dataset.hostTheme = c.theme;
    for (const [k, v] of Object.entries(c.styles?.variables || {})) if (v) root.style.setProperty(k, v);
    dispatchEvent(new CustomEvent('ep-host-context', {detail: c}));
  };
  host.on('ui/notifications/host-context-changed', c => { Object.assign(host.context, c); apply(); });
  host.ready = rpc('ui/initialize', {protocolVersion: '2026-01-26', appCapabilities: {},
                                     appInfo: {name: 'experiment-progress', version: '0.1.0'}})
    .then(r => { Object.assign(host.context, r?.hostContext || {}); apply(); send({method: 'ui/notifications/initialized', params: {}}); return r; });
  host.call = async (name, args) => {
    await host.ready;
    const r = await rpc('tools/call', {name, arguments: args || {}});
    if (r?.isError) throw Error(r.content?.[0]?.text || name + ' failed');
    return r.structuredContent;
  };
  window.EP_BRIDGE = async path => {
    let m;
    if (path === '/api/runs') return (await host.call('experiment.runs')).runs;
    if ((m = path.match(/^\/api\/run\/([a-f0-9]{12})\/tasks$/))) return host.call('experiment.tasks', {run_id: m[1]});
    if ((m = path.match(/^\/api\/run\/([a-f0-9]{12})$/))) return host.call('experiment.snapshot', {run_id: m[1], full: true});
    throw Error('unsupported ' + path);
  };
  window.EPHost = host;
})();
