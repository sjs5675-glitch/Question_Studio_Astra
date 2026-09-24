import { build } from 'esbuild';
import { readFile, writeFile, mkdir, cp, readdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.dirname(fileURLToPath(import.meta.url));
const out = path.join(root, 'dist/document');
const vendor = path.join(root, 'vendor/document');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
await mkdir(out, { recursive: true });
await mkdir(vendor, { recursive: true });
const lock = JSON.parse(await readFile(path.join(root, 'package-lock.json'), 'utf8'));
const notices = [];
for (const [relative, pkg] of Object.entries(lock.packages)) {
  if (!relative) continue;
  if (!pkg.integrity || !pkg.resolved.startsWith('https://registry.npmjs.org/')) throw Error(`Unpinned package: ${relative}`);
  const dir = path.join(root, relative);
  const files = await readdir(dir).catch(error => { if (error.code === 'ENOENT' && pkg.optional) return []; throw error; });
  for (const file of files.filter(name => /^(license|notice)(\.|$)/i.test(name))) {
    notices.push(`\n## ${relative} ${pkg.version} (${pkg.license})\n${await readFile(path.join(dir, file), 'utf8')}`);
  }
}
await writeFile(path.join(vendor, 'THIRD-PARTY-NOTICES.txt'), notices.join('\n'));
await cp(path.join(root, 'node_modules/mathlive/fonts'), path.join(out, 'mathlive-fonts'), { recursive: true });
await build({ absWorkingDir: root, entryPoints: ['document-entry.js'], bundle: true, format: 'esm', minify: true,
  outfile: path.join(out, 'document.js'), loader: { '.woff2': 'file', '.woff': 'file', '.ttf': 'file' },
  assetNames: 'fonts/[name]-[hash]', metafile: true }).then(result => writeFile(path.join(vendor, 'bundle-metafile.json'), JSON.stringify(result.metafile, null, 2)));

// Read the actual font name table: code MIT is not a font embedding license.
function fontNames(bytes) {
  const names = {};
  for (let i = 0; i < bytes.readUInt16BE(4); i++) {
    const table = 12 + i * 16;
    if (bytes.toString('ascii', table, table + 4) !== 'name') continue;
    const offset = bytes.readUInt32BE(table + 8);
    for (let j = 0; j < bytes.readUInt16BE(offset + 2); j++) {
      const record = offset + 6 + j * 12;
      const id = bytes.readUInt16BE(record + 6);
      if (![0, 1, 13, 14].includes(id)) continue;
      const start = offset + bytes.readUInt16BE(offset + 4) + bytes.readUInt16BE(record + 10);
      const value = Buffer.from(bytes.subarray(start, start + bytes.readUInt16BE(record + 8)));
      names[id] = bytes.readUInt16BE(record) === 3 ? value.swap16().toString('utf16le') : value.toString('latin1');
    }
  }
  return names;
}
const fontInventory = [];
for (const file of await readdir(path.join(out, 'mathlive-fonts'))) {
  const bytes = await readFile(path.join(out, 'mathlive-fonts', file));
  const katex = await readFile(path.join(root, 'node_modules/katex/dist/fonts', file));
  if (hash(bytes) !== hash(katex)) throw Error(`MathLive font differs: ${file}`);
  const metadata = fontNames(await readFile(path.join(root, 'node_modules/katex/dist/fonts', file.replace('.woff2', '.ttf'))));
  if (!metadata[13]?.includes('SIL Open Font License')) throw Error(`Missing font license: ${file}`);
  fontInventory.push({ file, sha256: hash(bytes), sameAsKaTeX: true, license: 'OFL-1.1', metadata });
}
for (const family of ['noto-sans-kr', 'noto-serif-kr']) {
  const license = await readFile(path.join(root, 'node_modules/@fontsource', family, 'LICENSE'), 'utf8');
  await writeFile(path.join(vendor, `${family}-OFL.txt`), license);
  fontInventory.push({ family, package: `@fontsource/${family}@5.3.0`, license: 'OFL-1.1', licenseSha256: hash(license) });
}
await writeFile(path.join(vendor, 'font-license-inventory.json'), JSON.stringify(fontInventory, null, 2));
await cp(vendor, path.join(out, 'notices'), { recursive: true });
const files = [];
async function inventory(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const name = path.join(dir, entry.name);
    if (entry.isDirectory()) await inventory(name);
    else if (entry.name !== 'manifest.json') {
      const bytes = await readFile(name);
      files.push({ path: path.relative(out, name).replaceAll('\\', '/'), size: bytes.length, sha256: hash(bytes) });
    }
  }
}
await inventory(out);
await writeFile(path.join(out, 'manifest.json'), JSON.stringify({ node: process.version, lockSha256: hash(await readFile(path.join(root, 'package-lock.json'))), files }, null, 2));
console.log(JSON.stringify({ files: files.length, fonts: files.filter(file => /\.(woff2?|ttf)$/.test(file.path)).length }));
