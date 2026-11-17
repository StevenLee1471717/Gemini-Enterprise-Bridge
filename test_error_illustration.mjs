import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

for (const filename of ['page-driver.js', 'extension/page-driver.js']) {
  const picture = (src) => ({ src, complete: true, naturalWidth: 256, naturalHeight: 256,
    getBoundingClientRect: () => ({ width: 256, height: 256 }) });
  const illustration = picture('https://www.gstatic.com/vertexaisearch/images/error_background.png');
  const generated = picture('data:image/png;base64,aGVsbG8=');
  const previous = picture('data:image/png;base64,b2xk');
  const images = [illustration, generated, previous];
  const fetches = [];
  const sandbox = {
    fetch: async (url) => { fetches.push(url); throw new TypeError('Failed to fetch'); },
    XMLHttpRequest: function () {}, TextDecoder, Headers, URL,
    location: { origin: 'https://vertexaisearch.cloud.google.com' },
    document: { querySelectorAll: (selector) => selector === 'img' ? images : [] },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }),
    console: { log() {}, warn() {}, error() {} },
    postMessage() {}, addEventListener() {}, removeEventListener() {},
  };
  sandbox.window = sandbox; sandbox.globalThis = sandbox;
  sandbox.XMLHttpRequest.prototype = { open() {}, send() {} };
  vm.createContext(sandbox);
  const source = fs.readFileSync(new URL(filename, import.meta.url), 'utf8');
  vm.runInContext(source.replace('    executePromptJob,',
    '    generatedImageNodes, readGeneratedImages, executePromptJob,'), sandbox);
  const api = sandbox.__GEB_MAIN_API__;
  const result = await api.readGeneratedImages(api.generatedImageNodes(new Set([previous.src])));
  assert.equal(result.length, 1);
  assert.equal(result[0].data_url, generated.src);
  assert.equal(fetches.length, 0, 'the website error illustration must never be downloaded as model output');
}
console.log('PASS: website error illustration excluded; generated images and baseline filtering preserved (both copies)');
