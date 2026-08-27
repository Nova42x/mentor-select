/* 当前轮次流程进度：学生端、导师端、管理员端共用。 */
(function () {
  window.flowProgress = function (info) {
    const round = Number(info?.round || 1);
    const settled = Boolean(info?.settled);
    const phase = info?.phase || 'closed';
    const activeIndex = settled ? -1 : (phase === 'submit' ? 0 : phase === 'review' ? 1 : -1);
    const doneCount = settled ? 3 : (phase === 'review' ? 1 : 0);
    const statusText = settled ? '本轮已完成' : phase === 'submit' ? '正在填报志愿' :
      phase === 'review' ? '导师正在审核' : '流程暂未开放';
    const steps = [
      ['学生填报志愿', round === 1 ? '提交和修改志愿' : '未匹配学生补选'],
      ['导师选择', '导师确认拟接收学生'],
      ['查看结果', '结算后查看匹配结果'],
    ];
    const nodes = steps.map((step, index) => {
      const state = index < doneCount ? 'done' : index === activeIndex ? 'active' : 'waiting';
      const stateText = state === 'done' ? '已完成' : state === 'active' ? '进行中' : '等待开始';
      return `<div class="flow-step ${state}" ${state === 'active' ? 'aria-current="step"' : ''}>
        <span class="flow-dot">${state === 'done' ? '✓' : index + 1}</span>
        <span class="flow-copy"><b>${step[0]}</b><small>${step[1]} · ${stateText}</small></span>
      </div>`;
    });
    return `<div class="flow-progress" aria-label="第${round}轮流程进度">
      <div class="flow-progress-head"><span>第 ${round} 轮流程进度</span><b class="${settled ? 'is-done' : ''}">${statusText}</b></div>
      <div class="flow-steps">
        <i class="flow-line segment-1 ${doneCount >= 1 ? 'done' : ''}"></i>
        <i class="flow-line segment-2 ${doneCount >= 2 ? 'done' : ''}"></i>
        ${nodes.join('')}
      </div>
    </div>`;
  };
})();
