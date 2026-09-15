export function verdictInfo(value, submission) {
  const raw = String(value || 'Pending');
  const key = raw.toLowerCase().replace(/[\s_-]/g, '');
  if (['ac', 'accepted', 'correct'].includes(key)) {
    return { code: 'AC', label: 'AC · Accepted', cls: 'accepted', icon: '✓' };
  }
  if (key === 'unknown') {
    return { label: submission?.qoj_id ? '等待 QOJ 结果' : '编号待确认', cls: 'pending', icon: '?' };
  }
  if (['pending', 'judging', 'running', 'compiling', 'waiting', 'submitted', 'submitting', 'queue', 'queued', 'sent'].includes(key)) {
    return { label: key === 'submitting' ? 'Submitting' : 'Pending', cls: 'pending', icon: '◷' };
  }
  const codes = {
    wa: 'WA', wronganswer: 'WA', tle: 'TLE', tl: 'TLE', timelimitexceeded: 'TLE',
    mle: 'MLE', ml: 'MLE', memorylimitexceeded: 'MLE', ole: 'OLE', ol: 'OLE', outputlimitexceeded: 'OLE',
    re: 'RE', rte: 'RE', runtimeerror: 'RE', runerror: 'RE', ce: 'CE', compileerror: 'CE', compilationerror: 'CE',
    compiletimeerror: 'CE', compilererror: 'CE', compilationfailed: 'CE', pe: 'PE', presentationerror: 'PE',
    je: 'JE', jf: 'JE', ie: 'JE', judgementfailed: 'JE', judgmentfailed: 'JE', judgeerror: 'JE',
    judgingerror: 'JE', systemerror: 'JE', internalerror: 'JE',
  };
  const names = {
    WA: 'Wrong Answer', TLE: 'Time Limit Exceeded', MLE: 'Memory Limit Exceeded', OLE: 'Output Limit Exceeded',
    RE: 'Runtime Error', CE: 'Compile Error', PE: 'Presentation Error', JE: 'Judgement Failed',
  };
  const code = Object.hasOwn(codes, key) ? codes[key] : undefined;
  return { code, label: code ? `${code} · ${names[code]}` : key === 'failed' ? 'Submission Failed' : raw, cls: 'wrong', icon: '×' };
}

// Remove only an entire synonymous verdict. Compiler output and other
// diagnostics must survive even when they contain a verdict name.
export function submissionMessage(submission) {
  const message = submission.message || (String(submission.verdict).toLowerCase() === 'unknown' && !submission.qoj_id ? '正在核对提交记录。' : '');
  const verdict = verdictInfo(submission.verdict, submission);
  if (typeof message !== 'string' || !verdict.code) return message;
  const plain = message.trim().replace(/\uFE0F/g, '');
  if (/^[\s✓✔√×✗✘✅❌?？]+$/u.test(plain)) return '';
  const undecorated = (text) => text.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '');
  const note = undecorated(undecorated(plain).replace(/^QOJ\s*:\s*/i, ''));
  if (verdictInfo(note).code === verdict.code) return '';
  const parts = note.split(/\s*[·/|]\s*|\s+[—–-]\s+/u);
  if (parts.length > 1 && parts.every((part) => verdictInfo(undecorated(part)).code === verdict.code)) return '';
  const score = /^(?:QOJ\s+score:\s*)?(\d+(?:\.\d+)?)\s*\/\s*(\d+(?:\.\d+)?)$/i.exec(note);
  if (verdict.code === 'AC' && score && Number.isFinite(Number(score[1])) && Number(score[1]) > 0 && Number(score[1]) === Number(score[2])) return '';
  return message;
}
