/* 导师实时互选系统 — 共享前端组件
   提供：转义、请求封装、Toast、模态框、确认框、抽屉、防重复提交、IME 安全搜索。 */
(function () {
  'use strict';

  var $ = function (s) { return document.querySelector(s); };
  var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };

  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };

  var csrfMeta = document.querySelector('meta[name=csrf-token]');
  var csrf = csrfMeta ? csrfMeta.content : '';
  var sessionExpired = false;

  /* 登录失效只处理一次：通知页面停止轮询，再跳回登录页。 */
  function handleSessionExpired() {
    if (sessionExpired) return;
    sessionExpired = true;
    window.dispatchEvent(new CustomEvent('ui:session-expired'));
    window.setTimeout(function () {
      window.location.replace('/login?expired=1');
    }, 50);
  }

  /* 弹层打开时补偿浏览器滚动条宽度，避免页面横向跳动。 */
  var scrollLockCount = 0;
  var savedBodyPaddingRight = '';

  function lockPageScroll() {
    scrollLockCount += 1;
    if (scrollLockCount > 1) return;
    var body = document.body;
    var scrollbarWidth = Math.max(0, window.innerWidth - document.documentElement.clientWidth);
    savedBodyPaddingRight = body.style.paddingRight;
    if (scrollbarWidth) {
      var currentPadding = parseFloat(window.getComputedStyle(body).paddingRight) || 0;
      body.style.paddingRight = (currentPadding + scrollbarWidth) + 'px';
    }
    body.classList.add('modal-open');
  }

  function unlockPageScroll() {
    if (!scrollLockCount) return;
    scrollLockCount -= 1;
    if (scrollLockCount) return;
    document.body.classList.remove('modal-open');
    document.body.style.paddingRight = savedBodyPaddingRight;
  }

  async function api(url, opt) {
    opt = opt || {};
    var headers = Object.assign({ 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' }, opt.headers || {});
    var r = await fetch(url, Object.assign({}, opt, { headers: headers }));
    var j;
    try { j = await r.json(); } catch (_) { j = {}; }
    if (!r.ok) {
      var e = new Error(j.error || '请求失败，请稍后重试');
      e.data = j; e.status = r.status;
      if (r.status === 401) {
        e.sessionExpired = true;
        handleSessionExpired();
      }
      throw e;
    }
    return j;
  }

  /* ---------- Toast ---------- */
  var toastTimer = null;
  function toast(msg, type) {
    type = type || 'success';
    if (sessionExpired) return;
    var el = $('#toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'toast'; el.className = 'toast';
      document.body.appendChild(el);
    }
    var icon = type === 'error' ? '✕' : type === 'info' ? 'ⓘ' : '✓';
    el.className = 'toast t-' + type;
    el.innerHTML = '<span class="t-icon">' + icon + '</span><span>' + esc(msg) + '</span>';
    void el.offsetWidth;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove('show'); }, 3000);
  }

  /* ---------- 模态框 ---------- */
  function openModal(html, opts) {
    opts = opts || {};
    var mask = $('#modalMask');
    if (!mask) return;
    var box = $('#modalBox');
    box.className = 'modal' + (opts.size ? ' modal-' + opts.size : '');
    box.innerHTML = html;
    var wasOpen = mask.classList.contains('show');
    mask.classList.add('show');
    if (!wasOpen) lockPageScroll();
    if (opts.onClose) mask._onClose = opts.onClose;
    var first = box.querySelector('input, select, textarea, button');
    if (first) setTimeout(function () { first.focus(); }, 60);
  }

  function closeModal() {
    var mask = $('#modalMask');
    if (!mask || !mask.classList.contains('show')) return;
    mask.classList.remove('show');
    unlockPageScroll();
    if (mask._onClose) { var fn = mask._onClose; mask._onClose = null; fn(); }
  }

  /* ---------- 确认框（返回 Promise<boolean>） ---------- */
  function confirm(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var mask = document.createElement('div');
      mask.className = 'confirm-mask show';
      var danger = !!opts.danger;
      var iconHtml = danger
        ? '<div class="cf-icon cf-danger">!</div>'
        : '<div class="cf-icon cf-warning">?</div>';
      mask.innerHTML =
        '<div class="confirm-box" role="dialog" aria-modal="true" aria-label="' + esc(opts.title || '请确认') + '">' +
          '<div class="cf-head">' + iconHtml +
            '<div class="cf-body">' +
              '<div class="cf-title">' + esc(opts.title || '请确认') + '</div>' +
              (opts.message ? '<div class="cf-msg">' + esc(opts.message) + '</div>' : '') +
            '</div>' +
          '</div>' +
          '<div class="cf-foot">' +
            '<button class="btn btn-default" type="button" data-cf="cancel">' + esc(opts.cancelText || '取消') + '</button>' +
            '<button class="btn ' + (danger ? 'btn-danger' : 'btn-primary') + '" type="button" data-cf="ok">' + esc(opts.okText || '确定') + '</button>' +
          '</div>' +
        '</div>';
      document.body.appendChild(mask);
      lockPageScroll();
      var done = function (val) {
        document.removeEventListener('keydown', keyHandler);
        mask.remove();
        unlockPageScroll();
        resolve(val);
      };
      var keyHandler = function (e) { if (e.key === 'Escape') done(false); };
      mask.addEventListener('click', function (e) {
        if (e.target === mask) done(false);
      });
      mask.querySelector('[data-cf=cancel]').addEventListener('click', function () { done(false); });
      mask.querySelector('[data-cf=ok]').addEventListener('click', function () { done(true); });
      document.addEventListener('keydown', keyHandler);
      var okBtn = mask.querySelector('[data-cf=ok]');
      setTimeout(function () { okBtn && okBtn.focus(); }, 30);
    });
  }

  /* ---------- 抽屉（长文本查看，不刷新页面） ---------- */
  function openDrawer(opts) {
    opts = opts || {};
    var mask = $('#drawerMask');
    if (!mask) {
      mask = document.createElement('div');
      mask.className = 'drawer-mask';
      mask.id = 'drawerMask';
      document.body.appendChild(mask);
    }
    mask.innerHTML =
      '<div class="drawer" role="dialog" aria-modal="true">' +
        '<div class="drawer-head">' +
          '<div><h3>' + esc(opts.title || '') + '</h3>' +
          (opts.meta ? '<div class="dh-meta">' + esc(opts.meta) + '</div>' : '') + '</div>' +
          '<button class="modal-close" type="button" data-drawer-close aria-label="关闭">✕</button>' +
        '</div>' +
        '<div class="drawer-body">' + (opts.body || '<p class="empty">暂无内容</p>') + '</div>' +
        '<div class="drawer-foot">' +
          (opts.foot || '<button class="btn btn-default" type="button" data-drawer-close>关闭</button>') +
        '</div>' +
      '</div>';
    var wasOpen = mask.classList.contains('show');
    mask.classList.add('show');
    if (!wasOpen) lockPageScroll();
    mask.addEventListener('click', function (e) {
      if (e.target === mask || e.target.closest('[data-drawer-close]')) closeDrawer();
    });
  }

  function closeDrawer() {
    var mask = $('#drawerMask');
    if (mask && mask.classList.contains('show')) {
      mask.classList.remove('show');
      unlockPageScroll();
    }
  }

  function hasOverlay() {
    return !!(document.querySelector('.modal-mask.show') ||
              document.querySelector('.confirm-mask.show') ||
              document.querySelector('.drawer-mask.show'));
  }

  /* ---------- 防重复提交 ---------- */
  function busy(btn, on) {
    if (!btn) return;
    if (on) {
      if (btn.dataset._label === undefined) btn.dataset._label = btn.innerHTML;
      btn.classList.add('is-busy');
      btn.setAttribute('disabled', '');
      btn.innerHTML = '<span class="spinner"></span><span>处理中…</span>';
    } else {
      btn.classList.remove('is-busy');
      btn.removeAttribute('disabled');
      if (btn.dataset._label !== undefined) btn.innerHTML = btn.dataset._label;
    }
  }

  async function withBusy(btn, fn) {
    busy(btn, true);
    try { return await fn(); }
    finally { busy(btn, false); }
  }

  /* ---------- 工具 ---------- */
  function debounce(fn, ms) {
    var t = null;
    var wrapped = function () {
      var args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { t = null; fn.apply(self, args); }, ms);
      wrapped._t = t;
    };
    return wrapped;
  }

  /*
   * 可控轮询：前台按正常频率刷新，后台降低频率；恢复前台时立即刷新。
   * 使用递归 setTimeout，确保上一次请求完成后才安排下一次，避免请求堆叠。
   */
  function startPolling(fn, opts) {
    opts = opts || {};
    var activeMs = opts.activeMs || 7000;
    var hiddenMs = opts.hiddenMs || 30000;
    var timer = null;
    var stopped = false;

    function clearTimer() {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    }

    function schedule(delay) {
      clearTimer();
      if (stopped || sessionExpired) return;
      timer = window.setTimeout(tick, delay);
    }

    async function tick() {
      timer = null;
      if (stopped || sessionExpired) return;
      try { await fn(); }
      finally {
        schedule(document.hidden ? hiddenMs : activeMs);
      }
    }

    function onVisibilityChange() {
      if (stopped || sessionExpired) return;
      schedule(document.hidden ? hiddenMs : 0);
    }

    function stop() {
      if (stopped) return;
      stopped = true;
      clearTimer();
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('ui:session-expired', stop);
      window.removeEventListener('pagehide', stop);
    }

    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('ui:session-expired', stop);
    window.addEventListener('pagehide', stop);
    schedule(opts.immediate === false ? activeMs : 0);
    return stop;
  }

  /* IME 安全搜索：中文输入法组词期间不触发，回车直接应用 */
  function bindSearch(input, opts) {
    opts = opts || {};
    var ms = opts.debounceMs || 400;
    var composing = false;
    var apply = debounce(function () { if (!composing && opts.onApply) opts.onApply(); }, ms);
    input.addEventListener('compositionstart', function () { composing = true; });
    input.addEventListener('compositionend', function () { composing = false; apply(); });
    input.addEventListener('input', function (e) { if (!e.isComposing && !composing) apply(); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.isComposing && !composing) {
        clearTimeout(apply._t);
        if (opts.onEnter) opts.onEnter(); else if (opts.onApply) opts.onApply();
      }
    });
  }

  /* 申请状态：文案与标签样式（全站语义一致） */
  var REQUEST_STATUS = {
    pending: '待导师审核',
    accepted: '已配对',
    rejected: '导师未同意',
    student_cancelled: '已撤回',
    auto_cancelled: '系统自动退回',
    admin_cancelled: '管理员已调整'
  };
  var REQUEST_TAG = {
    pending: 'tag-orange',
    accepted: 'tag-green',
    rejected: 'tag-red',
    student_cancelled: 'tag-gray',
    auto_cancelled: 'tag-orange',
    admin_cancelled: 'tag-purple'
  };
  var CANCEL_REASON_TEXT = {
    changed_selection: '更换导师后撤回',
    mentor_withdrew_pairing: '导师撤回确认',
    admin_adjusted: '管理员调整配对',
    auto_full: '导师满额自动退回',
    mentor_full: '导师名额已满',
    xueshu_limit: '导师学硕录取已达3人上限'
  };

  function statusLabel(status) { return REQUEST_STATUS[status] || status || '—'; }

  function statusTag(status, extraText) {
    var text = extraText || REQUEST_STATUS[status] || status || '—';
    return '<span class="tag ' + (REQUEST_TAG[status] || 'tag-gray') + '">' + esc(text) + '</span>';
  }

  function cancelReasonText(reason) { return CANCEL_REASON_TEXT[reason] || ''; }

  function sourceTag(source) {
    return source === 'manual'
      ? '<span class="tag tag-purple">管理员安排</span>'
      : '<span class="tag tag-blue">学生申请</span>';
  }

  window.UI = {
    $: $, $$: $$, esc: esc, api: api, csrf: csrf,
    toast: toast, openModal: openModal, closeModal: closeModal,
    confirm: confirm, openDrawer: openDrawer, closeDrawer: closeDrawer,
    hasOverlay: hasOverlay, busy: busy, withBusy: withBusy,
    debounce: debounce, bindSearch: bindSearch, startPolling: startPolling,
    statusLabel: statusLabel, statusTag: statusTag,
    cancelReasonText: cancelReasonText, sourceTag: sourceTag
  };
})();
