// ==UserScript==
// @name         题湖题库数学题分类助手
// @namespace    https://www.wulouai.com/
// @version      0.8.0
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
  const ATTRIBUTE_CONCURRENCY = 3;
  const LOCAL_REQUEST_TIMEOUT_MS = 30000;
  const CLASSIFICATION_JOB_POLL_INTERVAL_MS = 2000;
  const MODIFY_ENDPOINT = '/exercise/modifyData';

  function normalizeWhitespace(value) {
    return String(value || '').replace(/\u3000/g, ' ').replace(/\s+/g, ' ').trim();
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

  function stableCodeFromText(value) {
    const match = normalizeWhitespace(value).match(/\b[A-Z]{2,}[A-Z0-9-]{4,}\b/);
    return match ? match[0] : null;
  }

  function sourceTextWithoutAssistant(card) {
    const clone = card.cloneNode(true);
    for (const badge of [...clone.querySelectorAll(`.${BADGE_CLASS}`)]) badge.remove();
    return normalizeWhitespace(clone.textContent);
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

  function inputWarningLabels(snapshot) {
    const labels = {
      question_text_missing: '题干文本为空',
      answer_text_missing: '答案文本为空',
      question_unrecognized_typesetting: '题干含无法识别的排版字符',
      answer_unrecognized_typesetting: '答案含无法识别的排版字符',
      question_unbalanced_latex: '题干 LaTex 定界符不完整',
      answer_unbalanced_latex: '答案 LaTex 定界符不完整',
      answer_suspected_extra_parts: '答案疑似混入题干不存在的额外小题，已从模型输入中排除',
    };
    return [...new Set((snapshot?.warnings || []).map(item => labels[item] || normalizeWhitespace(item)).filter(Boolean))];
  }

  function appendModelInputSnapshot(badge, snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return;
    const question = snapshot.question?.text;
    const answer = snapshot.answer?.text;
    const capturedAnswer = snapshot.answer?.captured_text;
    if (!question && !answer && !capturedAnswer) return;
    const details = document.createElement('details');
    details.className = 'badge-input';
    const summary = document.createElement('summary');
    const warnings = inputWarningLabels(snapshot);
    summary.textContent = warnings.length ? `查看模型输入（${warnings.join('；')}）` : '查看模型输入';
    const content = document.createElement('div');
    content.className = 'badge-input-content';
    const questionLine = document.createElement('p');
    questionLine.textContent = `题干：${question || '（空）'}`;
    const answerLine = document.createElement('p');
    answerLine.textContent = snapshot.answer?.used_for_classification === false
      ? `答案（未发送给模型，疑似混入额外题目）：${capturedAnswer || '（空）'}`
      : `答案：${answer || '（空）'}`;
    content.append(questionLine, answerLine);
    details.append(summary, content);
    badge.append(details);
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      normalizeWhitespace, stableCodeFromText, runPool, chunkItems,
      classificationPayload, normalizeClassificationResult, resolveCataloguePath,
      serializeSuccessfulControls,
      sourceTextWithoutAssistant,
    };
    return;
  }

  if (window.top !== window.self || document.getElementById(PANEL_ID)) return;

  const state = {
    taxonomy: null,
    scope: { topic_id: '', level2_id: '' },
    results: new Map(),
    busy: false,
    currentQuestions: [],
    batchJobId: '',
    classificationJobId: '',
    cacheRestoreId: 0,
    cloudConfigured: false,
    cards: new Map(),
    accepting: new Set(),
  };

  const host = document.createElement('div');
  host.id = PANEL_ID;
  const shadow = host.attachShadow({ mode: 'open' });
  shadow.innerHTML = `
    <style>
      :host { all: initial; color-scheme: light; font: 14px/1.5 "Segoe UI", "Microsoft YaHei", sans-serif; color: #1f2c2a; }
      *, *::before, *::after { box-sizing: border-box; }
      button, input, select { font: inherit; }
      button { min-height: 38px; border: 1px solid #cfdad5; border-radius: 10px; background: #fff; color: #1f2c2a; cursor: pointer; padding: 8px 11px; transition: background .16s ease, border-color .16s ease, transform .16s ease; }
      button:hover { border-color: #8ba99f; background: #f2f7f5; }
      button:active { transform: translateY(1px); }
      button:focus-visible, input:focus-visible, select:focus-visible { outline: 3px solid #9bd0bf; outline-offset: 2px; }
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
      input, select { width: 100%; min-height: 36px; border: 1px solid #cfdad5; border-radius: 8px; background: #fff; color: #1f2c2a; padding: 7px 9px; }
      .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 12px; }
      .actions > :only-child { grid-column: 1 / -1; }
      .primary { border-color: #126b5c; background: #126b5c; color: #fff; font-weight: 650; }
      .primary:hover { border-color: #0c5649; background: #0c5649; }
      .status { min-height: 0; margin-top: 2px; padding: 8px 10px; border: 1px solid #dce7e3; border-radius: 10px; background: #f6faf8; color: #526660; font-size: 11px; }
      .status.error { border-color: #e6bbb8; background: #fff5f4; color: #7a312d; }
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
      .settings-header h3 { margin: 0; font-size: 16px; letter-spacing: -.02em; }
      .back-settings { min-height: 34px; padding: 6px 9px; border-color: transparent; background: #eef4f1; color: #39534c; font-size: 12px; font-weight: 650; }
      .settings-copy { margin: 0 0 2px; color: #687a74; font-size: 11px; }
       .model-settings { display: grid; gap: 8px; padding: 10px; border: 1px solid #dce7e3; border-radius: 10px; background: #fff; }
       .model-settings h4 { margin: 0; color: #29453d; font-size: 12px; }
       .model-settings p { margin: -2px 0 0; color: #687a74; font-size: 11px; }
       .settings-actions { display: flex; justify-content: flex-end; gap: 8px; }
      @media (max-width: 480px) { .panel { right: 10px; width: calc(100vw - 20px); padding: 15px; } }
      @media (prefers-reduced-motion: reduce) { *, *::before, *::after { transition: none !important; } }
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
      <div class="actions"><button class="clear-cache" type="button" disabled>清除本页缓存</button></div>
       <!-- 批处理仅适合数百题以上的离线任务；保留实现，暂不占用日常实时分类面板。 -->
       <section class="batch-actions" hidden aria-label="高级批处理操作">
         <div class="actions"><button class="export-batch" type="button" disabled>导出批处理</button><button class="submit-batch" type="button" disabled>提交云端批处理</button></div>
         <div class="actions"><button class="sync-batch" type="button" disabled>同步批处理结果</button></div>
       </section>
      <div class="status" role="status" aria-live="polite">正在连接服务…</div>
      <div class="legend" hidden><h3>当前页状态</h3><ul></ul></div>
      </section>
      <section class="settings-drawer" hidden aria-label="云端模型设置">
        <div class="settings-header"><button class="back-settings" type="button">‹ 返回</button><h3>云端模型设置</h3></div>
         <p class="settings-copy">专题路由先判断“最晚必备专题”，目录分类再选择该专题内的三级、四级目录。两者可分别设置推理强度。API 密钥留空会保留原密钥。</p>
         <div class="form">
           <section class="model-settings"><h4>目录分类</h4><p>决定最终三级、四级目录。建议“高”。</p><label>模型<input class="cloud-model" type="text" autocomplete="off" placeholder="例如你的模型部署名"></label><label>推理强度<select class="classification-reasoning-effort"><option value="none">无（none）</option><option value="low">低（low）</option><option value="medium">中（medium）</option><option value="high" selected>高（high）</option><option value="xhigh">极高（xhigh）</option><option value="max">最高（max）</option></select></label></section>
           <section class="model-settings"><h4>专题路由</h4><p>选择全局“最晚必备专题”。建议“中”。</p><label>模型（可选）<input class="routing-model" type="text" autocomplete="off" placeholder="留空则与目录分类模型相同"></label><label>推理强度<select class="routing-reasoning-effort"><option value="none">无（none）</option><option value="low">低（low）</option><option value="medium" selected>中（medium）</option><option value="high">高（high）</option><option value="xhigh">极高（xhigh）</option><option value="max">最高（max）</option></select></label></section>
           <label>接口地址<input class="cloud-base-url" type="url" autocomplete="off" placeholder="https://api.openai.com/v1"></label>
          <label>API 密钥<input class="cloud-api-key" type="password" autocomplete="new-password" placeholder="留空则保留已保存的密钥"></label>
        </div>
        <div class="settings-actions"><button class="cancel-settings" type="button">取消</button><button class="primary save-cloud" type="button">保存设置</button></div>
      </section>
    </section>`;
  document.body.append(host);

  const elements = {
    panel: shadow.querySelector('.panel'), tab: shadow.querySelector('.tab'), tabStatus: shadow.querySelector('.tab-status'), close: shadow.querySelector('.close'), mainView: shadow.querySelector('.main-view'), settings: shadow.querySelector('.settings'), settingsDrawer: shadow.querySelector('.settings-drawer'), backSettings: shadow.querySelector('.back-settings'),
     cloudModel: shadow.querySelector('.cloud-model'), routingModel: shadow.querySelector('.routing-model'), classificationReasoningEffort: shadow.querySelector('.classification-reasoning-effort'), routingReasoningEffort: shadow.querySelector('.routing-reasoning-effort'), cloudBaseUrl: shadow.querySelector('.cloud-base-url'), cloudApiKey: shadow.querySelector('.cloud-api-key'), saveCloud: shadow.querySelector('.save-cloud'),
    cancelSettings: shadow.querySelector('.cancel-settings'), classify: shadow.querySelector('.classify'), clearCache: shadow.querySelector('.clear-cache'), exportBatch: shadow.querySelector('.export-batch'), submitBatch: shadow.querySelector('.submit-batch'), syncBatch: shadow.querySelector('.sync-batch'), status: shadow.querySelector('.status'),
    legend: shadow.querySelector('.legend'), legendList: shadow.querySelector('.legend ul'),
  };

  function setOpen(open) {
    if (!open) closeSettings();
    elements.panel.classList.toggle('open', open);
    elements.panel.inert = !open;
    elements.tab.hidden = open;
    elements.tab.setAttribute('aria-expanded', String(open));
    (open ? elements.close : elements.tab).focus({ preventScroll: true });
  }

  function setStatus(message, isError = false) {
    elements.status.textContent = message;
    elements.status.classList.toggle('error', isError);
  }

  function setBusy(busy) {
    state.busy = busy;
    elements.settings.disabled = busy;
    elements.saveCloud.disabled = busy;
    elements.classify.disabled = busy || !state.taxonomy;
    elements.clearCache.disabled = busy || !state.taxonomy;
    elements.exportBatch.disabled = busy || !state.cloudConfigured || !state.currentQuestions.length;
    elements.submitBatch.disabled = busy || !state.cloudConfigured || !state.batchJobId;
    elements.syncBatch.disabled = busy || !state.cloudConfigured || !state.batchJobId;
  }

  function openSettings() {
    elements.mainView.hidden = true;
    elements.settingsDrawer.hidden = false;
    elements.settings.setAttribute('aria-expanded', 'true');
    elements.cloudModel.focus({ preventScroll: true });
  }

  function closeSettings() {
    elements.settingsDrawer.hidden = true;
    elements.mainView.hidden = false;
    elements.settings.setAttribute('aria-expanded', 'false');
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
      elements.cloudModel.value = cloud.model || '';
      elements.routingModel.value = cloud.routing_model || '';
      elements.classificationReasoningEffort.value = cloud.reasoning_effort || 'high';
      elements.routingReasoningEffort.value = cloud.routing_reasoning_effort || 'medium';
      elements.cloudBaseUrl.value = cloud.base_url || 'https://api.openai.com/v1';
      elements.cloudApiKey.value = '';
      const pageScope = detectPageScope(state.taxonomy);
      if (!pageScope) {
        state.scope = { topic_id: '', level2_id: '' };
      } else {
        state.scope = pageScope;
      }
      const connectedMessage = '服务已就绪';
      setStatus(connectedMessage);
      elements.tabStatus.textContent = '就绪';
      // 缓存恢复涉及逐题读取属性，不应阻塞设置或发起新一轮分类。
      void restoreCachedResults(restoreId).then(restored => {
        if (restored.cancelled) return;
        if (restored.restored) setStatus(`已恢复 ${restored.restored} 道缓存建议`);
        else if (restored.attributeFailed) setStatus(`${restored.attributeFailed} 道题属性暂时无法读取`);
      }).catch(error => {
        if (restoreId === state.cacheRestoreId) {
          setStatus(`${connectedMessage} · 缓存建议恢复失败：${error.message}`, true);
        }
      });
    } catch (error) {
      state.taxonomy = null;
      setStatus(error.message, true);
      elements.tabStatus.textContent = '!';
    } finally {
      setBusy(false);
    }
  }

  async function saveCloudSettings() {
    setBusy(true);
    try {
      const body = {
        model: normalizeWhitespace(elements.cloudModel.value),
        routing_model: normalizeWhitespace(elements.routingModel.value),
        reasoning_effort: elements.classificationReasoningEffort.value,
        routing_reasoning_effort: elements.routingReasoningEffort.value,
        base_url: normalizeWhitespace(elements.cloudBaseUrl.value) || 'https://api.openai.com/v1',
      };
      const key = elements.cloudApiKey.value.trim();
      if (key) body.api_key = key;
      const cloud = await request('POST', '/api/v1/settings/cloud', body);
      state.cloudConfigured = Boolean(cloud.api_key_configured);
      elements.cloudApiKey.value = '';
      closeSettings();
      setStatus('设置已保存，正在验证服务…');
      await connectService();
    } catch (error) { setStatus(error.message, true); }
    finally { setBusy(false); }
  }

  async function fetchAttributeDocument(question) {
    const response = await fetch(question.attributeUrl, { credentials: 'same-origin' });
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
    const cards = [...document.querySelectorAll(CARD_SELECTOR)];
    if (!cards.length) throw new Error('未找到题目卡片，请确认当前位于题湖题库题目列表页面');
    return cards.map(card => {
      const exerciseId = normalizeWhitespace(card.dataset.exercise);
      const attribute = card.querySelector(ATTRIBUTE_SELECTOR);
      if (!exerciseId || !attribute?.dataset.url) throw new Error('题目卡片缺少题目 ID 或属性入口，页面结构可能已更新');
      const text = sourceTextWithoutAssistant(card);
      return {
        card, exerciseId, stableCode: stableCodeFromText(text), source: text,
        catalogueId: new URL(location.href).pathname.split('/').filter(Boolean)[10] || null,
        attributeUrl: sameOriginUrl(attribute.dataset.url),
        questionImageUrl: imageUrl(card, '问题'), answerImageUrl: imageUrl(card, '答案'),
      };
    });
  }

  function pageCatalogueTree() {
    const pageWindow = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
    return Array.isArray(pageWindow.data) ? pageWindow.data : [];
  }

  function withAcceptanceState(result) {
    const question = state.cards.get(result.exercise_id);
    if (!question?.currentCatalogueId || result.status !== 'suggested' || !result.target?.path?.length) return result;
    try {
      const target = resolveCataloguePath(pageCatalogueTree(), result.target.path);
      return { ...result, accepted: target.id === String(question.currentCatalogueId), website_catalogue_id: target.id };
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

  function applyManualTarget(exerciseId, path) {
    const result = state.results.get(exerciseId);
    const question = state.cards.get(exerciseId);
    if (!result || !question?.card) return;
    const normalizedPath = path.map(normalizeWhitespace).filter(Boolean);
    try {
      resolveCataloguePath(pageCatalogueTree(), normalizedPath);
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
      manual_override: {
        source: 'manual',
        original_target_path: originalTargetPath,
        pending: true,
      },
    };
    state.results.set(exerciseId, manualResult);
    renderBadge(question.card, manualResult);
    updateLegend();
    setStatus(`题目 ${exerciseId} 已应用人工分类，点击“一键采纳”写入题湖。`);
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
        select.addEventListener('change', () => {
          selectedPath = selectedPath.slice(0, depth);
          if (select.value) selectedPath.push(select.value);
          const selectedNode = directoryNodeForPath(selectedPath);
          const children = Array.isArray(selectedNode?.child) ? selectedNode.child : [];
          if (selectedNode && !children.length) {
            applyManualTarget(exerciseId, selectedPath);
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
    title.textContent = result.accepted
      ? (isManual ? '人工已采纳' : '已采纳')
      : (isManual ? '人工修改' : (result.status === 'review' ? '待复核' : '建议分类'));
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
    const routedTopic = result.routing?.latest_topic_title;
    const meta = document.createElement('small');
    meta.className = 'badge-meta';
    meta.textContent = result.accepted
      ? '已写入题湖可视化分类'
      : isManual
      ? '人工选择 · 待采纳'
      : result.status === 'review'
      ? result.review_reasons.join(' · ')
      : `${routedTopic ? `最晚必备：${routedTopic} · ` : ''}置信度 ${Math.round(result.confidence * 100)}% · 待确认`;
    badge.append(header, path, meta);
    if (result.status === 'review') appendModelInputSnapshot(badge, result.model_input_snapshot);

    if (result.status === 'suggested') {
      const action = document.createElement('button');
      action.className = 'badge-action';
      action.type = 'button';
      action.disabled = Boolean(result.accepted || state.accepting.has(result.exercise_id));
      action.textContent = result.accepted ? '已采纳' : (state.accepting.has(result.exercise_id) ? '提交中…' : '一键采纳');
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

  async function acceptSuggestion(exerciseId) {
    if (state.accepting.has(exerciseId)) return;
    const result = state.results.get(exerciseId);
    const question = state.cards.get(exerciseId);
    if (!result || result.status !== 'suggested' || !result.target?.path?.length) {
      setStatus(`题目 ${exerciseId} 没有可采纳的完整建议路径`, true);
      return;
    }
    if (!question?.attributeUrl) {
      setStatus(`题目 ${exerciseId} 缺少属性接口地址`, true);
      return;
    }

    const isManual = result.manual_override?.source === 'manual';
    const sourceCatalogueId = question.currentCatalogueId || '__uncategorized__';
    state.accepting.add(exerciseId);
    renderBadge(question.card, result);
    try {
      const target = resolveCataloguePath(pageCatalogueTree(), result.target.path);
      const attribute = await fetchAttributeDocument(question);
      const exerciseField = attribute.form.querySelector('[name="exercise_id"]');
      const catalogueField = attribute.form.querySelector('[name="exercise_catalogue_id"]');
      if (!exerciseField || String(exerciseField.value) !== String(exerciseId)) {
        throw new Error('属性表单题目 ID 与当前题卡不一致');
      }
      if (!catalogueField) throw new Error('属性表单缺少可视化分类字段');

      if (String(catalogueField.value) !== target.id) {
        catalogueField.value = target.id;
        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
        const headers = {
          Accept: 'application/json',
          'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
          'X-Requested-With': 'XMLHttpRequest',
        };
        if (csrfToken) headers['X-CSRF-TOKEN'] = csrfToken;
        const response = await fetch(sameOriginUrl(MODIFY_ENDPOINT), {
          method: 'POST', credentials: 'same-origin', headers,
          body: serializeSuccessfulControls(attribute.form).toString(),
        });
        const responseText = await response.text();
        let payload;
        try { payload = JSON.parse(responseText); }
        catch (error) { throw new Error(`题湖提交接口未返回 JSON：${error.message}`); }
        if (!response.ok || Number(payload?.status) !== 200) {
          throw new Error(normalizeWhitespace(payload?.text) || `题湖提交失败（HTTP ${response.status}）`);
        }
      }

      const verified = await fetchAttribute(question);
      if (String(verified.currentCatalogueId) !== target.id) throw new Error('提交后回读的目录与建议目录不一致');
      let manualPersistenceError = '';
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
          // 题湖已完成回读确认；本地审计失败不能把真实提交误报为失败。
          manualPersistenceError = error.message;
        }
      }
      state.cards.set(exerciseId, { ...question, currentCatalogueId: target.id });
      state.results.set(exerciseId, {
        ...result,
        accepted: true,
        website_catalogue_id: target.id,
        acceptance_mapping_error: '',
        manual_override: isManual
          ? { ...result.manual_override, pending: false, accepted_at: new Date().toISOString() }
          : result.manual_override,
      });
      if (manualPersistenceError) {
        setStatus(`题目 ${exerciseId} 已写入题湖，但人工修正记录保存失败：${manualPersistenceError}`, true);
      } else {
        setStatus(`题目 ${exerciseId} 已采纳建议并写入题湖可视化分类。`);
      }
    } catch (error) {
      setStatus(`题目 ${exerciseId} 采纳失败：${error.message}`, true);
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
      .${BADGE_CLASS} .badge-input { grid-column: 1 / -1; margin-top: 2px; border-top: 1px dashed currentColor; padding-top: 6px; color: #667a73; }
      .${BADGE_CLASS} .badge-input summary { cursor: pointer; font-size: 11px; font-weight: 650; }
      .${BADGE_CLASS} .badge-input-content { display: grid; gap: 5px; margin-top: 7px; padding: 7px 8px; border-radius: 7px; background: rgb(255 255 255 / .62); color: #496059; font-size: 11px; overflow-wrap: anywhere; }
      .${BADGE_CLASS} .badge-input-content p { margin: 0; }
      .${BADGE_CLASS}.is-review .badge-path { color: #6c4b1d; }
      .${BADGE_CLASS}.is-review .badge-meta { color: #8a6a37; }
      .${BADGE_CLASS}.is-accepted .badge-path { color: #245d3c; }
      .${BADGE_CLASS} .badge-action { grid-area: action; align-self: start; min-height: 30px; padding: 5px 10px; border: 1px solid #126b5c; border-radius: 8px; background: #126b5c; color: #fff; font: 600 12px/1.2 "Segoe UI", "Microsoft YaHei", sans-serif; white-space: nowrap; cursor: pointer; }
      .${BADGE_CLASS} .badge-action:hover { background: #0c5649; border-color: #0c5649; }
      .${BADGE_CLASS} .badge-action:disabled { border-color: #a9bbb5; background: #a9bbb5; cursor: default; }
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
    return ({ queued: '建立本地作业', routing: '判断最晚必备专题', classifying: '专题内目录分类', completed: '整理结果' })[stage] || '分类';
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

  async function waitForClassificationJob(initialJob, cardById) {
    let job = initialJob;
    state.classificationJobId = job.job_id || '';
    while (true) {
      applyClassificationJobSnapshot(job, cardById);
      const failed = Array.isArray(job.failed_exercise_ids) ? job.failed_exercise_ids.length : 0;
      if (job.status === 'completed') return job;
      if (job.status === 'failed') throw new Error(job.error || '本地分类作业意外停止');
      setStatus(`正在${classificationStageLabel(job.stage)}：${job.completed || 0}/${job.total || 0} 道题已完成${failed ? `，${failed} 道请求失败` : ''}`);
      await delay(CLASSIFICATION_JOB_POLL_INTERVAL_MS);
      job = await request('GET', `/api/v1/classification-jobs/${encodeURIComponent(state.classificationJobId)}`);
    }
  }

  async function classifyPage() {
    if (state.busy) return;
    // 新分类优先级最高；丢弃尚未完成的旧缓存恢复结果。
    state.cacheRestoreId += 1;
    setBusy(true);
    state.results.clear();
    try {
      const cards = collectCards();
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
      const initialJob = await request('POST', '/api/v1/classification-jobs', { questions });
      const completedJob = await waitForClassificationJob(initialJob, cardById);
      const attributeFailed = cards.length - questions.length;
      const classificationFailed = Array.isArray(completedJob.failed_exercise_ids)
        ? completedJob.failed_exercise_ids.length : 0;
      setStatus(`识别完成：${questions.length} 道已处理${attributeFailed ? `，${attributeFailed} 道属性读取失败` : ''}${classificationFailed ? `，${classificationFailed} 道云端请求失败` : ''}。请先复核结果。`);
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
      const exerciseIds = [...new Set(cards.map(card => card.exerciseId))];
      if (!window.confirm(`将清除当前页 ${exerciseIds.length} 道题目的本地分类缓存。下次识别会重新调用云端模型。确定继续吗？`)) return;
      state.cacheRestoreId += 1;
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
    if (!window.confirm('将提交当前 JSONL 到云端模型并产生费用。确定继续吗？')) return;
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
    if (event.key !== 'Escape') return;
    if (!elements.settingsDrawer.hidden) {
      closeSettings();
      elements.settings.focus({ preventScroll: true });
    }
    else if (elements.panel.classList.contains('open')) setOpen(false);
  });
  elements.saveCloud.addEventListener('click', saveCloudSettings);
  elements.classify.addEventListener('click', classifyPage);
  elements.clearCache.addEventListener('click', clearCurrentPageCache);
  elements.exportBatch.addEventListener('click', exportBatch);
  elements.submitBatch.addEventListener('click', submitBatch);
  elements.syncBatch.addEventListener('click', syncBatch);
  (async () => {
    setBusy(false);
    // 刷新页面后自动读取服务端已持久化的 settings.local.yaml 配置和当前页面目录。
    await connectService();
  })().catch(error => setStatus(`无法自动连接本地服务：${error.message}`, true));
})();
