// ==UserScript==
// @name         题湖题库数学题分类助手
// @namespace    https://www.wulouai.com/
// @version      0.16.27
// @description  采集当前题目页，显示分类建议，并可将确认后的建议写入题湖可视化分类。
// @match        https://www.wulouai.com/user-center/exercise-part/*
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

(function () {
  'use strict';

  const SERVICE_URL = 'http://127.0.0.1:3232';
  const CARD_SELECTOR = '.zwh_part_exercise_one_div[data-exercise]';
  const ATTRIBUTE_SELECTOR = '.attributes_modify[data-url]';
  const PANEL_ID = 'wulou-question-curation-panel';
  const BADGE_CLASS = 'wulou-curation-badge';
  // 全量目录分析会跨分页读取属性；并发受限以减少站点限流，同时避免顺序等待。
  const PAGE_FETCH_CONCURRENCY = 4;
  const ATTRIBUTE_CONCURRENCY = 6;
  // 题湖没有已验证的批量写入接口，因此沿用单题接口并限制浏览器端并发，
  // 避免一次性请求过多导致登录态、CSRF 或站点限流问题。
  const ACCEPTANCE_CONCURRENCY = 3;
  const LOCAL_REQUEST_TIMEOUT_MS = 30000;
  // 连接测试只读取模型详情，不触发模型生成；超过 15 秒可视为未能快速连通。
  const CONNECTION_TEST_TIMEOUT_MS = 15000;
  const CONNECTION_TEST_POLL_INTERVAL_MS = 300;
  const CLASSIFICATION_JOB_POLL_INTERVAL_MS = 2000;
  // 服务端单作业上限。800 题级当前目录范围会作为一个总作业持续流水化；
  // 仅超过该保护上限时才拆分，避免浏览器和本机服务持有无限大的任务快照。
  const CLASSIFICATION_JOB_MAX_QUESTIONS = 1000;
  const FOCUS_SNAPSHOT_STORAGE_KEY = 'wulou-question-curation:focus-snapshot-v1';
  const MODIFY_ENDPOINT = '/exercise/modifyData';

  function normalizeWhitespace(value) {
    return String(value || '').replace(/\u3000/g, ' ').replace(/\s+/g, ' ').trim();
  }

  // 服务端 SQLite 的 CURRENT_TIMESTAMP 是没有偏移量的 UTC 时间；不能直接原样展示，
  // 否则会被误认为本地时间。所有面向使用者的时间统一按北京时间格式化。
  function formatChinaTime(value) {
    const raw = normalizeWhitespace(value);
    if (!raw) return '';
    const sqliteUtc = raw.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}(?:\.\d+)?)$/);
    const date = sqliteUtc
      ? new Date(`${sqliteUtc[1]}T${sqliteUtc[2]}Z`)
      : new Date(raw);
    if (Number.isNaN(date.getTime())) return raw;
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(date).filter(part => part.type !== 'literal').map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
  }

  function chinaDate(value = new Date()) {
    return formatChinaTime(value).slice(0, 10);
  }

  function directoryKey(value) {
    return normalizeWhitespace(value).replace(/[：:]/g, '').replace(/\s+/g, '').toLowerCase();
  }

  function resolveCataloguePath(tree, path) {
    if (!Array.isArray(tree) || !tree.length) throw new Error('题湖可视化目录尚未加载');
    const normalizedPath = Array.isArray(path) ? path.map(normalizeWhitespace).filter(Boolean) : [];
    if (!normalizedPath.length) throw new Error('建议分类缺少完整目录路径');
    let nodes = tree;
    let matched = null;
    for (const segment of normalizedPath) {
      const candidates = (Array.isArray(nodes) ? nodes : [])
        .filter(node => directoryKey(node?.name) === directoryKey(segment));
      if (candidates.length !== 1) {
        const reason = candidates.length ? '存在重名目录' : '未找到目录';
        throw new Error(`${reason}“${segment}”，请人工复核完整路径`);
      }
      [matched] = candidates;
      nodes = matched.child;
    }
    const id = normalizeWhitespace(matched?.id);
    if (!id) throw new Error('题湖目录缺少可提交的内部 ID');
    return { id, path: normalizedPath };
  }

  function serializeSuccessfulControls(form) {
    const params = new URLSearchParams();
    for (const field of [...(form?.elements || [])]) {
      const name = normalizeWhitespace(field.name);
      const type = String(field.type || '').toLowerCase();
      if (!name || field.disabled || ['button', 'submit', 'reset', 'file'].includes(type)) continue;
      if (['checkbox', 'radio'].includes(type) && !field.checked) continue;
      if (type === 'select-multiple') {
        for (const option of [...(field.options || [])]) {
          if (option.selected) params.append(name, String(option.value ?? ''));
        }
        continue;
      }
      params.append(name, String(field.value ?? ''));
    }
    return params;
  }

  // 题湖表单可能包含同名隐藏字段；URLSearchParams.set 会移除旧值，
  // 确保服务端只接收到一个、且正是当前采纳目标的目录 ID。
  function buildCatalogueMovePayload(form, targetCatalogueId) {
    const targetId = normalizeWhitespace(targetCatalogueId);
    if (!targetId) throw new Error('建议目录缺少可提交的内部 ID');
    const params = serializeSuccessfulControls(form);
    params.set('exercise_catalogue_id', targetId);
    return params;
  }

  function stableCodeFromText(value) {
    const match = normalizeWhitespace(value).match(/\b[A-Z]{2,}[A-Z0-9-]{4,}\b/);
    return match ? match[0] : null;
  }

  function sourceTextWithoutAssistant(card) {
    const clone = card.cloneNode(true);
    for (const badge of [...clone.querySelectorAll(`.${BADGE_CLASS}`)]) badge.remove();
    return normalizeWhitespace(clone.textContent);
  }

  function assistantFreeCardTemplate(card) {
    const clone = card.cloneNode(true);
    for (const badge of [...clone.querySelectorAll(`.${BADGE_CLASS}`)]) badge.remove();
    clone.classList.remove('wulou-curation-linked', 'wulou-curation-linked-review', 'wulou-curation-linked-accepted');
    return clone;
  }

  function sameOriginUrl(raw, base = location.href) {
    const url = new URL(raw, base);
    if (url.origin !== location.origin) throw new Error('题目属性接口不属于当前网站');
    return url.href;
  }

  function readFieldFromHtml(html, name) {
    const documentFragment = new DOMParser().parseFromString(String(html || ''), 'text/html');
    const field = documentFragment.querySelector(`[name="${CSS.escape(name)}"]`);
    if (!field) return '';
    return normalizeWhitespace('value' in field ? field.value : field.textContent);
  }

  function imageUrl(card, label) {
    const images = [...card.querySelectorAll('img')];
    const exact = images.find(image => normalizeWhitespace(image.alt) === label);
    const fuzzy = images.find(image => normalizeWhitespace(image.alt).includes(label));
    return (exact || fuzzy || null)?.src || null;
  }

  function catalogueIdFromPageUrl(pageUrl) {
    return new URL(pageUrl, location.href).pathname.split('/').filter(Boolean)[10] || null;
  }

  function collectCardsFromDocument(pageDocument, pageUrl) {
    const cards = [...pageDocument.querySelectorAll(CARD_SELECTOR)];
    if (!cards.length) return [];
    return cards.map(card => {
      const exerciseId = normalizeWhitespace(card.dataset.exercise);
      const attribute = card.querySelector(ATTRIBUTE_SELECTOR);
      if (!exerciseId || !attribute?.dataset.url) {
        throw new Error('题目卡片缺少题目 ID 或属性入口，页面结构可能已更新');
      }
      return {
        card: pageDocument === document ? card : null,
        // 当前目录审核优先视图需要把跨页题卡带回当前页面展示；保存无助手标记的模板，
        // 不携带当前页已经注入的按钮或状态。
        cardTemplate: assistantFreeCardTemplate(card),
        exerciseId,
        stableCode: stableCodeFromText(sourceTextWithoutAssistant(card)),
        source: sourceTextWithoutAssistant(card),
        catalogueId: catalogueIdFromPageUrl(pageUrl),
        pageUrl: new URL(pageUrl, location.href).href,
        attributeUrl: sameOriginUrl(attribute.dataset.url, pageUrl),
        questionImageUrl: imageUrl(card, '问题'),
        answerImageUrl: imageUrl(card, '答案'),
      };
    });
  }

  function paginationUrlsFromDocument(pageDocument, pageUrl) {
    const current = new URL(pageUrl, typeof location === 'undefined' ? undefined : location.href);
    const containers = [...pageDocument.querySelectorAll('.pagination, [class*="pagination"], nav[aria-label*="页"]')];
    const anchors = containers.flatMap(container => [...container.querySelectorAll('a[href]')]);
    const urls = new Set();
    for (const anchor of anchors) {
      const label = normalizeWhitespace(anchor.textContent);
      let candidate;
      try { candidate = new URL(anchor.getAttribute('href'), current); }
      catch { continue; }
      if (candidate.origin !== current.origin || candidate.pathname !== current.pathname) continue;
      const hasNumericPageParameter = [...candidate.searchParams.entries()]
        .some(([name, value]) => /^(page|p|page_no|page_num)$/i.test(name) && /^\d+$/.test(value));
      if (!hasNumericPageParameter && !/^(\d+|上一页|下一页|‹|›|«|»)$/.test(label)) continue;
      candidate.hash = '';
      urls.add(candidate.href);
    }
    return [...urls];
  }

  // 审核题必须先于普通建议展示；同一组内维持题湖原有顺序，避免人工复核时跳题。
  function reviewFirstItems(items, resultById) {
    const reviews = [];
    const others = [];
    for (const item of (Array.isArray(items) ? items : [])) {
      const exerciseId = normalizeWhitespace(item?.exerciseId || item?.exercise_id || item?.dataset?.exercise);
      const result = resultById?.get ? resultById.get(exerciseId) : resultById?.[exerciseId];
      (result?.status === 'review' ? reviews : others).push(item);
    }
    return [...reviews, ...others];
  }

  function pageSlice(items, pageSize, pageNumber) {
    const size = Math.max(1, Number.parseInt(pageSize, 10) || 1);
    const totalPages = Math.max(1, Math.ceil((items?.length || 0) / size));
    const page = Math.min(Math.max(1, Number.parseInt(pageNumber, 10) || 1), totalPages);
    return { page, totalPages, items: (items || []).slice((page - 1) * size, page * size) };
  }

  function classificationProgressText({ scopeLabel, stage, completed, total, failed = 0, jobIndex = 1, jobCount = 1 }) {
    const jobPrefix = jobCount > 1 ? `第 ${jobIndex}/${jobCount} 个作业，` : '';
    return `${scopeLabel}：正在${stage}（${jobPrefix}${completed}/${total} 道已完成）${failed ? `，${failed} 道请求失败` : ''}`;
  }

  async function runPool(items, concurrency, worker) {
    const results = new Array(items.length);
    let nextIndex = 0;
    const runWorker = async () => {
      while (true) {
        const index = nextIndex++;
        if (index >= items.length) return;
        try {
          results[index] = { status: 'fulfilled', value: await worker(items[index], index) };
        } catch (reason) {
          results[index] = { status: 'rejected', reason };
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(Math.max(1, concurrency), items.length) }, runWorker));
    return results;
  }

  function chunkItems(items, size) {
    const chunks = [];
    for (let index = 0; index < items.length; index += size) chunks.push(items.slice(index, index + size));
    return chunks;
  }

  function classificationPayload(question, scope) {
    return {
      exercise_id: question.exerciseId,
      stable_code: question.stableCode,
      catalogue_id: question.catalogueId,
      current_catalogue_id: question.currentCatalogueId,
      source: question.source,
      question_press: question.questionPress,
      answer_press: question.answerPress,
      question_text: question.questionText,
      question_latex: question.questionLatex,
      answer_text: question.answerText,
      answer_latex: question.answerLatex,
      question_image_url: question.questionImageUrl,
      answer_image_url: question.answerImageUrl,
      scope,
    };
  }

  function answerPreviewData(question) {
    const texts = [question?.answerPress, question?.answerText]
      .map(normalizeWhitespace)
      .filter((value, index, values) => value && values.indexOf(value) === index);
    return {
      texts,
      latex: normalizeWhitespace(question?.answerLatex),
      imageUrl: normalizeWhitespace(question?.answerImageUrl),
    };
  }

  function answerPreviewContent(question) {
    const preview = answerPreviewData(question);
    // 题湖的答案图片保真度高于属性接口中的方正富文本；有图时不重复展示后者。
    return preview.imageUrl ? { texts: [], latex: '', imageUrl: preview.imageUrl } : preview;
  }

  function focusSnapshotMatches(snapshot, pathname, scope) {
    if (!snapshot || snapshot.version !== 1 || !scope || typeof pathname !== 'string') return false;
    const savedScope = snapshot.scope || {};
    return snapshot.pathname === pathname
      && savedScope.level === scope.level
      && savedScope.topic_id === scope.topic_id
      && savedScope.level2_id === scope.level2_id
      && (savedScope.level3_id || null) === (scope.level3_id || null)
      && (savedScope.level4_id || null) === (scope.level4_id || null);
  }

  function sameDirectoryPath(left, right) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
    return left.every((segment, index) => directoryKey(segment) === directoryKey(right[index]));
  }

  function appendAnswerPreview(badge, header, question) {
    const preview = answerPreviewContent(question);
    if (!preview.texts.length && !preview.latex && !preview.imageUrl) return;

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'badge-answer-toggle';
    toggle.textContent = '查看本题答案';
    toggle.setAttribute('aria-expanded', 'false');
    const content = document.createElement('div');
    content.className = 'badge-answer-content';
    content.hidden = true;

    preview.texts.forEach((text, index) => {
      const line = document.createElement('p');
      line.textContent = `${index ? '答案补充' : '答案'}：${text}`;
      content.append(line);
    });
    if (preview.latex) {
      const formula = document.createElement('pre');
      formula.textContent = `公式：${preview.latex}`;
      content.append(formula);
    }
    if (preview.imageUrl) {
      const image = document.createElement('img');
      image.src = preview.imageUrl;
      image.alt = '本题答案图片';
      image.loading = 'lazy';
      content.append(image);
    }
    toggle.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      content.hidden = !content.hidden;
      toggle.textContent = content.hidden ? '查看本题答案' : '收起答案';
      toggle.setAttribute('aria-expanded', String(!content.hidden));
    });
    header.append(toggle);
    badge.append(content);
  }

  function navigationPathFromTreeRows(rows, selectedIndex) {
    const stack = [];
    for (let index = 0; index <= selectedIndex; index += 1) {
      const row = rows[index];
      if (!row?.name || !Number.isInteger(row.depth) || row.depth < 0) continue;
      stack.length = row.depth;
      stack[row.depth] = normalizeWhitespace(row.name);
    }
    let topicIndex = -1;
    for (let index = stack.length - 1; index >= 0; index -= 1) {
      if (/^专题\s*\d+\s*[：:]/.test(stack[index] || '')) {
        topicIndex = index;
        break;
      }
    }
    return topicIndex >= 0 ? stack.slice(topicIndex).filter(Boolean) : [];
  }

  function escapeReportHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, character => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
    })[character]);
  }

  function compactHistoryPath(path) {
    const parts = Array.isArray(path) ? path : [];
    return parts.slice(0, 4).filter(Boolean).map(normalizeWhitespace).filter(Boolean).join(' / ') || '—';
  }

  function buildHistoryReportHtml(report) {
    const summary = report?.summary || {};
    const records = Array.isArray(report?.records) ? report.records : [];
    const topics = Array.isArray(summary.topics) ? summary.topics : [];
    const periodDate = normalizeWhitespace(summary.period?.date);
    const periodLabel = normalizeWhitespace(summary.period?.label) || (periodDate ? `${periodDate} 工作成果` : '');
    const pathHtml = path => escapeReportHtml(compactHistoryPath(path));
    const rows = records.map(record => `
      <tr>
        <td class="code" data-label="Stable Code">${escapeReportHtml(record.stable_code || '未提取 Stable Code')}</td>
        <td data-label="原目录">${pathHtml(record.original_path)}</td>
        <td data-label="现目录">${pathHtml(record.target_path)}</td>
      </tr>`).join('') || '<tr><td colspan="3" class="empty" data-label="">暂无已分类题目记录。</td></tr>';
    const topicHtml = topics.map(topic => `<span>${escapeReportHtml(topic)}</span>`).join('') || '<span>暂无</span>';
    return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>题目分类成果汇总</title><style>
  :root { color: #1f2c2a; font: 14px/1.55 "Microsoft YaHei", "微软雅黑", sans-serif; -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
  body { max-width: 1280px; margin: 0 auto; padding: 20px; background: #f5f8f7; } main { padding: 24px; border: 1px solid #d7e1dd; border-radius: 14px; background: #fff; box-shadow: 0 14px 42px rgb(19 49 41 / .10); }
  h1 { margin: 0; color: #126b5c; font-size: 24px; font-weight: 700; letter-spacing: .02em; } .summary { display: grid; grid-template-columns: 170px minmax(0, 1fr); align-items: start; gap: 12px; margin: 16px 0; }.summary-card { min-height: 74px; padding: 13px 15px; border: 1px solid #cfe2db; border-radius: 11px; background: linear-gradient(135deg, #f4faf7, #e7f2ee); }.metric-label, .topic-label { color: #4e6860; font-size: 12px; font-weight: 700; }.count { margin-top: 2px; color: #0d6858; font-size: 29px; font-weight: 800; line-height: 1.1; }.count-label { color: #526660; font-size: 12px; }.topic-card { min-width: 0; min-height: 74px; padding: 13px 15px; border: 1px solid #dce7e3; border-radius: 11px; background: #fff; }.topics { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }.topics span { padding: 3px 8px; border-radius: 999px; background: #e7f2ee; color: #1d6554; font-size: 11px; font-weight: 650; }
  table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 11px; line-height: 1.45; } th, td { padding: 7px 8px; border: 1px solid #dce7e3; vertical-align: top; text-align: left; } th { background: #eef4f1; color: #29453d; font-size: 11px; } td { color: #405650; overflow-wrap: anywhere; } th:first-child, .code { width: 23%; } .code { color: #29453d; font-weight: 700; word-break: break-all; } .empty { padding: 22px; color: #71827c; text-align: center; }
  @media (max-width: 640px) { body { padding: 10px; } main { padding: 16px; border-radius: 12px; } h1 { font-size: 20px; }.summary { grid-template-columns: minmax(112px, .4fr) minmax(0, .6fr); gap: 8px; margin: 12px 0; }.summary-card, .topic-card { min-height: 0; padding: 11px; }.count { font-size: 25px; }.topics { flex-wrap: nowrap; overflow-x: auto; padding-bottom: 3px; -webkit-overflow-scrolling: touch; }.topics span { flex: 0 0 auto; } table { display: block; table-layout: auto; font-size: 12px; } thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; } tbody { display: grid; gap: 10px; } tr { display: grid; gap: 6px; padding: 11px; border: 1px solid #dce7e3; border-radius: 10px; background: #fff; } td { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 8px; padding: 0; border: 0; font-size: 12px; } td::before { content: attr(data-label); color: #4e6860; font-size: 11px; font-weight: 700; } th:first-child, .code { width: auto; }.empty { display: block; padding: 10px; }.empty::before { content: none; } }
  @page { size: A4 landscape; margin: 10mm; } @media print { body { max-width: none; padding: 0; background: #fff; } main { padding: 0; border: 0; box-shadow: none; } }
</style></head><body><main>
  <h1>题目分类成果汇总${periodLabel ? `（${escapeReportHtml(periodLabel)}）` : ''}</h1>
  <section class="summary"><div class="summary-card"><div class="metric-label">已分类题目</div><div class="count">${Number(summary.classified_count) || 0}</div></div><div class="topic-card"><div class="topic-label">涉及专题</div><div class="topics">${topicHtml}</div></div></section>
  <table><thead><tr><th>Stable Code</th><th>原目录</th><th>现目录</th></tr></thead><tbody>${rows}</tbody></table>
</main></body></html>`;
  }


  function normalizeClassificationResult(value, fallbackExerciseId = '') {
    const input = value && typeof value === 'object' ? value : {};
    const status = input.status === 'suggested' ? 'suggested' : 'review';
    const parsedConfidence = Number(input.confidence);
    const reviewReasons = Array.isArray(input.review_reasons)
      ? input.review_reasons.map(item => normalizeWhitespace(item)).filter(Boolean)
      : ['invalid_result_shape'];
    if (status === 'review' && !reviewReasons.length) reviewReasons.push('model_review');
    const target = input.target && typeof input.target === 'object'
      ? {
        ...input.target,
        path: Array.isArray(input.target.path)
          ? input.target.path.map(item => normalizeWhitespace(item)).filter(Boolean)
          : [],
      }
      : null;
    return {
      ...input,
      exercise_id: normalizeWhitespace(input.exercise_id || fallbackExerciseId),
      status,
      confidence: Number.isFinite(parsedConfidence) ? Math.max(0, Math.min(1, parsedConfidence)) : 0,
      reason: normalizeWhitespace(input.reason) || '服务端未提供分类说明',
      review_reasons: reviewReasons,
      target,
    };
  }

  // “待复核”只表示不能自动通过，不代表人工不能确认并写入。
  // 采纳前仍要求模型给出的目录路径完整，避免把不确定或无效目标提交到题湖。
  function canAcceptClassification(result) {
    return Boolean(
      result
      && ['suggested', 'review'].includes(result.status)
      && Array.isArray(result.target?.path)
      && result.target.path.length,
    );
  }

  // 目录 ID 相同表示题目已在建议目录中；采纳只确认当前分类结果，不应再发起属性读取或移动请求。
  function acceptanceModeForCatalogueIds(currentCatalogueId, targetCatalogueId) {
    const currentId = normalizeWhitespace(currentCatalogueId);
    const targetId = normalizeWhitespace(targetCatalogueId);
    return currentId && targetId && currentId === targetId ? 'local' : 'move';
  }

  function completionStateForTarget(result, currentCatalogueId, targetCatalogueId, pageCatalogueId = '') {
    if (result?.status !== 'suggested') return '';
    const attributeMatches = acceptanceModeForCatalogueIds(currentCatalogueId, targetCatalogueId) === 'local';
    // 题湖部分旧题的属性接口会返回已失效的目录 ID，而当前列表页 URL 仍准确指向
    // 实际所在叶子。仅当页面叶子 ID 与建议目标完全一致时，才以页面目录作为兜底，
    // 防止把已在目标目录中的题目再次提交移动。
    const pageMatches = acceptanceModeForCatalogueIds(pageCatalogueId, targetCatalogueId) === 'local';
    if (!attributeMatches && !pageMatches) return '';
    const movedTo = normalizeWhitespace(result?.catalogue_move?.target_catalogue_id);
    if (movedTo && movedTo === normalizeWhitespace(targetCatalogueId)) return 'moved';
    return attributeMatches ? 'at_target' : 'directory_consistent';
  }

  // 只返回尚未采纳、且拥有完整目录路径的建议。这个口径同时服务于
  // 主面板的计数、确认提示与实际批量处理，避免三处规则发生偏差。
  function pendingAcceptanceItems(entries) {
    const seen = new Set();
    return (Array.isArray(entries) ? entries : [])
      .map(([exerciseId, result]) => ({ exerciseId: normalizeWhitespace(exerciseId || result?.exercise_id), result }))
      .filter(({ exerciseId, result }) => {
        if (!exerciseId || seen.has(exerciseId) || result?.accepted || !canAcceptClassification(result)) return false;
        seen.add(exerciseId);
        return true;
      });
  }

  function reviewReasonLabels(reasons) {
    const labels = {
      cloud_model_required_for_skill_protocol: '未配置云端分类模型',
      invalid_model_confidence: '模型置信度格式无效',
      invalid_model_payload: '模型返回内容无效',
      invalid_model_proposal: '模型目录提议无效',
      invalid_model_review_reasons: '模型复核信息格式无效',
      invalid_model_target: '模型给出的目录不存在',
      low_confidence: '分类置信度不足',
      model_review: '模型要求人工复核',
      no_compatible_target: '没有匹配的现有目录',
      no_unique_target: '无法确定唯一目录',
      page_scope_differs_from_routed_topic: '建议专题与当前页面专题不同',
      routing_target_mismatch: '专题路由与目录分类结果不一致',
      scope_empty: '当前分类范围没有可用目录',
      cloud_request_failed: '云端分类请求失败',
      target_topic_large_question_directory_missing: '目标专题缺少“【大题】”目录',
      topic_reroute_limit_reached: '专题重路由次数已达上限',
      topic_routing_failed: '无法稳定确定所属专题',
    };
    return [...new Set((Array.isArray(reasons) ? reasons : [])
      .map(item => normalizeWhitespace(item))
      .filter(Boolean)
      // 后端使用的内部标记不应直接暴露给使用者；未知英文标记统一为可理解提示。
      .map(item => labels[item] || (/^[a-z][a-z0-9_]*$/i.test(item) ? '需要人工复核' : item)))];
  }

  // 原生翻页会清空当前页面的 JS 内存。只要目录范围仍可识别，主按钮应
  // 先汇总该目录的缓存，而不是把“全部采纳”悄悄降级为“本页采纳”。
  function acceptAllActionMode(workset, focusAvailable) {
    if (workset === 'focus') return 'focus';
    return focusAvailable ? 'hydrate_focus' : 'page';
  }

  function manualSelectionPayload(exerciseId, question, result) {
    return {
      exercise_id: normalizeWhitespace(exerciseId),
      current_catalogue_id: normalizeWhitespace(question?.currentCatalogueId),
      stable_code: normalizeWhitespace(question?.stableCode),
      original_target_path: Array.isArray(result?.manual_override?.original_target_path)
        ? result.manual_override.original_target_path : [],
      target_path: Array.isArray(result?.target?.path) ? result.target.path : [],
    };
  }

  function historyRangePreviewEnd(range, hoveredDate) {
    const start = normalizeWhitespace(range?.start);
    const end = normalizeWhitespace(range?.end);
    const candidate = normalizeWhitespace(hoveredDate);
    return start && !end && /^\d{4}-\d{2}-\d{2}$/.test(candidate) ? candidate : '';
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      normalizeWhitespace, formatChinaTime, chinaDate, stableCodeFromText, runPool, chunkItems,
      classificationPayload, answerPreviewData, answerPreviewContent, focusSnapshotMatches, normalizeClassificationResult, reviewReasonLabels, canAcceptClassification, resolveCataloguePath,
      serializeSuccessfulControls, buildCatalogueMovePayload, navigationPathFromTreeRows, buildHistoryReportHtml, compactHistoryPath,
      sourceTextWithoutAssistant, pendingAcceptanceItems, acceptanceModeForCatalogueIds, completionStateForTarget, paginationUrlsFromDocument,
      reviewFirstItems, pageSlice, classificationProgressText, focusScopeForNavigationPath, acceptAllActionMode, manualSelectionPayload, historyRangePreviewEnd, CLASSIFICATION_JOB_MAX_QUESTIONS,
    };
    return;
  }

  if (window.top !== window.self || document.getElementById(PANEL_ID)) return;

  const state = {
    taxonomy: null,
    scope: { topic_id: '', level2_id: '' },
    results: new Map(),
    busy: false,
    workset: 'page',
    currentQuestions: [],
    batchJobId: '',
    classificationJobId: '',
    cacheRestoreId: 0,
    cloudConfigured: false,
    cloudProfiles: [],
    cloudSettings: null,
    profileNameSave: Promise.resolve(),
    profileDrag: null,
    suppressProfileClickUntil: 0,
    availableModelsByProfile: new Map(),
    cards: new Map(),
    accepting: new Set(),
    acceptingAll: false,
    // 人工目录选择必须先落到本地服务；否则原生翻页、刷新或全目录缓存汇总
    // 会用旧模型结果覆盖仅存在于当前页面内存中的选择。
    manualSaveChains: new Map(),
    confirmation: null,
    historyReport: null,
    historyLoadId: 0,
    historyRange: { start: '', end: '' },
    historyRangePreviewEnd: '',
    historyCalendarMonth: '',
    directoryPlan: null,
    priorityView: null,
    focusSnapshot: null,
    pendingFocusRestore: false,
  };

  const host = document.createElement('div');
  host.id = PANEL_ID;
  const shadow = host.attachShadow({ mode: 'open' });
  shadow.innerHTML = `
    <style>
      :host { all: initial; color-scheme: light; font: 14px/1.5 "Microsoft YaHei", "微软雅黑", sans-serif; color: #1f2c2a; -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
      *, *::before, *::after { box-sizing: border-box; }
      button, input, select, textarea { font: inherit; }
      button { min-height: 38px; border: 1px solid #cfdad5; border-radius: 10px; background: #fff; color: #1f2c2a; cursor: pointer; padding: 8px 11px; transition: background .16s ease, border-color .16s ease, transform .16s ease; }
      button:hover { border-color: #8ba99f; background: #f2f7f5; }
      button:active { transform: translateY(1px); }
      button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline: 3px solid #9bd0bf; outline-offset: 2px; }
      button:disabled { opacity: .52; cursor: default; transform: none; }
      .tab { position: fixed; right: 0; top: 44%; z-index: 2147483000; display: grid; gap: 2px; justify-items: center; width: 50px; padding: 12px 6px; border-radius: 12px 0 0 12px; background: #126b5c; border-color: #126b5c; color: #fff; box-shadow: 0 10px 28px rgb(18 107 92 / .24); }
      .tab:hover { background: #0c5649; border-color: #0c5649; }
      .tab-arrow { font: 22px/1 sans-serif; }
      .tab-text { font-size: 11px; font-weight: 700; letter-spacing: .08em; }
      .tab-status { min-height: 12px; font-size: 10px; font-weight: 700; }
      .panel { position: fixed; right: 16px; top: 50%; z-index: 2147483001; width: min(364px, calc(100vw - 28px)); max-height: calc(100dvh - 28px); overflow: auto; padding: 18px; border: 1px solid #d7e1dd; border-radius: 16px; background: #fff; box-shadow: 0 20px 50px rgb(19 49 41 / .18); transform: translate(20px, -50%); opacity: 0; visibility: hidden; pointer-events: none; transition: transform .18s ease, opacity .18s ease, visibility .18s ease; }
      .panel.open { transform: translate(0, -50%); opacity: 1; visibility: visible; pointer-events: auto; }
      .header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
      .header-actions { display: flex; align-items: center; gap: 6px; }
      h2 { margin: 0; font-size: 17px; letter-spacing: -.02em; }
      .close { min-height: 34px; width: 34px; padding: 0; border: 0; font-size: 23px; background: #eef4f1; }
      .settings { min-height: 34px; padding: 6px 9px; border-color: transparent; background: #eef4f1; color: #39534c; font-size: 12px; font-weight: 650; }
      .form { display: grid; gap: 9px; padding: 12px; border: 1px solid #e2eae6; border-radius: 12px; background: #f8fbfa; }
      label { display: grid; gap: 4px; font-size: 12px; color: #526660; }
      input, select, textarea { width: 100%; min-height: 36px; border: 1px solid #cfdad5; border-radius: 8px; background: #fff; color: #1f2c2a; padding: 7px 9px; }
      .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 12px; }
      .actions > :only-child { grid-column: 1 / -1; }
      .primary { border-color: #126b5c; background: #126b5c; color: #fff; font-weight: 650; }
      .primary:hover { border-color: #0c5649; background: #0c5649; }
      .status { min-height: 0; margin-top: 2px; padding: 8px 10px; border: 1px solid #dce7e3; border-radius: 10px; background: #f6faf8; color: #526660; font-size: 11px; }
      .status.error { border-color: #e6bbb8; background: #fff5f4; color: #7a312d; }
      .status.warning { border-color: #ecd2a5; background: #fff8ec; color: #6c4b1d; }
      .legend { margin-top: 14px; border-top: 1px solid #e4ebe8; padding-top: 12px; }
      .legend h3 { margin: 0 0 6px; font-size: 12px; color: #526660; }
      .legend ul { display: grid; gap: 5px; margin: 0; padding: 0; list-style: none; }
      .legend li { display: flex; gap: 7px; align-items: flex-start; font-size: 12px; color: #405650; }
      .dot { flex: 0 0 auto; width: 8px; height: 8px; margin-top: 5px; border-radius: 50%; background: #126b5c; }
      .dot.review { background: #bc7a23; }
      .main-view, .settings-drawer { display: grid; gap: 10px; }
      .settings-drawer { margin: 0; }
      .main-view[hidden], .settings-drawer[hidden] { display: none; }
      .settings-header { display: flex; align-items: center; gap: 8px; min-height: 34px; }
      .settings-header h3 { min-width: 0; margin: 0; font-size: 16px; letter-spacing: -.02em; }
      .back-settings { min-height: 34px; padding: 6px 9px; border-color: transparent; background: #eef4f1; color: #39534c; font-size: 12px; font-weight: 650; }
      .profile-bar { display: grid; gap: 8px; padding: 9px; border: 1px solid #dce7e3; border-radius: 12px; background: #f6faf8; }
      .profile-tabs { display: flex; flex-wrap: wrap; gap: 7px; }
      .profile-card { position: relative; width: fit-content; min-width: 0; max-width: min(180px, 100%); border: 1px solid #d7e3de; border-radius: 10px; background: #fff; transition: border-color .16s ease, box-shadow .16s ease, transform .16s ease, opacity .16s ease; }
      @keyframes profile-ready-to-drag { 0%, 100% { transform: translateY(-1px) rotate(0); } 28% { transform: translateY(-2px) rotate(-.45deg); } 68% { transform: translateY(-2px) rotate(.45deg); } }
      .profile-card:hover { border-color: #9ebbb1; box-shadow: 0 5px 14px rgb(31 72 62 / .10); animation: profile-ready-to-drag .42s ease-in-out 1; }
      .profile-card[aria-current="true"] { border-color: #78a99a; background: #eaf5f0; }
      .profile-card.just-saved { border-color: #77aa96; box-shadow: 0 0 0 2px rgb(119 170 150 / .16); }
      .profile-card.drag-candidate { border-color: #78a99a; box-shadow: 0 0 0 2px rgb(119 170 150 / .16); }
      .profile-card.dragging { opacity: .28; animation: none; }
      .profile-card.sorting { animation: none !important; }
      .profile-drop-marker { flex: 0 0 auto; border: 1px dashed #78a99a; border-radius: 10px; background: #edf7f3; transition: width .14s ease, height .14s ease; }
      .profile-select { display: block; width: auto; max-width: min(178px, calc(100vw - 92px)); min-height: 36px; overflow: hidden; border: 0; border-radius: 9px; background: transparent; color: #526660; cursor: grab; font-size: 12px; font-weight: 650; text-align: left; text-overflow: ellipsis; white-space: nowrap; padding: 7px 9px; }
      .profile-select:active { cursor: grabbing; }
      .profile-drag-ghost { position: fixed; z-index: 2147483003; min-height: 36px; max-width: min(180px, calc(100vw - 32px)); overflow: hidden; border: 1px solid #78a99a; border-radius: 10px; background: #eaf5f0; box-shadow: 0 9px 20px rgb(31 72 62 / .18); color: #126b5c; font-size: 12px; font-weight: 650; line-height: 20px; pointer-events: none; text-overflow: ellipsis; white-space: nowrap; padding: 7px 9px; transform: translate(-50%, -50%) rotate(1.5deg); }
      .profile-select:hover { border-color: transparent; background: transparent; color: #31574d; }
      .profile-card[aria-current="true"] .profile-select { color: #126b5c; }
      .profile-card .profile-action { position: absolute; z-index: 1; display: grid; width: 18px; min-height: 18px; height: 18px; place-items: center; border: 1px solid #cbdad4; border-radius: 50%; background: #fff; box-shadow: 0 2px 6px rgb(31 72 62 / .14); color: #5c7069; font-size: 12px; line-height: 1; opacity: 0; pointer-events: none; padding: 0; transition: opacity .14s ease, background .14s ease, border-color .14s ease; }
      .profile-card:hover .profile-action, .profile-card.editing .profile-action { opacity: 1; pointer-events: auto; }
      .profile-edit { top: -7px; left: -7px; }
      .profile-delete { top: -7px; right: -7px; }
      .profile-card .profile-edit:hover { border-color: #84a99c; background: #eef7f3; color: #126b5c; }
      .profile-card .profile-delete:hover { border-color: #d7a39e; background: #fff5f4; color: #9a3430; }
      .profile-name-editor { width: 100%; min-height: 36px; border: 0; border-radius: 9px; background: #fff; color: #29453d; font-size: 12px; font-weight: 650; padding: 7px 9px; }
      .cloud-profile-name { display: none; }
      .profile-add { min-height: 36px; border-style: dashed; color: #526660; font-size: 12px; padding: 5px 9px; }
      .model-settings { display: grid; gap: 8px; padding: 10px; border: 1px solid #dce7e3; border-radius: 10px; background: #fff; }
      .model-settings h4 { margin: 0; color: #29453d; font-size: 12px; }
      .model-settings p { margin: -2px 0 0; color: #687a74; font-size: 11px; }
      .profile-actions, .settings-actions { display: flex; justify-content: flex-end; gap: 8px; }
      .profile-actions { justify-content: flex-start; }
      .test-cloud-connection { min-height: 30px; padding: 4px 9px; font-size: 12px; }
      .connection-test-status { color: #526660; font-size: 12px; font-weight: 650; line-height: 1.4; }
      .connection-test-status:empty { display: none; }
      .connection-test-status.error { color: #9a3430; }
      .connection-test-status.success { color: #28704d; }
      .advanced-connection { border-top: 1px solid #e4ebe8; margin-top: 2px; padding-top: 8px; }
      .advanced-connection summary { color: #526660; cursor: pointer; font-size: 11px; font-weight: 650; }
      .advanced-connection-fields { display: grid; gap: 8px; margin-top: 9px; }
      .custom-headers { min-height: 72px; resize: vertical; line-height: 1.45; }
      .custom-header-hint { margin: -3px 0 0; color: #687a74; font-size: 11px; line-height: 1.45; }
      label.clear-custom-headers-row { display: flex; align-items: center; gap: 6px; color: #687a74; font-size: 11px; }
      .clear-custom-headers { width: auto; min-height: auto; margin: 0; }
      .history-view { display: grid; gap: 12px; }
      .history-view[hidden] { display: none; }
      .history-header { display: flex; align-items: center; gap: 8px; min-height: 34px; }
      .history-header h3 { margin: 0; font-size: 16px; letter-spacing: -.02em; }
      .history-date-range { min-height: 34px; margin-left: auto; padding: 6px 9px; border-color: #c8d9d2; background: #fff; color: #31574d; font-size: 12px; font-weight: 650; white-space: nowrap; }
      .history-date-range:hover { border-color: #8ba99f; background: #f6faf8; }
      .back-history { min-height: 34px; padding: 6px 9px; border-color: transparent; background: #eef4f1; color: #39534c; font-size: 12px; font-weight: 650; }
      .history-export-actions { display: flex; gap: 6px; margin-left: auto; }
      .export-history { min-height: 34px; padding: 6px 9px; border-color: #126b5c; background: #126b5c; color: #fff; font-size: 12px; font-weight: 650; }
      .export-history:hover { border-color: #0c5649; background: #0c5649; }
      .history-summary { margin: 0; padding: 10px; border: 1px solid #dce7e3; border-radius: 10px; background: #f6faf8; color: #405650; font-size: 12px; }
      .history-topics { display: flex; flex-wrap: wrap; gap: 6px; }
      .history-topic { padding: 3px 7px; border-radius: 999px; background: #e7f2ee; color: #266657; font-size: 11px; }
      .history-list { display: grid; gap: 8px; max-height: min(48dvh, 420px); overflow: auto; }
      .history-record { display: grid; gap: 7px; padding: 10px; border: 1px solid #dce7e3; border-radius: 10px; background: #fff; }
      .history-code { color: #29453d; font-size: 12px; font-weight: 700; word-break: break-all; }
      .history-change { display: grid; gap: 5px; }
      .history-path { margin: 0; color: #526660; font-size: 11px; line-height: 1.6; }
      .history-path strong { color: #39534c; }
      .history-arrow { color: #126b5c; font-size: 11px; font-weight: 700; }
      .history-empty { margin: 0; padding: 18px 10px; color: #687a74; font-size: 12px; text-align: center; }
      .history-date-sheet[hidden] { display: none; }
      .history-date-sheet { position: fixed; inset: 0; z-index: 2147483003; display: grid; align-items: end; background: rgb(19 49 41 / .34); }
      .history-date-backdrop { position: absolute; inset: 0; border: 0; border-radius: 0; background: transparent; }
      .history-date-dialog { position: relative; display: grid; gap: 10px; width: min(364px, calc(100vw - 28px)); max-height: min(78dvh, 620px); margin: 0 auto; padding: 17px 18px calc(17px + env(safe-area-inset-bottom)); border: 1px solid #d7e1dd; border-bottom: 0; border-radius: 18px 18px 0 0; background: #fff; box-shadow: 0 -14px 42px rgb(19 49 41 / .2); }
      .history-date-picker-head { display: grid; grid-template-columns: 38px 1fr 38px; align-items: center; gap: 6px; }
      .history-date-picker-head strong { color: #29453d; font-size: 15px; text-align: center; }
      .history-month-prev, .history-month-next { min-height: 34px; padding: 4px; border-color: transparent; background: #eef4f1; color: #31574d; font-size: 20px; line-height: 1; }
      .history-weekdays, .history-calendar-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 4px; }
      .history-weekdays span { padding: 4px 0; color: #80918b; font-size: 11px; font-weight: 650; text-align: center; }
      .history-calendar-day { min-height: 38px; padding: 4px; border-color: transparent; background: transparent; color: #29453d; font-size: 12px; }
      .history-calendar-day:hover:not(:disabled) { background: #e7f2ee; }
      .history-calendar-day.in-range { border-radius: 0; background: #e7f2ee; color: #1d6554; }
      .history-calendar-day.preview-range { border-radius: 0; background: #c9e7dc; color: #145b4b; }
      .history-calendar-day.preview-range-end { border-radius: 8px; box-shadow: inset 0 0 0 1px #4f9784; font-weight: 700; }
      .history-calendar-day.range-start, .history-calendar-day.range-end { border-radius: 8px; background: #126b5c; color: #fff; font-weight: 700; }
      .history-calendar-day.today:not(.range-start):not(.range-end) { box-shadow: inset 0 0 0 1px #8ba99f; }
      .history-calendar-day:disabled { color: #c4cfca; cursor: not-allowed; }
      .history-calendar-spacer { min-height: 38px; }
      .history-date-hint { margin: 2px 0 0; color: #687a74; font-size: 11px; text-align: center; }
      .confirm-overlay[hidden] { display: none; }
      .confirm-overlay { position: fixed; inset: 0; z-index: 2147483002; display: grid; place-items: center; padding: 18px; background: rgb(19 49 41 / .34); }
      .confirm-dialog { width: min(352px, calc(100vw - 36px)); padding: 18px; border: 1px solid #d7e1dd; border-radius: 16px; background: #fff; box-shadow: 0 22px 56px rgb(19 49 41 / .26); }
      .confirm-dialog h3 { margin: 0; color: #29453d; font-size: 16px; letter-spacing: -.02em; }
      .confirm-dialog p { margin: 8px 0 0; color: #526660; font-size: 13px; }
      .confirm-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
      .danger { border-color: #b43c35; background: #b43c35; color: #fff; font-weight: 650; }
      .danger:hover { border-color: #8f2f2a; background: #8f2f2a; }
      @media (max-width: 480px) { .panel { right: 10px; width: calc(100vw - 20px); padding: 15px; } .history-date-dialog { width: calc(100vw - 20px); } .cloud-profile-name { width: 98px; } .history-header { flex-wrap: wrap; } .history-date-range { order: 3; margin-left: 0; } .history-export-actions { margin-left: auto; } }
      @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }
    </style>
    <button class="tab" type="button" aria-label="展开题目分类助手" aria-expanded="false">
      <span class="tab-arrow" aria-hidden="true">‹</span>
      <span class="tab-text">分类</span>
      <span class="tab-status" aria-hidden="true"></span>
    </button>
      <section class="panel" role="region" aria-label="题目分类助手" inert>
      <section class="main-view" aria-label="分类助手主界面">
      <div class="header"><h2>题目分类助手</h2><div class="header-actions"><button class="settings" type="button" aria-expanded="false">设置</button><button class="close" type="button" aria-label="收起面板">›</button></div></div>
      <div class="actions"><button class="primary classify" type="button" disabled>识别当前页</button></div>
      <div class="actions"><button class="primary classify-focus" type="button" disabled>识别当前目录全部题目</button></div>
      <div class="actions"><button class="restore-focus" type="button" hidden disabled>恢复上次审核队列</button></div>
      <div class="actions"><button class="primary accept-all" type="button" disabled>全部采纳</button></div>
      <div class="actions"><button class="primary export-all-questions" type="button" disabled>导出所有题库</button></div>
      <div class="actions"><button class="history" type="button">工作成果</button><button class="clear-cache" type="button" disabled>清除本页缓存</button></div>
       <!-- 批处理仅适合数百题以上的离线任务；保留实现，暂不占用日常实时分类面板。 -->
       <section class="batch-actions" hidden aria-label="高级批处理操作">
         <div class="actions"><button class="export-batch" type="button" disabled>导出批处理</button><button class="submit-batch" type="button" disabled>提交云端批处理</button></div>
         <div class="actions"><button class="sync-batch" type="button" disabled>同步批处理结果</button></div>
       </section>
      <div class="status" role="status" aria-live="polite">正在连接服务…</div>
      <section class="directory-plan" hidden aria-live="polite"><h3>目录重构方案</h3><p class="directory-plan-summary"></p><ul class="directory-plan-list"></ul></section>
      <div class="legend" hidden><h3>当前页状态</h3><ul></ul></div>
      </section>
      <section class="settings-drawer" hidden aria-label="模型设置">
        <div class="settings-header"><button class="back-settings" type="button">‹ 返回</button><h3>模型设置</h3></div>
        <section class="profile-bar" aria-label="模型方案"><select class="cloud-profile-select" hidden aria-hidden="true"></select><div class="profile-tabs" role="tablist" aria-label="选择模型方案"><button class="create-cloud-profile profile-add" type="button">＋ 新建方案</button></div><input class="cloud-profile-name" type="text" maxlength="40" autocomplete="off" aria-hidden="true" tabindex="-1"></section>
         <div class="form">
           <section class="model-settings"><h4>连接信息</h4><label>协议<select class="cloud-protocol"><option value="responses">Responses</option><option value="chat_completions">Chat Completions</option><option value="anthropic_messages">Claude Messages</option></select></label><label>接口地址<input class="cloud-base-url" type="url" autocomplete="off" placeholder="https://api.openai.com"></label><label>API 密钥<input class="cloud-api-key" type="password" autocomplete="new-password" placeholder="留空则保留已保存的密钥"></label><details class="advanced-connection"><summary>高级连接选项</summary><div class="advanced-connection-fields"><label>请求兼容方式<select class="request-compatibility"><option value="standard">标准（默认）</option><option value="go_http">兼容模式</option></select></label><p class="custom-header-hint">仅当接口拒绝标准连接时，才改用兼容模式。</p><label>自定义请求头（可选）<textarea class="custom-headers" autocomplete="off" spellcheck="false" placeholder="每行：名称: 值"></textarea></label><p class="custom-header-hint custom-header-status"></p><label class="clear-custom-headers-row"><input class="clear-custom-headers" type="checkbox">移除已保存的自定义请求头</label></div></details><div class="profile-actions"><button class="test-cloud-connection" type="button">测试连接</button></div><div class="connection-test-status" role="status" aria-live="polite"></div></section>
           <section class="model-settings"><h4>目录分类</h4><label>模型<span class="cloud-model-control"></span></label><label>思考强度<select class="classification-reasoning-effort"><option value="none">无</option><option value="low">低</option><option value="medium">中</option><option value="high" selected>高</option><option value="xhigh">很高</option><option value="max">最高</option></select></label></section>
           <section class="model-settings"><h4>专题选择</h4><p>留空时使用上面的模型。</p><label>模型（可选）<span class="routing-model-control"></span></label><label>思考强度<select class="routing-reasoning-effort"><option value="none">无</option><option value="low">低</option><option value="medium" selected>中</option><option value="high">高</option><option value="xhigh">很高</option><option value="max">最高</option></select></label></section>
           <section class="model-settings"><h4>处理速度</h4><p>所有方案共用。请求过多时调低。</p><label>同时处理的请求<select class="max-concurrent-requests"><option value="1">1</option><option value="2">2</option><option value="3" selected>3（推荐）</option><option value="4">4</option><option value="5">5</option></select></label></section>
        </div>
        <div class="settings-actions"><button class="cancel-settings" type="button">取消</button><button class="primary save-cloud" type="button">保存设置</button></div>
      </section>
      <section class="history-view" hidden aria-label="工作成果">
        <div class="history-header"><button class="back-history" type="button">‹ 返回</button><h3>工作成果</h3><button class="history-date-range" type="button" aria-haspopup="dialog" aria-expanded="false">选择日期</button><div class="history-export-actions"><button class="export-history" type="button" disabled>导出</button></div></div>
        <p class="history-summary" aria-live="polite">正在加载工作成果…</p>
        <div class="history-topics" aria-label="涉及专题"></div>
        <div class="history-list" aria-live="polite"></div>
      </section>
      <section class="history-date-sheet" hidden aria-hidden="true"><button class="history-date-backdrop" type="button" aria-label="关闭日期选择器"></button><div class="history-date-dialog" role="dialog" aria-modal="true" aria-label="选择工作成果日期范围"><div class="history-date-picker-head"><button class="history-month-prev" type="button" aria-label="上个月">‹</button><strong class="history-month-title"></strong><button class="history-month-next" type="button" aria-label="下个月">›</button></div><div class="history-weekdays" aria-hidden="true"><span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span></div><div class="history-calendar-grid"></div><p class="history-date-hint"></p></div></section>
      <section class="confirm-overlay" hidden aria-hidden="true">
        <div class="confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-message">
          <h3 id="confirm-title">确认操作</h3>
          <p id="confirm-message"></p>
          <div class="confirm-actions"><button class="confirm-cancel" type="button">取消</button><button class="confirm-accept danger" type="button">确认清除</button></div>
        </div>
      </section>
    </section>`;
  document.body.append(host);

  const elements = {
    panel: shadow.querySelector('.panel'), tab: shadow.querySelector('.tab'), tabStatus: shadow.querySelector('.tab-status'), close: shadow.querySelector('.close'), mainView: shadow.querySelector('.main-view'), settings: shadow.querySelector('.settings'), settingsDrawer: shadow.querySelector('.settings-drawer'), backSettings: shadow.querySelector('.back-settings'),
     cloudProfileSelect: shadow.querySelector('.cloud-profile-select'), cloudProfileName: shadow.querySelector('.cloud-profile-name'), profileTabs: shadow.querySelector('.profile-tabs'), createCloudProfile: shadow.querySelector('.create-cloud-profile'), cloudModelControl: shadow.querySelector('.cloud-model-control'), routingModelControl: shadow.querySelector('.routing-model-control'), cloudModel: null, routingModel: null, classificationReasoningEffort: shadow.querySelector('.classification-reasoning-effort'), routingReasoningEffort: shadow.querySelector('.routing-reasoning-effort'), maxConcurrentRequests: shadow.querySelector('.max-concurrent-requests'), cloudProtocol: shadow.querySelector('.cloud-protocol'), cloudBaseUrl: shadow.querySelector('.cloud-base-url'), cloudApiKey: shadow.querySelector('.cloud-api-key'), requestCompatibility: shadow.querySelector('.request-compatibility'), customHeaders: shadow.querySelector('.custom-headers'), customHeaderStatus: shadow.querySelector('.custom-header-status'), clearCustomHeaders: shadow.querySelector('.clear-custom-headers'), testCloudConnection: shadow.querySelector('.test-cloud-connection'), connectionTestStatus: shadow.querySelector('.connection-test-status'), saveCloud: shadow.querySelector('.save-cloud'),
    cancelSettings: shadow.querySelector('.cancel-settings'), classify: shadow.querySelector('.classify'), classifyFocus: shadow.querySelector('.classify-focus'), restoreFocus: shadow.querySelector('.restore-focus'), exportAllQuestions: shadow.querySelector('.export-all-questions'), acceptAll: shadow.querySelector('.accept-all'), history: shadow.querySelector('.history'), historyView: shadow.querySelector('.history-view'), backHistory: shadow.querySelector('.back-history'), historyDateRange: shadow.querySelector('.history-date-range'), historyDateSheet: shadow.querySelector('.history-date-sheet'), historyDateBackdrop: shadow.querySelector('.history-date-backdrop'), historyMonthPrev: shadow.querySelector('.history-month-prev'), historyMonthNext: shadow.querySelector('.history-month-next'), historyMonthTitle: shadow.querySelector('.history-month-title'), historyCalendarGrid: shadow.querySelector('.history-calendar-grid'), historyDateHint: shadow.querySelector('.history-date-hint'), exportHistory: shadow.querySelector('.export-history'), historySummary: shadow.querySelector('.history-summary'), historyTopics: shadow.querySelector('.history-topics'), historyList: shadow.querySelector('.history-list'), clearCache: shadow.querySelector('.clear-cache'), exportBatch: shadow.querySelector('.export-batch'), submitBatch: shadow.querySelector('.submit-batch'), syncBatch: shadow.querySelector('.sync-batch'), status: shadow.querySelector('.status'), directoryPlan: shadow.querySelector('.directory-plan'), directoryPlanSummary: shadow.querySelector('.directory-plan-summary'), directoryPlanList: shadow.querySelector('.directory-plan-list'),
    legend: shadow.querySelector('.legend'), legendList: shadow.querySelector('.legend ul'),
    confirmOverlay: shadow.querySelector('.confirm-overlay'), confirmTitle: shadow.querySelector('#confirm-title'), confirmMessage: shadow.querySelector('#confirm-message'), confirmCancel: shadow.querySelector('.confirm-cancel'), confirmAccept: shadow.querySelector('.confirm-accept'),
  };

  function setOpen(open) {
    if (!open) {
      closeSettings();
      closeHistory();
    }
    elements.panel.classList.toggle('open', open);
    elements.panel.inert = !open;
    elements.tab.hidden = open;
    elements.tab.setAttribute('aria-expanded', String(open));
    (open ? elements.close : elements.tab).focus({ preventScroll: true });
  }

  function setStatus(message, tone = 'normal') {
    // 兼容既有 true 参数：它仍表示红色错误；“已完成但有缺口”单独用黄色提醒。
    const resolvedTone = tone === true ? 'error' : (tone === 'warning' ? 'warning' : 'normal');
    elements.status.textContent = message;
    elements.status.classList.toggle('error', resolvedTone === 'error');
    elements.status.classList.toggle('warning', resolvedTone === 'warning');
  }

  function setConnectionTestStatus(message = '', tone = 'normal') {
    elements.connectionTestStatus.textContent = message;
    elements.connectionTestStatus.classList.toggle('error', tone === 'error');
    elements.connectionTestStatus.classList.toggle('success', tone === 'success');
  }

  function renderAvailableModels(
    profileId = elements.cloudProfileSelect.value,
    directoryModel = elements.cloudModel?.value || '',
    routingModel = elements.routingModel?.value || '',
  ) {
    const rawModels = state.availableModelsByProfile.get(profileId);
    const models = Array.isArray(rawModels) ? rawModels : [];
    const buildOption = (value, label = value, { disabled = false } = {}) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      option.disabled = disabled;
      return option;
    };
    const selectedDirectory = normalizeWhitespace(directoryModel);
    const selectedRouting = normalizeWhitespace(routingModel);
    const modelOptions = models.map(model => buildOption(model));
    const directoryOptions = [buildOption('', '请选择模型')];
    const routingOptions = [buildOption('', '使用目录分类模型')];
    if (selectedDirectory && !models.includes(selectedDirectory)) {
      directoryOptions.push(buildOption(selectedDirectory, selectedDirectory));
    }
    if (selectedRouting && !models.includes(selectedRouting)) {
      routingOptions.push(buildOption(selectedRouting, selectedRouting));
    }
    const renderControl = (container, className, selected, options, placeholder, optional) => {
      const control = document.createElement(models.length ? 'select' : 'input');
      control.className = className;
      control.setAttribute('aria-label', optional ? '专题选择模型' : '目录分类模型');
      if (control instanceof HTMLInputElement) {
        control.type = 'text';
        control.autocomplete = 'off';
        control.placeholder = placeholder;
        control.value = selected;
      } else {
        control.replaceChildren(...options);
        control.value = selected;
      }
      container.replaceChildren(control);
      return control;
    };
    elements.cloudModel = renderControl(
      elements.cloudModelControl, 'cloud-model', selectedDirectory,
      [...directoryOptions, ...modelOptions.map(option => option.cloneNode(true))],
      '例如你的模型部署名', false,
    );
    elements.routingModel = renderControl(
      elements.routingModelControl, 'routing-model', selectedRouting,
      [...routingOptions, ...modelOptions],
      '留空则使用目录分类模型', true,
    );
  }

  function setAvailableModels(profileId, values) {
    const seen = new Set();
    const models = [];
    for (const item of Array.isArray(values) ? values : []) {
      const model = normalizeWhitespace(item);
      if (model && !seen.has(model)) {
        seen.add(model);
        models.push(model);
      }
      if (models.length >= 500) break;
    }
    state.availableModelsByProfile.set(profileId, models);
    if (profileId === elements.cloudProfileSelect.value) renderAvailableModels(profileId);
  }

  function clearAvailableModels() {
    const profileId = elements.cloudProfileSelect.value;
    state.availableModelsByProfile.delete(profileId);
    renderAvailableModels(profileId);
  }

  function confirmAction({ title, message, confirmLabel = '确认', destructive = false }) {
    if (state.confirmation) return Promise.resolve(false);
    const previousFocus = shadow.activeElement || document.activeElement;
    elements.confirmTitle.textContent = title;
    elements.confirmMessage.textContent = message;
    elements.confirmAccept.textContent = confirmLabel;
    elements.confirmAccept.classList.toggle('danger', destructive);
    elements.confirmOverlay.hidden = false;
    elements.confirmOverlay.setAttribute('aria-hidden', 'false');
    elements.confirmAccept.focus({ preventScroll: true });
    return new Promise(resolve => {
      state.confirmation = { resolve, previousFocus };
    });
  }

  function closeConfirmation(confirmed) {
    const confirmation = state.confirmation;
    if (!confirmation) return;
    state.confirmation = null;
    elements.confirmOverlay.hidden = true;
    elements.confirmOverlay.setAttribute('aria-hidden', 'true');
    if (confirmation.previousFocus?.focus) confirmation.previousFocus.focus({ preventScroll: true });
    confirmation.resolve(confirmed);
  }

  function setBusy(busy) {
    state.busy = busy;
    elements.settings.disabled = busy;
    elements.saveCloud.disabled = busy;
    elements.cloudProfileSelect.disabled = busy;
    elements.cloudProfileName.disabled = busy;
    elements.createCloudProfile.disabled = busy;
    elements.profileTabs.querySelectorAll('button').forEach(button => { button.disabled = busy; });
    elements.cloudProtocol.disabled = busy;
    elements.cloudBaseUrl.disabled = busy;
    elements.cloudApiKey.disabled = busy;
    elements.requestCompatibility.disabled = busy;
    elements.customHeaders.disabled = busy;
    elements.clearCustomHeaders.disabled = busy;
    elements.testCloudConnection.disabled = busy;
    elements.classify.disabled = busy || !state.taxonomy;
    elements.classifyFocus.disabled = busy || !state.taxonomy;
    updateFocusRestoreAction();
    elements.exportAllQuestions.disabled = busy || !state.taxonomy;
    elements.history.disabled = busy;
    elements.exportHistory.disabled = busy || !state.historyReport?.records?.length;
    elements.clearCache.disabled = busy || !state.taxonomy;
    elements.exportBatch.disabled = busy || !state.cloudConfigured || !state.currentQuestions.length;
    elements.submitBatch.disabled = busy || !state.cloudConfigured || !state.batchJobId;
    elements.syncBatch.disabled = busy || !state.cloudConfigured || !state.batchJobId;
    updateAcceptAllAction();
  }

  function updateFocusRestoreAction() {
    elements.restoreFocus.hidden = !state.pendingFocusRestore;
    elements.restoreFocus.disabled = state.busy || !state.pendingFocusRestore;
  }

  function openSettings() {
    elements.mainView.hidden = true;
    elements.settingsDrawer.hidden = false;
    elements.settings.setAttribute('aria-expanded', 'true');
  }

  function closeSettings() {
    elements.settingsDrawer.hidden = true;
    elements.mainView.hidden = false;
    elements.settings.setAttribute('aria-expanded', 'false');
  }

  function closeHistory() {
    closeHistoryDatePicker();
    elements.historyView.hidden = true;
    elements.mainView.hidden = false;
  }

  function formatHistoryPath(path) {
    return (Array.isArray(path) ? path : []).slice(0, 4)
      .map((item, index) => item ? `${DIRECTORY_LEVEL_LABELS[index]}：${item}` : '')
      .filter(Boolean).join('　');
  }

  function renderHistory(report) {
    const summary = report?.summary || {};
    const records = Array.isArray(report?.records) ? report.records : [];
    const topics = Array.isArray(summary.topics) ? summary.topics : [];
    const periodLabel = normalizeWhitespace(summary.period?.label) || '所选日期';
    state.historyReport = { summary, records };
    elements.exportHistory.disabled = !records.length;
    elements.historySummary.textContent = `${periodLabel}：已分类 ${Number(summary.classified_count) || 0} 道题目，涉及 ${topics.length} 个专题。`;
    elements.historyTopics.replaceChildren(...topics.map(topic => {
      const item = document.createElement('span');
      item.className = 'history-topic';
      item.textContent = topic;
      return item;
    }));
    if (!records.length) {
      const empty = document.createElement('p');
      empty.className = 'history-empty';
      empty.textContent = `${periodLabel}暂无已分类题目记录。`;
      elements.historyList.replaceChildren(empty);
      return;
    }
    elements.historyList.replaceChildren(...records.map(record => {
      const item = document.createElement('article');
      item.className = 'history-record';
      const code = document.createElement('div');
      code.className = 'history-code';
      code.textContent = record.stable_code || '未提取 Stable Code';
      const change = document.createElement('div');
      change.className = 'history-change';
      const original = document.createElement('p');
      original.className = 'history-path';
      const originalLabel = document.createElement('strong');
      originalLabel.textContent = '原目录';
      const originalPath = Array.isArray(record.original_path) ? record.original_path : [];
      original.append(originalLabel, document.createTextNode(originalPath.length
        ? `　${formatHistoryPath(originalPath)}`
        : '　未能从当时页面加载的目录树读取'));
      const arrow = document.createElement('div');
      arrow.className = 'history-arrow';
      arrow.textContent = '↓ 移动至';
      const target = document.createElement('p');
      target.className = 'history-path';
      const targetLabel = document.createElement('strong');
      targetLabel.textContent = '现目录';
      target.append(targetLabel, document.createTextNode(`　${formatHistoryPath(record.target_path || [])}`));
      change.append(original, arrow, target);
      item.append(code, change);
      return item;
    }));
  }

  function historyRangeLabel(range = state.historyRange) {
    const start = normalizeWhitespace(range?.start);
    const end = normalizeWhitespace(range?.end);
    const format = value => value.replaceAll('-', '.');
    if (!start) return '选择日期';
    return !end || start === end ? format(start) : `${format(start)} - ${format(end)}`;
  }

  function updateHistoryDateRange() {
    const label = historyRangeLabel();
    elements.historyDateRange.textContent = label;
    elements.historyDateRange.setAttribute('aria-label', `选择工作成果日期范围，当前为${label}`);
  }

  function historyMonthValue(dateValue = chinaDate()) {
    return normalizeWhitespace(dateValue).slice(0, 7) || chinaDate().slice(0, 7);
  }

  function historyDateFromParts(year, month, day) {
    return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  }

  function moveHistoryCalendarMonth(offset) {
    const [year, month] = historyMonthValue(state.historyCalendarMonth).split('-').map(Number);
    const next = new Date(Date.UTC(year, month - 1 + offset, 1));
    state.historyCalendarMonth = historyDateFromParts(next.getUTCFullYear(), next.getUTCMonth() + 1, 1).slice(0, 7);
    state.historyRangePreviewEnd = '';
    renderHistoryCalendar();
  }

  function renderHistoryCalendar() {
    const monthValue = historyMonthValue(state.historyCalendarMonth);
    const [year, month] = monthValue.split('-').map(Number);
    const firstDay = new Date(Date.UTC(year, month - 1, 1));
    const today = chinaDate();
    const maxMonth = today.slice(0, 7);
    const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
    const start = state.historyRange.start;
    const end = state.historyRange.end;
    const previewDate = historyRangePreviewEnd(state.historyRange, state.historyRangePreviewEnd);
    const previewStart = previewDate && previewDate < start ? previewDate : start;
    const previewEnd = previewDate && previewDate > start ? previewDate : start;
    elements.historyMonthTitle.textContent = `${year} 年 ${month} 月`;
    elements.historyMonthNext.disabled = monthValue >= maxMonth;
    elements.historyCalendarGrid.replaceChildren();
    for (let index = 0; index < firstDay.getUTCDay(); index += 1) {
      const spacer = document.createElement('span');
      spacer.className = 'history-calendar-spacer';
      spacer.setAttribute('aria-hidden', 'true');
      elements.historyCalendarGrid.append(spacer);
    }
    for (let day = 1; day <= daysInMonth; day += 1) {
      const value = historyDateFromParts(year, month, day);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'history-calendar-day';
      button.textContent = String(day);
      button.dataset.date = value;
      button.disabled = value > today;
      button.setAttribute('aria-label', value);
      if (value === today) button.classList.add('today');
      if (value === start) button.classList.add('range-start');
      if (value === end) button.classList.add('range-end');
      if (start && end && value > start && value < end) button.classList.add('in-range');
      if (previewDate && value > previewStart && value < previewEnd) button.classList.add('preview-range');
      if (previewDate && value === previewDate && value !== start) button.classList.add('preview-range-end');
      elements.historyCalendarGrid.append(button);
    }
    elements.historyDateHint.textContent = start && !end
      ? '请选择范围的另一端日期；再次选择同一天即可查询当天。'
      : '先选择起始日期，再选择截止日期。';
  }

  function openHistoryDatePicker() {
    if (state.busy) return;
    state.historyCalendarMonth = historyMonthValue(state.historyRange.end || state.historyRange.start || chinaDate());
    state.historyRangePreviewEnd = '';
    renderHistoryCalendar();
    elements.historyDateSheet.hidden = false;
    elements.historyDateSheet.setAttribute('aria-hidden', 'false');
    elements.historyDateRange.setAttribute('aria-expanded', 'true');
  }

  function closeHistoryDatePicker() {
    state.historyRangePreviewEnd = '';
    elements.historyDateSheet.hidden = true;
    elements.historyDateSheet.setAttribute('aria-hidden', 'true');
    elements.historyDateRange.setAttribute('aria-expanded', 'false');
  }

  async function chooseHistoryDate(value) {
    const dateValue = normalizeWhitespace(value);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(dateValue) || dateValue > chinaDate()) return;
    if (!state.historyRange.start || state.historyRange.end) {
      state.historyRange = { start: dateValue, end: '' };
      state.historyRangePreviewEnd = '';
      updateHistoryDateRange();
      renderHistoryCalendar();
      return;
    }
    const firstDate = state.historyRange.start;
    state.historyRange = dateValue < firstDate
      ? { start: dateValue, end: firstDate }
      : { start: firstDate, end: dateValue };
    state.historyRangePreviewEnd = '';
    updateHistoryDateRange();
    closeHistoryDatePicker();
    await loadHistoryForRange();
  }

  async function loadHistoryForRange() {
    const start = normalizeWhitespace(state.historyRange.start) || chinaDate();
    const end = normalizeWhitespace(state.historyRange.end) || start;
    const loadId = ++state.historyLoadId;
    state.historyReport = null;
    elements.exportHistory.disabled = true;
    elements.historyDateRange.disabled = true;
    elements.historySummary.textContent = `正在加载 ${historyRangeLabel({ start, end })} 的移动记录…`;
    elements.historyTopics.replaceChildren();
    elements.historyList.replaceChildren();
    try {
      const report = await request('GET', `/api/v1/history/catalogue-moves?start_date=${encodeURIComponent(start)}&end_date=${encodeURIComponent(end)}`);
      if (loadId !== state.historyLoadId) return;
      renderHistory(report);
    } catch (error) {
      if (loadId === state.historyLoadId) elements.historySummary.textContent = `无法加载工作成果：${error.message}`;
    } finally {
      if (loadId === state.historyLoadId) elements.historyDateRange.disabled = false;
    }
  }

  async function openHistory() {
    if (state.busy) return;
    closeSettings();
    elements.mainView.hidden = true;
    elements.historyView.hidden = false;
    state.historyRange = { start: chinaDate(), end: chinaDate() };
    updateHistoryDateRange();
    await loadHistoryForRange();
  }

  function selectedHistoryDateRange() {
    const period = state.historyReport?.summary?.period || {};
    const start = normalizeWhitespace(period.start_date || period.date) || state.historyRange.start || chinaDate();
    const end = normalizeWhitespace(period.end_date) || state.historyRange.end || start;
    return start === end ? start : `${start}至${end}`;
  }

  function selectedHistoryLabel() {
    return normalizeWhitespace(state.historyReport?.summary?.period?.label) || `${selectedHistoryDateRange()} 工作成果`;
  }

  function exportHistoryReport() {
    const records = state.historyReport?.records;
    if (!Array.isArray(records) || !records.length) {
      setStatus('暂无可导出的工作成果记录', true);
      return;
    }
    const blob = new Blob([buildHistoryReportHtml(state.historyReport)], { type: 'text/html;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `题目分类成果-${selectedHistoryDateRange()}.html`;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
    setStatus(`已导出 ${selectedHistoryLabel()}的 ${records.length} 道题工作成果报告。`);
  }

  function request(method, path, body, { timeoutMs = LOCAL_REQUEST_TIMEOUT_MS } = {}) {
    return new Promise((resolve, reject) => {
      const headers = { Accept: 'application/json' };
      if (body) headers['Content-Type'] = 'application/json; charset=utf-8';
      const options = {
        method, url: `${SERVICE_URL}${path}`, headers,
        data: body ? JSON.stringify(body) : undefined,
        onload(response) {
          let parsed;
          try { parsed = JSON.parse(response.responseText); }
          catch { reject(new Error('本地服务未返回 JSON')); return; }
          if (response.status < 200 || response.status >= 300) {
            reject(new Error(parsed.message || parsed.error || `本地服务返回 HTTP ${response.status}`));
            return;
          }
          resolve(parsed);
        },
        onerror() { reject(new Error('无法连接本地服务，请确认 127.0.0.1:3232 已启动')); },
        ontimeout() { reject(new Error('本地服务响应超时')); },
      };
      // 分类可能受模型排队影响，不设置浏览器端硬截止；其它本地接口应快速失败。
      if (Number.isFinite(timeoutMs) && timeoutMs > 0) options.timeout = timeoutMs;
      GM_xmlhttpRequest(options);
    });
  }

  function delay(milliseconds) {
    return new Promise(resolve => setTimeout(resolve, milliseconds));
  }

  function detectPageScope(taxonomy) {
    const headings = [...document.querySelectorAll('h1, h2, h3, h4')]
      .map(node => normalizeWhitespace(node.textContent))
      .filter(Boolean);
    const topic = [...(taxonomy.topics || [])]
      .sort((left, right) => String(right.title).length - String(left.title).length)
      .find(item => headings.some(heading => {
        const pageKey = directoryKey(heading);
        const topicKey = directoryKey(item.title);
        return pageKey === topicKey || pageKey.includes(topicKey) || topicKey.includes(pageKey);
      }));
    if (!topic) return null;
    const level2Items = topic.level2 || [];
    const level2 = level2Items.find(item => directoryKey(item.title) === directoryKey('【大题】'))
      || (level2Items.length === 1 ? level2Items[0] : null);
    return level2 ? { topic_id: topic.id, level2_id: level2.id, title: `${topic.title} / ${level2.title}` } : null;
  }

  async function connectService() {
    const restoreId = ++state.cacheRestoreId;
    setBusy(true);
    try {
      const [health, taxonomy, cloud] = await Promise.all([
        request('GET', '/health'),
        request('GET', '/api/v1/taxonomy'),
        request('GET', '/api/v1/settings/cloud'),
      ]);
      state.taxonomy = taxonomy;
      state.cloudConfigured = Boolean(health.cloud_configured);
      renderCloudSettings(cloud);
      const pageScope = detectPageScope(state.taxonomy);
      if (!pageScope) {
        state.scope = { topic_id: '', level2_id: '' };
      } else {
        state.scope = pageScope;
      }
      const connectedMessage = '服务已就绪';
      setStatus(connectedMessage);
      elements.tabStatus.textContent = '就绪';
      const savedFocus = loadFocusSnapshot();
      const focus = focusedDirectoryScope(state.taxonomy);
      state.focusSnapshot = focusSnapshotMatches(savedFocus, location.pathname, focus) ? savedFocus : null;
      state.pendingFocusRestore = Boolean(state.focusSnapshot);
      updateFocusRestoreAction();

      // 刷新时只恢复当前页，避免在未明确操作时读取整个目录的所有分页。
      const restored = await restoreCachedResults(restoreId);
      if (state.pendingFocusRestore) {
        setStatus(`发现上次当前目录审核队列。已恢复当前页缓存；如需回到完整审核优先队列，请点击“恢复上次审核队列”。该操作只读取题目属性和本地缓存，不会调用云端模型。`);
      } else if (!restored.cancelled && restored.restored) {
        setStatus(`已恢复 ${restored.restored} 道当前页缓存建议`);
      } else if (!restored.cancelled && restored.attributeFailed) {
        setStatus(`${restored.attributeFailed} 道题属性暂时无法读取`, 'warning');
      }
    } catch (error) {
      state.taxonomy = null;
      setStatus(error.message, true);
      elements.tabStatus.textContent = '!';
    } finally {
      setBusy(false);
    }
  }

  function renderCloudSettings(cloud) {
    const profiles = Array.isArray(cloud.profiles) && cloud.profiles.length
      ? cloud.profiles : [{ id: cloud.active_profile_id || 'default', name: cloud.active_profile_name || '默认方案', ...cloud }];
    const activeId = cloud.active_profile_id || profiles[0].id;
    state.cloudSettings = cloud;
    state.cloudProfiles = profiles;
    elements.cloudProfileSelect.replaceChildren(...profiles.map(profile => {
      const option = document.createElement('option');
      option.value = profile.id;
      option.textContent = profile.name || '未命名方案';
      option.selected = profile.id === activeId;
      return option;
    }));
    const profileTabs = profiles.map(profile => {
      const card = document.createElement('div');
      card.className = 'profile-card';
      card.dataset.profileId = profile.id;
      card.dataset.profileName = profile.name || '未命名方案';
      card.setAttribute('aria-current', String(profile.id === activeId));
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'profile-select';
      tab.dataset.profileId = profile.id;
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(profile.id === activeId));
      tab.title = '单击切换；长按拖动排序';
      tab.disabled = state.busy;
      tab.textContent = profile.name || '未命名方案';
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'profile-action profile-edit';
      edit.dataset.profileId = profile.id;
      edit.setAttribute('aria-label', `编辑“${profile.name || '未命名方案'}”`);
      edit.title = '编辑方案名称';
      edit.textContent = '✎';
      edit.disabled = state.busy;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'profile-action profile-delete';
      remove.dataset.profileId = profile.id;
      remove.setAttribute('aria-label', `删除“${profile.name || '未命名方案'}”`);
      remove.title = '删除方案';
      remove.textContent = '×';
      remove.disabled = state.busy || profiles.length <= 1;
      card.append(tab, edit, remove);
      return card;
    });
    elements.profileTabs.replaceChildren(...profileTabs, elements.createCloudProfile);
    const active = profiles.find(profile => profile.id === activeId) || profiles[0];
    elements.cloudProfileName.value = active.name || '';
    elements.cloudProtocol.value = active.protocol || 'responses';
    elements.classificationReasoningEffort.value = active.reasoning_effort || 'high';
    elements.routingReasoningEffort.value = active.routing_reasoning_effort || 'medium';
    elements.maxConcurrentRequests.value = String((cloud.pipeline || {}).max_concurrent_requests || cloud.max_concurrent_requests || 3);
    elements.cloudBaseUrl.value = active.base_url || 'https://api.openai.com';
    elements.cloudApiKey.value = '';
    elements.requestCompatibility.value = active.request_compatibility || 'standard';
    elements.customHeaders.value = '';
    elements.clearCustomHeaders.checked = false;
    const savedHeaderNames = Array.isArray(active.custom_header_names) ? active.custom_header_names : [];
    elements.customHeaderStatus.textContent = savedHeaderNames.length
      ? `已保存：${savedHeaderNames.join('、')}。留空会保留；填写后会整体替换。`
      : '每行填写一个“名称: 值”。';
    renderAvailableModels(activeId, active.model || '', active.routing_model || '');
    setConnectionTestStatus('');
  }

  function profileCards() {
    return [...elements.profileTabs.querySelectorAll('.profile-card')];
  }

  function profileCardAtPoint(clientX, clientY) {
    return profileCards().find(card => {
      const rect = card.getBoundingClientRect();
      return clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom;
    }) || null;
  }

  function profileCardRects() {
    return new Map(profileCards().map(card => [card, card.getBoundingClientRect()]));
  }

  function animateProfileCardReflow(beforeRects) {
    for (const card of profileCards()) {
      const before = beforeRects.get(card);
      if (!before) continue;
      const after = card.getBoundingClientRect();
      const deltaX = before.left - after.left;
      const deltaY = before.top - after.top;
      if (!deltaX && !deltaY) continue;
      card.classList.add('sorting');
      card.style.transform = `translate(${deltaX}px, ${deltaY}px)`;
      card.getBoundingClientRect();
      requestAnimationFrame(() => {
        card.style.transform = '';
        window.setTimeout(() => card.classList.remove('sorting'), 180);
      });
    }
  }

  function moveProfileDropMarker(drag, reference) {
    if (!drag.marker || reference === drag.marker || drag.marker.nextSibling === reference) return;
    const beforeRects = profileCardRects();
    elements.profileTabs.insertBefore(drag.marker, reference);
    animateProfileCardReflow(beforeRects);
  }

  function moveDraggedProfileCard(drag, clientX, clientY) {
    if (!drag.ghost) return;
    drag.ghost.style.left = `${clientX}px`;
    drag.ghost.style.top = `${clientY}px`;
    if (drag.marker) {
      const markerRect = drag.marker.getBoundingClientRect();
      if (clientX >= markerRect.left && clientX <= markerRect.right && clientY >= markerRect.top && clientY <= markerRect.bottom) return;
    }
    const over = profileCardAtPoint(clientX, clientY);
    if (over) {
      const rect = over.getBoundingClientRect();
      const before = clientY < rect.top + rect.height / 2
        || (clientY <= rect.bottom && clientX < rect.left + rect.width / 2);
      moveProfileDropMarker(drag, before ? over : over.nextSibling);
      return;
    }
    const container = elements.profileTabs.getBoundingClientRect();
    if (clientX >= container.left && clientX <= container.right && clientY >= container.top && clientY <= container.bottom) {
      moveProfileDropMarker(drag, elements.createCloudProfile);
    }
  }

  function beginProfileDrag(drag) {
    if (state.profileDrag !== drag || drag.cancelled) return;
    drag.started = true;
    elements.profileTabs.setPointerCapture?.(drag.pointerId);
    drag.card.classList.add('drag-candidate', 'dragging');
    const rect = drag.card.getBoundingClientRect();
    const marker = document.createElement('div');
    marker.className = 'profile-drop-marker';
    marker.style.width = `${Math.ceil(rect.width)}px`;
    marker.style.height = `${Math.ceil(rect.height)}px`;
    drag.card.replaceWith(marker);
    drag.marker = marker;
    const ghost = document.createElement('div');
    ghost.className = 'profile-drag-ghost';
    ghost.textContent = drag.card.dataset.profileName || '未命名方案';
    shadow.append(ghost);
    drag.ghost = ghost;
    ghost.style.left = `${drag.clientX}px`;
    ghost.style.top = `${drag.clientY}px`;
  }

  function restoreProfileDragCard(drag, restoreOriginalOrder = false) {
    if (drag.marker?.isConnected) drag.marker.replaceWith(drag.card);
    if (restoreOriginalOrder) {
      const byId = new Map(profileCards().map(card => [card.dataset.profileId, card]));
      for (const id of drag.originalOrder) {
        const card = byId.get(id);
        if (card) elements.profileTabs.insertBefore(card, elements.createCloudProfile);
      }
    }
  }

  function finishProfileDrag(drag, restoreOriginalOrder = false) {
    if (!drag) return;
    window.clearTimeout(drag.timer);
    drag.ghost?.remove();
    if (drag.started) restoreProfileDragCard(drag, restoreOriginalOrder);
    drag.card.classList.remove('drag-candidate', 'dragging');
    if (state.profileDrag === drag) state.profileDrag = null;
  }

  async function saveProfileOrder(profileIds, originalOrder) {
    if (profileIds.every((id, index) => id === originalOrder[index])) return;
    setBusy(true);
    try {
      const cloud = await request('POST', '/api/v1/settings/cloud', {
        action: 'reorder_profiles', profile_ids: profileIds,
      });
      state.cloudConfigured = Boolean(cloud.api_key_configured);
      renderCloudSettings(cloud);
      setStatus('模型方案顺序已保存');
    } catch (error) {
      if (state.cloudSettings) renderCloudSettings(state.cloudSettings);
      setStatus(error.message || '模型方案排序保存失败', true);
    } finally { setBusy(false); }
  }

  function startProfileDragCandidate(event) {
    if (event.button !== 0 || state.busy) return;
    if (state.profileDrag) finishProfileDrag(state.profileDrag, true);
    const target = event.target instanceof Element ? event.target.closest('.profile-select') : null;
    if (!target) return;
    const card = target.closest('.profile-card');
    if (!card || card.classList.contains('editing')) return;
    const profileId = card.dataset.profileId;
    if (!profileId) return;
    const drag = {
      profileId, card, pointerId: event.pointerId, clientX: event.clientX, clientY: event.clientY,
      startX: event.clientX, startY: event.clientY, originalOrder: profileCards().map(item => item.dataset.profileId),
      started: false, cancelled: false, timer: null, ghost: null, marker: null,
    };
    state.profileDrag = drag;
    drag.timer = window.setTimeout(() => beginProfileDrag(drag), 220);
  }

  function moveProfileDragCandidate(event) {
    const drag = state.profileDrag;
    if (!drag || event.pointerId !== drag.pointerId) return;
    drag.clientX = event.clientX;
    drag.clientY = event.clientY;
    if (!drag.started) {
      if (Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) > 8) {
        drag.cancelled = true;
        finishProfileDrag(drag);
      }
      return;
    }
    event.preventDefault();
    moveDraggedProfileCard(drag, event.clientX, event.clientY);
  }

  function endProfileDragCandidate(event) {
    const drag = state.profileDrag;
    if (!drag || event.pointerId !== drag.pointerId) return;
    const started = drag.started;
    const cancelled = event.type === 'pointercancel' || event.type === 'lostpointercapture';
    finishProfileDrag(drag, cancelled);
    if (!started) return;
    state.suppressProfileClickUntil = Date.now() + 450;
    if (cancelled) return;
    const changedOrder = profileCards().map(item => item.dataset.profileId);
    saveProfileOrder(changedOrder, drag.originalOrder);
  }

  async function selectCloudProfile(profileId = elements.cloudProfileSelect.value) {
    setBusy(true);
    try {
      const cloud = await request('POST', '/api/v1/settings/cloud', {
        action: 'select_profile', profile_id: profileId,
      });
      state.cloudConfigured = Boolean(cloud.api_key_configured);
      renderCloudSettings(cloud);
      setStatus(`已切换到“${cloud.active_profile_name}”`);
    } catch (error) { setStatus(error.message, true); }
    finally { setBusy(false); }
  }

  async function createCloudProfile() {
    const number = state.cloudProfiles.length + 1;
    let created = false;
    setBusy(true);
    try {
      const cloud = await request('POST', '/api/v1/settings/cloud', {
        action: 'create_profile', name: `新方案 ${number}`,
      });
      state.cloudConfigured = Boolean(cloud.api_key_configured);
      renderCloudSettings(cloud);
      created = true;
      setStatus('已新建模型方案，请填写后保存');
    } catch (error) { setStatus(error.message, true); }
    finally {
      setBusy(false);
      if (created) startProfileRename(elements.cloudProfileSelect.value);
    }
  }

  async function deleteCloudProfile(profileId = elements.cloudProfileSelect.value) {
    const active = state.cloudProfiles.find(profile => profile.id === profileId);
    if (!active || state.cloudProfiles.length <= 1) return;
    const confirmed = await confirmAction({
      title: '删除模型方案？',
      message: `将删除“${active.name}”及其连接和模型设置。其他方案不受影响。`,
      confirmLabel: '删除方案',
      destructive: true,
    });
    if (!confirmed) return;
    setBusy(true);
    try {
      const cloud = await request('POST', '/api/v1/settings/cloud', {
        action: 'delete_profile', profile_id: active.id,
      });
      state.cloudConfigured = Boolean(cloud.api_key_configured);
      renderCloudSettings(cloud);
      setStatus('模型方案已删除');
    } catch (error) { setStatus(error.message, true); }
    finally { setBusy(false); }
  }

  async function startProfileRename(profileId) {
    if (profileId !== elements.cloudProfileSelect.value) {
      await selectCloudProfile(profileId);
      if (profileId !== elements.cloudProfileSelect.value) return;
    }
    const card = [...elements.profileTabs.querySelectorAll('.profile-card')]
      .find(item => item.dataset.profileId === profileId);
    if (!card || card.classList.contains('editing')) return;
    const tab = card.querySelector('.profile-select');
    if (!tab) return;
    const originalName = card.dataset.profileName || tab.textContent || '未命名方案';
    const editor = document.createElement('input');
    editor.type = 'text';
    editor.className = 'profile-name-editor';
    editor.maxLength = 40;
    editor.value = originalName;
    editor.setAttribute('aria-label', '编辑方案名称');
    card.classList.add('editing');
    tab.replaceWith(editor);
    const finish = commit => {
      if (!card.classList.contains('editing')) return;
      const nextName = commit ? normalizeWhitespace(editor.value) : originalName;
      const restoredTab = document.createElement('button');
      restoredTab.type = 'button';
      restoredTab.className = 'profile-select';
      restoredTab.dataset.profileId = profileId;
      restoredTab.setAttribute('role', 'tab');
      restoredTab.setAttribute('aria-selected', String(profileId === elements.cloudProfileSelect.value));
      restoredTab.textContent = nextName || originalName;
      editor.replaceWith(restoredTab);
      card.dataset.profileName = nextName || originalName;
      card.classList.remove('editing');
      if (nextName && profileId === elements.cloudProfileSelect.value) {
        elements.cloudProfileName.value = nextName;
      }
      if (nextName && nextName !== originalName) {
        state.profileNameSave = state.profileNameSave.then(() => saveProfileName(profileId, nextName, originalName, card));
      }
    };
    editor.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); finish(true); }
      if (event.key === 'Escape') { event.preventDefault(); finish(false); }
    });
    editor.addEventListener('blur', () => finish(true));
    editor.focus({ preventScroll: true });
    editor.select();
  }

  async function saveProfileName(profileId, name, previousName, card) {
    try {
      const cloud = await request('POST', '/api/v1/settings/cloud', {
        action: 'rename_profile', profile_id: profileId, name,
      });
      const saved = (cloud.profiles || []).find(profile => profile.id === profileId);
      const savedName = saved?.name || name;
      const profile = state.cloudProfiles.find(item => item.id === profileId);
      if (profile) profile.name = savedName;
      const tab = card.querySelector('.profile-select');
      if (tab) {
        tab.textContent = savedName;
        tab.setAttribute('aria-label', `选择“${savedName}”`);
      }
      card.dataset.profileName = savedName;
      card.classList.add('just-saved');
      window.setTimeout(() => card.classList.remove('just-saved'), 1100);
      if (profileId === elements.cloudProfileSelect.value) elements.cloudProfileName.value = savedName;
    } catch (error) {
      const profile = state.cloudProfiles.find(item => item.id === profileId);
      if (profile) profile.name = previousName;
      const tab = card.querySelector('.profile-select');
      if (tab) tab.textContent = previousName;
      card.dataset.profileName = previousName;
      if (profileId === elements.cloudProfileSelect.value) elements.cloudProfileName.value = previousName;
      setStatus(error.message || '方案名称保存失败', true);
    }
  }

  function readCustomHeaders() {
    const headers = {};
    const lines = elements.customHeaders.value.split(/\r?\n/);
    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line) continue;
      const separator = line.indexOf(':');
      if (separator <= 0) throw new Error('自定义请求头请按“名称: 值”逐行填写');
      const name = line.slice(0, separator).trim();
      const value = line.slice(separator + 1).trim();
      if (!name || !value) throw new Error('自定义请求头请填写完整的名称和值');
      if (Object.prototype.hasOwnProperty.call(headers, name)) throw new Error(`重复的自定义请求头：${name}`);
      headers[name] = value;
    }
    return headers;
  }

  function addConnectionOptions(body) {
    const customHeaders = readCustomHeaders();
    if (elements.clearCustomHeaders.checked && Object.keys(customHeaders).length) {
      throw new Error('请填写新的自定义请求头，或勾选移除已保存的请求头，二者择一');
    }
    body.request_compatibility = elements.requestCompatibility.value || 'standard';
    if (Object.keys(customHeaders).length) body.extra_headers = customHeaders;
    if (elements.clearCustomHeaders.checked) body.clear_extra_headers = true;
    return body;
  }

  async function testCloudConnection() {
    setConnectionTestStatus('正在测试…');
    setBusy(true);
    try {
      const body = addConnectionOptions({
        protocol: elements.cloudProtocol.value,
        base_url: normalizeWhitespace(elements.cloudBaseUrl.value) || 'https://api.openai.com',
      });
      const key = elements.cloudApiKey.value.trim();
      if (key) body.api_key = key;
      const started = await request('POST', '/api/v1/settings/cloud/test/start', body, { timeoutMs: 5000 });
      const deadline = Date.now() + CONNECTION_TEST_TIMEOUT_MS;
      let result;
      while (Date.now() < deadline) {
        await delay(CONNECTION_TEST_POLL_INTERVAL_MS);
        const snapshot = await request('GET', `/api/v1/settings/cloud/test/${encodeURIComponent(started.test_id)}`, undefined, { timeoutMs: 5000 });
        if (snapshot.status === 'succeeded') {
          result = snapshot.result;
          break;
        }
        if (snapshot.status === 'failed') throw new Error(snapshot.message || '连接测试失败');
      }
      if (!result) throw new Error('连接测试未完成');
      setAvailableModels(elements.cloudProfileSelect.value, result.models);
      setConnectionTestStatus('✓ 可连接', 'success');
      setStatus('可连接');
    } catch (error) {
      setConnectionTestStatus('✕ 不可连接', 'error');
      setStatus('不可连接', true);
    }
    finally { setBusy(false); }
  }

  async function saveCloudSettings() {
    setBusy(true);
    try {
      await state.profileNameSave;
      const body = addConnectionOptions({
        action: 'save_profile',
        profile_id: elements.cloudProfileSelect.value,
        name: normalizeWhitespace(elements.cloudProfileName.value),
        protocol: elements.cloudProtocol.value,
        model: normalizeWhitespace(elements.cloudModel.value),
        routing_model: normalizeWhitespace(elements.routingModel.value),
        reasoning_effort: elements.classificationReasoningEffort.value,
        routing_reasoning_effort: elements.routingReasoningEffort.value,
        pipeline: { max_concurrent_requests: Number(elements.maxConcurrentRequests.value) },
        base_url: normalizeWhitespace(elements.cloudBaseUrl.value) || 'https://api.openai.com',
      });
      const key = elements.cloudApiKey.value.trim();
      if (key) body.api_key = key;
      const cloud = await request('POST', '/api/v1/settings/cloud', body);
      state.cloudConfigured = Boolean(cloud.api_key_configured);
      renderCloudSettings(cloud);
      closeSettings();
      setStatus('设置已保存，正在验证服务…');
      await connectService();
    } catch (error) { setStatus(error.message, true); }
    finally { setBusy(false); }
  }

  async function fetchAttributeDocument(question) {
    // 用户脚本处于浏览器扩展隔离环境时，same-origin 可能不能稳定携带网页登录态。
    // attributeUrl 已在采集时校验为题湖同源地址，因此可显式携带站点凭据；同时禁止复用旧属性响应。
    const response = await fetch(question.attributeUrl, { credentials: 'include', cache: 'no-store' });
    if (!response.ok) throw new Error(`题目 ${question.exerciseId} 属性接口返回 HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload || typeof payload.html !== 'string') throw new Error(`题目 ${question.exerciseId} 属性内容缺失`);
    const documentFragment = new DOMParser().parseFromString(payload.html, 'text/html');
    const form = documentFragment.querySelector('form.modify_form, .modify_form');
    if (!form) throw new Error(`题目 ${question.exerciseId} 属性表单缺失`);
    return { html: payload.html, form };
  }

  async function fetchAttribute(question) {
    const attribute = await fetchAttributeDocument(question);
    return {
      ...question,
      questionPress: readFieldFromHtml(attribute.html, 'question_press'),
      answerPress: readFieldFromHtml(attribute.html, 'answer_press'),
      questionText: readFieldFromHtml(attribute.html, 'question_text'),
      questionLatex: readFieldFromHtml(attribute.html, 'question_latex'),
      answerText: readFieldFromHtml(attribute.html, 'answer_text'),
      answerLatex: readFieldFromHtml(attribute.html, 'answer_latex'),
      currentCatalogueId: readFieldFromHtml(attribute.html, 'exercise_catalogue_id'),
    };
  }

  function collectCards() {
    return collectCardsFromDocument(document, location.href);
  }

  async function fetchPageDocument(pageUrl) {
    const response = await fetch(pageUrl, { credentials: 'include', cache: 'no-store' });
    if (!response.ok) throw new Error(`分页题目列表返回 HTTP ${response.status}`);
    return new DOMParser().parseFromString(await response.text(), 'text/html');
  }

  async function collectAllFocusPages({ purpose = 'classification' } = {}) {
    const firstPageUrl = new URL(location.href).href;
    const queued = [firstPageUrl];
    const scheduled = new Set([firstPageUrl]);
    const visited = new Set();
    const questionsById = new Map();
    const conflictingExerciseIds = new Set();
    const failedPageUrls = [];
    let pageSize = 0;
    const pageLimit = 200;
    while (queued.length) {
      const pageBatch = queued.splice(0, PAGE_FETCH_CONCURRENCY);
      setStatus(`正在并发读取当前目录范围分页：已完成 ${visited.size}/${scheduled.size} 页…`);
      const fetchedPages = await runPool(pageBatch, PAGE_FETCH_CONCURRENCY, async pageUrl => {
        const pageKey = new URL(pageUrl, location.href).href;
        return { pageKey, pageDocument: pageKey === firstPageUrl ? document : await fetchPageDocument(pageKey) };
      });
      for (const item of fetchedPages) {
        if (item.status !== 'fulfilled') {
          failedPageUrls.push(pageBatch[fetchedPages.indexOf(item)]);
          continue;
        }
        const { pageKey, pageDocument } = item.value;
        visited.add(pageKey);
        const pageCards = collectCardsFromDocument(pageDocument, pageKey);
        pageSize = Math.max(pageSize, pageCards.length);
        for (const question of pageCards) {
          if (conflictingExerciseIds.has(question.exerciseId)) continue;
          const existing = questionsById.get(question.exerciseId);
          if (existing && existing.attributeUrl !== question.attributeUrl) {
            questionsById.delete(question.exerciseId);
            conflictingExerciseIds.add(question.exerciseId);
            continue;
          }
          questionsById.set(question.exerciseId, existing || question);
        }
        for (const nextUrl of paginationUrlsFromDocument(pageDocument, pageKey)) {
          const nextKey = new URL(nextUrl, location.href).href;
          if (scheduled.has(nextKey)) continue;
          if (scheduled.size >= pageLimit) throw new Error(`分页数量超过 ${pageLimit}，为避免异常请求已停止`);
          scheduled.add(nextKey);
          queued.push(nextKey);
        }
      }
    }
    if (!questionsById.size) throw new Error('当前目录范围内没有可读取的题目');
    const allCards = [...questionsById.values()];
    const cards = allCards;
    const completionAction = purpose === 'export'
      ? '导出为题库交接包'
      : (purpose === 'restore' ? '从本地缓存恢复审核队列'
        : (purpose === 'acceptance' ? '汇总本地缓存以供全量采纳' : '提交云端分类'));
    setStatus(`已发现 ${visited.size}/${scheduled.size} 页、${allCards.length} 道去重题目（仅含列表摘要），正在并发补全题干、公式、答案与解析文本，随后将${completionAction}…`);
    const fetched = await runPool(cards, ATTRIBUTE_CONCURRENCY, fetchAttribute);
    const questions = [];
    const cardById = new Map();
    const failedExerciseIds = [...conflictingExerciseIds];
    for (const [index, item] of fetched.entries()) {
      if (item.status !== 'fulfilled') {
        failedExerciseIds.push(cards[index].exerciseId);
        continue;
      }
      questions.push(item.value);
      if (item.value.card) cardById.set(item.value.exerciseId, item.value.card);
    }
    if (!questions.length) throw new Error('当前目录范围内题目的属性文本均无法读取');
    return {
      questions, cardById, pageCount: visited.size, pageSize, attributeFailed: failedExerciseIds.length,
      collection: {
        discovered_question_count: allCards.length + conflictingExerciseIds.size,
        collected_question_count: questions.length,
        failed_exercise_ids: [...new Set(failedExerciseIds)].sort(),
        failed_page_urls: failedPageUrls,
        sampling: { mode: 'full', source_question_count: allCards.length + conflictingExerciseIds.size },
      },
    };
  }

  function pageCatalogueTree() {
    const pageWindow = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
    return Array.isArray(pageWindow.data) ? pageWindow.data : [];
  }

  function withAcceptanceState(result) {
    const question = state.cards.get(result.exercise_id);
    if (!question?.currentCatalogueId || !canAcceptClassification(result)) return result;
    try {
      const target = resolveCataloguePath(pageCatalogueTree(), result.target.path);
      const completionState = completionStateForTarget(
        result, question.currentCatalogueId, target.id, question.catalogueId,
      );
      return {
        ...result,
        acceptance_mode: completionState
          ? 'local'
          : acceptanceModeForCatalogueIds(question.currentCatalogueId, target.id),
        website_catalogue_id: target.id,
        accepted: Boolean(result.accepted || completionState),
        completion_state: completionState || result.completion_state || '',
      };
    } catch (error) {
      return { ...result, acceptance_mapping_error: error.message };
    }
  }

  const DIRECTORY_LEVEL_LABELS = ['专题', '二级目录', '三级目录', '四级目录'];

  function directoryNodeForPath(path) {
    let nodes = pageCatalogueTree();
    let matched = null;
    for (const segment of (Array.isArray(path) ? path : [])) {
      const candidates = (Array.isArray(nodes) ? nodes : [])
        .filter(node => directoryKey(node?.name) === directoryKey(segment));
      if (candidates.length !== 1) return null;
      [matched] = candidates;
      nodes = Array.isArray(matched.child) ? matched.child : [];
    }
    return matched;
  }

  function currentNavigationCataloguePath() {
    const selected = document.querySelector('.list-group .node-tree.node-selected');
    const tree = selected?.closest('.list-group');
    if (!selected || !tree) return [];
    const nodes = [...tree.querySelectorAll('li.node-tree')];
    const selectedIndex = nodes.indexOf(selected);
    if (selectedIndex < 0) return [];
    return navigationPathFromTreeRows(nodes.map(node => ({
      name: node.querySelector(':scope > a')?.textContent || '',
      depth: node.querySelectorAll(':scope > .indent').length,
    })), selectedIndex);
  }

  function focusScopeForNavigationPath(taxonomy, rawNavigationPath) {
    const navigationPath = (Array.isArray(rawNavigationPath) ? rawNavigationPath : [])
      .map(normalizeWhitespace).filter(Boolean);
    // 只选中“专题”本身时范围过大；二、三、四级及其同层知识点目录均可作为 Focus。
    if (navigationPath.length < 2) return null;
    const topic = (taxonomy?.topics || []).find(item => directoryKey(item?.title) === directoryKey(navigationPath[0]));
    if (!topic) return null;

    // 题湖也有“2.2 / 考点3 / 考法1”这类旧知识点树：它们是有效的当前范围，
    // 不能因名称未出现在工作簿目标目录中而被误判为“没有选中目录”。
    const level2 = (topic.level2 || []).find(item => directoryKey(item?.title) === directoryKey(navigationPath[1]));
    const level2Path = level2 ? [topic.title, level2.title].map(normalizeWhitespace) : [];
    if (level2 && sameDirectoryPath(navigationPath, level2Path)) {
      return { level: 2, topic_id: topic.id, level2_id: level2.id, level3_id: null, level4_id: null, title: navigationPath.join(' / ') };
    }
    const level3 = level2 && (level2.level3 || []).find(item => directoryKey(item?.title) === directoryKey(navigationPath[2]));
    const level3Path = level3 ? [...level2Path, normalizeWhitespace(level3.title)] : [];
    if (level3 && sameDirectoryPath(navigationPath, level3Path)) {
      return { level: 3, topic_id: topic.id, level2_id: level2.id, level3_id: level3.id, level4_id: null, title: navigationPath.join(' / ') };
    }
    const level4 = level3 && (level3.level4 || []).find(item => directoryKey(item?.title) === directoryKey(navigationPath[3]));
    const level4Path = level4 ? [...level3Path, normalizeWhitespace(level4.title)] : [];
    if (level4 && sameDirectoryPath(navigationPath, level4Path)) {
      return { level: 4, topic_id: topic.id, level2_id: level2.id, level3_id: level3.id, level4_id: level4.id, title: navigationPath.join(' / ') };
    }
    return {
      level: Math.min(4, navigationPath.length),
      topic_id: topic.id,
      // 仅当前路径明确落在目标二级目录时才施加该约束，否则由云端在专题内选择。
      level2_id: level2?.id || '', level3_id: null, level4_id: null, title: navigationPath.join(' / '),
    };
  }

  function focusedDirectoryScope(taxonomy) {
    return focusScopeForNavigationPath(taxonomy, currentNavigationCataloguePath());
  }

  function loadFocusSnapshot() {
    try {
      const raw = localStorage.getItem(FOCUS_SNAPSHOT_STORAGE_KEY);
      if (!raw) return null;
      const snapshot = JSON.parse(raw);
      if (!snapshot || snapshot.version !== 1) {
        localStorage.removeItem(FOCUS_SNAPSHOT_STORAGE_KEY);
        return null;
      }
      return snapshot;
    } catch {
      return null;
    }
  }

  function saveFocusSnapshot(snapshot) {
    state.focusSnapshot = snapshot;
    state.pendingFocusRestore = false;
    updateFocusRestoreAction();
    try { localStorage.setItem(FOCUS_SNAPSHOT_STORAGE_KEY, JSON.stringify(snapshot)); }
    catch { /* 浏览器限制本地存储时，当前页面队列仍可正常使用。 */ }
  }

  function clearFocusSnapshot() {
    state.focusSnapshot = null;
    state.pendingFocusRestore = false;
    updateFocusRestoreAction();
    try { localStorage.removeItem(FOCUS_SNAPSHOT_STORAGE_KEY); }
    catch { /* 本地存储不可用时无需处理。 */ }
  }

  function updateFocusSnapshotPage(page) {
    if (!state.focusSnapshot) return;
    saveFocusSnapshot({ ...state.focusSnapshot, priority_page: Math.max(1, Number.parseInt(page, 10) || 1) });
  }

  function createFocusSnapshot(scope, pageSize) {
    return {
      version: 1,
      pathname: location.pathname,
      scope: {
        level: scope.level,
        topic_id: scope.topic_id,
        level2_id: scope.level2_id,
        level3_id: scope.level3_id || null,
        level4_id: scope.level4_id || null,
      },
      page_size: Math.max(1, Number.parseInt(pageSize, 10) || 1),
      priority_page: 1,
      saved_at: new Date().toISOString(),
    };
  }

  function renderDirectoryRefactorPlan(plan, focus) {
    const counts = new Map((plan.audit?.level4_counts || []).map(item => [
      `${item.level3_key}\u0000${item.level4_key}`, item,
    ]));
    const collection = plan.collection || {};
    const excelScope = plan.excel_scope || null;
    const gapCount = (collection.failed_exercise_ids || []).length + (collection.failed_page_urls || []).length + (collection.failed_signal_exercise_ids || []).length;
    const sampling = collection.sampling || {};
    const samplingText = sampling.mode === 'stratified_page' ? `；按分页分层抽样 ${plan.audit?.question_count || 0}/${sampling.source_question_count || 0} 道题` : '';
    const scopeText = excelScope ? `；Excel 将保留第 ${excelScope.container_row} 行锚点，仅作用于 ${excelScope.sheet}!${excelScope.replace_start_row}–${excelScope.replace_end_row} 行` : '';
    const sampled = sampling.mode === 'stratified_page';
    const evidenceRule = sampled
      ? `样本中至少 ${plan.sampled_level4_candidate_min_count || 3} 道匹配题才提出目录；${plan.sampled_level4_strong_candidate_min_count || 4} 道及以上标为强候选。抽样不证明实际题量，写入 Excel 前仍须全量核验不少于 ${plan.minimum_level4_question_count} 道`
      : `每个四级目录至少有 ${plan.minimum_level4_question_count} 道匹配题目，方案仅列出 ${plan.minimum_level4_question_count} 个题号作题量核验`;
    elements.directoryPlanSummary.textContent = `${focus.title}：已阅读 ${plan.audit?.question_count || 0} 道题${samplingText}；${evidenceRule}。此处仅生成目录骨架，不进行全量逐题归类${gapCount ? `；另有 ${gapCount} 项采集缺口，方案仅代表已成功读取的题目` : ''}${scopeText}。方案仍需人工审核后才能写入 Excel。`;
    elements.directoryPlanList.replaceChildren();
    for (const level3 of (plan.level3 || [])) {
      const item = document.createElement('li');
      const children = (level3.level4 || []).map(level4 => {
        const evidence = counts.get(`${level3.key}\u0000${level4.key}`) || {};
        const label = sampled ? (evidence.evidence_tier === 'strong_candidate' ? '强候选' : '候选') : '已核验';
        return `${level4.title}（${label}：${evidence.count || 0} 道匹配题目；依据：${level4.basis || '待审核'}）`;
      });
      item.textContent = children.length ? `${level3.title}（依据：${level3.basis || '待审核'}）：${children.join('；')}` : `${level3.title}（依据：${level3.basis || '待审核'}）：三级末级目录`;
      elements.directoryPlanList.append(item);
    }
    elements.directoryPlan.hidden = false;
  }

  function exportDirectoryRefactorPlan() {
    if (!state.directoryPlan) return;
    const payload = {
      ...state.directoryPlan,
      status: 'review',
      export_note: '请完成旧—新映射、Excel 行范围锚定和人工审核后，才可将 status 改为 approved。',
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `目录重构审核方案-${chinaDate()}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
    setStatus('已导出待审核目录方案；该文件尚不能直接写入 Excel。');
  }

  async function exportAllFocusQuestions() {
    if (state.busy) return;
    const focus = focusedDirectoryScope(state.taxonomy);
    if (!focus || focus.level === 4) {
      setStatus('请先在题湖左侧目录树选中当前专题内的二级或三级目录。', true);
      return;
    }
    setBusy(true);
    try {
      const collected = await collectAllFocusPages({ purpose: 'export' });
      const questions = collected.questions.map(question => classificationPayload(question, {
        topic_id: focus.topic_id, level2_id: focus.level2_id,
      }));
      state.currentQuestions = questions;
      for (const [exerciseId, card] of collected.cardById) state.cards.set(exerciseId, card);
      setStatus(`已读取 ${collected.pageCount} 页、${questions.length} 道题，正在保存本地题库交接包…`);
      const exported = await request('POST', '/api/v1/directory-exports', {
        focus: { level: focus.level, topic_id: focus.topic_id, level2_id: focus.level2_id, level3_id: focus.level3_id, level4_id: focus.level4_id },
        questions,
        collection: collected.collection,
      }, { timeoutMs: 0 });
      setStatus(`已导出 ${exported.question_count} 道题到 ${exported.export_dir}。`);
    } catch (error) {
      setStatus(`题库导出失败：${error.message}`, true);
    } finally {
      setBusy(false);
    }
  }

  function existingPathPrefix(path) {
    const selected = [];
    let nodes = pageCatalogueTree();
    for (const segment of (Array.isArray(path) ? path : [])) {
      const candidates = (Array.isArray(nodes) ? nodes : [])
        .filter(node => directoryKey(node?.name) === directoryKey(segment));
      if (candidates.length !== 1) break;
      const [matched] = candidates;
      selected.push(normalizeWhitespace(matched.name));
      nodes = Array.isArray(matched.child) ? matched.child : [];
    }
    return selected;
  }

  function queueManualSelectionSave(exerciseId, question, result) {
    const previous = state.manualSaveChains.get(exerciseId) || Promise.resolve();
    const payload = manualSelectionPayload(exerciseId, question, result);
    // 同题连续修改时严格按选择顺序写入，避免较早的网络请求晚到后覆盖最新目录。
    const write = previous.catch(() => undefined).then(() => request('POST', '/api/v1/manual-classifications', payload));
    state.manualSaveChains.set(exerciseId, write);
    return write.finally(() => {
      if (state.manualSaveChains.get(exerciseId) === write) state.manualSaveChains.delete(exerciseId);
    });
  }

  async function flushManualSelectionSaves() {
    const writes = [...state.manualSaveChains.values()];
    if (!writes.length) return;
    setStatus(`正在保存 ${writes.length} 道人工选择的目录…`);
    const outcomes = await Promise.allSettled(writes);
    const failed = outcomes.find(item => item.status === 'rejected');
    if (failed) throw new Error(`人工选择尚未保存：${failed.reason?.message || '本地服务请求失败'}`);
  }

  async function applyManualTarget(exerciseId, path) {
    const result = state.results.get(exerciseId);
    const question = state.cards.get(exerciseId);
    if (!result || !question?.card) return;
    const normalizedPath = path.map(normalizeWhitespace).filter(Boolean);
    let target;
    try {
      target = resolveCataloguePath(pageCatalogueTree(), normalizedPath);
    } catch (error) {
      setStatus(`题目 ${exerciseId} 的人工分类无效：${error.message}`, true);
      return;
    }
    const originalTargetPath = result.accepted
      ? (result.target?.path || [])
      : (result.manual_override?.original_target_path || result.target?.path || []);
    const manualResult = {
      ...result,
      accepted: false,
      status: 'suggested',
      confidence: 1,
      reason: '人工选择的题湖现有目录',
      review_reasons: [],
      target: { path: normalizedPath },
      acceptance_mode: acceptanceModeForCatalogueIds(question.currentCatalogueId, target.id),
      manual_override: {
        source: 'manual',
        original_target_path: originalTargetPath,
        pending: true,
      },
    };
    state.results.set(exerciseId, manualResult);
    renderBadge(question.card, manualResult);
    updateLegend();
    setStatus(`正在保存题目 ${exerciseId} 的人工分类…`);
    try {
      await queueManualSelectionSave(exerciseId, question, manualResult);
      // 用户可能在保存期间再次修改了该题；只为仍是本次选择的结果更新提示。
      if (state.results.get(exerciseId) === manualResult) {
        setStatus(`题目 ${exerciseId} 的人工分类已保存，点击“采纳”写入题湖。`);
      }
    } catch (error) {
      if (state.results.get(exerciseId) === manualResult) {
        setStatus(`题目 ${exerciseId} 的人工分类尚未保存：${error.message}`, true);
      }
    }
  }

  function openManualEditor(exerciseId, badge) {
    if (badge.querySelector('.badge-editor')) return;
    const result = state.results.get(exerciseId);
    if (!result) return;
    const tree = pageCatalogueTree();
    if (!tree.length) {
      setStatus('题湖可视化目录尚未加载，无法修改分类', true);
      return;
    }
    let selectedPath = existingPathPrefix(result.target?.path || []);
    const editor = document.createElement('div');
    editor.className = 'badge-editor';
    const fields = document.createElement('div');
    fields.className = 'badge-editor-fields';
    const hint = document.createElement('small');
    hint.className = 'badge-editor-hint';
    const actions = document.createElement('div');
    actions.className = 'badge-editor-actions';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.textContent = '取消';
    cancel.addEventListener('click', () => editor.remove());
    actions.append(cancel);
    editor.append(fields, hint, actions);

    const renderFields = () => {
      fields.replaceChildren();
      let nodes = tree;
      for (let depth = 0; depth < DIRECTORY_LEVEL_LABELS.length && nodes.length; depth += 1) {
        const label = document.createElement('label');
        label.textContent = DIRECTORY_LEVEL_LABELS[depth];
        const select = document.createElement('select');
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = `选择${DIRECTORY_LEVEL_LABELS[depth]}`;
        select.append(placeholder);
        for (const node of nodes) {
          const option = document.createElement('option');
          option.value = normalizeWhitespace(node.name);
          option.textContent = normalizeWhitespace(node.name);
          select.append(option);
        }
        select.value = selectedPath[depth] || '';
        select.addEventListener('change', async () => {
          selectedPath = selectedPath.slice(0, depth);
          if (select.value) selectedPath.push(select.value);
          const selectedNode = directoryNodeForPath(selectedPath);
          const children = Array.isArray(selectedNode?.child) ? selectedNode.child : [];
          if (selectedNode && !children.length) {
            await applyManualTarget(exerciseId, selectedPath);
            return;
          }
          renderFields();
        });
        label.append(select);
        fields.append(label);
        const selectedNode = directoryNodeForPath(selectedPath.slice(0, depth + 1));
        if (!selectedNode || !select.value) break;
        nodes = Array.isArray(selectedNode.child) ? selectedNode.child : [];
      }
      const selectedNode = directoryNodeForPath(selectedPath);
      const children = Array.isArray(selectedNode?.child) ? selectedNode.child : [];
      hint.textContent = selectedNode && !children.length
        ? '已选择可采纳的目录。'
        : '请选择至最末级目录；无四级目录时，三级目录可直接采纳。';
    };
    renderFields();
    badge.append(editor);
  }

  function renderBadge(card, result) {
    card.querySelector(`.${BADGE_CLASS}`)?.remove();
    card.classList.remove('wulou-curation-linked', 'wulou-curation-linked-review', 'wulou-curation-linked-accepted');
    card.classList.add('wulou-curation-linked');
    if (result.status === 'review') card.classList.add('wulou-curation-linked-review');
    if (result.accepted) card.classList.add('wulou-curation-linked-accepted');

    const badge = document.createElement('div');
    badge.className = `${BADGE_CLASS} ${result.status === 'review' ? 'is-review' : 'is-suggested'}`;
    if (result.accepted) badge.classList.add('is-accepted');
    const target = result.target?.path?.join(' / ');
    const cardPosition = [...document.querySelectorAll(CARD_SELECTOR)].indexOf(card) + 1;
    const questionLabel = cardPosition > 0 ? `第 ${cardPosition} 题` : '本题';
    badge.setAttribute('role', 'group');
    badge.setAttribute('aria-label', `${questionLabel}的分类建议`);

    const header = document.createElement('div');
    header.className = 'badge-header';
    const anchor = document.createElement('span');
    anchor.className = 'badge-anchor';
    anchor.textContent = questionLabel;
    const title = document.createElement('strong');
    const isManual = result.manual_override?.source === 'manual';
    const isLocalAcceptance = result.acceptance_mode === 'local';
    const completionState = result.completion_state || '';
    title.textContent = result.accepted
      ? (completionState === 'moved' ? '已移动' : (completionState === 'directory_consistent' ? '目录已一致' : (completionState === 'at_target' ? '已归位' : (isLocalAcceptance ? '已本地采纳' : (isManual ? '人工已采纳' : '已采纳')))))
      : (isLocalAcceptance ? '当前目录无需调整' : (isManual ? '人工修改' : (result.status === 'review' ? '待复核' : '建议分类')));
    header.append(anchor, title);

    const path = document.createElement('button');
    path.type = 'button';
    path.className = 'badge-path badge-path-edit';
    path.textContent = target || result.reason;
    path.title = '修改分类';
    path.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      openManualEditor(result.exercise_id, badge);
    });
    const routedTopic = result.routing?.routed_topic_title || result.routing?.latest_topic_title;
    const reviewReasonText = reviewReasonLabels(result.review_reasons).join(' · ');
    const meta = document.createElement('small');
    meta.className = 'badge-meta';
    meta.textContent = result.accepted
      ? (completionState === 'moved'
        ? `已移动至当前目录${result.catalogue_move?.moved_at ? ` · ${formatChinaTime(result.catalogue_move.moved_at)}` : ''}`
        : (completionState === 'directory_consistent' ? '当前页面目录已与建议一致，无需重复移动' : (completionState === 'at_target' ? '当前题湖目录已是建议目标，无需重复处理' : (isLocalAcceptance ? '已在本地确认，未发送题湖移动请求' : '已写入题湖可视化分类'))))
      : isLocalAcceptance
      ? '建议目录与当前目录一致；采纳仅在本地确认，不会发送题湖移动请求'
      : isManual
      ? '人工选择 · 采纳后将提交题湖移动请求'
      : result.status === 'review'
      ? `${reviewReasonText || '需要人工复核'} · 采纳后将提交题湖移动请求`
      : `${routedTopic ? `路由专题：${routedTopic} · ` : ''}置信度 ${Math.round(result.confidence * 100)}% · 采纳后将提交题湖移动请求`;
    badge.append(header, path, meta);
    if (state.workset === 'focus') appendAnswerPreview(badge, header, state.cards.get(result.exercise_id));

    if (canAcceptClassification(result)) {
      const action = document.createElement('button');
      action.className = 'badge-action';
      action.type = 'button';
      action.disabled = Boolean(result.accepted || state.acceptingAll || state.accepting.has(result.exercise_id));
      action.textContent = result.accepted
        ? (completionState === 'directory_consistent' ? '目录已一致' : '已采纳')
        : state.accepting.has(result.exercise_id)
        ? (isLocalAcceptance ? '确认中…' : '提交中…')
        : (isLocalAcceptance ? '采纳' : '采纳并移动');
      action.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        acceptSuggestion(result.exercise_id);
      });
      badge.append(action);
    }
    badge.title = result.reason || '';
    card.prepend(badge);
  }

  async function acceptSuggestion(exerciseId, { announce = true } = {}) {
    if (state.acceptingAll && announce) {
      const message = '正在执行当前页全部采纳，请等待本批次完成。';
      setStatus(message);
      return { exerciseId, accepted: false, skipped: true, message };
    }
    if (state.accepting.has(exerciseId)) return { exerciseId, accepted: false, skipped: true };
    const result = state.results.get(exerciseId);
    const question = state.cards.get(exerciseId);
    if (!canAcceptClassification(result)) {
      const message = `题目 ${exerciseId} 没有可采纳的完整建议路径`;
      if (announce) setStatus(message, true);
      return { exerciseId, accepted: false, skipped: true, message };
    }
    if (!question) {
      const message = `题目 ${exerciseId} 缺少当前页面题卡信息`;
      if (announce) setStatus(message, true);
      return { exerciseId, accepted: false, message };
    }

    const isManual = result.manual_override?.source === 'manual';
    state.accepting.add(exerciseId);
    if (question.card) renderBadge(question.card, result);
    try {
      const target = resolveCataloguePath(pageCatalogueTree(), result.target.path);
      const acceptanceMode = result.acceptance_mode
        || acceptanceModeForCatalogueIds(question.currentCatalogueId, target.id);
      if (acceptanceMode === 'local') {
        // 当前目录已在分类阶段读取并与建议目录比对一致；无需请求题湖，
        // 但人工选择仍必须持久化，避免刷新或进入其他目录范围后被模型缓存覆盖。
        let manualPersistenceError = '';
        if (isManual) {
          try {
            await request('POST', '/api/v1/manual-classifications', {
              exercise_id: exerciseId,
              current_catalogue_id: question.currentCatalogueId,
              stable_code: question.stableCode || '',
              original_target_path: result.manual_override?.original_target_path || [],
              target_path: result.target.path,
            });
          } catch (error) {
            // 当前题湖目录本身不需要修改；人工记录失败仍应如实提示，不能静默丢失决定。
            manualPersistenceError = error.message;
          }
        }
        state.cards.set(exerciseId, { ...question, currentCatalogueId: target.id });
        state.results.set(exerciseId, {
          ...result,
          accepted: true,
          acceptance_mode: 'local',
          website_catalogue_id: target.id,
          acceptance_mapping_error: '',
          manual_override: isManual
            ? { ...result.manual_override, pending: false, accepted_at: new Date().toISOString() }
            : result.manual_override,
        });
        const warnings = manualPersistenceError ? [`人工修正记录保存失败：${manualPersistenceError}`] : [];
        if (announce) {
          setStatus(
            warnings.length
              ? `题目 ${exerciseId} 已本地采纳，但${warnings.join('；')}`
              : `题目 ${exerciseId} 已本地采纳；建议目录与当前目录一致，未发送题湖移动请求。`,
            Boolean(warnings.length),
          );
        }
        return { exerciseId, accepted: true, local: true, moved: false, warnings };
      }
      if (!question.attributeUrl) throw new Error('缺少属性接口地址');
      const attribute = await fetchAttributeDocument(question);
      const exerciseField = attribute.form.querySelector('[name="exercise_id"]');
      const catalogueField = attribute.form.querySelector('[name="exercise_catalogue_id"]');
      if (!exerciseField || String(exerciseField.value) !== String(exerciseId)) {
        throw new Error('属性表单题目 ID 与当前题卡不一致');
      }
      if (!catalogueField) throw new Error('属性表单缺少可视化分类字段');
      const sourceCatalogueId = String(catalogueField.value || '').trim();
      if (!sourceCatalogueId) throw new Error('属性表单缺少当前目录值');
      const moved = sourceCatalogueId !== String(target.id);
      // 工作成果描述的是人工从当前页面导航目录执行移动的过程，不使用题目内部目录 ID 反查名称。
      const originalPath = moved ? currentNavigationCataloguePath() : [];

      if (moved) {
        catalogueField.value = target.id;
        const formPayload = buildCatalogueMovePayload(attribute.form, target.id);
        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
        const headers = {
          Accept: 'application/json',
          'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
          'X-Requested-With': 'XMLHttpRequest',
        };
        if (csrfToken) headers['X-CSRF-TOKEN'] = csrfToken;
        const response = await fetch(sameOriginUrl(MODIFY_ENDPOINT), {
          method: 'POST', credentials: 'include', cache: 'no-store', headers,
          body: formPayload.toString(),
        });
        const responseText = await response.text();
        let payload;
        try { payload = JSON.parse(responseText); }
        catch (error) { throw new Error(`题湖提交接口未返回 JSON：${error.message}`); }
        if (!response.ok || Number(payload?.status) !== 200) {
          throw new Error(normalizeWhitespace(payload?.text) || `题湖提交失败（HTTP ${response.status}）`);
        }
      }
      let manualPersistenceError = '';
      let historyPersistenceError = '';
      if (isManual) {
        try {
          await request('POST', '/api/v1/manual-classifications', {
            exercise_id: exerciseId,
            current_catalogue_id: sourceCatalogueId,
            stable_code: question.stableCode || '',
            original_target_path: result.manual_override?.original_target_path || [],
            target_path: result.target.path,
          });
        } catch (error) {
          // 题湖已返回修改成功；本地审计失败不能把真实提交误报为失败。
          manualPersistenceError = error.message;
        }
      }
      if (moved) {
        try {
          await request('POST', '/api/v1/history/catalogue-moves', {
            exercise_id: exerciseId,
            stable_code: question.stableCode || '',
            source_catalogue_id: sourceCatalogueId,
            target_catalogue_id: String(target.id),
            original_path: originalPath,
            target_path: result.target.path,
          });
        } catch (error) {
          // 题湖已返回修改成功，历史记录失败必须明确告知，但不能把真实移动误报为失败。
          historyPersistenceError = error.message;
        }
      }
      state.cards.set(exerciseId, { ...question, currentCatalogueId: target.id });
      state.results.set(exerciseId, {
        ...result,
        accepted: true,
        acceptance_mode: 'move',
        website_catalogue_id: target.id,
        acceptance_mapping_error: '',
        manual_override: isManual
          ? { ...result.manual_override, pending: false, accepted_at: new Date().toISOString() }
          : result.manual_override,
      });
      const warnings = [
        manualPersistenceError && `人工修正记录保存失败：${manualPersistenceError}`,
        historyPersistenceError && `工作成果记录保存失败：${historyPersistenceError}`,
      ].filter(Boolean);
      if (warnings.length) {
        if (announce) setStatus(`题目 ${exerciseId} 已写入题湖，但${warnings.join('；')}`, true);
      } else if (announce) {
        setStatus(`题目 ${exerciseId} 已采纳建议并写入题湖可视化分类。`);
      }
      return { exerciseId, accepted: true, moved, warnings };
    } catch (error) {
      const message = `题目 ${exerciseId} 采纳失败：${error.message}`;
      if (announce) setStatus(message, true);
      return { exerciseId, accepted: false, message };
    } finally {
      state.accepting.delete(exerciseId);
      const current = state.results.get(exerciseId);
      if (current && question.card) renderBadge(question.card, current);
      updateLegend();
    }
  }

  function installBadgeStyles() {
    if (document.getElementById('wulou-curation-badge-style')) return;
    const style = document.createElement('style');
    style.id = 'wulou-curation-badge-style';
    style.textContent = `
      ${CARD_SELECTOR} { position: relative; }
      ${CARD_SELECTOR}.wulou-curation-linked { outline: 1px solid #badbce; outline-offset: -1px; }
      ${CARD_SELECTOR}.wulou-curation-linked-review { outline-color: #ecd2a5; }
      ${CARD_SELECTOR}.wulou-curation-linked-accepted { outline-color: #bbdfc5; }
      .${BADGE_CLASS} { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "header action" "path path" "meta meta"; gap: 5px 12px; margin: 10px 10px 4px; padding: 10px 12px 10px 16px; border: 1px solid #badbce; border-radius: 10px; background: #f0f7f4; color: #27463e; font: 12px/1.45 "Segoe UI", "Microsoft YaHei", sans-serif; }
      .${BADGE_CLASS}::before { content: ""; position: absolute; left: 21px; bottom: -10px; width: 2px; height: 10px; border-radius: 2px; background: #126b5c; pointer-events: none; }
      .${BADGE_CLASS}.is-review { border-color: #ecd2a5; background: #fff8ec; color: #6c4b1d; }
      .${BADGE_CLASS}.is-review::before { background: #b87520; }
      .${BADGE_CLASS}.is-accepted { border-color: #bbdfc5; background: #eef8f1; color: #245d3c; }
      .${BADGE_CLASS}.is-accepted::before { background: #2f7d4f; }
      .${BADGE_CLASS} .badge-header { grid-area: header; display: flex; min-width: 0; align-items: center; gap: 7px; }
      .${BADGE_CLASS} .badge-anchor { display: inline-flex; flex: 0 0 auto; align-items: center; min-height: 21px; padding: 2px 7px; border: 1px solid currentColor; border-radius: 999px; font-size: 11px; font-weight: 700; line-height: 1; }
      .${BADGE_CLASS} strong { min-width: 0; font-size: 13px; }
      .${BADGE_CLASS} .badge-path { grid-area: path; overflow-wrap: anywhere; color: #36564d; font-weight: 600; }
      .${BADGE_CLASS} .badge-path-edit { width: fit-content; max-width: 100%; min-height: 0; padding: 0; border: 0; border-radius: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
      .${BADGE_CLASS} .badge-path-edit:hover { background: transparent; color: #126b5c; text-decoration: underline; text-underline-offset: 3px; }
      .${BADGE_CLASS} .badge-meta { grid-area: meta; color: #6a7c76; }
      .${BADGE_CLASS} .badge-editor { grid-column: 1 / -1; display: grid; gap: 7px; margin-top: 3px; padding: 9px; border: 1px solid rgb(18 107 92 / .26); border-radius: 8px; background: rgb(255 255 255 / .68); }
      .${BADGE_CLASS} .badge-editor-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }
      .${BADGE_CLASS} .badge-editor label { display: grid; gap: 3px; color: #526660; font-size: 11px; font-weight: 650; }
      .${BADGE_CLASS} .badge-editor select { width: 100%; min-height: 30px; border: 1px solid #c8d9d2; border-radius: 6px; background: #fff; color: #27463e; padding: 4px 6px; font: inherit; }
      .${BADGE_CLASS} .badge-editor-hint { color: #687a74; font-size: 11px; }
      .${BADGE_CLASS} .badge-editor-actions { display: flex; justify-content: flex-end; }
      .${BADGE_CLASS} .badge-editor-actions button { min-height: 28px; padding: 4px 9px; border: 1px solid #c8d9d2; border-radius: 6px; background: #fff; color: #39534c; font: 600 11px/1.2 "Segoe UI", "Microsoft YaHei", sans-serif; cursor: pointer; }
      .${BADGE_CLASS} .badge-editor-actions button:hover { border-color: #8ba99f; background: #f2f7f5; }
      .${BADGE_CLASS} .badge-answer-toggle { flex: 0 0 auto; min-height: 22px; margin-left: 2px; padding: 2px 6px; border: 1px solid #82a79a; border-radius: 6px; background: #f4fbf8; color: #245d4a; font: 650 11px/1.2 "Segoe UI", "Microsoft YaHei", sans-serif; white-space: nowrap; cursor: pointer; }
      .${BADGE_CLASS} .badge-answer-toggle:hover { border-color: #397b68; background: #e7f4ee; }
      .${BADGE_CLASS} .badge-answer-content { display: grid; gap: 5px; margin-top: 7px; padding: 7px 8px; border-radius: 7px; background: rgb(255 255 255 / .62); color: #496059; font-size: 11px; overflow-wrap: anywhere; }
      .${BADGE_CLASS} .badge-answer-content { grid-column: 1 / -1; }
      .${BADGE_CLASS} .badge-answer-content[hidden] { display: none; }
      .${BADGE_CLASS} .badge-answer-content p, .${BADGE_CLASS} .badge-answer-content pre { margin: 0; }
      .${BADGE_CLASS} .badge-answer-content pre { white-space: pre-wrap; font: inherit; }
      .${BADGE_CLASS} .badge-answer-content img { max-width: 100%; max-height: 420px; border: 1px solid #d5e1dc; border-radius: 5px; object-fit: contain; }
      .${BADGE_CLASS}.is-review .badge-path { color: #6c4b1d; }
      .${BADGE_CLASS}.is-review .badge-meta { color: #8a6a37; }
      .${BADGE_CLASS}.is-accepted .badge-path { color: #245d3c; }
      .${BADGE_CLASS} .badge-action { grid-area: action; align-self: start; min-height: 30px; padding: 5px 10px; border: 1px solid #126b5c; border-radius: 8px; background: #126b5c; color: #fff; font: 600 12px/1.2 "Segoe UI", "Microsoft YaHei", sans-serif; white-space: nowrap; cursor: pointer; }
      .${BADGE_CLASS} .badge-action:hover { background: #0c5649; border-color: #0c5649; }
      .${BADGE_CLASS} .badge-action:disabled { border-color: #a9bbb5; background: #a9bbb5; cursor: default; }
      .wulou-review-priority-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 14px 0; padding: 11px 13px; border: 1px solid #ecd2a5; border-radius: 10px; background: #fff8ec; color: #6c4b1d; font: 13px/1.45 "Segoe UI", "Microsoft YaHei", sans-serif; }
      .wulou-review-priority-controls strong { margin-right: auto; }
      .wulou-review-priority-controls small { flex-basis: 100%; color: #8a6a37; }
      .wulou-review-priority-controls button { min-height: 30px; padding: 4px 9px; border: 1px solid #c89855; border-radius: 7px; background: #fff; color: #6c4b1d; font: inherit; cursor: pointer; }
      .wulou-review-priority-controls button:disabled { opacity: .48; cursor: default; }
      @media (max-width: 520px) {
        .${BADGE_CLASS} { grid-template-columns: 1fr; grid-template-areas: "header" "action" "path" "meta"; gap: 6px; }
        .${BADGE_CLASS} .badge-action { justify-self: start; }
        .${BADGE_CLASS} .badge-editor-fields { grid-template-columns: 1fr; }
      }
      @media (prefers-reduced-motion: reduce) { .${BADGE_CLASS} { scroll-margin-top: 8px; } }
    `;
    document.head.append(style);
  }

  function updateLegend() {
    const results = [...state.results.values()];
    const accepted = results.filter(result => result.accepted).length;
    const suggested = results.filter(result => result.status === 'suggested' && !result.accepted).length;
    const review = results.filter(result => result.status !== 'suggested').length;
    elements.legend.hidden = !results.length;
    elements.legendList.replaceChildren();
    for (const [label, count, type] of [['已采纳', accepted, ''], ['建议分类', suggested, ''], ['待人工复核', review, 'review']]) {
      if (!count) continue;
      const item = document.createElement('li');
      const dot = document.createElement('span');
      dot.className = `dot ${type}`;
      item.append(dot, document.createTextNode(`${label}：${count}`));
      elements.legendList.append(item);
    }
    elements.tabStatus.textContent = review ? `${review}复核` : `${accepted + suggested}题`;
    updateAcceptAllAction();
  }

  function renderBadgesForCards(cards) {
    for (const card of cards) {
      const exerciseId = normalizeWhitespace(card?.dataset?.exercise);
      const result = state.results.get(exerciseId);
      if (result) renderBadge(card, result);
    }
  }

  function restorePriorityView({ announce = false } = {}) {
    const view = state.priorityView;
    if (!view) return;
    for (const card of view.renderedCards) card.remove();
    view.controls.remove();
    for (const pager of view.nativePagers) pager.element.style.display = pager.display;
    const originals = document.createDocumentFragment();
    for (const card of view.originalCards) {
      for (const badge of [...card.querySelectorAll(`.${BADGE_CLASS}`)]) badge.remove();
      card.classList.remove('wulou-curation-linked', 'wulou-curation-linked-review', 'wulou-curation-linked-accepted');
      originals.append(card);
      const exerciseId = normalizeWhitespace(card.dataset.exercise);
      const question = view.questionById.get(exerciseId);
      if (question) state.cards.set(exerciseId, { ...question, card });
    }
    view.parent.insertBefore(originals, view.marker.nextSibling);
    view.marker.remove();
    state.priorityView = null;
    renderBadgesForCards(view.originalCards);
    updateLegend();
    if (announce) setStatus('已退出审核优先视图，恢复题湖原始分页顺序。');
  }

  function renderPriorityViewPage(pageNumber) {
    const view = state.priorityView;
    if (!view) return;
    for (const card of view.renderedCards) card.remove();
    const page = pageSlice(view.orderedQuestions, view.pageSize, pageNumber);
    view.page = page.page;
    updateFocusSnapshotPage(page.page);
    const fragment = document.createDocumentFragment();
    const rendered = [];
    for (const question of page.items) {
      if (!question.cardTemplate) continue;
      const card = document.importNode(question.cardTemplate, true);
      for (const badge of [...card.querySelectorAll(`.${BADGE_CLASS}`)]) badge.remove();
      card.classList.remove('wulou-curation-linked', 'wulou-curation-linked-review', 'wulou-curation-linked-accepted');
      state.cards.set(question.exerciseId, { ...question, card });
      rendered.push(card);
      fragment.append(card);
    }
    view.parent.insertBefore(fragment, view.marker.nextSibling);
    view.renderedCards = rendered;
    renderBadgesForCards(rendered);
    view.pageLabel.textContent = `第 ${page.page} / ${page.totalPages} 页`;
    view.previous.disabled = page.page <= 1;
    view.next.disabled = page.page >= page.totalPages;
  }

  function activateFocusPriorityView(questions, pageSize, initialPage = 1) {
    restorePriorityView();
    const orderedQuestions = reviewFirstItems(questions, state.results);
    const reviewCount = orderedQuestions.filter(question => state.results.get(question.exerciseId)?.status === 'review').length;
    if (!reviewCount) return false;
    const originalCards = [...document.querySelectorAll(CARD_SELECTOR)];
    const parent = originalCards[0]?.parentElement;
    if (!parent || !originalCards.length || originalCards.some(card => card.parentElement !== parent)) {
      setStatus(`识别完成：${reviewCount} 道待人工复核。当前页面结构无法启用跨页审核优先视图，请根据题卡的“待复核”标记处理。`, true);
      return false;
    }
    const questionById = new Map(questions.map(question => [question.exerciseId, question]));
    const marker = document.createComment('wulou-review-priority');
    parent.insertBefore(marker, originalCards[0]);
    for (const card of originalCards) card.remove();

    const controls = document.createElement('section');
    controls.className = 'wulou-review-priority-controls';
    const summary = document.createElement('strong');
    summary.textContent = `审核优先视图：${reviewCount} 道待人工复核已前置`;
    const hint = document.createElement('small');
    hint.textContent = '请使用这里的分页逐题校对；审核题排完后才显示普通建议。';
    const previous = document.createElement('button');
    previous.type = 'button';
    previous.textContent = '上一页';
    const pageLabel = document.createElement('span');
    const next = document.createElement('button');
    next.type = 'button';
    next.textContent = '下一页';
    const exit = document.createElement('button');
    exit.type = 'button';
    exit.textContent = '退出审核优先视图';
    controls.append(summary, hint, previous, pageLabel, next, exit);
    parent.after(controls);
    // 当前目录范围已由脚本重排为自己的分页序列；隐藏站点原分页，避免用户误点后离开该快照。
    const nativePagers = [...document.querySelectorAll('.pagination, [class*="pagination"], nav[aria-label*="页"]')]
      .map(element => ({ element, display: element.style.display }));
    for (const pager of nativePagers) pager.element.style.display = 'none';

    state.priorityView = {
      parent, marker, originalCards, questionById, orderedQuestions,
      pageSize: Math.max(1, pageSize || originalCards.length), page: 1,
      renderedCards: [], controls, previous, next, pageLabel, nativePagers,
    };
    previous.addEventListener('click', () => renderPriorityViewPage(state.priorityView?.page - 1));
    next.addEventListener('click', () => renderPriorityViewPage(state.priorityView?.page + 1));
    exit.addEventListener('click', () => restorePriorityView({ announce: true }));
    renderPriorityViewPage(initialPage);
    return true;
  }

  function prioritizeCurrentPageReviews() {
    const cards = [...document.querySelectorAll(CARD_SELECTOR)];
    const parent = cards[0]?.parentElement;
    if (!parent || !cards.length || cards.some(card => card.parentElement !== parent)) return 0;
    const ordered = reviewFirstItems(cards, new Map(cards.map(card => [
      normalizeWhitespace(card.dataset.exercise), state.results.get(normalizeWhitespace(card.dataset.exercise)),
    ])));
    const reviewCount = ordered.filter(card => state.results.get(normalizeWhitespace(card.dataset.exercise))?.status === 'review').length;
    if (!reviewCount) return 0;
    const marker = document.createComment('wulou-current-page-review-priority');
    parent.insertBefore(marker, cards[0]);
    const fragment = document.createDocumentFragment();
    for (const card of ordered) fragment.append(card);
    parent.insertBefore(fragment, marker.nextSibling);
    marker.remove();
    renderBadgesForCards(ordered);
    return reviewCount;
  }

  function activePendingAcceptances() {
    return pendingAcceptanceItems([...state.results.entries()])
      .map(item => ({
        ...item,
        acceptanceMode: item.result.acceptance_mode || 'move',
      }))
      // “待复核”必须由人工逐题确认；一键采纳只处理已是普通建议的题目。
      .filter(item => item.result.status === 'suggested')
      .filter(({ exerciseId, acceptanceMode }) => acceptanceMode === 'local'
        || Boolean(state.cards.get(exerciseId)?.attributeUrl));
  }

  function updateAcceptAllAction() {
    const candidates = activePendingAcceptances();
    const count = candidates.length;
    const localCount = candidates.filter(item => item.acceptanceMode === 'local').length;
    const moveCount = count - localCount;
    const actionMode = acceptAllActionMode(state.workset, Boolean(focusedDirectoryScope(state.taxonomy)));
    const canHydrateFocus = actionMode === 'hydrate_focus';
    const scopeLabel = actionMode === 'focus'
      ? '当前目录全部采纳'
      : (canHydrateFocus ? '全部采纳' : '本页全部采纳');
    elements.acceptAll.textContent = count ? `${scopeLabel}（${count}）` : scopeLabel;
    elements.acceptAll.disabled = Boolean(state.busy || state.acceptingAll || (!count && !canHydrateFocus));
    elements.acceptAll.title = canHydrateFocus
      ? '点击后读取当前目录所有分页并查询已有本地缓存，不会重新调用云端分类；随后统一采纳已审核的建议'
      : (count
        ? `${state.workset === 'focus' ? '当前目录范围' : '当前页'} ${count} 道待采纳：${localCount} 道已归位，${moveCount} 道将并发移动`
        : `${state.workset === 'focus' ? '当前目录范围' : '当前页'}没有可采纳的建议`);
  }

  async function hydrateFocusAcceptanceQueue() {
    const focus = focusedDirectoryScope(state.taxonomy);
    if (!focus) return { restored: false, reason: '未选中可汇总的当前目录' };
    const restoreId = ++state.cacheRestoreId;
    setBusy(true);
    try {
      const collected = await collectAllFocusPages({ purpose: 'acceptance' });
      if (restoreId !== state.cacheRestoreId) return { restored: false, cancelled: true };
      state.workset = 'focus';
      state.results.clear();
      state.cards.clear();
      const questions = collected.questions.map(question => {
        state.cards.set(question.exerciseId, question);
        return classificationPayload(question, {
          topic_id: focus.topic_id,
          level2_id: focus.level2_id,
        });
      });
      state.currentQuestions = questions;
      const cachedResults = [];
      const cacheChunks = chunkItems(questions, 200);
      for (const [index, chunk] of cacheChunks.entries()) {
        setStatus(`正在汇总当前目录的已审核建议：查询本地缓存（第 ${index + 1}/${cacheChunks.length} 批）…`);
        const lookup = await request('POST', '/api/v1/cache/classifications/lookup', { questions: chunk });
        if (restoreId !== state.cacheRestoreId) return { restored: false, cancelled: true };
        cachedResults.push(...(lookup.results || []));
      }
      for (const rawResult of cachedResults) {
        const result = withAcceptanceState(normalizeClassificationResult(rawResult));
        if (result.exercise_id) state.results.set(result.exercise_id, result);
      }
      // 逐页识别后也建立目录快照：下次刷新可用“恢复上次审核队列”回到完整范围，
      // 无需再执行云端分类。
      saveFocusSnapshot(createFocusSnapshot(focus, collected.pageSize));
      updateLegend();
      return {
        restored: true,
        questionCount: questions.length,
        cachedCount: state.results.size,
        attributeFailed: collected.attributeFailed,
      };
    } catch (error) {
      return { restored: false, error };
    } finally {
      setBusy(false);
    }
  }

  async function acceptAllSuggestions() {
    if (state.busy || state.acceptingAll) return;
    try {
      // 先确认刚刚完成的人工目录选择已写入本地服务；随后若需要汇总全目录，
      // 缓存查询即可读到这些选择，而不会退回模型的待复核结果。
      await flushManualSelectionSaves();
    } catch (error) {
      setStatus(error.message, true);
      return;
    }
    if (acceptAllActionMode(state.workset, Boolean(focusedDirectoryScope(state.taxonomy))) === 'hydrate_focus') {
      const hydrated = await hydrateFocusAcceptanceQueue();
      if (hydrated.cancelled) return;
      if (!hydrated.restored) {
        const reason = hydrated.error?.message || hydrated.reason || '当前目录范围无法读取';
        setStatus(`未能汇总当前目录的已审核建议：${reason}`, true);
        return;
      }
    }
    const candidates = activePendingAcceptances();
    const scopeLabel = state.workset === 'focus' ? '当前目录全部题目' : '当前页';
    if (!candidates.length) {
      setStatus(`${scopeLabel}没有可采纳的完整分类建议。`, true);
      return;
    }
    const localCount = candidates.filter(item => item.acceptanceMode === 'local').length;
    const moveCount = candidates.length - localCount;
    const confirmed = await confirmAction({
      title: `全部采纳${scopeLabel}建议？`,
      message: `其中 ${localCount} 道题已在目标目录，将跳过；${moveCount} 道将并发提交题湖移动请求。失败题目会保留为待采纳状态。`,
      confirmLabel: `采纳 ${candidates.length} 道`,
    });
    if (!confirmed) return;

    state.acceptingAll = true;
    setBusy(true);
    for (const [exerciseId, result] of state.results.entries()) {
      const card = state.cards.get(exerciseId)?.card;
      if (card) renderBadge(card, result);
    }
    setStatus(`正在采纳${scopeLabel} ${candidates.length} 道题：${localCount} 道跳过，${moveCount} 道并发移动…`);
    try {
      const outcomes = await runPool(candidates, ACCEPTANCE_CONCURRENCY,
        ({ exerciseId }) => acceptSuggestion(exerciseId, { announce: false }));
      const completed = outcomes.map(item => item.status === 'fulfilled' ? item.value : {
        accepted: false,
        message: item.reason?.message || '采纳任务意外中断',
      });
      const accepted = completed.filter(item => item.accepted).length;
      const localAccepted = completed.filter(item => item.accepted && item.local).length;
      const moved = completed.filter(item => item.accepted && item.moved).length;
      const failures = completed.filter(item => !item.accepted && !item.skipped);
      const warnings = completed.reduce((total, item) => total + (item.warnings?.length || 0), 0);
      const details = [
        `${scopeLabel}已采纳 ${accepted}/${candidates.length} 道题`,
        localAccepted && `其中 ${localAccepted} 道仅本地确认（未发送移动请求）`,
        moved && `已移动 ${moved} 道`,
        failures.length && `${failures.length} 道失败并保留待采纳`,
        warnings && `${warnings} 条本地记录未保存`,
      ].filter(Boolean).join('；');
      setStatus(`${details}。`, Boolean(failures.length || warnings));
    } finally {
      state.acceptingAll = false;
      // 失败题此前因整页操作而被禁用；批次结束后重新渲染，使其可单题重试。
      for (const [exerciseId, result] of state.results.entries()) {
        const card = state.cards.get(exerciseId)?.card;
        if (card) renderBadge(card, result);
      }
      setBusy(false);
      updateLegend();
    }
  }

  async function restoreFocusSnapshot(restoreId) {
    const snapshot = loadFocusSnapshot();
    const focus = focusedDirectoryScope(state.taxonomy);
    if (!focusSnapshotMatches(snapshot, location.pathname, focus)) {
      state.pendingFocusRestore = false;
      updateFocusRestoreAction();
      return { attempted: false, restored: false };
    }

    state.focusSnapshot = snapshot;
    try {
      setStatus('正在恢复上次当前目录审核队列：读取题目与答案数据…');
      const collected = await collectAllFocusPages({ purpose: 'restore' });
      if (restoreId !== state.cacheRestoreId) return { attempted: true, restored: false, cancelled: true };

      state.workset = 'focus';
      state.results.clear();
      state.cards.clear();
      const questions = collected.questions.map(question => {
        state.cards.set(question.exerciseId, question);
        return classificationPayload(question, {
          topic_id: focus.topic_id,
          level2_id: focus.level2_id,
        });
      });
      state.currentQuestions = questions;
      installBadgeStyles();

      const cachedResults = [];
      const cacheChunks = chunkItems(questions, 200);
      for (const [index, chunk] of cacheChunks.entries()) {
        setStatus(`正在恢复上次当前目录审核队列：查询本地缓存（第 ${index + 1}/${cacheChunks.length} 批）…`);
        const lookup = await request('POST', '/api/v1/cache/classifications/lookup', { questions: chunk });
        if (restoreId !== state.cacheRestoreId) return { attempted: true, restored: false, cancelled: true };
        cachedResults.push(...(lookup.results || []));
      }
      for (const rawResult of cachedResults) {
        const result = withAcceptanceState(normalizeClassificationResult(rawResult));
        if (result.exercise_id) state.results.set(result.exercise_id, result);
      }
      const priorityEnabled = activateFocusPriorityView(
        collected.questions,
        snapshot.page_size,
        snapshot.priority_page,
      );
      const reviewCount = collected.questions.filter(question => state.results.get(question.exerciseId)?.status === 'review').length;
      const missing = questions.length - state.results.size;
      const partial = Boolean(collected.attributeFailed || missing);
      setStatus(
        `已恢复上次当前目录审核队列：${state.results.size}/${questions.length} 道缓存建议${collected.attributeFailed ? `，${collected.attributeFailed} 道属性读取失败` : ''}${missing ? `，${missing} 道缓存未命中` : ''}${priorityEnabled ? `；${reviewCount} 道待人工复核已回到第 ${state.priorityView?.page || 1} 页优先队列` : ''}。${missing ? '缓存未命中的题目可执行“识别当前目录全部题目”补全。' : '无需重新调用模型。'}`,
        partial ? 'warning' : 'normal',
      );
      state.pendingFocusRestore = false;
      updateFocusRestoreAction();
      return { attempted: true, restored: true, attributeFailed: collected.attributeFailed, missing };
    } catch (error) {
      return { attempted: true, restored: false, error };
    }
  }

  async function restoreSavedFocusQueue() {
    if (state.busy || !state.pendingFocusRestore) return;
    const restoreId = ++state.cacheRestoreId;
    setBusy(true);
    try {
      const restored = await restoreFocusSnapshot(restoreId);
      if (restored.cancelled) return;
      if (!restored.restored) {
        const reason = restored.error ? `：${restored.error.message}` : '，上次队列与当前目录不一致';
        setStatus(`未能恢复上次当前目录审核队列${reason}`, 'warning');
      }
    } finally {
      setBusy(false);
    }
  }

  async function restoreCachedResults(restoreId) {
    const cards = collectCards();
    const fetched = await runPool(cards, ATTRIBUTE_CONCURRENCY, fetchAttribute);
    const questions = [];
    const cardById = new Map();
    for (const item of fetched) {
      if (item.status !== 'fulfilled') continue;
      const question = item.value;
      questions.push(classificationPayload(question, state.scope));
      cardById.set(question.exerciseId, question.card);
    }
    if (restoreId !== state.cacheRestoreId) return { cancelled: true, restored: 0, attributeFailed: 0 };
    if (!questions.length) return { restored: 0, attributeFailed: cards.length };
    const lookup = await request('POST', '/api/v1/cache/classifications/lookup', { questions });
    if (restoreId !== state.cacheRestoreId) return { cancelled: true, restored: 0, attributeFailed: 0 };
    state.results.clear();
    state.cards.clear();
    state.currentQuestions = questions;
    for (const item of fetched) {
      if (item.status === 'fulfilled') state.cards.set(item.value.exerciseId, item.value);
    }
    installBadgeStyles();
    for (const rawResult of (lookup.results || [])) {
      const result = withAcceptanceState(normalizeClassificationResult(rawResult));
      state.results.set(result.exercise_id, result);
      const card = cardById.get(result.exercise_id);
      if (card) renderBadge(card, result);
    }
    updateLegend();
    return { restored: (lookup.results || []).length, attributeFailed: cards.length - questions.length };
  }

  function classificationStageLabel(stage) {
    return ({ queued: '建立本地作业', routing: '判断最终归属专题', classifying: '专题内目录分类', completed: '整理结果' })[stage] || '分类';
  }

  function applyClassificationJobSnapshot(job, cardById) {
    const rows = Array.isArray(job.results) ? job.results : [];
    for (const rawResult of rows) {
      const result = withAcceptanceState(normalizeClassificationResult(rawResult));
      if (!result.exercise_id) continue;
      const isNew = !state.results.has(result.exercise_id);
      state.results.set(result.exercise_id, result);
      const card = cardById.get(result.exercise_id);
      if (card && isNew) renderBadge(card, result);
    }
    updateLegend();
  }

  async function waitForClassificationJob(initialJob, cardById, {
    jobIndex = 1, jobCount = 1, completedBefore = 0, totalQuestions = initialJob.total || 0, scopeLabel = '当前页',
  } = {}) {
    let job = initialJob;
    state.classificationJobId = job.job_id || '';
    while (true) {
      applyClassificationJobSnapshot(job, cardById);
      const failed = Array.isArray(job.failed_exercise_ids) ? job.failed_exercise_ids.length : 0;
      if (job.status === 'completed') return job;
      if (job.status === 'failed') throw new Error(job.error || '本地分类作业意外停止');
      setStatus(classificationProgressText({
        scopeLabel,
        stage: classificationStageLabel(job.stage),
        completed: completedBefore + (job.completed || 0),
        total: totalQuestions,
        failed,
        jobIndex,
        jobCount,
      }));
      await delay(CLASSIFICATION_JOB_POLL_INTERVAL_MS);
      job = await request('GET', `/api/v1/classification-jobs/${encodeURIComponent(state.classificationJobId)}`);
    }
  }

  async function runClassificationJobs(questions, cardById, scopeLabel) {
    const jobs = chunkItems(questions, CLASSIFICATION_JOB_MAX_QUESTIONS);
    const failedExerciseIds = [];
    let completedBefore = 0;
    for (const [index, jobQuestions] of jobs.entries()) {
      const jobRange = `${completedBefore + 1}-${completedBefore + jobQuestions.length}/${questions.length} 题`;
      setStatus(jobs.length > 1
        ? `${scopeLabel}：正在提交第 ${index + 1}/${jobs.length} 个本地分类作业（${jobRange}）…`
        : `${scopeLabel}：正在提交本地分类总作业（${jobRange}）…`);
      const initialJob = await request('POST', '/api/v1/classification-jobs', { questions: jobQuestions });
      const completedJob = await waitForClassificationJob(initialJob, cardById, {
        jobIndex: index + 1,
        jobCount: jobs.length,
        completedBefore,
        totalQuestions: questions.length,
        scopeLabel,
      });
      failedExerciseIds.push(...(Array.isArray(completedJob.failed_exercise_ids) ? completedJob.failed_exercise_ids : []));
      completedBefore += jobQuestions.length;
    }
    return { failed_exercise_ids: [...new Set(failedExerciseIds)] };
  }

  async function classifyPage() {
    if (state.busy) return;
    restorePriorityView();
    clearFocusSnapshot();
    // 新分类优先级最高；丢弃尚未完成的旧缓存恢复结果。
    state.cacheRestoreId += 1;
    setBusy(true);
    state.workset = 'page';
    state.results.clear();
    try {
      const cards = collectCards();
      if (!cards.length) {
        setStatus('当前页暂无可识别题目。');
        return;
      }
      setStatus(`正在读取 ${cards.length} 道题目的属性文本…`);
      const fetched = await runPool(cards, ATTRIBUTE_CONCURRENCY, fetchAttribute);
      const questions = [];
      const cardById = new Map();
      state.cards.clear();
      for (const item of fetched) {
        if (item.status === 'fulfilled') {
          questions.push(classificationPayload(item.value, state.scope));
          cardById.set(item.value.exerciseId, item.value.card);
          state.cards.set(item.value.exerciseId, item.value);
        }
      }
      if (!questions.length) throw new Error('当前页题目属性均无法读取');
      state.currentQuestions = questions;
      installBadgeStyles();
      setStatus(`已读取 ${questions.length}/${cards.length} 道题，正在提交本地分类作业…`);
      const completedJob = await runClassificationJobs(questions, cardById, '当前页识别');
      const attributeFailed = cards.length - questions.length;
      const classificationFailed = Array.isArray(completedJob.failed_exercise_ids)
        ? completedJob.failed_exercise_ids.length : 0;
      const reviewCount = prioritizeCurrentPageReviews();
      const hasPartialFailure = Boolean(attributeFailed || classificationFailed);
      setStatus(`识别完成：${questions.length} 道已处理${attributeFailed ? `，${attributeFailed} 道属性读取失败` : ''}${classificationFailed ? `，${classificationFailed} 道云端请求失败` : ''}${reviewCount ? `；${reviewCount} 道待人工复核已置顶` : ''}。请先复核结果。`, hasPartialFailure ? 'warning' : 'normal');
    } catch (error) {
      setStatus(error.message, true);
      elements.tabStatus.textContent = '!';
    } finally {
      setBusy(false);
    }
  }

  async function classifyFocus() {
    if (state.busy) return;
    restorePriorityView();
    const focus = focusedDirectoryScope(state.taxonomy);
    if (!focus) {
      setStatus('请先在题湖左侧目录树选中当前专题内的二级、三级或四级目录。', true);
      return;
    }
    state.cacheRestoreId += 1;
    setBusy(true);
    state.results.clear();
    try {
      const collected = await collectAllFocusPages({ purpose: 'classification' });
      const classificationJobCount = chunkItems(collected.questions, CLASSIFICATION_JOB_MAX_QUESTIONS).length;
      const confirmed = await confirmAction({
        title: '识别当前目录全部题目？',
        message: `将读取当前目录及其下级目录中的 ${collected.pageCount} 页、${collected.questions.length} 道题，并按每个最多 ${CLASSIFICATION_JOB_MAX_QUESTIONS} 题拆为 ${classificationJobCount} 个本地分类作业；这会产生相应的云端分类请求，但不会自动移动题目。`,
        confirmLabel: `识别 ${collected.questions.length} 道`,
      });
      if (!confirmed) {
        setStatus('已取消当前目录全部题目的识别。');
        return;
      }
      clearFocusSnapshot();
      state.workset = 'focus';
      state.cards.clear();
      const questions = collected.questions.map(question => {
        state.cards.set(question.exerciseId, question);
        return classificationPayload(question, {
          topic_id: focus.topic_id,
          level2_id: focus.level2_id,
        });
      });
      state.currentQuestions = questions;
      installBadgeStyles();
      setStatus(`已固定当前目录快照：${questions.length} 道题，正在提交云端分类总作业…`);
      const completedJob = await runClassificationJobs(questions, collected.cardById, '当前目录全量识别');
      const classificationFailed = Array.isArray(completedJob.failed_exercise_ids)
        ? completedJob.failed_exercise_ids.length : 0;
      saveFocusSnapshot(createFocusSnapshot(focus, collected.pageSize));
      const priorityEnabled = activateFocusPriorityView(collected.questions, collected.pageSize, 1);
      const reviewCount = collected.questions.filter(question => state.results.get(question.exerciseId)?.status === 'review').length;
      const hasPartialFailure = Boolean(collected.attributeFailed || classificationFailed);
      setStatus(`当前目录全量识别完成：${questions.length} 道题已固定在本次队列中${collected.attributeFailed ? `，${collected.attributeFailed} 道属性读取失败` : ''}${classificationFailed ? `，${classificationFailed} 道云端请求失败` : ''}${priorityEnabled ? `；${reviewCount} 道待人工复核已按分页置顶` : ''}。${priorityEnabled ? '请先在审核优先视图逐题校对，再退出视图检查普通建议。' : '可使用“当前目录全部采纳”处理普通建议。'}`, hasPartialFailure ? 'warning' : 'normal');
    } catch (error) {
      setStatus(error.message, true);
      elements.tabStatus.textContent = '!';
    } finally {
      setBusy(false);
    }
  }

  async function clearCurrentPageCache() {
    if (state.busy) return;
    try {
      const cards = collectCards();
      if (!cards.length) {
        setStatus('当前页暂无题目，无需清除缓存。');
        return;
      }
      const exerciseIds = [...new Set(cards.map(card => card.exerciseId))];
      const confirmed = await confirmAction({
        title: '清除本页缓存？',
        message: `将清除当前页 ${exerciseIds.length} 道题目的本地分类缓存。下次识别会重新调用云端模型。`,
        confirmLabel: '确认清除',
        destructive: true,
      });
      if (!confirmed) return;
      state.cacheRestoreId += 1;
      clearFocusSnapshot();
      setBusy(true);
      const response = await request('POST', '/api/v1/cache/classifications/delete', { exercise_ids: exerciseIds });
      for (const card of cards) card.card.querySelector(`.${BADGE_CLASS}`)?.remove();
      state.results.clear();
      state.currentQuestions = [];
      updateLegend();
      setStatus(`已清除 ${response.deleted} 条当前页缓存；下次识别将重新请求云端模型。`);
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function exportBatch() {
    if (!state.currentQuestions.length) { setStatus('请先识别当前页以读取题目文本', true); return; }
    setBusy(true);
    try {
      const job = await request('POST', '/api/v1/batches', { questions: state.currentQuestions });
      state.batchJobId = job.job_id;
      setStatus(`已在本机导出 ${job.question_count} 道题的 JSONL。提交后将产生云端模型费用。`);
    } catch (error) { setStatus(error.message, true); }
    finally { setBusy(false); }
  }

  async function submitBatch() {
    if (!state.batchJobId) return;
    const confirmed = await confirmAction({
      title: '提交云端批处理？',
      message: '将提交当前 JSONL 到云端模型并产生费用。',
      confirmLabel: '确认提交',
    });
    if (!confirmed) return;
    setBusy(true);
    try {
      const job = await request('POST', '/api/v1/batches/submit', { job_id: state.batchJobId });
      setStatus(`云端批处理已提交，状态：${job.status}。完成后可通过本地服务同步并导入结果。`);
    } catch (error) { setStatus(error.message, true); }
    finally { setBusy(false); }
  }

  async function syncBatch() {
    if (!state.batchJobId) return;
    setBusy(true);
    try {
      const job = await request('POST', '/api/v1/batches/refresh', { job_id: state.batchJobId });
      if (job.status !== 'completed') { setStatus(`云端任务尚未完成，当前状态：${job.status}`); return; }
      const imported = await request('POST', '/api/v1/batches/import', { job_id: state.batchJobId });
      setStatus(`已导入 ${imported.imported} 道题结果${imported.failed_exercise_ids.length ? `，${imported.failed_exercise_ids.length} 道需重试` : ''}。点击“识别当前页”即可展示缓存结果。`);
    } catch (error) { setStatus(error.message, true); }
    finally { setBusy(false); }
  }

  elements.tab.addEventListener('click', () => setOpen(true));
  elements.close.addEventListener('click', () => setOpen(false));
  elements.settings.addEventListener('click', openSettings);
  elements.backSettings.addEventListener('click', () => {
    closeSettings();
    elements.settings.focus({ preventScroll: true });
  });
  elements.cancelSettings.addEventListener('click', () => {
    closeSettings();
    elements.settings.focus({ preventScroll: true });
  });
  shadow.addEventListener('keydown', event => {
    if (state.confirmation && event.key === 'Tab') {
      event.preventDefault();
      (event.shiftKey ? elements.confirmAccept : elements.confirmCancel).focus({ preventScroll: true });
      return;
    }
    if (event.key !== 'Escape') return;
    if (state.confirmation) {
      event.preventDefault();
      closeConfirmation(false);
      return;
    }
    if (!elements.historyDateSheet.hidden) {
      event.preventDefault();
      closeHistoryDatePicker();
      elements.historyDateRange.focus({ preventScroll: true });
      return;
    }
    if (!elements.historyView.hidden) {
      closeHistory();
      elements.history.focus({ preventScroll: true });
    }
    else if (!elements.settingsDrawer.hidden) {
      closeSettings();
      elements.settings.focus({ preventScroll: true });
    }
    else if (elements.panel.classList.contains('open')) setOpen(false);
  });
  elements.saveCloud.addEventListener('click', saveCloudSettings);
  elements.profileTabs.addEventListener('pointerdown', startProfileDragCandidate);
  elements.profileTabs.addEventListener('pointermove', moveProfileDragCandidate);
  elements.profileTabs.addEventListener('pointerup', endProfileDragCandidate);
  elements.profileTabs.addEventListener('pointercancel', endProfileDragCandidate);
  elements.profileTabs.addEventListener('lostpointercapture', endProfileDragCandidate);
  window.addEventListener('pointerup', endProfileDragCandidate, true);
  window.addEventListener('pointercancel', endProfileDragCandidate, true);
  elements.profileTabs.addEventListener('click', event => {
    if (Date.now() < state.suppressProfileClickUntil) {
      event.preventDefault();
      return;
    }
    const target = event.target instanceof Element ? event.target.closest('button') : null;
    const profileId = target?.dataset.profileId;
    if (!profileId) return;
    if (target.classList.contains('profile-delete')) {
      deleteCloudProfile(profileId);
      return;
    }
    if (target.classList.contains('profile-edit')) {
      startProfileRename(profileId);
      return;
    }
    selectCloudProfile(profileId);
  });
  elements.createCloudProfile.addEventListener('click', createCloudProfile);
  elements.cloudProtocol.addEventListener('change', clearAvailableModels);
  elements.cloudBaseUrl.addEventListener('input', clearAvailableModels);
  elements.cloudApiKey.addEventListener('input', clearAvailableModels);
  elements.requestCompatibility.addEventListener('change', clearAvailableModels);
  elements.customHeaders.addEventListener('input', clearAvailableModels);
  elements.clearCustomHeaders.addEventListener('change', clearAvailableModels);
  elements.testCloudConnection.addEventListener('click', testCloudConnection);
  elements.classify.addEventListener('click', classifyPage);
  elements.exportAllQuestions.addEventListener('click', exportAllFocusQuestions);
  elements.classifyFocus.addEventListener('click', classifyFocus);
  elements.restoreFocus.addEventListener('click', restoreSavedFocusQueue);
  elements.acceptAll.addEventListener('click', acceptAllSuggestions);
  elements.history.addEventListener('click', openHistory);
  elements.historyDateRange.addEventListener('click', openHistoryDatePicker);
  elements.historyDateBackdrop.addEventListener('click', closeHistoryDatePicker);
  elements.historyMonthPrev.addEventListener('click', () => moveHistoryCalendarMonth(-1));
  elements.historyMonthNext.addEventListener('click', () => moveHistoryCalendarMonth(1));
  elements.historyCalendarGrid.addEventListener('click', event => {
    const dateButton = event.target.closest('.history-calendar-day[data-date]');
    if (dateButton && !dateButton.disabled) chooseHistoryDate(dateButton.dataset.date);
  });
  elements.historyCalendarGrid.addEventListener('pointermove', event => {
    const dateButton = event.target.closest('.history-calendar-day[data-date]');
    const previewEnd = dateButton && !dateButton.disabled
      ? historyRangePreviewEnd(state.historyRange, dateButton.dataset.date) : '';
    if (state.historyRangePreviewEnd === previewEnd) return;
    state.historyRangePreviewEnd = previewEnd;
    renderHistoryCalendar();
  });
  elements.historyCalendarGrid.addEventListener('pointerleave', () => {
    if (!state.historyRangePreviewEnd) return;
    state.historyRangePreviewEnd = '';
    renderHistoryCalendar();
  });
  elements.backHistory.addEventListener('click', () => {
    closeHistory();
    elements.history.focus({ preventScroll: true });
  });
  elements.exportHistory.addEventListener('click', exportHistoryReport);
  elements.clearCache.addEventListener('click', clearCurrentPageCache);
  elements.confirmCancel.addEventListener('click', () => closeConfirmation(false));
  elements.confirmAccept.addEventListener('click', () => closeConfirmation(true));
  elements.confirmOverlay.addEventListener('click', event => {
    if (event.target === elements.confirmOverlay) closeConfirmation(false);
  });
  elements.exportBatch.addEventListener('click', exportBatch);
  elements.submitBatch.addEventListener('click', submitBatch);
  elements.syncBatch.addEventListener('click', syncBatch);
  (async () => {
    setBusy(false);
    // 刷新页面后自动读取服务端已持久化的 settings.local.yaml 配置和当前页面目录。
    await connectService();
  })().catch(error => setStatus(`无法自动连接本地服务：${error.message}`, true));
})();
