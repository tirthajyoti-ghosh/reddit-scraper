// Robust challenge solver: runs Reddit's ACTUAL challenge JS in a sandbox with a
// minimal DOM shim, capturing whatever it writes into the form's "solution" field.
// Survives Reddit changing the transform (e.g. seed+seed -> hash(seed)).
const fs = require('fs');
const vm = require('vm');

const src = process.argv[2] === '-' ? fs.readFileSync(0, 'utf8') : fs.readFileSync(process.argv[2], 'utf8');
const scripts = [...src.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/gi)].map(m => m[1]);
const code = scripts.find(s => /requestSubmit|namedItem\(["']solution|solution/.test(s));
if (!code) { console.error('no challenge script found'); process.exit(1); }

const captured = {};
const handlers = [];
const field = (name) => ({ set value(v){ captured[name] = v; }, get value(){ return captured[name]; } });
const form = {
  set onsubmit(fn){}, get onsubmit(){ return null; },
  elements: { namedItem: (n) => field(n) },
  appendChild(){},
  requestSubmit(){},                       // no-op: solution already captured
};
const documentShim = {
  forms: [form],
  location: { search: '' },
  createElement: () => ({}),
  addEventListener: (_e, fn) => handlers.push(fn),
};
const sandbox = { document: documentShim, URLSearchParams, Object, console, setTimeout, clearTimeout };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);

(async () => {
  for (const h of handlers) await h();
  if (captured.solution) process.stdout.write(String(captured.solution));
  else { console.error('no solution captured'); process.exit(2); }
})();
