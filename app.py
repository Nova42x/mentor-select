# -*- coding: utf-8 -*-
"""研究生导师实时互选系统 — Flask 主程序。"""
import csv
import io
import json
import os
import re
import secrets
import functools
import threading
import time
from collections import defaultdict, deque
from urllib.parse import quote
from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, Response)
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_db, get_setting, set_setting, init_db, BASE_DIR
import options_service
from matching_service import (
    MatchingError, accept_request, reject_request, cancel_student_request,
    mentor_cancel_pairing, admin_set_pairing, pairing_count,
    is_xueshu_major, xueshu_limit_enabled, xueshu_pairing_count,
    cancel_pending_xueshu_at_limit, XUESHU_LIMIT,
)

try:
    import openpyxl
except ImportError:
    openpyxl = None

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '0') == '1',
    PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
)

if os.environ.get('TRUST_PROXY', '0') == '1':
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# 导师信息总表上传目录（学生端下载；未上传时提供空白占位表）
MENTOR_INFO_DIR = os.path.join(BASE_DIR, 'data', 'mentor_info')
ALLOWED_INFO_EXT = {'.xlsx', '.xls', '.csv'}

# 持久化 secret_key（重启不失效）
KEY_FILE = os.path.join(BASE_DIR, 'data', 'secret_key')
os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
if os.path.exists(KEY_FILE):
    with open(KEY_FILE, 'r') as f:
        app.secret_key = f.read().strip()
else:
    app.secret_key = secrets.token_hex(32)
    with open(KEY_FILE, 'w') as f:
        f.write(app.secret_key)
try:
    os.chmod(KEY_FILE, 0o600)
except OSError:
    pass

# ---------- 工具 ----------

def row2dict(row):
    return dict(row) if row else None


def parse_major_list(value):
    """规范化导师可招生专业；支持数组、换行及常见中英文分隔符。"""
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = re.split(r'[\r\n|,，、;；]+', str(value or ''))
    result = []
    for item in items:
        major = normalize_major(item)
        if major and major not in result:
            result.append(major)
    return result


def normalize_major(value):
    """移除来源表中的专业代码后缀，如“计算机技术(085404)”。"""
    return options_service.normalize_major(value)


# 临时导师账号：未取得正式工号时使用，通过环境变量 MENTOR_TEMP_ACCOUNTS 配置（逗号分隔）。
TEMP_MENTOR_ACCOUNTS = {x.strip() for x in os.environ.get('MENTOR_TEMP_ACCOUNTS', '').split(',') if x.strip()}


def valid_mentor_username(username):
    return bool(re.fullmatch(r'\d{7}', username or '') or username in TEMP_MENTOR_ACCOUNTS)


def student_degree_category(major):
    """按管理员配置的专业-学位类别映射推导；源学生表未单列学硕/专硕。"""
    return options_service.degree_category_for(major)


def mentor_allows_degree_category(admission_category, degree_category):
    """导师招生类别与学生学位类别匹配；旧数据缺少类别时由专业资格兜底。"""
    mentor_category = (admission_category or '').strip()
    if not mentor_category or not degree_category:
        return True
    if degree_category == '学硕':
        return '学硕' in mentor_category or '学术学位' in mentor_category
    if degree_category == '专硕':
        return '专硕' in mentor_category or '专业学位' in mentor_category
    return False


def mentor_major_names(conn, mentor_id):
    return [r['major'] for r in conn.execute(
        'SELECT major FROM mentor_majors WHERE mentor_id=? ORDER BY major',
        (mentor_id,),
    ).fetchall()]


def replace_mentor_majors(conn, mentor_id, majors):
    conn.execute('DELETE FROM mentor_majors WHERE mentor_id=?', (mentor_id,))
    for major in parse_major_list(majors):
        conn.execute('INSERT INTO mentor_majors(mentor_id,major) VALUES(?,?)',
                     (mentor_id, major))


def mentor_allows_major(conn, mentor_id, student_major):
    """已配置专业规则时严格匹配；旧导师未配置时兼容为不限专业。"""
    configured = conn.execute(
        'SELECT 1 FROM mentor_majors WHERE mentor_id=? LIMIT 1', (mentor_id,)
    ).fetchone()
    if not configured:
        return True
    return bool(conn.execute(
        'SELECT 1 FROM mentor_majors WHERE mentor_id=? AND major=?',
        (mentor_id, normalize_major(student_major)),
    ).fetchone())


def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(16)
    return session['_csrf']


app.jinja_env.globals['csrf_token'] = csrf_token


@app.before_request
def csrf_protect():
    if request.method in ('POST', 'PUT', 'DELETE'):
        token = request.headers.get('X-CSRF-Token') or request.form.get('_csrf')
        if not token or token != session.get('_csrf'):
            return jsonify({'error': 'CSRF校验失败，请刷新页面重试'}), 400


# 单进程登录失败限流：同一来源 10 分钟内最多连续失败 8 次。
_login_failures = defaultdict(deque)
_login_lock = threading.Lock()
LOGIN_WINDOW_SECONDS = 10 * 60
LOGIN_MAX_FAILURES = 8


def _login_ip():
    return request.remote_addr or 'unknown'


def _login_is_limited(ip):
    now = time.time()
    with _login_lock:
        attempts = _login_failures[ip]
        while attempts and attempts[0] < now - LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        return len(attempts) >= LOGIN_MAX_FAILURES


def _login_failed(ip):
    with _login_lock:
        _login_failures[ip].append(time.time())


def _login_succeeded(ip):
    with _login_lock:
        _login_failures.pop(ip, None)


@app.after_request
def add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
    )
    if request.is_secure:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    if request.path.startswith('/api/') or request.path in ('/login', '/password'):
        response.headers.setdefault('Cache-Control', 'no-store')
    return response


@app.after_request
def record_page_access(response):
    """记录真实页面访问；排除静态文件和自动轮询 API，避免访问量失真。"""
    if (request.method != 'GET' or response.status_code >= 400 or
            request.endpoint not in ('login', 'role_home', 'password_page')):
        return response
    conn = None
    try:
        uid = session.get('uid')
        username = display_name = role = ''
        conn = get_db()
        if uid:
            user = conn.execute(
                'SELECT username,name,role FROM users WHERE id=?', (uid,)
            ).fetchone()
            if user:
                username = user['username']
                display_name = user['name']
                role = user['role']
        conn.execute(
            """INSERT INTO access_logs(user_id,username,display_name,role,ip,path)
               VALUES(?,?,?,?,?,?)""",
            (uid, username, display_name, role, _login_ip(), request.path[:160]),
        )
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        app.logger.exception('记录页面访问失败')
    finally:
        if conn is not None:
            conn.close()
    return response


_AUDIT_ACTIONS = {
    '/api/admin/phase': '实时互选控制',
    '/api/admin/mentor': '保存导师',
    '/api/admin/mentor/toggle': '启停导师账号',
    '/api/admin/mentor/delete': '删除导师',
    '/api/admin/student': '保存学生',
    '/api/admin/student/toggle': '启停学生账号',
    '/api/admin/student/delete': '删除学生',
    '/api/admin/reset_password': '重置密码',
    '/api/admin/reset_password_batch': '批量重置密码',
    '/api/admin/student/delete_batch': '批量删除学生',
    '/api/admin/mentor/delete_batch': '批量删除导师',
    '/api/admin/batch_edit': '批量编辑',
    '/api/admin/import': '批量导入',
    '/api/admin/match': '手动调整匹配',
    '/api/admin/mentor_info': '上传导师信息表',
    '/api/admin/mentor_info/delete': '删除导师信息表',
    '/api/admin/options': '保存基础选项设置',
}

_PHASE_ACTIONS = {
    'open': '开放实时互选', 'close': '暂停学生申请',
    'password_change_on': '开启首次登录强制改密',
    'password_change_off': '关闭首次登录强制改密',
    'xueshu_limit_on': '开启每位导师最多录取3名学硕',
    'xueshu_limit_off': '关闭每位导师最多录取3名学硕',
    'mentor_hot_badge_on': '开启学生端热门导师火苗标记',
    'mentor_hot_badge_off': '关闭学生端热门导师火苗标记',
    'reset_all': '清空实时申请与配对数据',
}


def _audit_payload():
    if request.files:
        f = request.files.get('file')
        return {'filename': os.path.basename(f.filename) if f and f.filename else ''}
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {}
    safe = {}
    for key, value in data.items():
        if 'password' in key.lower() or key in ('text', 'intro'):
            continue
        safe[key] = value
    if request.path == '/api/admin/phase':
        safe['action_name'] = _PHASE_ACTIONS.get(safe.get('action'), safe.get('action', ''))
    return safe


@app.after_request
def record_admin_audit(response):
    if request.method != 'POST' or request.path not in _AUDIT_ACTIONS:
        return response
    uid = session.get('uid')
    if not uid:
        return response
    conn = None
    try:
        conn = get_db()
        u = conn.execute(
            "SELECT username,name FROM users WHERE id=? AND role='admin'", (uid,)
        ).fetchone()
        if u:
            payload = _audit_payload()
            result = response.get_json(silent=True)
            if isinstance(result, dict):
                for key in ('count', 'added', 'updated', 'deleted'):
                    if key in result:
                        payload[f'result_{key}'] = result[key]
                if response.status_code >= 400 and result.get('error'):
                    payload['error'] = str(result['error'])[:160]
            conn.execute(
                """INSERT INTO audit_logs(user_id,username,operator_name,action,detail,status,ip)
                   VALUES(?,?,?,?,?,?,?)""",
                (uid, u['username'], u['name'], _AUDIT_ACTIONS[request.path],
                 json.dumps(payload, ensure_ascii=False, separators=(',', ':'))[:1000],
                 'success' if response.status_code < 400 else 'failed', _login_ip()),
            )
            conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        app.logger.exception('记录管理员操作日志失败')
    finally:
        if conn is not None:
            conn.close()
    return response


def current_user():
    uid = session.get('uid')
    if not uid:
        return None
    conn = get_db()
    u = conn.execute('SELECT * FROM users WHERE id=? AND active=1', (uid,)).fetchone()
    conn.close()
    return u


def login_required(role=None):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u:
                if request.path.startswith('/api/'):
                    return jsonify({'error': '请先登录'}), 401
                return redirect(url_for('login'))
            force_password_change = get_setting('force_password_change', '1') == '1'
            if (force_password_change and u['must_change_password'] and
                    request.path not in ('/password', '/api/password', '/logout')):
                if request.path.startswith('/api/'):
                    return jsonify({'error': '首次登录请先修改初始密码', 'must_change_password': True}), 403
                return redirect(url_for('password_page'))
            if role and u['role'] != role:
                if request.path.startswith('/api/'):
                    return jsonify({'error': '无权限'}), 403
                return redirect(url_for('home'))
            return fn(*args, **kwargs)
        return wrapper
    return deco


def phase_info():
    opened = get_setting('selection_open', '1') == '1'
    force_password_change = get_setting('force_password_change', '1') == '1'
    xueshu_limit = get_setting('xueshu_limit_enabled', '1') == '1'
    mentor_hot_badge = get_setting('mentor_hot_badge_enabled', '0') == '1'
    return {'mode': 'realtime', 'open': opened,
            # 保留字段用于兼容旧模板/接口，但V2不再存在轮次与集中结算。
            'round': 1, 'phase': 'open' if opened else 'closed', 'settled': False,
            'force_password_change': force_password_change,
            'xueshu_limit_enabled': xueshu_limit,
            'xueshu_limit': XUESHU_LIMIT,
            'mentor_hot_badge_enabled': mentor_hot_badge,
            'sys_title': get_setting('sys_title', '研究生导师互选系统')}


# ---------- 名额相关 ----------

def mentor_taken(conn, mentor_id):
    """实时流程中只有已建立的配对才占用名额。"""
    return pairing_count(conn, mentor_id)


def mentor_remaining(conn, mentor_id):
    row = conn.execute('SELECT quota FROM mentors WHERE id=?', (mentor_id,)).fetchone()
    if not row:
        return 0
    return max(0, row['quota'] - mentor_taken(conn, mentor_id))


def mentor_accepts_preferences(conn, mentor_id):
    """尚有剩余名额时才允许学生新申请。"""
    row = conn.execute('SELECT quota FROM mentors WHERE id=?', (mentor_id,)).fetchone()
    if not row:
        return False
    return mentor_taken(conn, mentor_id) < row['quota']


def mentor_interest_count(conn, mentor_id):
    """当前仍有效的学生选择数：待审核申请与由申请形成的已配对记录。"""
    return conn.execute(
        """SELECT COUNT(*) c FROM selection_requests
           WHERE mentor_id=? AND status IN ('pending','accepted')""",
        (mentor_id,),
    ).fetchone()['c']


# ---------- 页面 ----------

@app.route('/')
def home():
    u = current_user()
    if not u:
        return redirect(url_for('login'))
    return redirect(url_for('role_home', role=u['role']))


@app.route('/<role>/')
@login_required()
def role_home(role):
    u = current_user()
    if u['role'] != role:
        return redirect(url_for('role_home', role=u['role']))
    tpl = {'admin': 'admin.html', 'student': 'student.html', 'mentor': 'mentor.html'}[role]
    view_user = row2dict(u)
    if role == 'student':
        conn = get_db()
        student = student_me(conn, u['id'])
        conn.close()
        view_user['major'] = student['major'] if student else ''
        view_user['phone'] = student['phone'] if student else ''
        view_user['degree_category'] = student_degree_category(view_user['major'])
    return render_template(tpl, me=view_user, info=phase_info())


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = _login_ip()
        if _login_is_limited(ip):
            return render_template('login.html', error='登录失败次数过多，请10分钟后再试', info=phase_info()), 429
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        conn = get_db()
        u = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        conn.close()
        if u and u['active'] and check_password_hash(u['password_hash'], password):
            _login_succeeded(ip)
            session.clear()
            session['_csrf'] = secrets.token_hex(16)  # 登录后立即生成新 CSRF token
            session['uid'] = u['id']
            if (get_setting('force_password_change', '1') == '1' and
                    u['must_change_password']):
                return redirect(url_for('password_page'))
            return redirect(url_for('role_home', role=u['role']))
        _login_failed(ip)
        return render_template('login.html', error='账号或密码错误', info=phase_info())
    error = '登录状态已过期，请重新登录' if request.args.get('expired') == '1' else None
    return render_template('login.html', error=error, info=phase_info())


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/password')
@login_required()
def password_page():
    return render_template('password.html', me=row2dict(current_user()), info=phase_info())


@app.post('/api/password')
@login_required()
def api_password():
    old = request.json.get('old', '')
    new = request.json.get('new', '')
    if len(new) < 6:
        return jsonify({'error': '新密码至少6位'}), 400
    conn = get_db()
    u = conn.execute('SELECT * FROM users WHERE id=?', (session['uid'],)).fetchone()
    if not check_password_hash(u['password_hash'], old):
        conn.close()
        return jsonify({'error': '原密码错误'}), 400
    conn.execute(
        'UPDATE users SET password_hash=?, initial_password=NULL, '
        'must_change_password=0 WHERE id=?',
        (generate_password_hash(new), session['uid']))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ---------- 学生端 API ----------

def student_me(conn, uid):
    return conn.execute('SELECT * FROM students WHERE user_id=?', (uid,)).fetchone()


def normalize_student_phone(value):
    """规范学生自行填写的手机号；允许留空。"""
    phone = re.sub(r'[\s-]+', '', str(value or '').strip())
    if phone.startswith('+86'):
        phone = phone[3:]
    elif phone.startswith('0086'):
        phone = phone[4:]
    if phone and not re.fullmatch(r'1\d{10}', phone):
        raise MatchingError('请输入正确的11位手机号码')
    return phone


@app.get('/api/student/me')
@login_required('student')
def api_student_me():
    conn = get_db()
    s = student_me(conn, session['uid'])
    info = phase_info()
    current_request = conn.execute(
        """SELECT r.*, u.name mentor_name, m.title mentor_title
           FROM selection_requests r JOIN mentors m ON m.id=r.mentor_id
           JOIN users u ON u.id=m.user_id
           WHERE r.student_id=? AND r.status='pending' ORDER BY r.id DESC LIMIT 1""",
        (s['id'],),
    ).fetchone()
    pairing = conn.execute(
        """SELECT p.*, u.name mentor_name, m.title mentor_title
           FROM pairings p JOIN mentors m ON m.id=p.mentor_id
           JOIN users u ON u.id=m.user_id WHERE p.student_id=?""",
        (s['id'],),
    ).fetchone()
    conn.close()
    return jsonify({
        'me': row2dict(s), 'info': info,
        'request': row2dict(current_request),
        'pairing': row2dict(pairing),
    })


@app.post('/api/student/profile')
@login_required('student')
def api_student_profile():
    data = request.get_json(silent=True) or {}
    try:
        phone = normalize_student_phone(data.get('phone'))
    except MatchingError as e:
        return jsonify({'error': str(e)}), 400
    conn = get_db()
    cur = conn.execute('UPDATE students SET phone=? WHERE user_id=?',
                       (phone, session['uid']))
    conn.commit()
    conn.close()
    if not cur.rowcount:
        return jsonify({'error': '学生资料不存在'}), 400
    return jsonify({'ok': True, 'phone': phone})


@app.get('/api/mentors')
@login_required('student')
def api_mentors():
    conn = get_db()
    student = student_me(conn, session['uid'])
    degree_category = student_degree_category(student['major'] if student else '')
    rows = conn.execute(
        """SELECT m.id, u.name, m.title, m.area, m.intro, m.admission_category, m.quota
           FROM mentors m JOIN users u ON u.id=m.user_id
           WHERE u.active=1 AND m.active=1 ORDER BY m.id""").fetchall()
    hot_badge_enabled = get_setting('mentor_hot_badge_enabled', '0') == '1'
    out = []
    for r in rows:
        if not mentor_allows_major(conn, r['id'], student['major'] if student else ''):
            continue
        if not mentor_allows_degree_category(r['admission_category'], degree_category):
            continue
        if (degree_category == '学硕' and xueshu_limit_enabled(conn) and
                xueshu_pairing_count(conn, r['id']) >= XUESHU_LIMIT):
            continue
        d = dict(r)
        quota = d.pop('quota')
        d['allowed_majors'] = mentor_major_names(conn, r['id'])
        d['accepts_preferences'] = mentor_accepts_preferences(conn, r['id'])
        d['is_hot'] = bool(
            hot_badge_enabled and quota > 0 and
            mentor_interest_count(conn, r['id']) >= quota * 2
        )
        out.append(d)
    conn.close()
    return jsonify({
        'mentors': out,
        'student_major': student['major'] if student else '',
        'student_degree_category': degree_category,
        'mentor_hot_badge_enabled': hot_badge_enabled,
    })


@app.post('/api/student/preference')
@login_required('student')
def api_student_preference():
    info = phase_info()
    if not info['open']:
        return jsonify({'error': '学院暂时关闭了导师申请，请稍后再试'}), 400
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        s = student_me(conn, session['uid'])
        if not s:
            raise MatchingError('学生资料不存在')
        if conn.execute('SELECT 1 FROM pairings WHERE student_id=?', (s['id'],)).fetchone():
            raise MatchingError('您已配对成功，如需调整请联系管理员')
        mid = data.get('mentor_id', data.get('r1'))
        if mid in (None, ''):
            raise MatchingError('请选择导师')
        mid = int(mid)
        m = conn.execute(
            """SELECT m.id,m.quota,m.admission_category,u.name
               FROM mentors m JOIN users u ON u.id=m.user_id
               WHERE m.id=? AND u.active=1 AND m.active=1""", (mid,)
        ).fetchone()
        if not m:
            raise MatchingError('所选导师不存在或已停用')
        if not mentor_allows_major(conn, mid, s['major']):
            raise MatchingError(f'导师「{m["name"]}」不招收您的专业')
        degree_category = student_degree_category(s['major'])
        if not mentor_allows_degree_category(m['admission_category'], degree_category):
            raise MatchingError(f'导师「{m["name"]}」不招收您的招生类别')
        if (degree_category == '学硕' and xueshu_limit_enabled(conn) and
                xueshu_pairing_count(conn, mid) >= XUESHU_LIMIT):
            raise MatchingError(
                f'导师「{m["name"]}」已录取{XUESHU_LIMIT}名学硕，请选择其他导师')
        if pairing_count(conn, mid) >= m['quota']:
            raise MatchingError(f'导师「{m["name"]}」当前已完成招生，请选择其他导师')
        existing = conn.execute(
            "SELECT * FROM selection_requests WHERE student_id=? AND status='pending'",
            (s['id'],),
        ).fetchone()
        if existing and existing['mentor_id'] == mid:
            conn.commit()
            return jsonify({'ok': True, 'request_id': existing['id'], 'idempotent': True})
        if existing:
            conn.execute(
                """UPDATE selection_requests SET status='student_cancelled',
                   cancel_reason='changed_selection', operated_by=?,
                   responded_at=datetime('now','localtime'), updated_at=datetime('now','localtime')
                   WHERE id=?""", (session['uid'], existing['id']))
        intro = (data.get('intro') or '').strip()
        if len(intro) > 1000:
            raise MatchingError('个人介绍最多1000字')
        phone = normalize_student_phone(data.get('phone', s['phone']))
        remark = (data.get('remark') or '').strip()[:200]
        conn.execute('UPDATE students SET intro=?,phone=?,remark=? WHERE id=?',
                     (intro, phone, remark, s['id']))
        cur = conn.execute(
            """INSERT INTO selection_requests(student_id,mentor_id,operated_by)
               VALUES(?,?,?)""", (s['id'], mid, session['uid']))
        conn.commit()
        return jsonify({'ok': True, 'request_id': cur.lastrowid, 'idempotent': False})
    except (TypeError, ValueError):
        conn.rollback()
        return jsonify({'error': '导师格式错误'}), 400
    except MatchingError as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()


@app.post('/api/student/request/cancel')
@login_required('student')
def api_student_request_cancel():
    conn = get_db()
    try:
        s = student_me(conn, session['uid'])
        if not s:
            return jsonify({'error': '学生资料不存在'}), 400
        cancel_student_request(conn, s['id'], session['uid'])
        return jsonify({'ok': True})
    except MatchingError as e:
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()


@app.get('/api/student/status')
@login_required('student')
def api_student_status():
    conn = get_db()
    s = student_me(conn, session['uid'])
    info = phase_info()
    history = conn.execute(
        """SELECT r.*,u.name mentor_name,m.title mentor_title
           FROM selection_requests r JOIN mentors m ON m.id=r.mentor_id
           JOIN users u ON u.id=m.user_id WHERE r.student_id=? ORDER BY r.id DESC""",
        (s['id'],),
    ).fetchall()
    pairing = conn.execute(
        """SELECT p.*,u.name mentor_name,m.title mentor_title
           FROM pairings p JOIN mentors m ON m.id=p.mentor_id
           JOIN users u ON u.id=m.user_id WHERE p.student_id=?""",
        (s['id'],),
    ).fetchone()
    pending = next((r for r in history if r['status'] == 'pending'), None)
    state = 'matched' if pairing else ('pending' if pending else 'selectable')
    conn.close()
    return jsonify({'info': info, 'me': row2dict(s), 'request': row2dict(pending),
                    'history': [dict(r) for r in history],
                    'pairing': row2dict(pairing), 'state': state})


# ---------- 导师端 API ----------

@app.get('/api/mentor/me')
@login_required('mentor')
def api_mentor_me():
    conn = get_db()
    m = conn.execute('SELECT * FROM mentors WHERE user_id=?', (session['uid'],)).fetchone()
    info = phase_info()
    taken = mentor_taken(conn, m['id'])
    xueshu_taken = xueshu_pairing_count(conn, m['id'])
    conn.close()
    return jsonify({'me': row2dict(m), 'info': info,
                    'taken': taken, 'remaining': max(0, m['quota'] - taken),
                    'xueshu_taken': xueshu_taken,
                    'xueshu_remaining': max(0, XUESHU_LIMIT - xueshu_taken)})


@app.get('/api/mentor/applicants')
@login_required('mentor')
def api_mentor_applicants():
    conn = get_db()
    m = conn.execute('SELECT * FROM mentors WHERE user_id=?', (session['uid'],)).fetchone()
    info = phase_info()
    rows = conn.execute(
        """SELECT r.id request_id,r.student_id,r.created_at,r.status,
                  st.remark,st.intro,u.name sname,u.username sno,st.major,st.phone
           FROM selection_requests r JOIN students st ON st.id=r.student_id
           JOIN users u ON u.id=st.user_id
           WHERE r.mentor_id=? AND r.status='pending'
           ORDER BY r.created_at,r.id""", (m['id'],)).fetchall()
    applicants = [dict(r) for r in rows]
    for applicant in applicants:
        applicant['degree_category'] = student_degree_category(applicant['major'])
    acc = conn.execute(
        """SELECT p.id pairing_id,p.source,p.created_at,u.name sname,
                  u.username sno,st.major,st.phone
           FROM pairings p JOIN students st ON st.id=p.student_id
           JOIN users u ON u.id=st.user_id
           WHERE p.mentor_id=? ORDER BY p.created_at,p.id""", (m['id'],)).fetchall()
    accepted = [dict(a) for a in acc]
    for student in accepted:
        student['degree_category'] = student_degree_category(student['major'])
    conn.close()
    return jsonify({'info': info, 'applicants': applicants, 'accepted': accepted})


@app.post('/api/mentor/select')
@login_required('mentor')
def api_mentor_select():
    """导师同意或拒绝学生申请；同意后立即完成配对。"""
    data = request.get_json(silent=True) or {}
    try:
        request_id = int(data['request_id'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': '申请格式错误'}), 400
    decision = data.get('decision')
    if decision is None and isinstance(data.get('selected'), bool):
        decision = 'accept' if data['selected'] else 'reject'
    if decision not in ('accept', 'reject'):
        return jsonify({'error': '审核结果格式错误'}), 400
    conn = get_db()
    try:
        if decision == 'accept':
            result = accept_request(conn, session['uid'], request_id)
        else:
            result = reject_request(conn, session['uid'], request_id)
        return jsonify({'ok': True, **result})
    except MatchingError as e:
        return jsonify({'error': str(e)}), 409
    finally:
        conn.close()


@app.post('/api/mentor/pairing/cancel')
@login_required('mentor')
def api_mentor_pairing_cancel():
    """导师撤回自己确认的配对；管理员安排的配对仍由管理员处理。"""
    data = request.get_json(silent=True) or {}
    try:
        pairing_id = int(data['pairing_id'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': '配对记录格式错误'}), 400
    conn = get_db()
    try:
        result = mentor_cancel_pairing(conn, session['uid'], pairing_id)
        return jsonify({'ok': True, **result})
    except MatchingError as e:
        return jsonify({'error': str(e)}), 409
    finally:
        conn.close()


# ---------- 管理员 API ----------

@app.get('/api/admin/options')
@login_required('admin')
def api_admin_options_get():
    """返回管理员可维护的学院/专业/招生类别/职称选项及学位类别映射。"""
    conn = get_db()
    try:
        saved = options_service.load(conn)
        observed = options_service.merge_observed(conn, saved)
    finally:
        conn.close()
    return jsonify({
        'options': observed,
        'saved': saved,
        'degree_options': list(options_service.DEGREE_OPTIONS),
        'defaults': options_service.defaults(),
    })


@app.post('/api/admin/options')
@login_required('admin')
def api_admin_options_save():
    """保存基础选项设置，导师端、学生端与批量导入会立即使用新选项。"""
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        saved = options_service.save(conn, data)
        conn.commit()
        observed = options_service.merge_observed(conn, saved)
    except Exception:
        conn.rollback()
        app.logger.exception('保存基础选项设置失败')
        return jsonify({'error': '保存失败，请稍后重试'}), 500
    finally:
        conn.close()
    return jsonify({'ok': True, 'options': observed})


@app.get('/api/admin/overview')
@login_required('admin')
def api_admin_overview():
    conn = get_db()
    info = phase_info()
    stats = {}
    stats['mentors'] = conn.execute('SELECT COUNT(*) c FROM mentors WHERE active=1').fetchone()['c']
    stats['students'] = conn.execute('SELECT COUNT(*) c FROM students').fetchone()['c']
    stats['submitted'] = conn.execute(
        "SELECT COUNT(*) c FROM selection_requests WHERE status='pending'"
    ).fetchone()['c']
    stats['matched'] = conn.execute('SELECT COUNT(*) c FROM pairings').fetchone()['c']
    stats['unmatched'] = max(0, stats['students'] - conn.execute(
        'SELECT COUNT(*) c FROM pairings').fetchone()['c'])
    # 每导师名额占用
    mentors = conn.execute(
        """SELECT m.id, u.username, u.name, m.quota, m.title, m.area FROM mentors m
           JOIN users u ON u.id=m.user_id WHERE m.active=1 AND u.active=1 ORDER BY m.id""").fetchall()
    mlist = []
    for m in mentors:
        ml = dict(m)
        ml['applied'] = conn.execute(
            "SELECT COUNT(*) c FROM selection_requests WHERE mentor_id=? AND status='pending'",
            (m['id'],)).fetchone()['c']
        ml['applied1'] = ml['applied']
        ml['applied1_names'] = [r['name'] for r in conn.execute(
            """SELECT u.name FROM selection_requests r JOIN students s ON s.id=r.student_id
               JOIN users u ON u.id=s.user_id WHERE r.mentor_id=? AND r.status='pending'
               ORDER BY r.created_at,r.id""", (m['id'],)).fetchall()]
        ml['matched'] = conn.execute(
            'SELECT COUNT(*) c FROM pairings WHERE mentor_id=?', (m['id'],)).fetchone()['c']
        ml['approved'] = ml['matched']
        ml['occupied'] = mentor_taken(conn, m['id'])
        ml['xueshu_matched'] = xueshu_pairing_count(conn, m['id'])
        ml['interest_count'] = mentor_interest_count(conn, m['id'])
        ml['is_hot'] = bool(m['quota'] > 0 and ml['interest_count'] >= m['quota'] * 2)
        mlist.append(ml)
    stats['xueshu_limit_reached'] = sum(
        1 for mentor in mlist if mentor['xueshu_matched'] >= XUESHU_LIMIT)
    stats['xueshu_limit_exceeded'] = sum(
        1 for mentor in mlist if mentor['xueshu_matched'] > XUESHU_LIMIT)
    stats['hot_mentor_count'] = sum(1 for mentor in mlist if mentor['is_hot'])
    conn.close()
    return jsonify({'info': info, 'stats': stats, 'mentors': mlist})


@app.get('/api/admin/dashboard')
@login_required('admin')
def api_admin_dashboard():
    """管理员数据看板：互选进度、访问趋势及 IP/账户访问明细。"""
    try:
        visitor_page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        visitor_page = 1
    try:
        visitor_page_size = int(request.args.get('page_size', 20))
    except (TypeError, ValueError):
        visitor_page_size = 20
    if visitor_page_size not in (10, 20, 50, 100):
        visitor_page_size = 20
    visitor_query = request.args.get('q', '').strip()[:80]
    visitor_role = request.args.get('role', 'all').strip()
    if visitor_role not in ('all', 'student', 'mentor', 'admin', 'anonymous'):
        visitor_role = 'all'

    conn = get_db()
    total_students = conn.execute(
        """SELECT COUNT(*) c FROM students s JOIN users u ON u.id=s.user_id
           WHERE u.active=1"""
    ).fetchone()['c']
    matched = conn.execute('SELECT COUNT(*) c FROM pairings').fetchone()['c']
    pending = conn.execute(
        "SELECT COUNT(DISTINCT student_id) c FROM selection_requests WHERE status='pending'"
    ).fetchone()['c']
    unselected = conn.execute(
        """SELECT COUNT(*) c FROM students s JOIN users u ON u.id=s.user_id
           WHERE u.active=1
             AND NOT EXISTS (SELECT 1 FROM pairings p WHERE p.student_id=s.id)
             AND NOT EXISTS (
               SELECT 1 FROM selection_requests r
               WHERE r.student_id=s.id AND r.status='pending'
             )"""
    ).fetchone()['c']
    visits = conn.execute('SELECT COUNT(*) c FROM access_logs').fetchone()['c']
    today_visits = conn.execute(
        "SELECT COUNT(*) c FROM access_logs WHERE date(created_at)=date('now','localtime')"
    ).fetchone()['c']
    unique_ips = conn.execute(
        "SELECT COUNT(DISTINCT ip) c FROM access_logs WHERE ip!=''"
    ).fetchone()['c']
    account_visitors = conn.execute(
        "SELECT COUNT(DISTINCT username) c FROM access_logs WHERE username!=''"
    ).fetchone()['c']

    daily_rows = conn.execute(
        """SELECT date(created_at) day, COUNT(*) visits,
                  COUNT(DISTINCT CASE WHEN username!='' THEN username END) accounts
           FROM access_logs
           WHERE date(created_at) >= date('now','localtime','-6 days')
           GROUP BY date(created_at) ORDER BY day"""
    ).fetchall()
    daily_map = {r['day']: dict(r) for r in daily_rows}
    days = []
    for offset in range(6, -1, -1):
        day = conn.execute(
            "SELECT date('now','localtime',?) d", (f'-{offset} days',)
        ).fetchone()['d']
        row = daily_map.get(day, {})
        days.append({'date': day, 'label': day[5:],
                     'visits': row.get('visits', 0),
                     'accounts': row.get('accounts', 0)})

    role_rows = conn.execute(
        """SELECT role, COUNT(*) visits, COUNT(DISTINCT username) accounts
           FROM access_logs WHERE username!=''
           GROUP BY role ORDER BY visits DESC"""
    ).fetchall()
    role_labels = {'admin': '管理员', 'mentor': '导师', 'student': '学生'}
    roles = [{'role': r['role'], 'label': role_labels.get(r['role'], '其他'),
              'visits': r['visits'], 'accounts': r['accounts']} for r in role_rows]

    visitor_where = []
    visitor_params = []
    if visitor_query:
        visitor_where.append('(ip LIKE ? OR username LIKE ? OR display_name LIKE ?)')
        visitor_params.extend((f'%{visitor_query}%',) * 3)
    if visitor_role == 'anonymous':
        visitor_where.append("username='' ")
    elif visitor_role != 'all':
        visitor_where.append('role=?')
        visitor_params.append(visitor_role)
    visitor_where_sql = (' WHERE ' + ' AND '.join(visitor_where)) if visitor_where else ''
    visitor_group_sql = ' GROUP BY ip, username, display_name, role'
    visitor_total = conn.execute(
        'SELECT COUNT(*) c FROM (SELECT 1 FROM access_logs' +
        visitor_where_sql + visitor_group_sql + ')', visitor_params
    ).fetchone()['c']
    visitor_total_pages = max(1, (visitor_total + visitor_page_size - 1) // visitor_page_size)
    visitor_page = min(visitor_page, visitor_total_pages)
    visitor_rows = conn.execute(
        """SELECT ip, username, display_name, role, COUNT(*) visits,
                  MAX(created_at) last_visit
           FROM access_logs""" + visitor_where_sql + visitor_group_sql +
        ' ORDER BY last_visit DESC, visits DESC, ip, username LIMIT ? OFFSET ?',
        visitor_params + [visitor_page_size, (visitor_page - 1) * visitor_page_size],
    ).fetchall()
    visitors = []
    for row in visitor_rows:
        item = dict(row)
        item['role_label'] = role_labels.get(item['role'], '未登录')
        visitors.append(item)
    conn.close()
    pairing_rate = round(matched * 100 / total_students, 1) if total_students else 0
    return jsonify({
        'stats': {
            'visits': visits, 'today_visits': today_visits,
            'unique_ips': unique_ips, 'account_visitors': account_visitors,
            'students': total_students, 'matched': matched,
            'unselected': unselected, 'pending': pending,
            'pairing_rate': pairing_rate,
        },
        'daily': days, 'roles': roles, 'visitors': visitors,
        'visitors_pagination': {
            'page': visitor_page, 'page_size': visitor_page_size,
            'total': visitor_total, 'total_pages': visitor_total_pages,
            'start': (visitor_page - 1) * visitor_page_size + 1 if visitor_total else 0,
            'end': min(visitor_page * visitor_page_size, visitor_total),
        },
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    })


@app.get('/api/admin/dashboard/detail')
@login_required('admin')
def api_admin_dashboard_detail():
    """看板指标下钻：返回访问、已配对、未选择或待审核的具体名单。"""
    detail_type = request.args.get('type', '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
        page_size = int(request.args.get('page_size', 20))
    except (TypeError, ValueError):
        page, page_size = 1, 20
    if page_size not in (10, 20, 50, 100):
        page_size = 20

    conn = get_db()
    params = []
    if detail_type == 'matched':
        title = '已录学生名单'
        count_sql = 'SELECT COUNT(*) c FROM pairings'
        rows_sql = """SELECT su.username,su.name,s.major,
                             mu.username mentor_username,mu.name mentor_name,p.created_at
                      FROM pairings p
                      JOIN students s ON s.id=p.student_id
                      JOIN users su ON su.id=s.user_id
                      JOIN mentors m ON m.id=p.mentor_id
                      JOIN users mu ON mu.id=m.user_id
                      ORDER BY p.created_at DESC,p.id DESC"""
    elif detail_type == 'unselected':
        title = '未选择学生名单'
        source = """ FROM students s JOIN users u ON u.id=s.user_id
                     WHERE u.active=1
                       AND NOT EXISTS (SELECT 1 FROM pairings p WHERE p.student_id=s.id)
                       AND NOT EXISTS (SELECT 1 FROM selection_requests r
                                       WHERE r.student_id=s.id AND r.status='pending')"""
        count_sql = 'SELECT COUNT(*) c' + source
        rows_sql = ('SELECT u.username,u.name,s.major,s.phone' + source +
                    ' ORDER BY u.username')
    elif detail_type == 'pending':
        title = '等待审核学生名单'
        source = """ FROM selection_requests r
                     JOIN students s ON s.id=r.student_id
                     JOIN users su ON su.id=s.user_id
                     JOIN mentors m ON m.id=r.mentor_id
                     JOIN users mu ON mu.id=m.user_id
                     WHERE r.status='pending'"""
        count_sql = 'SELECT COUNT(*) c' + source
        rows_sql = ("""SELECT su.username,su.name,s.major,
                               mu.username mentor_username,mu.name mentor_name,r.created_at""" +
                    source + ' ORDER BY r.created_at DESC,r.id DESC')
    elif detail_type == 'visits':
        role = request.args.get('role', 'all').strip()
        day = request.args.get('date', '').strip()
        if role not in ('all', 'authenticated', 'student', 'mentor', 'admin', 'anonymous'):
            role = 'all'
        if day and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            day = ''
        where = []
        if role == 'anonymous':
            where.append("username=''")
        elif role == 'authenticated':
            where.append("username!=''")
        elif role != 'all':
            where.append('role=?')
            params.append(role)
        if day:
            where.append('date(created_at)=?')
            params.append(day)
        where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
        group_sql = ' GROUP BY ip,username,display_name,role'
        count_sql = ('SELECT COUNT(*) c FROM (SELECT 1 FROM access_logs' +
                     where_sql + group_sql + ')')
        rows_sql = """SELECT ip,username,display_name,role,COUNT(*) visits,
                             MAX(created_at) last_visit FROM access_logs""" + where_sql + group_sql + \
                   ' ORDER BY last_visit DESC,visits DESC,ip,username'
        role_names = {'student': '学生', 'mentor': '导师', 'admin': '管理员',
                      'anonymous': '未登录', 'authenticated': '已登录账户', 'all': '全部'}
        title = ((day + ' ') if day else '') + role_names[role] + '访问明细'
    else:
        conn.close()
        return jsonify({'error': '不支持的看板明细类型'}), 400

    total = conn.execute(count_sql, params).fetchone()['c']
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    rows = conn.execute(
        rows_sql + ' LIMIT ? OFFSET ?', params + [page_size, (page - 1) * page_size]
    ).fetchall()
    result = [dict(row) for row in rows]
    if detail_type == 'visits':
        role_labels = {'admin': '管理员', 'mentor': '导师', 'student': '学生'}
        for item in result:
            item['role_label'] = role_labels.get(item['role'], '未登录')
    conn.close()
    return jsonify({
        'type': detail_type, 'title': title, 'rows': result,
        'pagination': {
            'page': page, 'page_size': page_size, 'total': total,
            'total_pages': total_pages,
        },
    })


@app.post('/api/admin/phase')
@login_required('admin')
def api_admin_phase():
    data = request.get_json(silent=True) or {}
    action = data.get('action', '')
    conn = get_db()
    try:
        upsert = ('INSERT INTO settings(key,value) VALUES(?,?) '
                  'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
        cancelled = 0
        if action == 'open':
            conn.execute(upsert, ('selection_open', '1'))
        elif action == 'close':
            conn.execute(upsert, ('selection_open', '0'))
        elif action == 'password_change_on':
            conn.execute(upsert, ('force_password_change', '1'))
        elif action == 'password_change_off':
            conn.execute(upsert, ('force_password_change', '0'))
        elif action == 'xueshu_limit_on':
            conn.execute(upsert, ('xueshu_limit_enabled', '1'))
            cancelled = cancel_pending_xueshu_at_limit(conn, session['uid'])
        elif action == 'xueshu_limit_off':
            conn.execute(upsert, ('xueshu_limit_enabled', '0'))
        elif action == 'mentor_hot_badge_on':
            conn.execute(upsert, ('mentor_hot_badge_enabled', '1'))
        elif action == 'mentor_hot_badge_off':
            conn.execute(upsert, ('mentor_hot_badge_enabled', '0'))
        elif action == 'reset_all':
            if data.get('confirm') != 'RESET':
                return jsonify({'error': '清空实时数据需要二次确认',
                                'confirm_required': True}), 409
            conn.execute('DELETE FROM pairings')
            conn.execute('DELETE FROM selection_requests')
        else:
            return jsonify({'error': '未知操作'}), 400
        conn.commit()
        return jsonify({'ok': True, 'info': phase_info(), 'cancelled': cancelled})
    except Exception:
        conn.rollback()
        app.logger.exception('更新互选控制失败')
        return jsonify({'error': '设置更新失败，请稍后重试'}), 500
    finally:
        conn.close()


@app.get('/api/admin/mentors')
@login_required('admin')
def api_admin_mentors():
    conn = get_db()
    q = (request.args.get('q') or '').strip()[:80]
    category = (request.args.get('category') or '').strip()[:80]
    college = (request.args.get('college') or '').strip()[:100]
    major = (request.args.get('major') or '').strip()[:100]
    status = (request.args.get('status') or '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
        page_size = int(request.args.get('page_size', 20))
    except (TypeError, ValueError):
        page, page_size = 1, 20
    if page_size not in (10, 20, 50, 100):
        page_size = 20
    where, params = [], []
    if q:
        where.append('(u.name LIKE ? OR u.username LIKE ?)')
        params.extend((f'%{q}%', f'%{q}%'))
    if category:
        where.append('m.admission_category=?')
        params.append(category)
    if college:
        where.append('m.college=?')
        params.append(college)
    if major:
        where.append('EXISTS (SELECT 1 FROM mentor_majors mm WHERE mm.mentor_id=m.id AND mm.major=?)')
        params.append(major)
    if status == 'active':
        where.append('m.active=1 AND u.active=1')
    elif status == 'disabled':
        where.append('(m.active=0 OR u.active=0)')
    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
    source_sql = ' FROM mentors m JOIN users u ON u.id=m.user_id'
    total = conn.execute('SELECT COUNT(*) c' + source_sql + where_sql, params).fetchone()['c']
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    # 学院排序跟随管理员在“互选控制”里维护的顺序，未列出的学院排在最后。
    ordered_colleges = options_service.load(conn).get('colleges') or []
    order_cases = ' '.join(
        'WHEN ? THEN %d' % index for index, _ in enumerate(ordered_colleges)
    )
    order_params = list(ordered_colleges)
    college_order_sql = (
        f"CASE m.college {order_cases} ELSE {len(ordered_colleges)} END"
        if order_cases else '0'
    )
    rows = conn.execute(
        """SELECT m.id, m.user_id, u.username, u.name, u.active, m.college, m.title, m.area, m.intro,
                  m.admission_category, m.quota, m.active m_active
           FROM mentors m JOIN users u ON u.id=m.user_id""" + where_sql +
        f" ORDER BY {college_order_sql}, m.college, u.username "
        'LIMIT ? OFFSET ?',
        params + order_params + [page_size, (page - 1) * page_size],
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d['allowed_majors'] = mentor_major_names(conn, r['id'])
        d['approved_cur'] = conn.execute(
            'SELECT COUNT(*) c FROM pairings WHERE mentor_id=?',
            (r['id'],)).fetchone()['c']
        out.append(d)
    options = options_service.merge_observed(conn, options_service.load(conn))
    conn.close()
    return jsonify({
        'mentors': out,
        'categories': options['categories'],
        'colleges': options['colleges'],
        'majors': options['majors'],
        'titles': options['titles'],
        'pagination': {'page': page, 'page_size': page_size,
                       'total': total, 'total_pages': total_pages},
    })


@app.post('/api/admin/mentor')
@login_required('admin')
def api_admin_mentor_save():
    d = request.json
    college = (d.get('college') or '').strip()[:100]
    try:
        quota = int(d.get('quota', 3))
    except (TypeError, ValueError):
        return jsonify({'error': '招生名额必须为整数'}), 400
    if quota < 1:
        return jsonify({'error': '招生名额至少为1'}), 400
    conn = get_db()
    if d.get('id'):
        m = conn.execute('SELECT * FROM mentors WHERE id=?', (d['id'],)).fetchone()
        if not m:
            conn.close()
            return jsonify({'error': '导师不存在'}), 400
        occupied = mentor_taken(conn, m['id'])
        if quota < occupied:
            conn.close()
            return jsonify({'error': f'该导师已有 {occupied} 个名额被占用，名额不能低于 {occupied}'}), 400
        conn.execute(
            'UPDATE mentors SET college=?, title=?, area=?, intro=?, admission_category=?, '
            'quota=? WHERE id=?',
            (college, d.get('title', ''), d.get('area', ''), d.get('intro', ''),
             d.get('admission_category', ''), quota, d['id']))
        if 'allowed_majors' in d:
            replace_mentor_majors(conn, m['id'], d.get('allowed_majors'))
        current_account = conn.execute(
            'SELECT username FROM users WHERE id=?', (m['user_id'],)
        ).fetchone()['username']
        requested_account = (d.get('username') or current_account).strip()
        if requested_account != current_account:
            if re.fullmatch(r'\d{7}', current_account):  # 正式工号不可直接修改，临时账号可迁移
                conn.close()
                return jsonify({'error': '正式工号账号不能直接修改'}), 400
            if not re.fullmatch(r'\d{7}', requested_account):
                conn.close()
                return jsonify({'error': '临时账号只能迁移为7位正式工号'}), 400
            duplicate = conn.execute(
                'SELECT 1 FROM users WHERE username=? AND id!=?',
                (requested_account, m['user_id']),
            ).fetchone()
            if duplicate:
                conn.close()
                return jsonify({'error': f'工号 {requested_account} 已存在'}), 409
            conn.execute('UPDATE users SET username=? WHERE id=?',
                         (requested_account, m['user_id']))
        conn.execute('UPDATE users SET name=? WHERE id=?', (d['name'], m['user_id']))
        if d.get('password'):
            conn.execute(
                'UPDATE users SET password_hash=?,initial_password=?,must_change_password=1 '
                'WHERE id=?',
                (generate_password_hash(d['password']), d['password'], m['user_id']))
    else:
        username = (d.get('username') or '').strip()
        if not valid_mentor_username(username):
            conn.close()
            return jsonify({'error': '导师账号必须为7位工号；临时账号需通过 MENTOR_TEMP_ACCOUNTS 环境变量配置'}), 400
        exists = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if exists:
            conn.close()
            return jsonify({'error': f'工号 {username} 已存在'}), 400
        initial_password = d.get('password') or username
        cur = conn.execute(
            'INSERT INTO users(username,password_hash,initial_password,name,role,must_change_password) '
            'VALUES(?,?,?,?,?,1)',
            (username, generate_password_hash(initial_password), initial_password,
             d['name'], 'mentor'))
        mcur = conn.execute(
            'INSERT INTO mentors(user_id,college,title,area,intro,admission_category,quota) '
            'VALUES(?,?,?,?,?,?,?)',
            (cur.lastrowid, college, d.get('title', ''), d.get('area', ''),
             d.get('intro', ''), d.get('admission_category', ''), quota))
        replace_mentor_majors(conn, mcur.lastrowid, d.get('allowed_majors'))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.post('/api/admin/mentor/toggle')
@login_required('admin')
def api_admin_mentor_toggle():
    d = request.json
    conn = get_db()
    m = conn.execute('SELECT * FROM mentors WHERE id=?', (d['id'],)).fetchone()
    if not m:
        conn.close()
        return jsonify({'error': '导师不存在'}), 400
    new_active = 0 if m['active'] else 1
    conn.execute('UPDATE mentors SET active=? WHERE id=?', (new_active, m['id']))
    conn.execute('UPDATE users SET active=? WHERE id=?', (new_active, m['user_id']))
    if not new_active:
        conn.execute(
            """UPDATE selection_requests SET status='admin_cancelled',
               cancel_reason='mentor_disabled', operated_by=?,
               responded_at=datetime('now','localtime'), updated_at=datetime('now','localtime')
               WHERE mentor_id=? AND status='pending'""",
            (session['uid'], m['id']))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.post('/api/admin/mentor/delete')
@login_required('admin')
def api_admin_mentor_delete():
    """删除导师；存在志愿/匹配记录时禁止（需求文档 6.3.2）"""
    conn = get_db()
    ok, msg = _delete_mentor(conn, int(request.json['id']))
    conn.commit()
    conn.close()
    if not ok:
        return jsonify({'error': msg or '导师不存在'}), 400
    return jsonify({'ok': True})


@app.get('/api/admin/students')
@login_required('admin')
def api_admin_students():
    conn = get_db()
    q = (request.args.get('q') or '').strip()[:80]
    major = (request.args.get('major') or '').strip()[:100]
    status = (request.args.get('status') or 'all').strip()
    account = (request.args.get('account') or 'all').strip()
    if status not in ('all', 'matched', 'pending', 'unselected'):
        status = 'all'
    if account not in ('all', 'active', 'inactive'):
        account = 'all'
    try:
        page = max(1, int(request.args.get('page', 1)))
        page_size = int(request.args.get('page_size', 20))
    except (TypeError, ValueError):
        page, page_size = 1, 20
    if page_size not in (10, 20, 50, 100):
        page_size = 20

    where, params = [], []
    if q:
        where.append('(u.name LIKE ? OR u.username LIKE ? OR s.phone LIKE ?)')
        params.extend((f'%{q}%', f'%{q}%', f'%{q}%'))
    if major:
        where.append('s.major=?')
        params.append(major)
    if account == 'active':
        where.append('u.active=1')
    elif account == 'inactive':
        where.append('u.active=0')
    if status == 'matched':
        where.append('EXISTS (SELECT 1 FROM pairings p WHERE p.student_id=s.id)')
    elif status == 'pending':
        where.append("""NOT EXISTS (SELECT 1 FROM pairings p WHERE p.student_id=s.id)
                      AND EXISTS (SELECT 1 FROM selection_requests r
                                  WHERE r.student_id=s.id AND r.status='pending')""")
    elif status == 'unselected':
        where.append("""NOT EXISTS (SELECT 1 FROM pairings p WHERE p.student_id=s.id)
                      AND NOT EXISTS (SELECT 1 FROM selection_requests r
                                      WHERE r.student_id=s.id AND r.status='pending')""")
    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''

    summary_row = conn.execute(
        """SELECT COUNT(*) total,
                  SUM(CASE WHEN u.active=1 THEN 1 ELSE 0 END) active,
                  SUM(CASE WHEN u.active=0 THEN 1 ELSE 0 END) inactive,
                  SUM(CASE WHEN EXISTS (
                        SELECT 1 FROM pairings p WHERE p.student_id=s.id
                      ) THEN 1 ELSE 0 END) matched,
                  SUM(CASE WHEN NOT EXISTS (
                        SELECT 1 FROM pairings p WHERE p.student_id=s.id
                      ) AND EXISTS (
                        SELECT 1 FROM selection_requests r
                        WHERE r.student_id=s.id AND r.status='pending'
                      ) THEN 1 ELSE 0 END) pending,
                  SUM(CASE WHEN NOT EXISTS (
                        SELECT 1 FROM pairings p WHERE p.student_id=s.id
                      ) AND NOT EXISTS (
                        SELECT 1 FROM selection_requests r
                        WHERE r.student_id=s.id AND r.status='pending'
                      ) THEN 1 ELSE 0 END) unselected
           FROM students s JOIN users u ON u.id=s.user_id"""
    ).fetchone()
    summary = {key: int(summary_row[key] or 0) for key in summary_row.keys()}

    total = conn.execute(
        'SELECT COUNT(*) c FROM students s JOIN users u ON u.id=s.user_id' + where_sql,
        params,
    ).fetchone()['c']
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    rows = conn.execute(
        """SELECT s.id, u.username, u.name, u.active, s.major, s.phone, s.accept_adjust, s.remark
           FROM students s JOIN users u ON u.id=s.user_id""" + where_sql +
        ' ORDER BY u.username LIMIT ? OFFSET ?', params + [page_size, (page - 1) * page_size]
    ).fetchall()
    # 专业筛选同时提供配置项与数据中已有的取值，便于筛选历史专业。
    majors = sorted(
        set(options_service.load(conn).get('majors') or []) |
        {r['major'] for r in conn.execute(
            "SELECT DISTINCT major FROM students WHERE TRIM(major)!=''")}
    )

    # 每学生当前配对与待审核申请（实时可见）
    out = []
    for r in rows:
        d = dict(r)
        match = conn.execute(
            'SELECT u.name mentor_name,u.username mentor_username FROM pairings p '
            'JOIN mentors mt ON mt.id=p.mentor_id JOIN users u ON u.id=mt.user_id '
            'WHERE p.student_id=?', (d['id'],)).fetchone()
        d['match'] = match['mentor_name'] if match else None
        d['match_username'] = match['mentor_username'] if match else None
        d['match_round'] = None
        pref = conn.execute(
            """SELECT r.*,u.name mentor_name,u.username mentor_username FROM selection_requests r
               JOIN mentors mt ON mt.id=r.mentor_id JOIN users u ON u.id=mt.user_id
               WHERE r.student_id=? AND r.status='pending'""", (d['id'],)).fetchone()
        d['round_info'] = []
        if pref:
            d['round_info'].append({
                'rank': 1, 'mentor': pref['mentor_name'],
                'mentor_username': pref['mentor_username'],
                'created_at': pref['created_at'], 'chosen': False,
            })
        d['selection_status'] = 'matched' if match else ('pending' if pref else 'unselected')
        out.append(d)
    conn.close()
    return jsonify({'students': out, 'majors': majors, 'summary': summary,
                    'filters': {'q': q, 'major': major, 'status': status, 'account': account},
                    'pagination': {'page': page, 'page_size': page_size,
                                   'total': total, 'total_pages': total_pages}})


@app.post('/api/admin/student')
@login_required('admin')
def api_admin_student_save():
    d = request.json
    initial_password = (d.get('password') or '').strip()
    conn = get_db()
    if d.get('id'):
        s = conn.execute('SELECT * FROM students WHERE id=?', (d['id'],)).fetchone()
        if not s:
            conn.close()
            return jsonify({'error': '学生不存在'}), 400
        conn.execute('UPDATE students SET major=?, phone=?, accept_adjust=?, remark=? WHERE id=?',
                     (normalize_major(d.get('major', '')), d.get('phone', ''), 0,
                      d.get('remark', ''), d['id']))
        conn.execute('UPDATE users SET name=? WHERE id=?', (d['name'], s['user_id']))
        if initial_password:
            conn.execute(
                'UPDATE users SET password_hash=?,initial_password=?,must_change_password=1 '
                'WHERE id=?',
                (generate_password_hash(initial_password), initial_password, s['user_id']))
    else:
        username = (d.get('username') or '').strip()
        if not re.fullmatch(r'\d{8}', username):
            conn.close()
            return jsonify({'error': '学生学号必须为8位数字'}), 400
        exists = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if exists:
            conn.close()
            return jsonify({'error': f'学生账号 {username} 已存在'}), 400
        initial_password = initial_password or username
        cur = conn.execute(
            'INSERT INTO users(username,password_hash,initial_password,name,role,must_change_password) '
            'VALUES(?,?,?,?,?,1)',
            (username, generate_password_hash(initial_password), initial_password,
             d['name'], 'student'))
        conn.execute(
            'INSERT INTO students(user_id,major,phone,accept_adjust,remark) VALUES(?,?,?,?,?)',
            (cur.lastrowid, normalize_major(d.get('major', '')), d.get('phone', ''),
             0, d.get('remark', '')))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.post('/api/admin/student/toggle')
@login_required('admin')
def api_admin_student_toggle():
    d = request.json
    conn = get_db()
    try:
        s = conn.execute('SELECT * FROM students WHERE id=?', (d['id'],)).fetchone()
        if not s:
            return jsonify({'error': '学生不存在'}), 400
        u = conn.execute('SELECT active FROM users WHERE id=?', (s['user_id'],)).fetchone()
        new_active = 0 if u['active'] else 1
        conn.execute('UPDATE users SET active=? WHERE id=?', (new_active, s['user_id']))
        if not new_active:
            conn.execute(
                """UPDATE selection_requests SET status='admin_cancelled',
                   cancel_reason='student_disabled', operated_by=?,
                   responded_at=datetime('now','localtime'), updated_at=datetime('now','localtime')
                   WHERE student_id=? AND status='pending'""",
                (session['uid'], s['id']))
        conn.commit()
        return jsonify({'ok': True})
    except Exception:
        conn.rollback()
        app.logger.exception('启停学生账号失败')
        return jsonify({'error': '操作失败，请稍后重试'}), 500
    finally:
        conn.close()


@app.post('/api/admin/student/delete')
@login_required('admin')
def api_admin_student_delete():
    """删除学生，连带清理其志愿/决定/匹配记录"""
    conn = get_db()
    try:
        ok = _delete_student(conn, int(request.json['id']))
        if not ok:
            return jsonify({'error': '学生不存在'}), 400
        conn.commit()
        return jsonify({'ok': True})
    except Exception:
        conn.rollback()
        app.logger.exception('删除学生失败')
        return jsonify({'error': '删除失败，请稍后重试'}), 500
    finally:
        conn.close()


def _delete_student(conn, sid):
    """删除学生（连带清理志愿/决定/匹配），返回是否成功"""
    s = conn.execute('SELECT * FROM students WHERE id=?', (sid,)).fetchone()
    if not s:
        return False
    for p in conn.execute('SELECT id FROM preferences WHERE student_id=?',
                          (sid,)).fetchall():
        conn.execute('DELETE FROM decisions WHERE preference_id=?', (p['id'],))
    conn.execute('DELETE FROM preferences WHERE student_id=?', (sid,))
    conn.execute('DELETE FROM matches WHERE student_id=?', (sid,))
    conn.execute('DELETE FROM pairings WHERE student_id=?', (sid,))
    conn.execute('DELETE FROM selection_requests WHERE student_id=?', (sid,))
    conn.execute('DELETE FROM students WHERE id=?', (sid,))
    # 访问记录用于安全审计，需要保留账号、姓名和 IP；解除外键后才能删除用户。
    conn.execute('UPDATE access_logs SET user_id=NULL WHERE user_id=?', (s['user_id'],))
    conn.execute('DELETE FROM users WHERE id=?', (s['user_id'],))
    return True


def _delete_mentor(conn, mid):
    """删除导师；存在志愿/匹配记录时拒绝。返回 (成功?, 失败原因)"""
    m = conn.execute('SELECT * FROM mentors WHERE id=?', (mid,)).fetchone()
    if not m:
        return False, '导师不存在'
    u = conn.execute('SELECT name FROM users WHERE id=?', (m['user_id'],)).fetchone()
    mname = u['name'] if u else ''
    used = conn.execute(
        'SELECT COUNT(*) c FROM preferences WHERE r1=? OR r2=? OR r3=?',
        (m['id'], m['id'], m['id'])).fetchone()['c']
    matched = conn.execute('SELECT COUNT(*) c FROM matches WHERE mentor_id=?',
                           (m['id'],)).fetchone()['c']
    realtime_used = conn.execute(
        'SELECT COUNT(*) c FROM selection_requests WHERE mentor_id=?', (m['id'],)
    ).fetchone()['c']
    realtime_matched = conn.execute(
        'SELECT COUNT(*) c FROM pairings WHERE mentor_id=?', (m['id'],)
    ).fetchone()['c']
    if used or matched or realtime_used or realtime_matched:
        return False, (f'「{mname}」存在历史或实时申请/配对记录，已跳过')
    conn.execute('DELETE FROM mentor_majors WHERE mentor_id=?', (m['id'],))
    conn.execute('DELETE FROM mentors WHERE id=?', (m['id'],))
    conn.execute('DELETE FROM users WHERE id=?', (m['user_id'],))
    return True, None


def _reset_pwd(conn, kind, rid, new_pwd):
    """重置学生/导师密码，返回是否成功"""
    if kind == 'student':
        s = conn.execute('SELECT * FROM students WHERE id=?', (rid,)).fetchone()
        if not s:
            return False
        conn.execute(
            'UPDATE users SET password_hash=?,initial_password=?,must_change_password=1 '
            'WHERE id=?',
            (generate_password_hash(new_pwd), new_pwd, s['user_id']))
        return True
    if kind == 'mentor':
        m = conn.execute('SELECT * FROM mentors WHERE id=?', (rid,)).fetchone()
        if not m:
            return False
        conn.execute(
            'UPDATE users SET password_hash=?,initial_password=?,must_change_password=1 '
            'WHERE id=?',
            (generate_password_hash(new_pwd), new_pwd, m['user_id']))
        return True
    return False


@app.post('/api/admin/reset_password')
@login_required('admin')
def api_admin_reset_password():
    """管理员重置学生/导师个人密码"""
    d = request.json
    kind = d.get('kind')
    new_pwd = (d.get('password') or '').strip()
    if len(new_pwd) < 6:
        return jsonify({'error': '新密码至少6位'}), 400
    conn = get_db()
    ok = _reset_pwd(conn, kind, int(d['id']), new_pwd)
    conn.commit()
    conn.close()
    if not ok:
        return jsonify({'error': '用户不存在或类型错误'}), 400
    return jsonify({'ok': True})


@app.post('/api/admin/reset_password_batch')
@login_required('admin')
def api_admin_reset_password_batch():
    """批量重置密码（复选框选中多个用户）"""
    d = request.json
    kind = d.get('kind')
    ids = d.get('ids') or []
    new_pwd = (d.get('password') or '').strip()
    if len(new_pwd) < 6:
        return jsonify({'error': '新密码至少6位'}), 400
    conn = get_db()
    count = 0
    for rid in ids:
        if _reset_pwd(conn, kind, int(rid), new_pwd):
            count += 1
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'count': count})


@app.post('/api/admin/student/delete_batch')
@login_required('admin')
def api_admin_student_delete_batch():
    """批量删除学生（连带清理其数据）"""
    d = request.json
    ids = d.get('ids') or []
    conn = get_db()
    try:
        count = 0
        for rid in ids:
            if _delete_student(conn, int(rid)):
                count += 1
        conn.commit()
        return jsonify({'ok': True, 'deleted': count})
    except Exception:
        conn.rollback()
        app.logger.exception('批量删除学生失败')
        return jsonify({'error': '批量删除失败，请稍后重试'}), 500
    finally:
        conn.close()


@app.post('/api/admin/mentor/delete_batch')
@login_required('admin')
def api_admin_mentor_delete_batch():
    """批量删除导师；有志愿/匹配记录的自动跳过并返回名单"""
    d = request.json
    ids = d.get('ids') or []
    conn = get_db()
    deleted, skipped = 0, []
    for rid in ids:
        ok, msg = _delete_mentor(conn, int(rid))
        if ok:
            deleted += 1
        elif msg:
            skipped.append(msg)
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'deleted': deleted, 'skipped': skipped})


@app.post('/api/admin/batch_edit')
@login_required('admin')
def api_admin_batch_edit():
    """批量编辑：导师名额或学生专业。"""
    d = request.json
    kind, field = d.get('kind'), d.get('field')
    ids = d.get('ids') or []
    value = d.get('value')
    conn = get_db()
    try:
        if kind == 'mentor' and field == 'quota':
            q = int(value)
            if q < 1:
                conn.close()
                return jsonify({'error': '名额至少为1'}), 400
            for mid in ids:
                occupied = mentor_taken(conn, int(mid))
                if q < occupied:
                    row = conn.execute(
                        'SELECT u.name FROM mentors m JOIN users u ON u.id=m.user_id WHERE m.id=?',
                        (mid,),
                    ).fetchone()
                    conn.close()
                    name = row['name'] if row else f'ID {mid}'
                    return jsonify({'error': f'导师「{name}」已有 {occupied} 个名额被占用，不能设为 {q}'}), 400
            cur = conn.execute('UPDATE mentors SET quota=? WHERE id IN (%s)'
                               % ','.join('?' * len(ids)), [q] + ids)
        elif kind == 'student' and field == 'major':
            cur = conn.execute('UPDATE students SET major=? WHERE id IN (%s)'
                               % ','.join('?' * len(ids)), [normalize_major(value)] + ids)
        else:
            conn.close()
            return jsonify({'error': '不支持的批量编辑字段'}), 400
    except ValueError:
        conn.close()
        return jsonify({'error': '数值格式错误'}), 400
    count = cur.rowcount
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'count': count})


def parse_csv_import(text, kind):
    """解析导入 CSV。支持带表头（按列名映射）或不带表头（固定顺序）。
    学生：账号,姓名,专业,手机号,密码?；
    导师：工号,姓名,学院,职称,招生类别,可招生专业,研究方向,简介,名额,启用状态?,密码?
    返回 (ok, list[dict] | error)"""
    try:
        raw_rows = [[c.strip() for c in row] for row in csv.reader(io.StringIO(text))
                    if any(c.strip() for c in row)]
    except csv.Error:
        return False, 'CSV格式错误，请检查引号和分隔符'
    if not raw_rows:
        return False, '内容为空'
    header = None
    first = raw_rows[0]
    first[0] = first[0].replace('\ufeff', '') if first else ''
    if any(name in first for name in ('账号', '学号', '工号', '姓名')):
        header = first
        raw_rows = raw_rows[1:]
    rows = []
    for cells in raw_rows:
        if header:
            row = {header[i]: cells[i] if i < len(cells) else '' for i in range(len(header))}
        else:
            # 无表头兼容旧格式：学生=账号,姓名,专业,手机号,密码；导师沿用旧七列格式。
            row = {'username': cells[0] if len(cells) > 0 else '',
                   'name': cells[1] if len(cells) > 1 else '',
                   'major': cells[2] if len(cells) > 2 else '',
                   'phone': cells[3] if len(cells) > 3 else '',
                   'password': cells[4] if len(cells) > 4 else '',
                   'title': cells[2] if len(cells) > 2 else '',
                   'area': cells[3] if len(cells) > 3 else '',
                   'intro': cells[4] if len(cells) > 4 else '',
                   'quota': cells[5] if len(cells) > 5 else '',
                   'ext1': cells[2] if len(cells) > 2 else '',
                   'ext2': cells[3] if len(cells) > 3 else '',
                   'ext3': cells[4] if len(cells) > 4 else ''}
        rows.append(row)
    out = []

    def parse_enabled(value):
        normalized = str(value or '').strip().lower()
        if normalized in ('0', '否', '停用', '禁用', 'false', 'no', 'n'):
            return 0
        return 1

    for row in rows:
        if kind == 'student':
            username = row.get('username') or row.get('账号') or row.get('学号') or row.get('工号') or ''
            name = row.get('name') or row.get('姓名') or ''
            if not username or not name:
                continue
            out.append({'username': username, 'name': name,
                        'major': normalize_major(
                            row.get('major') or row.get('专业') or row.get('ext1') or ''),
                        'phone': row.get('phone') or row.get('手机号') or row.get('ext2') or '',
                        'password': row.get('password') or row.get('密码') or ''})
        else:
            username = row.get('username') or row.get('工号') or row.get('账号') or row.get('学号') or ''
            name = row.get('name') or row.get('姓名') or ''
            if not username or not name:
                continue
            out.append({'username': username, 'name': name,
                        'college': row.get('college') or row.get('学院') or
                                   row.get('所在学院') or '',
                        'title': row.get('title') or row.get('职称') or '',
                        'area': row.get('area') or row.get('研究方向') or row.get('ext1') or '',
                        'intro': row.get('intro') or row.get('简介') or '',
                        'admission_category': row.get('admission_category') or row.get('招生类别') or '',
                        'allowed_majors': parse_major_list(
                            row.get('allowed_majors') or row.get('可招生专业') or
                            row.get('招生专业') or row.get('导师招生资格') or ''),
                        'quota': int(row.get('quota') or row.get('名额') or 3),
                        'active': parse_enabled(
                            row.get('active') or row.get('启用状态') or row.get('状态') or '1'),
                        'password': row.get('password') or row.get('密码') or ''})
    return True, out


@app.post('/api/admin/import')
@login_required('admin')
def api_admin_import():
    d = request.json
    kind = d.get('kind')  # student / mentor
    if kind not in ('student', 'mentor'):
        return jsonify({'error': '导入类型错误'}), 400
    text = d.get('text', '')
    ok, result = parse_csv_import(text, kind)
    if not ok:
        return jsonify({'error': result}), 400
    if not result:
        return jsonify({'error': '没有解析到有效行'}), 400
    invalid_accounts = [r['username'] for r in result if not (
        re.fullmatch(r'\d{8}', r['username']) if kind == 'student'
        else valid_mentor_username(r['username'])
    )]
    if invalid_accounts:
        label = '学号' if kind == 'student' else '工号'
        return jsonify({'error': f'{label}格式错误：' + '、'.join(invalid_accounts[:8])}), 400
    conn = get_db()
    added, updated = 0, 0
    for r in result:
        exists = conn.execute('SELECT id,role FROM users WHERE username=?', (r['username'],)).fetchone()
        if exists:
            if exists['role'] != kind:
                conn.rollback()
                conn.close()
                return jsonify({'error': f'账号 {r["username"]} 已被其他角色使用'}), 409
            # 更新姓名/扩展字段
            active = r.get('active', 1) if kind == 'mentor' else 1
            conn.execute('UPDATE users SET name=?, active=? WHERE id=?',
                         (r['name'], active, exists['id']))
            if r.get('password'):
                conn.execute(
                    'UPDATE users SET password_hash=?,initial_password=?,must_change_password=1 '
                    'WHERE id=?',
                    (generate_password_hash(r['password']), r['password'], exists['id']))
            if kind == 'student':
                conn.execute(
                    'INSERT INTO students(user_id,major,phone) VALUES(?,?,?) '
                    'ON CONFLICT(user_id) DO UPDATE SET major=excluded.major, phone=excluded.phone',
                    (exists['id'], r['major'], r['phone']))
            else:
                conn.execute(
                    'INSERT INTO mentors(user_id,college,title,area,intro,admission_category,quota,active) '
                    'VALUES(?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(user_id) DO UPDATE SET college=excluded.college, '
                    'title=excluded.title, area=excluded.area, intro=excluded.intro, '
                    'admission_category=excluded.admission_category, '
                    'quota=excluded.quota, active=excluded.active',
                    (exists['id'], r['college'], r['title'], r['area'], r['intro'],
                     r['admission_category'], r['quota'], r['active']))
                mentor_row = conn.execute('SELECT id FROM mentors WHERE user_id=?',
                                          (exists['id'],)).fetchone()
                replace_mentor_majors(conn, mentor_row['id'], r['allowed_majors'])
            updated += 1
        else:
            initial_password = r.get('password') or r['username']
            cur = conn.execute(
                'INSERT INTO users(username,password_hash,initial_password,name,role,active,'
                'must_change_password) VALUES(?,?,?,?,?,?,1)',
                (r['username'], generate_password_hash(initial_password), initial_password,
                 r['name'], kind, r.get('active', 1)))
            if kind == 'student':
                conn.execute('INSERT INTO students(user_id,major,phone) VALUES(?,?,?)',
                             (cur.lastrowid, r['major'], r['phone']))
            else:
                mcur = conn.execute(
                    'INSERT INTO mentors(user_id,college,title,area,intro,admission_category,quota,active) '
                    'VALUES(?,?,?,?,?,?,?,?)',
                    (cur.lastrowid, r['college'], r['title'], r['area'], r['intro'],
                     r['admission_category'], r['quota'], r['active']))
                replace_mentor_majors(conn, mcur.lastrowid, r['allowed_majors'])
            added += 1
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'added': added, 'updated': updated})


@app.get('/api/admin/matches')
@login_required('admin')
def api_admin_matches():
    """匹配管理：按导师或学生视角返回服务端筛选、排序与分页数据。"""
    view = (request.args.get('view') or 'mentor').strip()
    if view not in ('mentor', 'student'):
        view = 'mentor'
    q = (request.args.get('q') or '').strip().lower()[:80]
    try:
        page = max(1, int(request.args.get('page', 1)))
        page_size = int(request.args.get('page_size', 20))
    except (TypeError, ValueError):
        page, page_size = 1, 20
    page_size = page_size if page_size in (10, 20, 50, 100) else 20

    conn = get_db()
    info = phase_info()

    mentor_source = [dict(r) for r in conn.execute(
        """SELECT m.id,u.username,u.name,m.college,m.title,m.admission_category,
                  m.quota,m.active mentor_active,u.active user_active
           FROM mentors m JOIN users u ON u.id=m.user_id
           ORDER BY m.college,u.name,u.username""").fetchall()]
    pairing_source = [dict(r) for r in conn.execute(
        """SELECT p.id,p.student_id,p.mentor_id,p.source,p.created_at,
                  su.name student_name,su.username student_no,s.major
           FROM pairings p JOIN students s ON s.id=p.student_id
           JOIN users su ON su.id=s.user_id ORDER BY p.created_at,p.id""").fetchall()]
    pending_source = [dict(r) for r in conn.execute(
        """SELECT r.id request_id,r.student_id,r.mentor_id,r.created_at,
                  su.name student_name,su.username student_no,s.major
           FROM selection_requests r JOIN students s ON s.id=r.student_id
           JOIN users su ON su.id=s.user_id
           WHERE r.status='pending' ORDER BY r.created_at,r.id""").fetchall()]

    paired_by_mentor = defaultdict(list)
    pending_by_mentor = defaultdict(list)
    pairing_by_student = {}
    for row in pairing_source:
        row['degree_category'] = student_degree_category(row['major'])
        paired_by_mentor[row['mentor_id']].append(row)
        pairing_by_student[row['student_id']] = row
    for row in pending_source:
        row['degree_category'] = student_degree_category(row['major'])
        pending_by_mentor[row['mentor_id']].append(row)

    mentor_rows = []
    mentor_options = []
    for mentor in mentor_source:
        accepted = paired_by_mentor[mentor['id']]
        pending = pending_by_mentor[mentor['id']]
        xueshu_matched = sum(r['degree_category'] == '学硕' for r in accepted)
        zhuanshu_matched = sum(r['degree_category'] == '专硕' for r in accepted)
        xueshu_pending = sum(r['degree_category'] == '学硕' for r in pending)
        zhuanshu_pending = sum(r['degree_category'] == '专硕' for r in pending)
        taken = len(accepted)
        row = {
            **mentor,
            'active': bool(mentor['mentor_active'] and mentor['user_active']),
            'taken': taken,
            'remaining': max(0, mentor['quota'] - taken),
            'xueshu_matched': xueshu_matched,
            'zhuanshu_matched': zhuanshu_matched,
            'xueshu_pending': xueshu_pending,
            'zhuanshu_pending': zhuanshu_pending,
            'pending': len(pending),
            'accepted_preview': [r['student_name'] for r in accepted[:3]],
            '_search': ' '.join([
                mentor['username'], mentor['name'], mentor['college'] or '',
                *[f"{r['student_no']} {r['student_name']}" for r in accepted],
                *[f"{r['student_no']} {r['student_name']}" for r in pending],
            ]).lower(),
        }
        mentor_rows.append(row)
        if row['active']:
            mentor_options.append({
                'id': row['id'], 'name': row['name'], 'username': row['username'],
                'quota': row['quota'], 'taken': taken, 'remaining': row['remaining'],
                'xueshu_matched': xueshu_matched,
                'xueshu_remaining': max(0, XUESHU_LIMIT - xueshu_matched),
            })

    active_students = [dict(r) for r in conn.execute(
        """SELECT s.id,u.username,u.name,s.major FROM students s
           JOIN users u ON u.id=s.user_id WHERE u.active=1 ORDER BY u.username""").fetchall()]
    mentor_lookup = {m['id']: m for m in mentor_source}
    student_rows = []
    for student in active_students:
        pairing = pairing_by_student.get(student['id'])
        mentor = mentor_lookup.get(pairing['mentor_id']) if pairing else None
        student_rows.append({
            **student,
            'degree_category': student_degree_category(student['major']),
            'matched': bool(pairing),
            'pairing_id': pairing['id'] if pairing else None,
            'mentor_id': pairing['mentor_id'] if pairing else None,
            'mentor_name': mentor['name'] if mentor else '',
            'mentor_username': mentor['username'] if mentor else '',
            'source': pairing['source'] if pairing else '',
            'created_at': pairing['created_at'] if pairing else '',
        })

    summary = {
        'paired': len(pairing_source),
        'unmatched': max(0, len(active_students) - len(pairing_source)),
        'pending': len(pending_source),
        'full_mentors': sum(m['taken'] >= m['quota'] for m in mentor_rows),
        'xueshu_at_limit': sum(m['xueshu_matched'] >= XUESHU_LIMIT for m in mentor_rows),
        'xueshu_exceeded': sum(m['xueshu_matched'] > XUESHU_LIMIT for m in mentor_rows),
    }

    result_rows = mentor_rows if view == 'mentor' else student_rows
    if view == 'mentor':
        college = (request.args.get('college') or '').strip()
        quota_status = (request.args.get('quota_status') or '').strip()
        xueshu_status = (request.args.get('xueshu_status') or '').strip()
        pending_status = (request.args.get('pending_status') or '').strip()
        if q:
            result_rows = [r for r in result_rows if q in r['_search']]
        if college:
            result_rows = [r for r in result_rows if r['college'] == college]
        if quota_status == 'available':
            result_rows = [r for r in result_rows if r['remaining'] > 0]
        elif quota_status == 'full':
            result_rows = [r for r in result_rows if r['remaining'] <= 0]
        if xueshu_status == 'below':
            result_rows = [r for r in result_rows if r['xueshu_matched'] < XUESHU_LIMIT]
        elif xueshu_status == 'reached':
            result_rows = [r for r in result_rows if r['xueshu_matched'] == XUESHU_LIMIT]
        elif xueshu_status == 'exceeded':
            result_rows = [r for r in result_rows if r['xueshu_matched'] > XUESHU_LIMIT]
        elif xueshu_status == 'at_limit':
            result_rows = [r for r in result_rows if r['xueshu_matched'] >= XUESHU_LIMIT]
        if pending_status == 'has_pending':
            result_rows = [r for r in result_rows if r['pending'] > 0]
        elif pending_status == 'none':
            result_rows = [r for r in result_rows if r['pending'] == 0]
        result_rows.sort(key=lambda r: (
            0 if r['xueshu_matched'] > XUESHU_LIMIT else
            1 if r['xueshu_matched'] == XUESHU_LIMIT else
            2 if r['remaining'] <= 0 else 3,
            r['college'] or '', r['name'], r['username']))
    else:
        status = (request.args.get('student_status') or '').strip()
        degree = (request.args.get('degree') or '').strip()
        major = (request.args.get('major') or '').strip()
        if q:
            result_rows = [r for r in result_rows if q in ' '.join([
                r['username'], r['name'], r['major'] or '', r['mentor_name'],
                r['mentor_username']]).lower()]
        if status == 'matched':
            result_rows = [r for r in result_rows if r['matched']]
        elif status == 'unmatched':
            result_rows = [r for r in result_rows if not r['matched']]
        if degree:
            result_rows = [r for r in result_rows if r['degree_category'] == degree]
        if major:
            result_rows = [r for r in result_rows if r['major'] == major]
        result_rows.sort(key=lambda r: r['username'])

    total = len(result_rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    page_rows = result_rows[start:start + page_size]
    for row in page_rows:
        row.pop('_search', None)
    # 筛选下拉同时提供配置项与数据中已在用的取值，保证旧数据也能被筛选到。
    option_source = options_service.merge_observed(conn, options_service.load(conn))
    colleges = option_source['colleges']
    majors = sorted(
        set(option_source['majors']) | {s['major'] for s in active_students if s['major']}
    )
    conn.close()
    return jsonify({
        'info': info, 'view': view, 'summary': summary,
        'mentor_rows': page_rows if view == 'mentor' else [],
        'student_rows': page_rows if view == 'student' else [],
        'mentors': mentor_options, 'colleges': colleges, 'majors': majors,
        'pagination': {'page': page, 'page_size': page_size, 'total': total,
                       'total_pages': total_pages},
    })


@app.get('/api/admin/matches/mentor/<int:mentor_id>')
@login_required('admin')
def api_admin_match_mentor_detail(mentor_id):
    """匹配管理导师详情抽屉。"""
    conn = get_db()
    mentor = conn.execute(
        """SELECT m.id,u.username,u.name,m.college,m.title,m.admission_category,
                  m.quota,m.active mentor_active,u.active user_active
           FROM mentors m JOIN users u ON u.id=m.user_id WHERE m.id=?""",
        (mentor_id,),
    ).fetchone()
    if not mentor:
        conn.close()
        return jsonify({'error': '导师不存在'}), 404
    accepted = [dict(r) for r in conn.execute(
        """SELECT p.id pairing_id,p.student_id,p.source,p.created_at,p.note,
                  u.username student_no,u.name student_name,s.major,s.phone
           FROM pairings p JOIN students s ON s.id=p.student_id
           JOIN users u ON u.id=s.user_id WHERE p.mentor_id=?
           ORDER BY s.major,u.username""", (mentor_id,)).fetchall()]
    pending = [dict(r) for r in conn.execute(
        """SELECT r.id request_id,r.student_id,r.created_at,
                  u.username student_no,u.name student_name,s.major,s.phone
           FROM selection_requests r JOIN students s ON s.id=r.student_id
           JOIN users u ON u.id=s.user_id
           WHERE r.mentor_id=? AND r.status='pending'
           ORDER BY r.created_at,r.id""", (mentor_id,)).fetchall()]
    for row in accepted + pending:
        row['degree_category'] = student_degree_category(row['major'])
    detail = dict(mentor)
    detail['active'] = bool(detail.pop('mentor_active') and detail.pop('user_active'))
    detail['taken'] = len(accepted)
    detail['remaining'] = max(0, detail['quota'] - len(accepted))
    detail['xueshu_matched'] = sum(r['degree_category'] == '学硕' for r in accepted)
    detail['zhuanshu_matched'] = sum(r['degree_category'] == '专硕' for r in accepted)
    conn.close()
    return jsonify({'mentor': detail, 'accepted': accepted, 'pending': pending,
                    'info': phase_info()})


@app.post('/api/admin/match')
@login_required('admin')
def api_admin_match():
    """管理员指定、改配或解除实时配对。"""
    d = request.get_json(silent=True) or {}
    try:
        student_id = int(d['student_id'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': '学生格式错误'}), 400
    mentor_id = d.get('mentor_id')
    if mentor_id not in (None, ''):
        try:
            mentor_id = int(mentor_id)
        except (TypeError, ValueError):
            return jsonify({'error': '导师格式错误'}), 400
    conn = get_db()
    try:
        result = admin_set_pairing(
            conn, session['uid'], student_id, mentor_id, (d.get('note') or '').strip())
        return jsonify({'ok': True, **result})
    except MatchingError as e:
        return jsonify({'error': str(e)}), 409
    finally:
        conn.close()


@app.get('/api/admin/audit_logs')
@login_required('admin')
def api_admin_audit_logs():
    q = (request.args.get('q') or '').strip()[:80]
    action = (request.args.get('action') or '').strip()[:80]
    status = (request.args.get('status') or '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
        page_size = int(request.args.get('page_size', 20))
    except (TypeError, ValueError):
        page, page_size = 1, 20
    if page_size not in (20, 50, 100):
        page_size = 20
    where, params = [], []
    if q:
        where.append('(username LIKE ? OR operator_name LIKE ? OR detail LIKE ? OR ip LIKE ?)')
        params.extend((f'%{q}%',) * 4)
    if action:
        where.append('action=?')
        params.append(action)
    if status in ('success', 'failed'):
        where.append('status=?')
        params.append(status)
    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) c FROM audit_logs' + where_sql, params).fetchone()['c']
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    rows = conn.execute(
        'SELECT * FROM audit_logs' + where_sql + ' ORDER BY id DESC LIMIT ? OFFSET ?',
        params + [page_size, (page - 1) * page_size],
    ).fetchall()
    actions = [r['action'] for r in conn.execute(
        'SELECT DISTINCT action FROM audit_logs ORDER BY action'
    ).fetchall()]
    conn.close()
    return jsonify({'logs': [dict(r) for r in rows], 'actions': actions,
                    'pagination': {'page': page, 'page_size': page_size,
                                   'total': total, 'total_pages': total_pages}})


def csv_safe(value):
    """阻止导出的单元格被 Excel 当作公式执行。"""
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    return "'" + value if stripped.startswith(('=', '+', '-', '@')) else value


def csv_row(writer, values):
    writer.writerow([csv_safe(v) for v in values])


ACCOUNT_ROLE_LABELS = {'student': '学生', 'mentor': '导师', 'admin': '管理员'}
ACCOUNT_EXPORT_HEADERS = ['账号', '姓名', '身份', '初始密码', '初始密码状态', '账号状态', '首次登录改密']


def account_export_rows(conn, role):
    """返回指定身份的初始凭据及其当前是否仍有效。"""
    rows = conn.execute(
        """SELECT username,name,role,password_hash,initial_password,active,must_change_password
           FROM users WHERE role=? ORDER BY username""", (role,)
    ).fetchall()
    result = []
    for row in rows:
        initial_password = row['initial_password']
        if not initial_password and row['role'] != 'student':
            initial_password = ('admin123' if row['role'] == 'admin' and
                                row['username'] == 'admin' else row['username'])
        initial_valid = bool(initial_password) and check_password_hash(
            row['password_hash'], initial_password)
        if initial_valid:
            initial_status = '初始密码可用'
        elif row['role'] == 'student' and not initial_password:
            initial_status = '已修改，初始密码不再保留'
        else:
            initial_status = '已修改或重置，初始密码已失效'
        result.append([
            row['username'], row['name'], ACCOUNT_ROLE_LABELS[row['role']],
            initial_password or '—', initial_status,
            '启用' if row['active'] else '停用',
            '是' if row['must_change_password'] else '否',
        ])
    return result


def combined_accounts_workbook(conn):
    """生成包含学生、导师和管理员三个工作表的账号工作簿。"""
    if openpyxl is None:
        return None
    from openpyxl.styles import Alignment, Font, PatternFill

    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    widths = [18, 16, 12, 18, 30, 12, 16]
    for role, sheet_name in (('student', '学生账户'), ('mentor', '导师账户'),
                             ('admin', '管理员账户')):
        sheet = workbook.create_sheet(sheet_name)
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = 'A2'
        sheet.append(ACCOUNT_EXPORT_HEADERS)
        for values in account_export_rows(conn, role):
            sheet.append([csv_safe(value) for value in values])
        sheet.auto_filter.ref = f'A1:G{max(1, sheet.max_row)}'
        sheet.row_dimensions[1].height = 26
        for cell in sheet[1]:
            cell.fill = PatternFill('solid', fgColor='1677FF')
            cell.font = Font(color='FFFFFF', bold=True)
            cell.alignment = Alignment(horizontal='center', vertical='center')
        for row_index in range(2, sheet.max_row + 1):
            sheet.row_dimensions[row_index].height = 22
            if row_index % 2 == 0:
                for cell in sheet[row_index]:
                    cell.fill = PatternFill('solid', fgColor='F5F8FF')
            for cell in sheet[row_index]:
                cell.alignment = Alignment(vertical='center')
            sheet.cell(row_index, 1).number_format = '@'
            sheet.cell(row_index, 4).number_format = '@'
        for col_index, width in enumerate(widths, 1):
            sheet.column_dimensions[openpyxl.utils.get_column_letter(col_index)].width = width
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


@app.get('/api/admin/export')
@login_required('admin')
def api_admin_export():
    etype = request.args.get('type', 'final')
    conn = get_db()
    if etype == 'accounts_all':
        data = combined_accounts_workbook(conn)
        conn.close()
        if data is None:
            return jsonify({'error': '服务器未安装 openpyxl，无法生成 Excel 文件'}), 500
        filename = quote('初始账号密码汇总.xlsx')
        return Response(data, mimetype=(
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            headers={'Content-Disposition': f"attachment; filename*=UTF-8''{filename}",
                     'Cache-Control': 'no-store'})
    buf = io.StringIO()
    w = csv.writer(buf)
    if etype == 'final':
        csv_row(w, ['学号', '姓名', '专业', '手机号', '最终导师', '配对时间', '配对方式'])
        rows = conn.execute(
            """SELECT s.id, u.username, u.name, s.major, s.phone, s.accept_adjust, s.remark
               FROM students s JOIN users u ON u.id=s.user_id WHERE u.active=1
               ORDER BY u.username""").fetchall()
        for r in rows:
            m = conn.execute(
                'SELECT mu.name mname,p.created_at,p.source FROM pairings p '
                'JOIN mentors mt ON mt.id=p.mentor_id JOIN users mu ON mu.id=mt.user_id '
                'WHERE p.student_id=?', (r['id'],)).fetchone()
            csv_row(w, [r['username'], r['name'], r['major'], r['phone'],
                        m['mname'] if m else '未匹配', m['created_at'] if m else '',
                        ('学生申请·导师同意' if m and m['source'] == 'request' else
                         '管理员调整' if m else '')])
    elif etype == 'preferences':
        csv_row(w, ['学号', '姓名', '专业', '申请导师', '申请状态', '申请时间', '处理时间', '取消原因'])
        rows = conn.execute(
            """SELECT r.*,su.username,su.name,s.major,mu.name mentor_name
               FROM selection_requests r JOIN students s ON s.id=r.student_id
               JOIN users su ON su.id=s.user_id JOIN mentors m ON m.id=r.mentor_id
               JOIN users mu ON mu.id=m.user_id ORDER BY r.id""").fetchall()
        status_cn = {'pending': '待导师审核', 'accepted': '已配对', 'rejected': '导师已拒绝',
                     'student_cancelled': '学生已撤回', 'auto_cancelled': '系统自动退回',
                     'admin_cancelled': '管理员已取消'}
        reason_cn = {'mentor_full': '导师名额已满', 'xueshu_limit': '导师学硕录取已达3人上限',
                     'mentor_rejected': '导师拒绝',
                     'student_withdrew': '学生撤回', 'changed_selection': '学生改选导师',
                     'admin_unpaired': '管理员解除配对', 'admin_reassigned': '管理员改配',
                     'admin_assigned_elsewhere': '管理员分配其他导师',
                     'mentor_disabled': '导师账号停用', 'student_disabled': '学生账号停用'}
        for r in rows:
            csv_row(w, [r['username'], r['name'], r['major'], r['mentor_name'],
                        status_cn.get(r['status'], r['status']), r['created_at'],
                        r['responded_at'] or '', reason_cn.get(r['cancel_reason'], r['cancel_reason'])])
    elif etype == 'unmatched':
        csv_row(w, ['学号', '姓名', '专业', '状态'])
        rows = conn.execute(
            """SELECT s.id, u.username, u.name, s.major, s.phone, s.accept_adjust FROM students s
               JOIN users u ON u.id=s.user_id WHERE u.active=1 ORDER BY u.username""").fetchall()
        for r in rows:
            m = conn.execute('SELECT 1 FROM pairings WHERE student_id=?', (r['id'],)).fetchone()
            if m:
                continue
            pending = conn.execute(
                "SELECT 1 FROM selection_requests WHERE student_id=? AND status='pending'",
                (r['id'],)).fetchone()
            unmatched_status = '待导师审核' if pending else '可选择导师'
            csv_row(w, [r['username'], r['name'], r['major'], unmatched_status])
    elif etype == 'mentors':
        csv_row(w, ['工号', '姓名', '学院', '职称', '招生类别', '可招生专业', '研究方向', '简介', '名额', '已匹配', '剩余'])
        rows = conn.execute(
            'SELECT m.*, u.username, u.name FROM mentors m JOIN users u ON u.id=m.user_id '
            'ORDER BY m.id').fetchall()
        for r in rows:
            taken = conn.execute('SELECT COUNT(*) c FROM pairings WHERE mentor_id=?',
                                 (r['id'],)).fetchone()['c']
            csv_row(w, [r['username'], r['name'], r['college'], r['title'], r['admission_category'],
                        '；'.join(mentor_major_names(conn, r['id'])), r['area'], r['intro'],
                        r['quota'], taken, max(0, r['quota'] - taken)])
    elif etype == 'students':
        csv_row(w, ['账号', '姓名', '专业', '手机号', '备注'])
        rows = conn.execute(
            'SELECT u.username, u.name, s.major, s.phone, s.remark FROM students s '
            'JOIN users u ON u.id=s.user_id ORDER BY u.username').fetchall()
        for r in rows:
            csv_row(w, [r['username'], r['name'], r['major'], r['phone'], r['remark']])
    elif etype == 'accounts':
        role = request.args.get('role', '').strip()
        if role not in ACCOUNT_ROLE_LABELS:
            conn.close()
            return jsonify({'error': '请选择学生、导师或管理员账号'}), 400
        csv_row(w, ACCOUNT_EXPORT_HEADERS)
        for values in account_export_rows(conn, role):
            csv_row(w, values)
    else:
        conn.close()
        return jsonify({'error': '不支持的导出类型'}), 400
    conn.close()
    data = buf.getvalue()
    if etype == 'accounts':
        fname = f'{ACCOUNT_ROLE_LABELS[role]}初始账号密码.csv'
    else:
        fname = {'final': '最终互选名单.csv', 'preferences': '申请处理明细.csv',
                 'unmatched': '未匹配名单.csv', 'mentors': '导师名单.csv',
                 'students': '学生名单.csv'}.get(etype, 'export.csv')
    return Response('\ufeff' + data, mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(fname)}",
                             'Cache-Control': 'no-store'})


# 导入即初始化数据库（幂等）
init_db()

# ---------- 导师信息总表（管理员上传，学生端下载） ----------

def _info_files():
    if not os.path.isdir(MENTOR_INFO_DIR):
        return []
    return [f for f in os.listdir(MENTOR_INFO_DIR)
            if os.path.splitext(f)[1].lower() in ALLOWED_INFO_EXT]


@app.post('/api/admin/mentor_info')
@login_required('admin')
def api_admin_upload_mentor_info():
    """管理员上传导师信息表格（.xlsx/.xls/.csv），供学生端下载"""
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': '请选择要上传的文件'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_INFO_EXT:
        return jsonify({'error': '仅支持 .xlsx / .xls / .csv 格式'}), 400
    os.makedirs(MENTOR_INFO_DIR, exist_ok=True)
    temp_path = os.path.join(MENTOR_INFO_DIR, f'.upload-{secrets.token_hex(8)}{ext}')
    final_path = os.path.join(MENTOR_INFO_DIR, f'mentor_info{ext}')
    try:
        f.save(temp_path)
        size = os.path.getsize(temp_path)
        if size <= 0 or size > 8 * 1024 * 1024:
            return jsonify({'error': '文件必须小于8MB且内容不能为空'}), 400
        with open(temp_path, 'rb') as fh:
            head = fh.read(8)
        if ext == '.xlsx' and not head.startswith(b'PK'):
            return jsonify({'error': '文件内容不是有效的 .xlsx 文件'}), 400
        if ext == '.xls' and not head.startswith(b'\xd0\xcf\x11\xe0'):
            return jsonify({'error': '文件内容不是有效的 .xls 文件'}), 400
        if ext == '.csv':
            with open(temp_path, 'rb') as fh:
                sample = fh.read(min(size, 65536))
            if b'\x00' in sample:
                return jsonify({'error': 'CSV 文件包含无效的二进制内容'}), 400
        os.replace(temp_path, final_path)
        try:
            os.chmod(final_path, 0o600)
        except OSError:
            pass
        for old in _info_files():
            old_path = os.path.join(MENTOR_INFO_DIR, old)
            if os.path.abspath(old_path) != os.path.abspath(final_path):
                os.remove(old_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    from datetime import datetime
    conn = get_db()
    conn.execute("INSERT INTO settings(key,value) VALUES('mentor_info_file',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f.filename,))
    conn.execute("INSERT INTO settings(key,value) VALUES('mentor_info_time',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (datetime.now().strftime('%Y-%m-%d %H:%M'),))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.get('/api/admin/mentor_info/status')
@login_required('admin')
def api_admin_mentor_info_status():
    files = _info_files()
    fname = files[0] if files else None
    size = os.path.getsize(os.path.join(MENTOR_INFO_DIR, fname)) if fname else 0
    return jsonify({'exists': bool(fname),
                    'filename': get_setting('mentor_info_file', ''),
                    'updated_at': get_setting('mentor_info_time', ''),
                    'size': size})


@app.post('/api/admin/mentor_info/delete')
@login_required('admin')
def api_admin_mentor_info_delete():
    """删除已上传的导师信息表，学生端恢复为空白占位表"""
    for old in _info_files():
        os.remove(os.path.join(MENTOR_INFO_DIR, old))
    conn = get_db()
    conn.execute("DELETE FROM settings WHERE key='mentor_info_file'")
    conn.execute("DELETE FROM settings WHERE key='mentor_info_time'")
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


def build_placeholder_xlsx():
    """空白占位导师信息表（管理员未上传时供学生下载）"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '导师信息'
    ws.append(['序号', '姓名', '职称', '研究方向', '招生名额', '简介'])
    for col, w in zip('ABCDEF', (8, 14, 10, 30, 12, 50)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@app.get('/download/mentor_info')
@login_required()
def download_mentor_info():
    """学生端下载导师信息总表：有上传用上传的，否则返回空白占位表"""
    files = _info_files()
    if files:
        fname = files[0]
        with open(os.path.join(MENTOR_INFO_DIR, fname), 'rb') as fh:
            data = fh.read()
        dl_name = get_setting('mentor_info_file', '') or fname
        mime = {'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'xls': 'application/vnd.ms-excel',
                'csv': 'text/csv; charset=utf-8'}.get(fname.rsplit('.', 1)[-1], 'application/octet-stream')
        return Response(data, mimetype=mime,
                        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(dl_name)}"})
    if openpyxl is None:
        return jsonify({'error': '服务器未安装 openpyxl，无法生成占位表，请联系管理员'}), 500
    buf = build_placeholder_xlsx()
    return Response(buf.getvalue(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': "attachment; filename*=UTF-8''%E5%AF%BC%E5%B8%88%E4%BF%A1%E6%81%AF%E6%80%BB%E8%A1%A8.xlsx"})


if __name__ == '__main__':
    print(' * Starting on 0.0.0.0:8080')
    app.run(host='0.0.0.0', port=8080, debug=False)
