const { test } = require('node:test');
const assert = require('node:assert/strict');
const { safeName, responseName, fetchChecked, validPdf, unusedName, retryDelay, runPool } = require('./wulou-teacher-pdf-downloader.user.js');

test('保留中文专题名称，清理 Windows 非法名称', () => {
  assert.equal(safeName('专题6：反比例函数'), '专题6：反比例函数');
  assert.equal(safeName('a/b:c. '), 'a-b-c');
  assert.equal(safeName('CON.pdf'), '资料-CON.pdf');
});
test('还原网站响应头中的中文文件名', () => {
  const original = '考点1-概念-莆田沈(老师版).pdf';
  const latin = Buffer.from(original).toString('latin1');
  assert.equal(responseName(`attachment; filename="${latin}"`, 'fallback.pdf'), original);
  assert.equal(responseName(`attachment; filename*=UTF-8''${encodeURIComponent(original)}`, ''), original);
  assert.equal(responseName(null, '备用'), '备用.pdf');
});
test('拒绝把登录页或错误 HTML 当作 PDF', async () => {
  assert.equal(await validPdf(new Blob(['%PDF-1.4\n'])), true);
  assert.equal(await validPdf(new Blob(['<html>login</html>'])), false);
});
test('同名文件不覆盖，匹配 Windows 大小写规则', async () => {
  const directory = { async *keys() { yield 'A.PDF'; yield 'a (2).pdf'; } };
  assert.equal(await unusedName(directory, 'a.pdf'), 'a (3).pdf');
});

test('并发池限制活动任务数，并隔离单项失败', async () => {
  let active = 0;
  let peak = 0;
  const results = await runPool([1, 2, 3, 4, 5, 6], 3, async value => {
    active++;
    peak = Math.max(peak, active);
    await new Promise(resolve => setTimeout(resolve, 10));
    active--;
    if (value === 2) throw new Error('单项失败');
    return value * 2;
  });
  assert.equal(peak, 3);
  assert.equal(results[1].status, 'rejected');
  assert.deepEqual(results.filter(Boolean).map(result => result.status), [
    'fulfilled', 'rejected', 'fulfilled', 'fulfilled', 'fulfilled', 'fulfilled'
  ]);
});

test('重试等待优先遵循 Retry-After，并限制最长等待', () => {
  assert.equal(retryDelay({ retryAfter: '2' }, 1, 0), 2000);
  assert.equal(retryDelay({ retryAfter: '100' }, 1, 0), 30000);
  assert.equal(retryDelay({}, 1, 0), 1000);
  assert.equal(retryDelay({}, 3, 0), 4000);
});

test('临时服务错误会重试，永久错误立即失败', async () => {
  const originalFetch = global.fetch;
  const temporary = status => ({
    ok: false,
    status,
    headers: { get: name => name === 'retry-after' ? '0' : null },
    body: { cancel: async () => {} }
  });
  const success = { ok: true, status: 200, url: 'https://www.wulouai.com/example' };
  try {
    let calls = 0;
    global.fetch = async () => ++calls === 1 ? temporary(503) : success;
    assert.equal(await fetchChecked('https://www.wulouai.com/example'), success);
    assert.equal(calls, 2);

    calls = 0;
    global.fetch = async () => { calls++; return temporary(404); };
    await assert.rejects(fetchChecked('https://www.wulouai.com/missing'), /HTTP 404/);
    assert.equal(calls, 1);
  } finally {
    global.fetch = originalFetch;
  }
});
