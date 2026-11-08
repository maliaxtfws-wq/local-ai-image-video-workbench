const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const functions = script.slice(0, script.indexOf('prompt.addEventListener("input"'));

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
}

async function until(check) {
  for (let i = 0; i < 500; i++) {
    if (check()) return;
    await new Promise(r => setTimeout(r, 1));
  }
  assert.fail('Image batch did not reach the expected state');
}

function harness() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, textContent: '', disabled: false, files: [],
      src: '', href: '', download: '', title: '',
      classList: {toggle() {}, add() {}, remove() {}},
      focus() {}, setAttribute() {},
    });
    return elements.get(id);
  };
  const context = vm.createContext({document: {getElementById: get}, Intl, FormData, setTimeout, clearTimeout});
  vm.runInContext(functions + `
    renderImageBatchQueue = () => {};
    previewImageBatchResult = () => {};
    globalThis.control = {state, startImageBatchGeneration, stopImageBatchGeneration, toggleImageBatchPause};
  `, context);
  const control = context.control;
  control.state.imageReady = true;
  control.state.cutoutReady = true;
  control.state.imageBatch.files = [0, 1, 2].map(i => ({selectionId: 'selection', serverFileId: 'image-' + i}));
  control.state.imageBatch.items = [0, 1, 2].map(i => ({index: i, path: i + '.jpg', status: 'pending'}));
  get('prompt').value = 'original image prompt';
  get('imageMode').value = 'edit';
  get('imageQuality').value = 'draft';
  const submissions = [], cancellations = [], jobs = [];
  const health = deferred();
  context.mockApi = async (url, options) => {
    if (url === '/api/status') return health.promise;
    if (url === '/api/images/generate') {
      submissions.push({file: options.body.get('batch_file_id'), prompt: options.body.get('prompt')});
      const completion = deferred();
      jobs.push(completion);
      return {job: {id: String(jobs.length - 1)}};
    }
    if (url.endsWith('/cancel')) { cancellations.push(url); return {}; }
    throw new Error('Unexpected request: ' + url);
  };
  context.mockWait = id => jobs[Number(id)].promise;
  vm.runInContext('api = mockApi; waitForImageBatchJob = mockWait;', context);
  const complete = (i, status = 'completed') => jobs[i].resolve({
    id: String(i), status,
    image_url: status === 'completed' ? '/image.png' : null,
  });
  return {
    control, get, submissions, cancellations, complete, health,
    ready: () => health.resolve({image_models: {ready: true}, active_job: null, active_image_job: null, active_sequence: null}),
  };
}

test('image batch finishes the active image before pausing and resumes remaining references once', async () => {
  const h = harness();
  h.ready();
  const running = h.control.startImageBatchGeneration();
  await until(() => h.submissions.length === 1);
  h.control.toggleImageBatchPause();
  assert.equal(h.get('pauseImageBatchButton').textContent, '取消暂停');
  assert.deepEqual(h.cancellations, []);
  h.complete(0);
  await until(() => h.control.state.imageBatch.paused);
  assert.equal(h.submissions.length, 1);
  assert.equal(h.get('pauseImageBatchButton').textContent, '继续生成');
  assert.equal(h.get('prompt').disabled, true);
  h.get('prompt').value = 'changed while paused';
  h.control.toggleImageBatchPause();
  await until(() => h.submissions.length === 2);
  h.complete(1);
  await until(() => h.submissions.length === 3);
  h.complete(2);
  await running;
  assert.deepEqual(h.submissions.map(s => s.file), ['image-0', 'image-1', 'image-2']);
  assert.ok(h.submissions.every(s => s.prompt === 'original image prompt'));
  assert.equal(h.control.state.imageBatch.running, false);
  assert.equal(h.get('imageBatchProgress').value, 3);
});

test('image batch paused before submission can stop without creating a job', async () => {
  const h = harness();
  const running = h.control.startImageBatchGeneration();
  h.control.toggleImageBatchPause();
  h.ready();
  await until(() => h.control.state.imageBatch.paused);
  assert.equal(h.submissions.length, 0);
  await h.control.stopImageBatchGeneration();
  await running;
  assert.deepEqual(h.cancellations, []);
  assert.equal(h.control.state.imageBatch.running, false);
  assert.equal(h.control.state.imageBatch.resumeWaiter, null);
});

test('image batch failure stops before the next reference is submitted', async () => {
  const h = harness();
  h.ready();
  const running = h.control.startImageBatchGeneration();
  await until(() => h.submissions.length === 1);
  h.complete(0, 'failed');
  await running;
  assert.equal(h.submissions.length, 1);
  assert.equal(h.control.state.imageBatch.running, false);
  assert.equal(h.control.state.imageBatch.items[1].status, 'cancelled');
});
