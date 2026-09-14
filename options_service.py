# -*- coding: utf-8 -*-
"""学院、专业、招生类别等基础选项的可配置存储。

管理员在“互选控制”里维护这些选项；导师设置、学生设置与批量导入都以这里
的配置作为下拉来源，不再把学院/专业写死在代码里。
"""
import json
import re
import threading

from db import get_db


DEFAULT_COLLEGES = ['人工智能学院', '电子与电气工程学院']
DEFAULT_MAJORS = [
    '计算机科学与技术', '智能科学与技术', '计算机技术',
    '应用统计', '农业工程与信息技术',
]
DEFAULT_CATEGORIES = ['学硕/专硕', '学硕', '专硕']
DEFAULT_TITLES = ['教授', '副教授', '特任教授', '特任副教授', '讲师', '高级实验师']
DEFAULT_DEGREE_MAP = {
    '计算机科学与技术': '学硕',
    '智能科学与技术': '学硕',
    '人工智能': '学硕',  # 兼容既有演示/历史数据
    '计算机技术': '专硕',
    '计算机技术（联培）': '专硕',
    '应用统计': '专硕',
    '农业工程与信息技术': '专硕',
}

DEGREE_OPTIONS = ('学硕', '专硕')

SETTING_KEYS = {
    'colleges': 'options_colleges',
    'majors': 'options_majors',
    'categories': 'options_categories',
    'titles': 'options_titles',
    'degree_map': 'options_degree_map',
}

MAX_ITEMS = 200
MAX_ITEM_LENGTH = 60

_lock = threading.Lock()
_cache = None


def normalize_major(value):
    """移除来源表中的专业代码后缀，如“计算机技术(085404)”。"""
    major = str(value or '').strip()
    return re.sub(r'\s*[（(]\d{6}[）)]\s*$', '', major).strip()


def _clean_list(value, limit=MAX_ITEMS):
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = re.split(r'[\r\n|,，、;；]+', str(value or ''))
    result = []
    for item in items:
        text = str(item or '').strip()[:MAX_ITEM_LENGTH]
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def defaults():
    return {
        'colleges': list(DEFAULT_COLLEGES),
        'majors': list(DEFAULT_MAJORS),
        'categories': list(DEFAULT_CATEGORIES),
        'titles': list(DEFAULT_TITLES),
        'degree_map': dict(DEFAULT_DEGREE_MAP),
    }


def _read_setting(conn, key):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    if not row or row['value'] in (None, ''):
        return None
    try:
        return json.loads(row['value'])
    except (TypeError, ValueError):
        # 兼容手工写入的纯文本列表
        return row['value']


def load(conn=None):
    """读取当前配置；缺省项回落到内置默认值。"""
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        options = defaults()
        stored_colleges = _read_setting(conn, SETTING_KEYS['colleges'])
        if stored_colleges is not None:
            options['colleges'] = _clean_list(stored_colleges)
        stored_majors = _read_setting(conn, SETTING_KEYS['majors'])
        if stored_majors is not None:
            options['majors'] = [normalize_major(m) for m in _clean_list(stored_majors)]
        stored_categories = _read_setting(conn, SETTING_KEYS['categories'])
        if stored_categories is not None:
            options['categories'] = _clean_list(stored_categories)
        stored_titles = _read_setting(conn, SETTING_KEYS['titles'])
        if stored_titles is not None:
            options['titles'] = _clean_list(stored_titles)
        stored_degrees = _read_setting(conn, SETTING_KEYS['degree_map'])
        if isinstance(stored_degrees, dict):
            degree_map = {}
            for key, value in stored_degrees.items():
                major = normalize_major(key)
                degree = str(value or '').strip()
                if major and degree in DEGREE_OPTIONS:
                    degree_map[major] = degree
            options['degree_map'] = degree_map
        return options
    finally:
        if own_conn:
            conn.close()


def current():
    """进程内缓存快照，供无数据库连接的校验函数使用。"""
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                _cache = load()
    return _cache


def refresh(conn=None):
    """重新加载配置缓存。传入正在写入的连接时，可以读到尚未提交的最新值。"""
    global _cache
    with _lock:
        _cache = load(conn)
    return _cache


def save(conn, payload):
    """写入配置并刷新缓存；返回规范化后的配置。"""
    previous = load(conn).get('degree_map') or {}
    options = {
        'colleges': _clean_list(payload.get('colleges')),
        'majors': [normalize_major(m) for m in _clean_list(payload.get('majors'))],
        'categories': _clean_list(payload.get('categories')),
        'titles': _clean_list(payload.get('titles')),
        'degree_map': {},
    }
    raw_degrees = payload.get('degree_map') or {}
    if not isinstance(raw_degrees, dict):
        raw_degrees = {}
    for major in options['majors']:
        # 未提交的专业沿用上一次配置，避免前端漏传导致学位类别被清空。
        submitted = raw_degrees.get(major)
        if submitted is None:
            degree = str(previous.get(major) or '').strip()
        else:
            degree = str(submitted).strip()
        options['degree_map'][major] = degree if degree in DEGREE_OPTIONS else ''
    upsert = ('INSERT INTO settings(key,value) VALUES(?,?) '
              'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
    for field, key in SETTING_KEYS.items():
        conn.execute(upsert, (key, json.dumps(options[field], ensure_ascii=False)))
    # 用当前连接刷新缓存，保证紧接着的请求就按新配置校验。
    refresh(conn)
    return options


def merge_observed(conn, options):
    """把数据库里已在用、但不在配置里的取值补进选项，避免旧数据无法筛选。"""
    merged = {
        'colleges': list(options.get('colleges') or []),
        'majors': list(options.get('majors') or []),
        'categories': list(options.get('categories') or []),
        'titles': list(options.get('titles') or []),
        'degree_map': dict(options.get('degree_map') or {}),
    }
    for row in conn.execute(
        "SELECT DISTINCT college FROM mentors WHERE TRIM(college)!=''"
    ):
        value = row['college']
        if value not in merged['colleges']:
            merged['colleges'].append(value)
    for row in conn.execute(
        "SELECT DISTINCT admission_category FROM mentors WHERE TRIM(admission_category)!=''"
    ):
        value = row['admission_category']
        if value not in merged['categories']:
            merged['categories'].append(value)
    for row in conn.execute(
        "SELECT DISTINCT title FROM mentors WHERE TRIM(title)!=''"
    ):
        value = row['title']
        if value not in merged['titles']:
            merged['titles'].append(value)
    for table in ('mentor_majors', 'students'):
        for row in conn.execute(
            f"SELECT DISTINCT major FROM {table} WHERE TRIM(major)!=''"
        ):
            value = normalize_major(row['major'])
            if value and value not in merged['majors']:
                merged['majors'].append(value)
    return merged


def degree_category_for(major, options=None):
    """按配置推导学位类别；未配置时回落到内置默认表。"""
    name = normalize_major(major)
    if not name:
        return ''
    options = options or current()
    degree_map = options.get('degree_map') or {}
    degree = str(degree_map.get(name) or '').strip()
    if degree in DEGREE_OPTIONS:
        return degree
    return DEFAULT_DEGREE_MAP.get(name, '')
