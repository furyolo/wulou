// 在浏览器执行；调用时传入油猴脚本导出的 collect 和 teacherUrl。
function runBrowserRegression(api) {
  const origin = 'https://www.wulouai.com';
  const page = id => `${origin}/user-center/course-wrong-learn/${id}/1`;
  const doc = new DOMParser().parseFromString(`
    <div class="learn_catalogue_div"><div class="cy_v_left"><ul>
      <li><a href="javascript:;">其他专题</a><ul><li><a href="${page(100)}">范围外</a></li></ul></li>
      <li><a href="javascript:;">题型二 解答拉分题（压轴）</a><ul>
        <li><a href="javascript:;">小节</a><ul>
          <li><a href="${page(201)}">操作型递进</a></li>
          <li><a href="${page(202)}">条件型递进</a><a href="${page(202)}">重复链接</a></li>
        </ul></li>
      </ul></li>
    </ul></div></div>`, 'text/html');
  const plan = api.collect(doc, page(201));
  if (plan.name !== '题型二 解答拉分题（压轴）' || plan.items.map(x => x.id).join() !== '201,202') {
    throw new Error('无侧栏 PDF 图标的子项识别、去重或专题隔离失败');
  }
  const buttons = new DOMParser().parseFromString(`
    <button class="catalogue_pdf_click" data-url="${origin}/user-center/download-catalogue-pdf?catalogue_id=201&create_type=1&create_file=1">下载配套PDF-学生版</button>
    <button class="catalogue_pdf_click" data-url="${origin}/user-center/download-catalogue-pdf?catalogue_id=201&create_type=2&create_file=1">下载配套PDF-教师版</button>`, 'text/html');
  if (new URL(api.teacherUrl(buttons, page(201))).searchParams.get('create_type') !== '2') throw new Error('误选学生版');
  return { passed: true, checks: ['无下载图标仍识别子项', '排除其他专题', '重复链接去重', '只选择教师版'] };
}
