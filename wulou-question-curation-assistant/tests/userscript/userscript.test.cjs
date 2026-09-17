const test = require('node:test');
const assert = require('node:assert/strict');
const {
  normalizeWhitespace,
  formatChinaTime,
  chinaDate,
  stableCodeFromText,
  runPool,
  chunkItems,
  classificationPayload,
  answerPreviewData,
  answerPreviewContent,
  focusSnapshotMatches,
  normalizeClassificationResult,
  reviewReasonLabels,
  canAcceptClassification,
  resolveCataloguePath,
  serializeSuccessfulControls,
  buildCatalogueMovePayload,
  navigationPathFromTreeRows,
  buildHistoryReportHtml,
  compactHistoryPath,
  sourceTextWithoutAssistant,
  pendingAcceptanceItems,
  acceptanceModeForCatalogueIds,
  completionStateForTarget,
  paginationUrlsFromDocument,
  reviewFirstItems,
  pageSlice,
  classificationProgressText,
  focusScopeForNavigationPath,
  acceptAllActionMode,
  manualSelectionPayload,
  historyRangePreviewEnd,
  needsDirectoryReview,
  skillImportBody,
  confirmAcceptClass,
  CLASSIFICATION_JOB_MAX_QUESTIONS,
} = require('../../userscript/wulou-question-curation-assistant.user.js');

test('标准化题干空白', () => {
  assert.equal(normalizeWhitespace('  实数　计算\n\n'), '实数 计算');
});

test('用户可见时间统一按北京时间显示', () => {
  assert.equal(formatChinaTime('2026-09-14 02:52:59'), '2026-09-14 10:52:59');
  assert.equal(formatChinaTime('2026-09-14T02:52:59Z'), '2026-09-14 10:52:59');
  assert.equal(chinaDate('2026-09-13T16:30:00Z'), '2026-09-14');
  assert.equal(formatChinaTime('not-a-time'), 'not-a-time');
});

test('面向用户的复核标记不显示内部英文代码', () => {
  assert.deepEqual(reviewReasonLabels(['topic_reroute_limit_reached', 'low_confidence']), [
    '专题重路由次数已达上限', '分类置信度不足',
  ]);
  assert.deepEqual(reviewReasonLabels(['unknown_internal_code']), ['需要人工复核']);
  assert.deepEqual(reviewReasonLabels(['目录依据不足']), ['目录依据不足']);
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

test('答案预览只使用当前题的已采集答案数据', () => {
  assert.deepEqual(answerPreviewData({
    answerPress: '  A  ',
    answerText: '补充说明',
    answerLatex: ' x = 1 ',
    answerImageUrl: 'https://example.test/answer.png',
  }), {
    texts: ['A', '补充说明'],
    latex: 'x = 1',
    imageUrl: 'https://example.test/answer.png',
  });
  assert.deepEqual(answerPreviewData({ answerPress: 'A', answerText: ' A ' }).texts, ['A']);
});

test('答案图片存在时不重复展示方正格式文本', () => {
  assert.deepEqual(answerPreviewContent({
    answerPress: '方正格式答案',
    answerText: '备用答案文本',
    answerLatex: 'x = 1',
    answerImageUrl: 'https://example.test/answer.png',
  }), {
    texts: [],
    latex: '',
    imageUrl: 'https://example.test/answer.png',
  });
  assert.deepEqual(answerPreviewContent({ answerPress: '纯文本答案', answerImageUrl: '' }), {
    texts: ['纯文本答案'],
    latex: '',
    imageUrl: '',
  });
});

test('当前目录快照只在相同目录身份与页面路径下恢复', () => {
  const scope = { level: 3, topic_id: 'topic-1', level2_id: 'level2-1', level3_id: 'level3-1' };
  const snapshot = {
    version: 1,
    pathname: '/user-center/exercise-part/topic-1',
    scope: { ...scope },
    page_size: 30,
    priority_page: 2,
  };
  assert.equal(focusSnapshotMatches(snapshot, '/user-center/exercise-part/topic-1', scope), true);
  assert.equal(focusSnapshotMatches(snapshot, '/user-center/exercise-part/other', scope), false);
  assert.equal(focusSnapshotMatches(snapshot, snapshot.pathname, { ...scope, level3_id: 'level3-2' }), false);
  assert.equal(focusSnapshotMatches({ ...snapshot, version: 2 }, snapshot.pathname, scope), false);

  const level4Scope = { ...scope, level: 4, level4_id: 'level4-1' };
  const level4Snapshot = { ...snapshot, scope: { ...level4Scope } };
  assert.equal(focusSnapshotMatches(level4Snapshot, snapshot.pathname, level4Scope), true);
  assert.equal(focusSnapshotMatches(level4Snapshot, snapshot.pathname, { ...level4Scope, level4_id: 'level4-2' }), false);
});

test('题湖旧知识点目录可作为当前聚焦范围，不要求与工作簿目标目录同名', () => {
  const taxonomy = { topics: [{
    id: 'topic-2', title: '专题2：代数式', level2: [{
      id: 'large-2', title: '【大题】', level3: [{ id: 'l3-1', title: '整式的化简与求值', level4: [] }],
    }],
  }] };
  assert.deepEqual(
    focusScopeForNavigationPath(taxonomy, ['专题2：代数式', '2.2 整式的相关概念', '考点3：整式的基本运算']),
    {
      level: 3, topic_id: 'topic-2', level2_id: '', level3_id: null, level4_id: null,
      title: '专题2：代数式 / 2.2 整式的相关概念 / 考点3：整式的基本运算',
    },
  );
  assert.deepEqual(
    focusScopeForNavigationPath(taxonomy, ['专题2：代数式', '【大题】', '整式的化简与求值']),
    {
      level: 3, topic_id: 'topic-2', level2_id: 'large-2', level3_id: 'l3-1', level4_id: null,
      title: '专题2：代数式 / 【大题】 / 整式的化简与求值',
    },
  );
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

test('800 题当前目录范围作为一个总作业提交，超过保护上限才拆分', () => {
  const questions = Array.from({ length: 801 }, (_, index) => ({ exerciseId: String(index + 1) }));
  const jobs = chunkItems(questions, CLASSIFICATION_JOB_MAX_QUESTIONS);
  assert.deepEqual(jobs.map(job => job.length), [801]);
  assert.equal(jobs.flat().length, 801);
  const oversized = chunkItems(Array.from({ length: 1001 }, (_, index) => index), CLASSIFICATION_JOB_MAX_QUESTIONS);
  assert.deepEqual(oversized.map(job => job.length), [1000, 1]);
});

test('单个总作业不显示冗余的第 1/1 个作业', () => {
  assert.equal(
    classificationProgressText({ scopeLabel: '当前目录全量识别', stage: '专题内目录分类', completed: 1, total: 831 }),
    '当前目录全量识别：正在专题内目录分类（1/831 道已完成）',
  );
  assert.equal(
    classificationProgressText({ scopeLabel: '当前目录全量识别', stage: '专题内目录分类', completed: 101, total: 1201, jobIndex: 2, jobCount: 2 }),
    '当前目录全量识别：正在专题内目录分类（第 2/2 个作业，101/1201 道已完成）',
  );
});

test('分页收集只保留当前列表的同路径页码链接', () => {
  const anchor = (href, text) => ({ getAttribute() { return href; }, textContent: text });
  const pagination = { querySelectorAll() { return [
    anchor('?page=1', '1'), anchor('?page=2', '2'), anchor('?page=3', '下一页'),
    anchor('/other?page=2', '2'), anchor('https://example.test/exercises?page=2', '2'),
  ]; } };
  const pageDocument = { querySelectorAll() { return [pagination]; } };
  assert.deepEqual(paginationUrlsFromDocument(pageDocument, 'https://www.wulouai.com/user-center/exercise-part/1?page=1'), [
    'https://www.wulouai.com/user-center/exercise-part/1?page=1',
    'https://www.wulouai.com/user-center/exercise-part/1?page=2',
    'https://www.wulouai.com/user-center/exercise-part/1?page=3',
  ]);
});

test('待人工复核题目稳定置顶，普通建议保持原有相对顺序', () => {
  const items = [
    { dataset: { exercise: '1' } }, { dataset: { exercise: '2' } },
    { dataset: { exercise: '3' } }, { dataset: { exercise: '4' } },
  ];
  const results = new Map([
    ['1', { status: 'suggested' }],
    ['2', { status: 'review' }],
    ['3', { status: 'suggested' }],
    ['4', { status: 'review' }],
  ]);
  assert.deepEqual(reviewFirstItems(items, results).map(item => item.dataset.exercise), ['2', '4', '1', '3']);
});

test('审核优先视图按原始页容量连续分页', () => {
  const items = Array.from({ length: 65 }, (_, index) => index + 1);
  const first = pageSlice(items, 30, 1);
  const second = pageSlice(items, 30, 2);
  const last = pageSlice(items, 30, 3);
  assert.deepEqual(first.items, Array.from({ length: 30 }, (_, index) => index + 1));
  assert.deepEqual(second.items, Array.from({ length: 30 }, (_, index) => index + 31));
  assert.deepEqual(last.items, [61, 62, 63, 64, 65]);
  assert.equal(last.totalPages, 3);
});

test('异常分类字段会被规范为可渲染的复核结果', () => {
  const result = normalizeClassificationResult({ status: 'review', review_reasons: null }, '42');
  assert.equal(result.exercise_id, '42');
  assert.equal(result.status, 'review');
  assert.equal(result.confidence, 0);
  assert.deepEqual(result.review_reasons, ['invalid_result_shape']);
  assert.equal(result.target, null);
});

test('待复核结果有完整目录路径时可由人工采纳', () => {
  const target = { path: ['专题12：锐角三角函数', '【大题】', '实数综合计算（含三角比）'] };
  assert.equal(canAcceptClassification({ status: 'suggested', target }), true);
  assert.equal(canAcceptClassification({ status: 'review', target }), true);
  assert.equal(canAcceptClassification({ status: 'review', target: null }), false);
  assert.equal(canAcceptClassification({ status: 'unknown', target }), false);
});

test('建议目录与当前目录相同时只需本地采纳', () => {
  assert.equal(acceptanceModeForCatalogueIds('level3-1', 'level3-1'), 'local');
  assert.equal(acceptanceModeForCatalogueIds('level3-1', 'level3-2'), 'move');
  assert.equal(acceptanceModeForCatalogueIds('', 'level3-1'), 'move');
});

test('进入新目录范围后，已到达目标叶子的题目恢复为终态而非待采纳', () => {
  assert.equal(completionStateForTarget({ status: 'suggested' }, 'level4-target', 'level4-target'), 'at_target');
  assert.equal(
    completionStateForTarget({ status: 'suggested', catalogue_move: { target_catalogue_id: 'level4-target' } }, 'level4-target', 'level4-target'),
    'moved',
  );
  assert.equal(completionStateForTarget({ status: 'suggested' }, 'level4-other', 'level4-target'), '');
  assert.equal(
    completionStateForTarget({ status: 'suggested' }, 'legacy-catalogue-id', 'level4-target', 'level4-target'),
    'directory_consistent',
  );
});

test('全部采纳只收集当前尚未采纳且目录路径完整的建议', () => {
  const target = { path: ['专题12：锐角三角函数', '【大题】', '实数综合计算（含三角比）'] };
  const items = pendingAcceptanceItems([
    ['1', { exercise_id: '1', status: 'suggested', target }],
    ['2', { exercise_id: '2', status: 'review', target }],
    ['3', { exercise_id: '3', status: 'suggested', target, accepted: true }],
    ['4', { exercise_id: '4', status: 'suggested', target: null }],
    ['1', { exercise_id: '1', status: 'suggested', target }],
  ]);
  assert.deepEqual(items.map(item => item.exerciseId), ['1', '2']);
});

test('原生翻页后全部采纳会先汇总当前目录，而不是静默退化为本页', () => {
  assert.equal(acceptAllActionMode('page', true), 'hydrate_focus');
  assert.equal(acceptAllActionMode('focus', true), 'focus');
  assert.equal(acceptAllActionMode('page', false), 'page');
});

test('人工目录选择会使用当前题目的来源目录持久化为待采纳决定', () => {
  assert.deepEqual(manualSelectionPayload(' 42 ', {
    currentCatalogueId: 'source-leaf', stableCode: 'CS2026-42',
  }, {
    target: { path: ['专题2：代数式', '【大题】', '整式运算'] },
    manual_override: { original_target_path: ['专题1：实数', '【大题】', '实数运算'] },
  }), {
    exercise_id: '42', current_catalogue_id: 'source-leaf', stable_code: 'CS2026-42',
    original_target_path: ['专题1：实数', '【大题】', '实数运算'],
    target_path: ['专题2：代数式', '【大题】', '整式运算'],
  });
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
    summary: {
      classified_count: 1,
      topics: ['专题4：分式方程与不等式'],
      period: { date: '2026-09-12' },
    },
    records: [{
      stable_code: 'CS2026REPORT001',
      original_path: ['专题1：实数', '【大题】', '实数综合计算'],
      target_path: ['专题4：分式方程与不等式', '【大题】', '解不等式'],
      moved_at: '2026-09-11 08:00:00',
    }],
  });
  assert.match(html, /题目分类成果汇总（2026-09-12 工作成果）/);
  assert.match(html, /CS2026REPORT001/);
  assert.match(html, /实数综合计算/);
  assert.match(html, /解不等式/);
  assert.match(html, /已分类题目/);
  assert.match(html, /data-label="原目录"/);
  assert.match(html, /@media \(max-width: 640px\)/);
  assert.doesNotMatch(html, /最近移动时间/);
});

test('工作成果路径会省略空四级目录', () => {
  assert.equal(
    compactHistoryPath(['专题1：实数', '【大题】', '实数综合计算', '']),
    '专题1：实数 / 【大题】 / 实数综合计算',
  );
  assert.equal(compactHistoryPath([]), '—');
});

test('工作成果日期范围在选择截止日期前预览悬停区间', () => {
  assert.equal(
    historyRangePreviewEnd({ start: '2026-09-04', end: '' }, '2026-09-10'),
    '2026-09-10',
  );
  assert.equal(historyRangePreviewEnd({ start: '2026-09-04', end: '' }, '2026-09-03'), '2026-09-03');
  assert.equal(historyRangePreviewEnd({ start: '2026-09-04', end: '2026-09-08' }, '2026-09-10'), '');
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

test('只对仍然成立、且目录变动后尚未采纳的结论提示复核', () => {
  const stale = { source: 'manual', directory_changed: true };
  assert.equal(needsDirectoryReview({ manual_override: stale }), true);
  // Skill 归档与人工采纳共用同一套判定，来源不影响是否提示。
  assert.equal(needsDirectoryReview({ manual_override: { source: 'skill', directory_changed: true } }), true);
  assert.equal(needsDirectoryReview({ manual_override: { source: 'manual', directory_changed: false } }), false);
  // 已采纳的题不再提示：它已经按这条结论处理过了。
  assert.equal(needsDirectoryReview({ manual_override: stale, accepted: true }), false);
  // 模型建议没有人工结论，也就没有“陈旧”一说。
  assert.equal(needsDirectoryReview({ target: { path: ['专题1：实数'] } }), false);
  assert.equal(needsDirectoryReview(null), false);
});

test('导入归类结果的请求体带上保留人工修正的选择', () => {
  assert.equal(skillImportBody('{"items":[]}', true).preserve_manual_decisions, true);
  assert.equal(skillImportBody('{"items":[]}', false).preserve_manual_decisions, false);
  assert.equal(skillImportBody('{"items":[]}', undefined).preserve_manual_decisions, false);
  assert.equal(skillImportBody('逐行 JSONL', true).jsonl, '逐行 JSONL');
});

test('确认框的确认按钮与面板按钮同一套语义', () => {
  // 常规确认（导入归类结果、全部采纳、识别）是主操作，用面板统一的绿底。
  assert.equal(confirmAcceptClass(false), 'primary');
  assert.equal(confirmAcceptClass(undefined), 'primary');
  // 只有删除方案、清除缓存这类破坏性操作才用红底。
  assert.equal(confirmAcceptClass(true), 'danger');
});
