# -*- coding: utf-8 -*-
"""数据库初始化与连接。SQLite 单文件数据库。"""
import os
import sqlite3
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.environ.get('MENTOR_DB_PATH', os.path.join(DATA_DIR, 'mentor.db'))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    initial_password TEXT,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','student','mentor')),
    active INTEGER DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS mentors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE NOT NULL REFERENCES users(id),
    college TEXT DEFAULT '',
    title TEXT DEFAULT '',
    area TEXT DEFAULT '',
    intro TEXT DEFAULT '',
    admission_category TEXT DEFAULT '',
    quota INTEGER NOT NULL DEFAULT 3,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS mentor_majors (
    mentor_id INTEGER NOT NULL REFERENCES mentors(id) ON DELETE CASCADE,
    major TEXT NOT NULL,
    PRIMARY KEY(mentor_id, major)
);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE NOT NULL REFERENCES users(id),
    major TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    accept_adjust INTEGER DEFAULT 1,
    remark TEXT DEFAULT '',
    intro TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    round INTEGER NOT NULL DEFAULT 1,
    r1 INTEGER, r2 INTEGER, r3 INTEGER,
    submitted_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(student_id, round)
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    preference_id INTEGER NOT NULL REFERENCES preferences(id),
    student_id INTEGER NOT NULL,
    mentor_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    decision TEXT DEFAULT 'pending' CHECK(decision IN ('pending','approved','rejected')),
    decided_at TEXT,
    UNIQUE(preference_id, mentor_id, rank)
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    mentor_id INTEGER NOT NULL,
    round INTEGER NOT NULL,
    source TEXT DEFAULT 'volunteer' CHECK(source IN ('volunteer','adjust','manual')),
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(student_id, round)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT NOT NULL DEFAULT '',
    operator_name TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'success' CHECK(status IN ('success','failed')),
    ip TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS access_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    username TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- V2 实时互选申请。旧 preferences/decisions/matches 表保留，便于历史回滚，
-- 新流程只读写 selection_requests/pairings。
CREATE TABLE IF NOT EXISTS selection_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    mentor_id INTEGER NOT NULL REFERENCES mentors(id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
        'pending','accepted','rejected','student_cancelled',
        'auto_cancelled','admin_cancelled'
    )),
    cancel_reason TEXT NOT NULL DEFAULT '',
    operated_by INTEGER REFERENCES users(id),
    created_at TEXT DEFAULT (datetime('now','localtime')),
    responded_at TEXT,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS pairings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER UNIQUE NOT NULL REFERENCES students(id),
    mentor_id INTEGER NOT NULL REFERENCES mentors(id),
    request_id INTEGER REFERENCES selection_requests(id),
    source TEXT NOT NULL DEFAULT 'request' CHECK(source IN ('request','manual')),
    operated_by INTEGER REFERENCES users(id),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_access_logs_created_at ON access_logs(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_access_logs_ip_account ON access_logs(ip, username, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mentor_majors_major ON mentor_majors(major, mentor_id);
CREATE INDEX IF NOT EXISTS idx_selection_requests_mentor_status
    ON selection_requests(mentor_id, status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_selection_requests_student_history
    ON selection_requests(student_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_selection_requests_student_active
    ON selection_requests(student_id) WHERE status IN ('pending','accepted');
CREATE INDEX IF NOT EXISTS idx_pairings_mentor ON pairings(mentor_id, id);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 10000')
    conn.execute('PRAGMA journal_mode = WAL')  # 多线程读写安全
    return conn


def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute('INSERT INTO settings(key,value) VALUES(?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
    conn.commit()
    conn.close()

def init_db(seed=True):
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_db()
    conn.executescript(SCHEMA)

    # 默认设置
    defaults = {'current_round': '1', 'phase': 'closed',
                'matching_mode': 'realtime', 'selection_open': '0',
                'force_password_change': '1',
                'xueshu_limit_enabled': '1',
                'mentor_hot_badge_enabled': '0',
                'sys_title': '研究生导师互选系统'}
    for k, v in defaults.items():
        conn.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (k, v))

    # 当前轮是否已经正式结算。旧数据库首次升级时：若当前轮已有匹配且流程已关闭，
    # 可安全推断为已结算；无法判断时宁可显示“等待处理”，避免提前宣告学生落选。
    if not conn.execute("SELECT 1 FROM settings WHERE key='current_round_settled'").fetchone():
        current_round = int(conn.execute(
            "SELECT value FROM settings WHERE key='current_round'").fetchone()['value'])
        phase = conn.execute("SELECT value FROM settings WHERE key='phase'").fetchone()['value']
        has_match = conn.execute(
            'SELECT 1 FROM matches WHERE round=? LIMIT 1', (current_round,)).fetchone()
        inferred = '1' if phase == 'closed' and has_match else '0'
        conn.execute("INSERT INTO settings(key,value) VALUES('current_round_settled',?)", (inferred,))

    # 账号初始凭据需在创建默认管理员前迁移，兼容已有空库升级。
    user_cols = {r['name'] for r in conn.execute('PRAGMA table_info(users)').fetchall()}
    if 'initial_password' not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN initial_password TEXT")

    if seed:
        # 管理员
        if not conn.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            conn.execute(
                'INSERT INTO users(username,password_hash,initial_password,name,role) '
                'VALUES(?,?,?,?,?)',
                ('admin', generate_password_hash('admin123'), 'admin123', '管理员', 'admin'))

    # 旧库自动迁移：补齐新增列
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(students)').fetchall()}
    if 'intro' not in cols:
        conn.execute("ALTER TABLE students ADD COLUMN intro TEXT DEFAULT ''")

    mentor_cols = {r['name'] for r in conn.execute('PRAGMA table_info(mentors)').fetchall()}
    if 'college' not in mentor_cols:
        conn.execute("ALTER TABLE mentors ADD COLUMN college TEXT DEFAULT ''")
    if 'admission_category' not in mentor_cols:
        conn.execute("ALTER TABLE mentors ADD COLUMN admission_category TEXT DEFAULT ''")

    user_cols = {r['name'] for r in conn.execute('PRAGMA table_info(users)').fetchall()}
    if 'must_change_password' not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")

    if seed and os.environ.get('SEED_DEMO', '0') == '1':
        # 演示导师（仅在没有任何导师时）
        if not conn.execute("SELECT 1 FROM users WHERE role='mentor'").fetchone():
            demo_mentors = [
                ('t001', '张老师', '教授', '生物信息学：耐药基因识别、机器学习', '专注肠球菌耐药基因识别与深度学习建模，欢迎有编程基础的同学。', 3),
                ('t002', '王老师', '副教授', '计算机视觉、图像处理', '研究方向为计算机视觉，课题偏算法应用。', 2),
                ('t003', '李老师', '讲师', '自然语言处理、大模型应用', '研究大语言模型与领域应用，可指导 Agent 相关课题。', 2),
            ]
            for u, n, t, a, i, q in demo_mentors:
                cur = conn.execute(
                    'INSERT INTO users(username,password_hash,initial_password,name,role) '
                    'VALUES(?,?,?,?,?)',
                    (u, generate_password_hash('123456'), '123456', n, 'mentor'))
                conn.execute('INSERT INTO mentors(user_id,title,area,intro,quota) VALUES(?,?,?,?,?)',
                             (cur.lastrowid, t, a, i, q))
        # 演示学生
        if not conn.execute("SELECT 1 FROM users WHERE role='student'").fetchone():
            demo_students = [
                ('2026001', '张小', '人工智能', '13800000001'),
                ('2026002', '李四', '计算机技术', '13800000002'),
                ('2026003', '王五', '人工智能', '13800000003'),
            ]
            for u, n, m, p in demo_students:
                cur = conn.execute(
                    'INSERT INTO users(username,password_hash,initial_password,name,role) '
                    'VALUES(?,?,?,?,?)',
                    (u, generate_password_hash('123456'), '123456', n, 'student'))
                conn.execute('INSERT INTO students(user_id,major,phone) VALUES(?,?,?)',
                             (cur.lastrowid, m, p))
    conn.commit()
    conn.close()

    # Linux 生产环境中数据库包含账号与联系方式，仅允许服务账号读写。
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


if __name__ == '__main__':
    init_db()
    print('DB ready:', DB_PATH)
