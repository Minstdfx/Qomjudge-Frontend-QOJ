export function duration(seconds) {
  const n = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${String(Math.floor(n / 3600)).padStart(2, '0')}:${String(Math.floor(n / 60) % 60).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}`;
}
export const timeOfDay = seconds => new Date(Number(seconds) * 1000).toLocaleTimeString('zh-CN', { hour12: false });
export function dateTime(value) {
  const numeric = Number(value);
  const date = new Date(Number.isFinite(numeric) && numeric > 0 ? numeric * 1000 : value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false });
}
export const color = value => /^#[0-9a-f]{3}([0-9a-f]{3})?$/i.test(value || '') ? value : '#d9eaf7';
export function foreground(hex) {
  let s = color(hex).slice(1); if (s.length === 3) s = s.split('').map(c => c + c).join('');
  const n = parseInt(s, 16);
  return (n >> 16) * .299 + ((n >> 8) & 255) * .587 + (n & 255) * .114 > 165 ? '#32485b' : '#ffffff';
}
export function bytes(value) {
  const n = Number(value); if (!Number.isFinite(n) || n < 0) return '—';
  return n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KiB` : `${(n / (1024 * 1024)).toFixed(1)} MiB`;
}
export function safeQojURL(value, id) {
  if (value) { try { const u = new URL(value, 'https://qoj.ac'); if (u.protocol === 'https:' && u.hostname === 'qoj.ac' && !u.username && !u.password && !u.port) return u.href; } catch {} }
  return id ? `https://qoj.ac/submission/${encodeURIComponent(id)}` : null;
}
export function safeAttachmentURL(value, external = false) {
  if (typeof value !== 'string' || !value) return null;
  try {
    const u = new URL(value, location.origin);
    if (u.username || u.password) return null;
    if (external) return ['https:', 'http:'].includes(u.protocol) ? u.href : null;
    return u.origin === location.origin && /^\/api\/attachments\/[^/]+\/download$/.test(u.pathname) && !u.search && !u.hash ? u.href : null;
  } catch { return null; }
}
