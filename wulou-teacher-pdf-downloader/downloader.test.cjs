const { test } = require('node:test');
const assert = require('node:assert/strict');
const { safeName, responseName, validPdf, unusedName } = require('./wulou-teacher-pdf-downloader.user.js');

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
