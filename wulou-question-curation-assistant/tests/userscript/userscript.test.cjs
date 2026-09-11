const test = require('node:test');
const assert = require('node:assert/strict');
const {
  normalizeWhitespace,
  stableCodeFromText,
  runPool,
  chunkItems,
  classificationPayload,
  normalizeClassificationResult,
  canAcceptClassification,
  resolveCataloguePath,
  serializeSuccessfulControls,
  buildCatalogueMovePayload,
  navigationPathFromTreeRows,
  buildHistoryReportHtml,
  compactHistoryPath,
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

test('待复核结果有完整目录路径时可由人工一键采纳', () => {
  const target = { path: ['专题12：锐角三角函数', '【大题】', '实数综合计算（含三角比）'] };
  assert.equal(canAcceptClassification({ status: 'suggested', target }), true);
  assert.equal(canAcceptClassification({ status: 'review', target }), true);
  assert.equal(canAcceptClassification({ status: 'review', target: null }), false);
  assert.equal(canAcceptClassification({ status: 'unknown', target }), false);
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

test('工作成果从页面导航树提取三级或四级名称路径', () => {
  const rows = [
    { name: '——专题篇——', depth: 0 },
    { name: '■■■■模块一：数与代数', depth: 0 },
    { name: '专题1：实数', depth: 0 },
    { name: '【大题】', depth: 1 },
    { name: '实数综合计算', depth: 2 },
    { name: '考法1', depth: 3 },
  ];
  assert.deepEqual(navigationPathFromTreeRows(rows, 4), ['专题1：实数', '【大题】', '实数综合计算']);
  assert.deepEqual(navigationPathFromTreeRows(rows, 5), ['专题1：实数', '【大题】', '实数综合计算', '考法1']);
});

test('工作成果导出为包含概览和目录变更的独立 HTML', () => {
  const html = buildHistoryReportHtml({
    summary: { classified_count: 1, topics: ['专题4：分式方程与不等式'] },
    records: [{
      stable_code: 'CS2026REPORT001',
      original_path: ['专题1：实数', '【大题】', '实数综合计算'],
      target_path: ['专题4：分式方程与不等式', '【大题】', '解不等式'],
      moved_at: '2026-09-11 08:00:00',
    }],
  });
  assert.match(html, /题目分类成果汇总/);
  assert.match(html, /CS2026REPORT001/);
  assert.match(html, /实数综合计算/);
  assert.match(html, /解不等式/);
  assert.match(html, /已分类题目/);
  assert.doesNotMatch(html, /最近移动时间/);
});

test('工作成果路径会省略空四级目录', () => {
  assert.equal(
    compactHistoryPath(['专题1：实数', '【大题】', '实数综合计算', '']),
    '专题1：实数 / 【大题】 / 实数综合计算',
  );
  assert.equal(compactHistoryPath([]), '—');
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

test('目录移动请求会以建议目录覆盖所有同名旧值', () => {
  const params = buildCatalogueMovePayload({ elements: [
    { name: 'exercise_id', type: 'hidden', value: '2508900', disabled: false },
    { name: 'exercise_catalogue_id', type: 'hidden', value: 'old-a', disabled: false },
    { name: 'exercise_catalogue_id', type: 'hidden', value: 'old-b', disabled: false },
  ] }, '760476014');
  assert.equal(params.get('exercise_id'), '2508900');
  assert.deepEqual(params.getAll('exercise_catalogue_id'), ['760476014']);
});
