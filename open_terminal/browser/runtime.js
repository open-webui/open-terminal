(() => {
  const config = window.__terminalBrowser;
  if (!config) return;

  const api = new URL(config.prefix + "/", location.href).pathname.replace(/\/$/, "");
  const prefix = `${api}/frame`;
  const nativeFetch = window.fetch.bind(window);
  const nativeWebSocket = window.WebSocket;

  function remoteUrl(value = location.href) {
    const url = new URL(value, location.href);
    if (url.pathname === prefix) return url.searchParams.get('url') || config.url;
    if (!url.pathname.startsWith(`${prefix}/`)) return url.href;
    const [scheme, encodedHost, ...path] = url.pathname.slice(prefix.length + 1).split('/');
    if (!scheme || !encodedHost) return config.url;
    return `${scheme}://${decodeURIComponent(encodedHost)}/${path.join('/')}${url.search}${url.hash}`;
  }

  function proxyUrl(value, base = remoteUrl()) {
    try {
      const current = new URL(value, location.href);
      if (current.pathname.startsWith(`${prefix}/`)) return `${current.pathname}${current.search}${current.hash}`;
      let url = new URL(value, base);
      if (url.origin === location.origin && !url.pathname.startsWith(prefix)) {
        url = new URL(url.pathname + url.search + url.hash, config.url);
      }
      if (!/^https?:$/.test(url.protocol)) return value;
      return `${prefix}/${url.protocol.slice(0, -1)}/${encodeURIComponent(url.host)}${url.pathname}${url.search}${url.hash}`;
    } catch {
      return value;
    }
  }

  function report(replace = false) {
    parent.postMessage({ type: 'terminal-browser-state', url: remoteUrl(), title: document.title, replace: replace === true }, '*');
  }

  const originalPushState = history.pushState.bind(history);
  const originalReplaceState = history.replaceState.bind(history);
  history.pushState = (state, title, url) => {
    let result;
    try { result = originalPushState(state, title, url ? proxyUrl(url) : url); }
    catch (error) { if (error.name !== 'SecurityError') throw error;
      parent.postMessage({type:'terminal-browser-navigate', url:new URL(url, remoteUrl()).href}, '*'); return; }
    queueMicrotask(report);
    return result;
  };
  history.replaceState = (state, title, url) => {
    let result;
    try { result = originalReplaceState(state, title, url ? proxyUrl(url) : url); }
    catch (error) { if (error.name !== 'SecurityError') throw error;
      parent.postMessage({type:'terminal-browser-navigate', url:new URL(url, remoteUrl()).href, replace:true}, '*'); return; }
    queueMicrotask(() => report(true));
    return result;
  };
  addEventListener('popstate', report);
  addEventListener('pageshow', report);
  new MutationObserver(report).observe(document.querySelector('title') || document.head, {
    childList: true,
    subtree: true,
    characterData: true
  });

  window.fetch = (input, init) => {
    if (input instanceof Request) return nativeFetch(new Request(proxyUrl(input.url), input), { ...init, credentials: "omit" });
    return nativeFetch(proxyUrl(input), { ...init, credentials: "omit" });
  };

  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    return open.call(this, method, proxyUrl(url), ...rest);
  };

  window.WebSocket = function ProxyWebSocket(url, protocols) {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const native = new nativeWebSocket(`${scheme}://${location.host}${api}/ws`);
    const proxy = new EventTarget();
    let state = 0;
    let protocol = '';
    Object.defineProperties(proxy, {
      url: { value: String(url) },
      readyState: { get: () => state },
      protocol: { get: () => protocol },
      binaryType: { get: () => native.binaryType, set: (value) => { native.binaryType = value; } },
      onopen: { get: () => proxy._onopen, set: (value) => { proxy._onopen = value; } },
      onmessage: { get: () => proxy._onmessage, set: (value) => { proxy._onmessage = value; } },
      onclose: { get: () => proxy._onclose, set: (value) => { proxy._onclose = value; } },
      onerror: { get: () => proxy._onerror, set: (value) => { proxy._onerror = value; } }
    });
    const emit = (type, event) => {
      proxy.dispatchEvent(event);
      proxy[`_on${type}`]?.call(proxy, event);
    };
    native.addEventListener('open', () => {
      const target = new URL(url, remoteUrl());
      if (target.host === location.host) {
        const upstream = new URL(config.url);
        target.host = upstream.host;
        target.protocol = upstream.protocol === 'https:' ? 'wss:' : 'ws:';
      }
      native.send(JSON.stringify({ url: target.href, protocols }));
    });
    native.addEventListener('message', (event) => {
      if (typeof event.data === 'string') {
        try {
          const control = JSON.parse(event.data);
          if (state === 0 && control.type === 'open') {
            state = 1; protocol = control.protocol || ''; emit('open', new Event('open')); return;
          }
        } catch {}
      }
      emit('message', new MessageEvent('message', { data: event.data }));
    });
    native.addEventListener('close', (event) => { state = 3; emit('close', new CloseEvent('close', {code:event.code,reason:event.reason,wasClean:event.wasClean})); });
    native.addEventListener('error', (event) => emit('error', new Event('error')));
    proxy.send = (data) => {
      if (state !== 1) throw new DOMException('WebSocket is not open', 'InvalidStateError');
      native.send(data);
    };
    proxy.close = (code, reason) => { state = 2; native.close(code, reason); };
    return proxy;
  };
  window.WebSocket.prototype = nativeWebSocket.prototype;
  for (const [name, value] of Object.entries({CONNECTING:0,OPEN:1,CLOSING:2,CLOSED:3})) {
    Object.defineProperty(window.WebSocket, name, {value});
  }

  const NativeEventSource = window.EventSource;
  if (NativeEventSource) {
    window.EventSource = function ProxyEventSource(url, options) {
      return new NativeEventSource(proxyUrl(url), options);
    };
    window.EventSource.prototype = NativeEventSource.prototype;
  }

  document.addEventListener('click', (event) => {
    const link = event.target.closest?.('a[href]');
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const url = new URL(remoteUrl(link.href));
    if (!/^https?:$/.test(url.protocol)) return;
    event.preventDefault();
    if (link.target && link.target !== '_self') {
      parent.postMessage({ type: 'terminal-browser-popup', url: url.href }, '*');
    } else {
      location.assign(proxyUrl(url.href));
    }
  }, true);

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const url = new URL(remoteUrl(form.action || location.href));
    if (/^https?:$/.test(url.protocol)) form.action = proxyUrl(url.href);
  }, true);

  window.open = (url, target, features) => {
    if (target === '_self') location.assign(proxyUrl(url || remoteUrl()));
    else if (url) parent.postMessage({ type: 'terminal-browser-popup', url: new URL(url, remoteUrl()).href }, '*');
    return null;
  };

  addEventListener('message', event => {
    if(event.source !== parent || event.data?.type !== 'terminal-browser-command') return;
    if(event.data.command === 'back') history.back();
    if(event.data.command === 'forward') history.forward();
    if(event.data.command === 'reload') location.reload();
    if(event.data.command === 'navigate') location.assign(proxyUrl(event.data.url));
    if(event.data.command === 'replace') location.replace(proxyUrl(event.data.url));
  });
  addEventListener('hashchange', report);
  report();
})();
