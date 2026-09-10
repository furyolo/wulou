const test = require('node:test');
const assert = require('node:assert/strict');
const {
  normalizeWhitespace,
  stableCodeFromText,
  runPool,
  chunkItems,
  classificationPayload,
  normalizeClassificationResult,
  resolveCataloguePath,
  serializeSuccessfulControls,
  sourceTextWithoutAssistant,
} = require('../../userscript/wulou-question-curation-assistant.user.js');

test('标准化题干空白', () => {
  assert.equal(normalizeWhitespace('  实数　计算\n\n'), '实数 计算');
});

test('提取稳定题目编号', () => {
  assert.equal(stableCodeFromText('福建中考 CS2026-EXAM-001 题目'), 'CS2026-EXAM-001');
  assert.equal(stableCodeFromText('没有稳定编号'), null);
});

test('采集题卡文本时排除分类助手自身建议', () => {
  let removed = false;
  const clone = {
    get textContent() { return removed ? '第11题 计算根式' : '第11题 计算根式 待复核：答案异常'; },
    querySelectorAll() { return [{ remove() { removed = true; } }]; },
  };
  const card = { cloneNode() { return clone; } };
  assert.equal(sourceTextWithoutAssistant(card), '第11题 计算根式');
});

test('分类请求不会携带 DOM 或属性接口地址', () => {
  const payload = classificationPayload({
    exerciseId: '2529221', stableCode: 'CS2026-EXAM-001', catalogueId: '760593307', source: '题目',
    currentCatalogueId: '760593399', questionPress: '题干', answerPress: '答案', questionText: '题干备用文本', questionLatex: '\\frac{1}{2}', answerText: '答案备用文本', answerLatex: 'x=1', questionImageUrl: 'https://example.test/q.png', answerImageUrl: null,
    card: { unsafe: true }, attributeUrl: 'https://example.test/attribute',
  }, { topic_id: 'topic-01-real-numbers', level2_id: 'topic-01-large' });
  assert.equal(payload.exercise_id, '2529221');
  assert.equal(payload.current_catalogue_id, '760593399');
  assert.equal(payload.question_latex, '\\frac{1}{2}');
  assert.equal(payload.answer_text, '答案备用文本');
  assert.equal('card' in payload, false);
  assert.equal('attributeUrl' in payload, false);
});

test('有限并发保留输入顺序', async () => {
  let active = 0;
  let peak = 0;
  const result = await runPool([1, 2, 3, 4], 2, async value => {
    active += 1;
    peak = Math.max(peak, active);
    await new Promise(resolve => setTimeout(resolve, 5));
    active -= 1;
    return value * 2;
  });
  assert.equal(peak, 2);
  assert.deepEqual(result.map(item => item.value), [2, 4, 6, 8]);
});

test('整页题目按六题分块', () => {
  const chunks = chunkItems(Array.from({ length: 14 }, (_, index) => index + 1), 6);
  assert.deepEqual(chunks.map(chunk => chunk.length), [6, 6, 2]);
});

test('异常分类字段会被规范为可渲染的复核结果', () => {
  const result = normalizeClassificationResult({ status: 'review', review_reasons: null }, '42');
  assert.equal(result.exercise_id, '42');
  assert.equal(result.status, 'review');
  assert.equal(result.confidence, 0);
  assert.deepEqual(result.review_reasons, ['invalid_result_shape']);
  assert.equal(result.target, null);
});

test('按完整层级路径唯一解析题湖目录 ID', () => {
  const tree = [{
    id: 1, name: '专题1：实数', child: [{
      id: 2, name: '【大题】', child: [
        { id: 3, name: '实数综合计算', child: [] },
      ],
    }],
  }];
  const target = resolveCataloguePath(tree, ['专题1: 实数', '【大题】', '实数综合计算']);
  assert.equal(target.id, '3');
  assert.throws(() => resolveCataloguePath(tree, ['实数综合计算']), /未找到目录/);
});

test('提交属性时序列化完整成功控件并排除文件和未选复选框', () => {
  const params = serializeSuccessfulControls({ elements: [
    { name: '_token', type: 'hidden', value: 'csrf', disabled: false },
    { name: 'exercise_id', type: 'hidden', value: '42', disabled: false },
    { name: 'exercise_catalogue_id', type: 'hidden', value: '99', disabled: false },
    { name: 'area[]', type: 'checkbox', value: '17', checked: true, disabled: false },
    { name: 'area[]', type: 'checkbox', value: '18', checked: false, disabled: false },
    { name: 'file', type: 'file', value: 'ignored', disabled: false },
  ] });
  assert.equal(params.get('_token'), 'csrf');
  assert.equal(params.get('exercise_catalogue_id'), '99');
  assert.deepEqual(params.getAll('area[]'), ['17']);
  assert.equal(params.has('file'), false);
});
