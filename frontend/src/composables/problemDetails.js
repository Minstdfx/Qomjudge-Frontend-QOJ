export const statementSandbox = 'allow-same-origin allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads';

export async function getProblemDetails(c, id, { refresh = false, signal } = {}) {
  if (!c.canRead) throw new Error('这台电脑已没有访问比赛题目的权限。');
  const key = String(id);
  if (refresh) c.detailsCache.delete(key);
  if (c.detailsCache.has(key)) return c.detailsCache.get(key);
  const context = c.context;
  const result = await c.api(`/api/problems/${encodeURIComponent(id)}${refresh ? '?refresh=1' : ''}`, undefined, { signal });
  if (signal?.aborted || context !== c.context || !c.canRead) throw new DOMException('Request cancelled', 'AbortError');
  if (!result || typeof result !== 'object') throw new Error('服务器返回了无效的题目信息。');
  if (typeof result.statement_url === 'string' && result.statement_url) c.detailsCache.set(key, result);
  return result;
}

export function safeStatementPDF(value, problemId) {
  if (typeof value !== 'string' || !value) return null;
  try {
    const url = new URL(value, location.origin);
    const prefix = `/api/problems/${encodeURIComponent(problemId)}/pdf/`;
    return url.origin === location.origin && !url.username && !url.password && url.pathname.startsWith(prefix)
      && /^[a-f0-9]{24}$/.test(url.pathname.slice(prefix.length)) && !url.search && !url.hash ? url : null;
  } catch { return null; }
}

export async function checkStatementPDF(url, signal) {
  let response;
  try { response = await fetch(url.href, { method: 'HEAD', credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal }); }
  catch (error) { if (error.name === 'AbortError') throw error; throw new Error('无法连接 PDF 题面，请检查网络后重试。'); }
  if (!response.ok) throw new Error([401, 403].includes(response.status) ? `当前账号或电脑无权访问此 PDF 题面（HTTP ${response.status}）。` : `PDF 题面请求失败（HTTP ${response.status}），请重试。`);
  if (!/^application\/pdf\b/i.test(response.headers.get('Content-Type') || '')) throw new Error('服务器返回的 PDF 题面格式无效，请重试。');
}

export async function fetchStatement(value, id, signal, pdfs) {
  let url;
  try { url = new URL(value, location.origin); } catch { throw new Error('服务器返回了无效的题面地址。'); }
  if (url.origin !== location.origin || url.username || url.password || url.pathname !== `/api/problems/${encodeURIComponent(id)}/statement` || url.search || url.hash) throw new Error('服务器返回了无效的题面地址。');
  let response;
  try { response = await fetch(url.href, { credentials: 'same-origin', headers: { Accept: 'text/html' }, cache: 'no-store', redirect: 'error', signal }); }
  catch (error) { if (error.name === 'AbortError') throw error; throw new Error('无法加载题面，请检查网络连接后重试。'); }
  if (!response.ok) {
    let message = `题面请求失败（HTTP ${response.status}）。`;
    if (/^application\/json\b/i.test(response.headers.get('Content-Type') || '')) {
      try { const result = await response.json(); if (typeof result.error === 'string' && result.error.trim()) message = result.error; }
      catch (error) { if (error.name === 'AbortError') throw error; }
    }
    throw new Error(message);
  }
  if (!/^text\/html\b/i.test(response.headers.get('Content-Type') || '')) throw new Error('服务器返回的题面格式无效，请重试。');
  const html = await response.text();
  if (!html.trim()) throw new Error('服务器返回了空题面，请重试。');
  // This document belongs exclusively to the statement iframe. Preserve the
  // server-sanitized markup and MathJax while avoiding nested native PDF viewers.
  const document = new DOMParser().parseFromString(html, 'text/html');
  const frames = [...document.querySelectorAll('iframe')];
  if (frames.length) {
    const links = document.createElement('p');
    if (pdfs.length) {
      links.append('PDF 题面可通过上方格式选项查看，或在新窗口打开：');
      pdfs.forEach((pdf) => {
        const link = document.createElement('a');
        link.href = pdf.url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = pdf.name;
        links.append(document.createElement('br'), link);
      });
    } else links.textContent = '此处的 PDF 题面暂不可用，请重新加载题面。';
    frames[0].replaceWith(links);
    frames.slice(1).forEach((frame) => frame.remove());
  }
  // A srcdoc document does not inherit the fetched response's CSP header.
  const policy = document.createElement('meta');
  policy.setAttribute('http-equiv', 'Content-Security-Policy');
  policy.content = response.headers.get('Content-Security-Policy') || "default-src 'none'; script-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; font-src 'self'; frame-src 'self'; object-src 'self'; base-uri 'none'; form-action 'none'";
  document.head.prepend(policy);
  return '<!doctype html>' + document.documentElement.outerHTML;
}
