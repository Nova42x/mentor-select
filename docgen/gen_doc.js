// 生成《研究生导师双选系统使用说明.docx》
const fs = require('fs');
const path = require('path');
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  WidthType, AlignmentType, HeadingLevel, BorderStyle, ShadingType, LevelFormat,
} = require('docx');

const FONT = '微软雅黑';
const OUT = path.join(__dirname, '..', '导师双选系统使用说明.docx');

// ---- 工具 ----
function h1(text) { return new Paragraph({ heading: HeadingLevel.HEADING_1, spacing: { before: 280, after: 120 }, children: [new TextRun({ text, font: FONT, bold: true, size: 30 })] }); }
function h2(text) { return new Paragraph({ heading: HeadingLevel.HEADING_2, spacing: { before: 200, after: 80 }, children: [new TextRun({ text, font: FONT, bold: true, size: 25 })] }); }
function p(text, opts = {}) {
  return new Paragraph({ spacing: { after: 80 }, children: [new TextRun({ text, font: FONT, size: 21, ...opts })] });
}
function bullet(text) {
  return new Paragraph({ numbering: { reference: 'list', level: 0 }, spacing: { after: 60 }, children: [new TextRun({ text, font: FONT, size: 21 })] });
}
function numItem(text) {
  return new Paragraph({ numbering: { reference: 'steps', level: 0 }, spacing: { after: 60 }, children: [new TextRun({ text, font: FONT, size: 21 })] });
}
function cell(text, w, opts = {}) {
  return new TableCell({
    width: { size: w, type: WidthType.DXA },
    shading: opts.head ? { type: ShadingType.CLEAR, fill: 'E8EEF7' } : undefined,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    children: [new Paragraph({ children: [new TextRun({ text, font: FONT, size: 20, bold: !!opts.head })] })],
  });
}
function table(headers, rows, widths) {
  const total = widths.reduce((a, b) => a + b, 0);
  return new Table({
    columnWidths: widths,
    width: { size: total, type: WidthType.DXA },
    borders: {
      top: { style: BorderStyle.SINGLE, size: 4, color: '9CA3AF' },
      bottom: { style: BorderStyle.SINGLE, size: 4, color: '9CA3AF' },
      left: { style: BorderStyle.SINGLE, size: 4, color: '9CA3AF' },
      right: { style: BorderStyle.SINGLE, size: 4, color: '9CA3AF' },
      insideHorizontal: { style: BorderStyle.SINGLE, size: 4, color: 'D1D5DB' },
      insideVertical: { style: BorderStyle.SINGLE, size: 4, color: 'D1D5DB' },
    },
    rows: [
      new TableRow({ tableHeader: true, children: headers.map((t, i) => cell(t, widths[i], { head: true })) }),
      ...rows.map(r => new TableRow({ children: r.map((t, i) => cell(t, widths[i])) })),
    ],
  });
}

// ---- 文档内容 ----
const children = [];

children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 200, after: 100 }, children: [new TextRun({ text: '研究生导师双选系统', font: FONT, bold: true, size: 44 })] }));
children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 60 }, children: [new TextRun({ text: '使 用 说 明', font: FONT, bold: true, size: 32 })] }));
children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 300 }, children: [new TextRun({ text: '网页版双向互选 · 两轮流程 · 自动结算', font: FONT, size: 22, color: '6B7280' })] }));

// 一、系统概述
children.push(h1('一、系统概述'));
children.push(p('本系统是面向研究生导师与学生双向互选的网页应用：学生填报志愿 → 导师审核 → 系统按既定规则自动结算匹配，并支持第二轮互选与管理员手动调剂。系统部署于云服务器，浏览器直接访问即可使用，无需安装任何软件，手机、电脑均可。'));
children.push(bullet('技术栈：Python Flask + SQLite，轻量稳定、零成本'));
children.push(bullet('数据安全：数据落盘存储，服务重启不丢失'));
children.push(bullet('并发可靠：多名学生同时填报实测无冲突'));

// 二、访问与登录
children.push(h1('二、访问与登录'));
children.push(numItem('访问地址：http://< 服务器公网 IP >:8080 （首次使用需在云服务器控制台安全组放行 TCP 8080）'));
children.push(numItem('账号说明：'));
children.push(table(
  ['角色', '账号', '初始密码', '说明'],
  [
    ['管理员', 'admin', 'admin123', '首次登录后请立即修改密码'],
    ['导师', '工号（如 t001）', '123456', '由管理员创建或批量导入，可自行改密'],
    ['学生', '学号（如 2026001）', '123456', '可自助注册，也可由管理员批量导入'],
  ],
  [1400, 2400, 1400, 3200],
));
children.push(numItem('所有角色登录后均可在页面修改个人密码。'));

// 三、双选流程
children.push(h1('三、双选流程（每轮）'));
children.push(table(
  ['步骤', '操作人', '内容'],
  [
    ['第 1 步', '管理员', '开放填报阶段'],
    ['第 2 步', '学生', '选择 1-3 个志愿（第一志愿必填），填写个人简介'],
    ['第 3 步', '管理员', '开放审核阶段'],
    ['第 4 步', '导师', '查看申报学生，逐一选择"同意 / 拒绝 / 待定"'],
    ['第 5 步', '管理员', '点击"结算"，系统按规则自动匹配'],
    ['第 6 步', '全体', '公布结果；管理员可导出名单'],
    ['第二轮', '管理员', '未匹配学生与剩余名额导师再走一轮上述流程'],
    ['调剂', '管理员', '可随时手动改配，手动结果不被自动结算覆盖'],
  ],
  [1600, 1800, 5000],
));

// 四、结算规则
children.push(h1('四、结算规则（自动匹配原则）'));
children.push(numItem('志愿层级优先：先处理所有第一志愿，再第二、第三志愿'));
children.push(numItem('同一导师、同一志愿层级：同意 ＞ 待定/未审核 ＞ 拒绝'));
children.push(numItem('同一层级：按填报时间先后录取'));
children.push(numItem('名额满即止；导师"待定"的学生在名额有剩余时自动录取，"拒绝"即淘汰'));
children.push(numItem('管理员手动调剂（改配）的结果优先级最高，不会被后续自动结算覆盖'));

// 五、管理员
children.push(h1('五、管理员功能'));
children.push(bullet('概览面板：各导师名额、已匹配数、被填报数（超额红色高亮）'));
children.push(bullet('导师管理：新增、批量导入（CSV）、编辑、停用、删除；可上传"导师信息总表"供学生端下载（未上传时学生下载空白占位表）'));
children.push(bullet('学生管理：批量导入自动建号、删除、查看志愿与匹配情况'));
children.push(bullet('阶段控制：开放填报 → 开放审核 → 结算，可随时推进或回退'));
children.push(bullet('匹配结果：按导师 / 按学生双视角查看，支持手动调剂（区分轮次）'));
children.push(bullet('数据导出：最终互选名单、未匹配名单、导师名单（CSV 格式）'));

// 六、导师
children.push(h1('六、导师功能'));
children.push(bullet('查看申报自己的学生列表：个人简介、备注、志愿层级、联系方式'));
children.push(bullet('三态审核：同意 / 拒绝 / 待定（待定 = 名额有剩余时自动录取）'));
children.push(bullet('每轮可录取人数受名额限制，超出后无法再点"同意"'));

// 七、学生
children.push(h1('七、学生功能'));
children.push(bullet('自助注册（学号唯一）或使用管理员导入的账号登录'));
children.push(bullet('下载"导师信息总表"（Excel），查看导师研究方向与简介'));
children.push(bullet('填报 1-3 个志愿，开放期内可随时修改；满员导师不可选择'));
children.push(bullet('随时查看各志愿审核状态与最终匹配结果'));

// 八、部署与维护
children.push(h1('八、部署与维护（技术信息）'));
children.push(table(
  ['项目', '内容'],
  [
    ['服务器', '云服务器（如阿里云 ECS，Ubuntu 24.04）'],
    ['部署位置', '/opt/mentor-select，systemd 服务 mentor-select（开机自启）'],
    ['重启服务', 'systemctl restart mentor-select'],
    ['查看日志', 'journalctl -u mentor-select'],
    ['数据备份', '备份 /opt/mentor-select/data/ 目录即可备份全部数据'],
    ['监听端口', '8080（公网需安全组放行）'],
  ],
  [2200, 6200],
));

children.push(new Paragraph({ spacing: { before: 400 }, children: [new TextRun({ text: '—— 如有问题或功能调整需求，请与技术负责人联系 ——', font: FONT, size: 20, color: '9CA3AF' })] }));

const doc = new Document({
  styles: { default: { document: { run: { font: FONT, size: 21 } } } },
  numbering: {
    config: [
      { reference: 'list', levels: [{ level: 0, format: LevelFormat.BULLET, text: '•', alignment: AlignmentType.START }] },
      { reference: 'steps', levels: [{ level: 0, format: LevelFormat.DECIMAL, text: '%1.', alignment: AlignmentType.START }] },
    ],
  },
  sections: [{ properties: {}, children }],
});

Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync(OUT, buf);
  console.log('OK ->', OUT, buf.length, 'bytes');
});
