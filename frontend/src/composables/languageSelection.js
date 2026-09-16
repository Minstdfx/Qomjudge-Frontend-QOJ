// Compare language standards, not compiler versions or the upstream option order.
const cppPattern = /(?:c\+\+|g\+\+|gnu\+\+|cpp)/i;
const standards = { '0x': 2011, '1y': 2014, '1z': 2017, '2a': 2020, '2b': 2023, '2c': 2026 };
const patterns = {
  cpp: cppPattern, cc: cppPattern, cxx: cppPattern,
  c: /^(gnu\s*)?c(?:\d|\s|$)|clang\s*c\b/i, py: /python\s*3|pypy\s*3|python|pypy/i,
  java: /java/i, kt: /kotlin/i, rs: /rust/i, go: /go(?:lang)?\b/i, pas: /pascal/i,
  js: /javascript|node/i, cs: /c#|csharp/i, hs: /haskell/i, rb: /ruby/i,
};

function languageText(option) { return `${option.label || ''} ${option.value || ''}`; }

export function cppStandard(option) {
  let newest = -1;
  const pattern = /(?:c\+\+|g\+\+|gnu\+\+|cpp)\s*(20\d{2}|1998|0x|1y|1z|2a|2b|2c|\d{2})\b/gi;
  for (const match of languageText(option).matchAll(pattern)) {
    const token = match[1].toLowerCase();
    const year = standards[token] ?? (token.length === 4 ? Number(token) : Number(token) + (Number(token) >= 80 ? 1900 : 2000));
    newest = Math.max(newest, year);
  }
  return newest;
}

export function latestCppLanguage(options) {
  // Keep the upstream order as a stable tie-breaker between equivalent compilers.
  return options.filter((option) => cppPattern.test(languageText(option)))
    .reduce((best, option) => !best || cppStandard(option) > cppStandard(best) ? option : best, null);
}

export function inferLanguage(options, filename = '', source = '') {
  let extension = filename.split('.').pop().toLowerCase();
  // Source detection is deliberately conservative; an explicit file suffix wins.
  if ((!patterns[extension] || extension === 'txt') && /#\s*include\s*[<"](?:bits\/stdc\+\+\.h|iostream|vector|string|algorithm|array|map|set|unordered_map)[>"]|\bstd::|\busing\s+namespace\s+std\s*;|\btemplate\s*</.test(source)) {
    extension = 'cpp';
  }
  if (['cpp', 'cc', 'cxx'].includes(extension)) return latestCppLanguage(options);
  const pattern = patterns[extension];
  return pattern ? options.find((option) => pattern.test(languageText(option))) || null : null;
}
