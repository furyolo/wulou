// ==UserScript==
// @name         无漏 AI · 当前专题教师版 PDF 下载
// @namespace    wulou-teacher-pdf-downloader
// @version      1.1.0
// @description  下载当前专题下全部配套教师版 PDF，保存到同名文件夹。
// @match        https://www.wulouai.com/user-center/course-wrong-learn/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(() => {
  'use strict';

  function safeName(value) {
    const name = String(value).normalize('NFC').replace(/[<>:"/\\|?*\x00-\x1f]/g, '-').replace(/[. ]+$/g, '').trim();
    if (!name || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(name)) return `资料-${name || '未命名'}`;
    return name;
  }

  // 服务器把中文 UTF-8 字节直接放在响应头里，Fetch 会按 Latin-1 读取。
  function responseName(header, fallback) {
    const extended = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header || '');
    let name;
    if (extended) {
      try { name = decodeURIComponent(extended[1].trim()); }
      catch (error) { console.warn('[无漏 PDF] 文件名编码无效', error); }
    }
    if (!name) {
      const match = /filename\s*=\s*(?:"([^"]+)"|([^;]+))/i.exec(header || '');
      name = match ? (match[1] || match[2]).trim() : fallback;
      if ([...name].every(c => c.charCodeAt(0) <= 255)) {
        try { name = new TextDecoder('utf-8', { fatal: true }).decode(Uint8Array.from(name, c => c.charCodeAt(0))); }
        catch (error) { console.warn('[无漏 PDF] 保留原始文件名', error); }
      }
    }
    name = safeName(name);
    return /\.pdf$/i.test(name) ? name : `${name}.pdf`;
  }

  function sameSite(raw, base) {
    const url = new URL(raw, base);
    if (url.origin !== 'https://www.wulouai.com') throw new Error('下载链接不属于无漏网站');
    return url;
  }

  // 顶层 li 就是用户定义的“二级目录”：专题或题型，而非其下的 6.1 等小节。
  function collect(doc, currentUrl) {
    const root = doc.querySelector('.learn_catalogue_div .cy_v_left > ul');
    if (!root) throw new Error('未找到学习目录，请进入讲义的具体子项页面');
    const current = new URL(currentUrl);
    const link = [...root.querySelectorAll('a[href]')].find(a => {
      const raw = a.getAttribute('href');
      return raw && !raw.startsWith('javascript:') && new URL(raw, current).pathname === current.pathname;
    });
    if (!link) throw new Error('无法在学习目录中定位当前子项');
    let topic = link.closest('li');
    while (topic && topic.parentElement !== root) topic = topic.parentElement.closest('li');
    if (!topic) throw new Error('无法确定当前专题，已停止以避免扩大范围');
    const name = topic.querySelector(':scope > a')?.textContent.trim();
    if (!name) throw new Error('专题名称为空');
    const seen = new Set();
    const items = [];
    // 侧栏下载图标并不完整：压轴题没有图标，但学习页仍提供教师版按钮。
    // 因此遍历本专题的学习链接，并在下载阶段逐页核验真实教师版入口。
    for (const page of topic.querySelectorAll('a[href]')) {
      const raw = page.getAttribute('href');
      if (!raw || raw.startsWith('javascript:')) continue;
      const url = new URL(raw, current);
      const match = /^\/user-center\/course-wrong-learn\/(\d+)\/\d+\/?$/.exec(url.pathname);
      if (!match) continue;
      sameSite(url.href, current);
      const id = match[1];
      if (seen.has(id)) continue;
      seen.add(id);
      items.push({ id, title: page.textContent.trim(), page: url.href });
    }
    if (!items.length) throw new Error('当前专题没有可识别的学习子项');
    return { name, folder: safeName(name), items };
  }

  function teacherUrl(doc, page) {
    const button = [...doc.querySelectorAll('button.catalogue_pdf_click[data-url]')].find(b => /PDF.*教师版/.test(b.textContent));
    if (!button) throw new Error('该子项没有教师版 PDF 按钮，或登录已失效');
    const url = sameSite(button.dataset.url, page);
    if (url.pathname !== '/user-center/download-catalogue-pdf' || url.searchParams.get('create_type') !== '2' || url.searchParams.get('create_file') !== '1') {
      throw new Error('教师版下载参数发生变化，请检查网站');
    }
    return url.href;
  }

  async function fetchChecked(url, signal) {
    const response = await fetch(url, { credentials: 'same-origin', signal, redirect: 'follow' });
    if (!response.ok) throw new Error(`服务器返回 HTTP ${response.status}`);
    if (new URL(response.url).pathname.startsWith('/login')) throw new Error('登录已失效，请重新登录');
    return response;
  }

  async function validPdf(blob) {
    return (await blob.slice(0, 5).text()) === '%PDF-';
  }

  // 只创建未占用的文件名；原文件以及同名目录均不覆盖。
  async function unusedName(directory, name) {
    const existing = new Set();
    for await (const key of directory.keys()) existing.add(key.toLocaleLowerCase());
    let candidate = name;
    let n = 2;
    while (existing.has(candidate.toLocaleLowerCase())) candidate = name.replace(/\.pdf$/i, ` (${n++}).pdf`);
    return candidate;
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { safeName, responseName, collect, teacherUrl, validPdf, unusedName };
    return;
  }
  if (window.top !== window.self || document.getElementById('wulou-pdf-panel')) return;
  const host = document.createElement('div');
  host.id = 'wulou-pdf-panel';
  // 隔离网站的 Bootstrap、按钮和字体样式，避免互相污染。
  const shadow = host.attachShadow({ mode: 'open' });
  shadow.innerHTML = `
    <style>
      :host { all: initial; color-scheme: light; font: 14px/1.6 "Segoe UI", "Microsoft YaHei", sans-serif; color: #243330; }
      * { box-sizing: border-box; }
      button, summary { -webkit-tap-highlight-color: transparent; }
      button { font: inherit; cursor: pointer; border: 1px solid #dce3e0; background: #fff; color: #243330; border-radius: 10px; min-height: 44px; padding: 10px 14px; transition: background .16s, border-color .16s; }
      button:hover { background: #f1f5f3; border-color: #aabbb4; }
      button:focus-visible, summary:focus-visible { outline: 3px solid #367b69; outline-offset: 3px; }
      button:disabled { cursor: default; opacity: .5; }
      [hidden] { display: none !important; }
      .tab { position: fixed; right: 0; top: 46%; z-index: 2147483000; border-radius: 12px 0 0 12px; padding: 13px 10px; min-width: 48px; display: grid; justify-items: center; gap: 2px; box-shadow: 0 3px 18px #193b2514; }
      .arrow { font: 24px/1 sans-serif; }
      .tab-label { font-size: 11px; font-weight: 700; letter-spacing: .04em; }
      .badge { color: #17634f; font-size: 11px; font-weight: 600; }
      .panel { position: fixed; z-index: 2147483001; right: 16px; top: 50%; width: min(352px, calc(100vw - 32px)); max-height: calc(100dvh - 32px); overflow-y: auto; overscroll-behavior: contain; padding: 24px; background: #fff; border: 1px solid #e1e7e4; border-radius: 18px; box-shadow: 0 16px 56px #203a3026, 0 2px 8px #203a300a; transform: translate(24px, -50%); opacity: 0; visibility: hidden; pointer-events: none; transition: transform .2s ease, opacity .2s ease, visibility .2s; }
      .panel.open { transform: translate(0, -50%); opacity: 1; visibility: visible; pointer-events: auto; }
      header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 22px; }
      h2 { font-size: 18px; letter-spacing: -.02em; line-height: 1.4; margin: 0; font-weight: 650; }
      .close { padding: 4px; width: 44px; border: 0; background: #f3f6f4; font-size: 26px; line-height: 1; }
      .topic { font-size: 15px; font-weight: 600; line-height: 1.65; overflow-wrap: anywhere; }
      .meta, .hint { font-size: 12px; color: #62716b; }
      .meta { margin-top: 5px; }
      .status { font-size: 13px; margin-top: 18px; overflow-wrap: anywhere; }
      progress { display: block; width: 100%; height: 5px; border: 0; border-radius: 9px; overflow: hidden; margin: 12px 0 0; accent-color: #17634f; }
      progress::-webkit-progress-bar { background: #eaf0ed; }
      progress::-webkit-progress-value { background: #17634f; }
      .actions { display: flex; gap: 8px; margin-top: 20px; }
      .primary { flex: 1; white-space: nowrap; background: #17634f; border-color: #17634f; color: #fff; font-weight: 600; }
      .primary:hover { background: #104b3c; border-color: #104b3c; }
      .hint { margin: 10px 0 0; }
      details { margin-top: 20px; border-top: 1px solid #e8edea; padding-top: 14px; }
      summary { cursor: pointer; color: #52635b; font-size: 12px; padding: 4px 0; }
      pre { max-height: 160px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.8 "Segoe UI", "Microsoft YaHei", sans-serif; color: #52635b; margin: 12px 0 0; }
      pre:empty::before { content: '下载记录将显示在这里'; color: #62716b; }
      @media (max-width: 480px) { .panel { padding: 20px; right: 12px; width: calc(100vw - 24px); } }
      @media (prefers-reduced-motion: reduce) { *, .panel { transition: none; } }
    </style>
    <button class="tab" aria-label="展开教师版 PDF 下载面板" aria-expanded="false" aria-controls="pdf-drawer" title="教师版 PDF 下载">
      <span class="arrow" aria-hidden="true">‹</span><span class="tab-label">PDF</span><span class="badge" hidden></span>
    </button>
    <section class="panel" id="pdf-drawer" role="region" aria-labelledby="pdf-heading" inert>
      <header><h2 id="pdf-heading">教师版 PDF</h2><button class="close" aria-label="收起下载面板" title="收起 · Esc">›</button></header>
      <div class="topic">当前专题</div><div class="meta">按专题自动归档</div>
      <div class="status" role="status" aria-live="polite">正在识别目录…</div>
      <progress value="0" max="1" aria-label="已处理子项" hidden></progress>
      <div class="actions"><button class="primary">选择文件夹并下载</button><button class="stop" disabled hidden>停止</button></div>
      <p class="hint">选择归档根目录，自动建立专题文件夹。</p>
      <details><summary>下载记录</summary><pre></pre></details>
    </section>`;
  document.body.append(host);
  const panel = shadow.querySelector('.panel');
  const tab = shadow.querySelector('.tab');
  const close = shadow.querySelector('.close');
  const status = shadow.querySelector('.status');
  const start = shadow.querySelector('.primary');
  const stop = shadow.querySelector('.stop');
  const log = shadow.querySelector('pre');
  const progress = shadow.querySelector('progress');
  const badge = shadow.querySelector('.badge');
  const setOpen = open => {
    panel.classList.toggle('open', open);
    panel.inert = !open;
    tab.hidden = open;
    tab.setAttribute('aria-expanded', String(open));
    (open ? close : tab).focus({ preventScroll: true });
  };
  tab.onclick = () => setOpen(true);
  close.onclick = () => setOpen(false);
  // 只处理面板内的 Esc，不抢占网页原有快捷键；收起不会中止任务。
  shadow.addEventListener('keydown', event => {
    if (event.key === 'Escape' && panel.classList.contains('open')) {
      event.stopPropagation();
      setOpen(false);
    }
  });
  let controller;
  let cancelled = false;
  try {
    const plan = collect(document, location.href);
    shadow.querySelector('.topic').textContent = plan.name;
    shadow.querySelector('.meta').textContent = `${plan.items.length} 个子项 · 按专题自动归档`;
    status.textContent = '准备就绪';
    if (!window.showDirectoryPicker) throw new Error('请使用支持文件夹选择的 Chrome 或 Edge 浏览器');
  } catch (error) {
    status.textContent = error.message;
    badge.hidden = false;
    badge.textContent = '!';
    start.disabled = true;
    console.error('[无漏 PDF] 初始化失败', error);
  }
  stop.onclick = () => {
    cancelled = true;
    controller?.abort();
    stop.disabled = true;
    status.textContent = '正在停止，已保存文件会保留';
  };
  start.onclick = async () => {
    let success = 0;
    let failed = 0;
    start.disabled = true;
    cancelled = false;
    log.textContent = '';
    progress.hidden = true;
    badge.hidden = false;
    badge.textContent = '…';
    try {
      const plan = collect(document, location.href);
      // 必须在用户点击期间调用；不能在网络请求之后才打开选择框。
      const root = await window.showDirectoryPicker({ id: 'wulou-pdf-root', mode: 'readwrite' });
      const directory = await root.getDirectoryHandle(plan.folder, { create: true });
      stop.disabled = false;
      stop.hidden = false;
      progress.max = plan.items.length;
      progress.value = 0;
      progress.hidden = false;
      console.info('[无漏 PDF] 开始', { topic: plan.name, count: plan.items.length, root: root.name });
      for (const [index, item] of plan.items.entries()) {
        if (cancelled) break;
        controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 120000);
        status.textContent = `${index + 1}/${plan.items.length} · ${item.title}`;
        badge.textContent = `${index + 1}/${plan.items.length}`;
        try {
          const page = await fetchChecked(item.page, controller.signal);
          const doc = new DOMParser().parseFromString(await page.text(), 'text/html');
          const url = teacherUrl(doc, item.page);
          if (new URL(url).searchParams.get('catalogue_id') !== item.id) throw new Error('返回页面与目标目录项不一致');
          const response = await fetchChecked(url, controller.signal);
          const blob = await response.blob();
          if (!await validPdf(blob)) throw new Error('服务器未返回 PDF，可能是权限提示或生成失败');
          if (cancelled) break;
          const name = await unusedName(directory, responseName(response.headers.get('content-disposition'), `${item.title}-${item.id}(老师版).pdf`));
          const file = await directory.getFileHandle(name, { create: true });
          const writer = await file.createWritable();
          try { await writer.write(blob); await writer.close(); }
          catch (error) {
            try { await writer.abort(); }
            catch (abortError) { console.error('[无漏 PDF] 中止写入失败', abortError); }
            throw error;
          }
          success++;
          log.textContent += `成功：${name}\n`;
        } catch (error) {
          if (cancelled) break;
          failed++;
          log.textContent += `失败：${item.title}：${error.name === 'AbortError' ? '请求超时' : error.message}\n`;
          console.error('[无漏 PDF] 下载失败', { id: item.id, error });
        } finally { clearTimeout(timer); progress.value = success + failed; }
        if (!cancelled) await new Promise(resolve => setTimeout(resolve, 800));
      }
      status.textContent = `${cancelled ? '已停止' : '已结束'}：成功 ${success}，失败 ${failed}，未处理 ${plan.items.length - success - failed}。保存到 ${root.name}/${plan.folder}`;
      badge.textContent = cancelled ? '已停' : failed ? '注意' : '完成';
      console.info('[无漏 PDF] 结束', { success, failed, cancelled });
    } catch (error) {
      status.textContent = error.name === 'AbortError' ? '已取消文件夹选择' : `无法开始：${error.message}`;
      badge.textContent = error.name === 'AbortError' ? '' : '!';
      badge.hidden = error.name === 'AbortError';
      console.error('[无漏 PDF] 任务未完成', error);
    } finally {
      controller = undefined;
      start.disabled = false;
      stop.disabled = true;
      stop.hidden = true;
    }
  };
})();
