// Publish the precompiled Vue UI for Flask. Preserve statement support files.
import { cp, readFile, readdir, rename, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const build = new URL('../frontend-dist/', import.meta.url);
const target = new URL('../static/', import.meta.url);
const html = await readFile(new URL('index.html', build), 'utf8');
if (!html.includes('/static/assets/')) throw new Error('Missing compiled frontend assets');
for (const entry of await readdir(build, { withFileTypes: true })) {
  if (entry.name !== 'index.html') await cp(new URL(entry.name, build), new URL(entry.name, target), { recursive: true });
}
// Switch the entry only after all referenced assets are present; existing tabs
// can finish loading the previous bundle, so old hashed assets are retained.
await writeFile(new URL('index.html.next', target), html);
await rename(new URL('index.html.next', target), new URL('index.html', target));
console.log(`Vue frontend published to ${fileURLToPath(target)}`);
