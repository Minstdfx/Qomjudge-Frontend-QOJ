// Keep QOJ's published values intact: the display format also carries frozen results.
export function scoreboardResult(c) {
  if (!c.canRead) return { rows: [], message: '' };
  const part = c.part || {};
  const message = typeof part.scoreboard_message === 'string' ? part.scoreboard_message
    : typeof c.state?.scoreboard_message === 'string' ? c.state.scoreboard_message : '';
  if (c.scoreboardError) return { error: c.scoreboardError };
  if (part.scoreboard_available === false || ['unavailable', 'error'].includes(part.scoreboard_status)) {
    return { error: message || 'QOJ 暂未返回可读取的榜单，请刷新重试。' };
  }
  const record = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
  const scalar = (value) => value == null || typeof value === 'string'
    || typeof value === 'number' && Number.isFinite(value);
  const validRow = (row) => record(row)
    && ['rank', 'team_name', 'username', 'solved', 'penalty'].every((key) => scalar(row[key]))
    && (row.problem_stats == null || record(row.problem_stats)
      && Object.values(row.problem_stats).every((cell) => record(cell) && scalar(cell.display) && scalar(cell.text)));
  const rows = c.state?.scoreboard;
  if (!Array.isArray(rows) || !rows.every(validRow)) {
    return { error: '服务器返回的榜单格式不正确，请刷新重试。' };
  }
  return { rows, message };
}

export function scoreboardSourceURL(c) {
  if (!c.canRead || c.state?.demo || typeof c.part?.scoreboard_url !== 'string') return undefined;
  try {
    const url = new URL(c.part.scoreboard_url);
    if (url.origin === 'https://qoj.ac' && !url.username && !url.password) return url.href;
  } catch {}
  return undefined;
}

export function scoreboardSourceLabel(c) {
  if (c.state?.demo) return '离线演示榜单';
  if (c.canRead && c.part?.scoreboard_source === 'qoj-pc2') return 'QOJ PC2 榜单';
  if (c.canRead && c.part?.scoreboard_source === 'qoj-external') return 'QOJ External Standings';
  return 'QOJ 榜单';
}

export function scoreboardCell(c, stat = {}) {
  if (!c.state?.demo) {
    const text = String(stat.display ?? stat.text ?? '');
    const source = c.part?.scoreboard_source;
    const external = source === 'qoj-external';
    const failed = stat.failed === true || !external && text.trim().startsWith('-');
    const empty = !text || external && text.trim() === '-';
    const status = stat.pending || text.trim().startsWith('?') ? 'pending'
      : stat.solved ? 'accepted' : failed ? 'wrong' : empty ? 'empty' : 'neutral';
    const firstBlood = ['qoj', 'qoj-external'].includes(source)
      && status === 'accepted' && stat.first_blood === true;
    const detail = firstBlood ? `${text}\nFirst to solve / 一血` : text;
    return {
      status, firstBlood, text: text || '\u00a0', detail,
      ariaLabel: firstBlood ? detail : undefined, demo: false,
    };
  }
  const attempts = Number(stat.attempts) || 0;
  const wrong = Number(stat.wrong) || 0;
  const status = stat.solved ? 'accepted' : stat.pending ? 'pending'
    : wrong ? 'wrong' : attempts ? 'neutral' : 'empty';
  const value = stat.solved ? stat.time ?? '—' : stat.pending ? '?' : wrong ? `−${wrong}` : '—';
  const detail = attempts
    ? `${stat.solved ? `第 ${stat.time} 分钟通过；` : ''}${attempts} 次模拟提交，${wrong} 次模拟错误`
    : '尚未提交';
  return {
    status, firstBlood: false, value, detail, attempts,
    hasResult: !!(stat.solved || stat.pending || wrong || attempts),
    ariaLabel: status === 'empty' ? '尚未提交' : undefined, demo: true,
  };
}
