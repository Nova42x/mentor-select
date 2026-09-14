# -*- coding: utf-8 -*-
"""基础选项设置（学院/专业/招生类别/职称）回归测试。"""
import json
import os
import sys

from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import db

db.DB_PATH = os.path.join(BASE, 'data', 'test-options.db')
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)

from app import app
from matching_service import is_xueshu_major
import options_service

checks = []


def check(name, condition):
    checks.append(bool(condition))
    print(('PASS' if condition else 'FAIL'), name)


def token(client):
    with client.session_transaction() as sess:
        return sess['_csrf']


def post(client, url, payload=None, form=None):
    headers = {'X-CSRF-Token': token(client)}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        return client.post(url, headers=headers, data=json.dumps(payload))
    return client.post(url, headers=headers, data=form or {})


def login(client, username, password):
    client.get('/login')
    return post(client, '/login', form={'username': username, 'password': password})


conn = db.get_db()
mentor_user_id = conn.execute(
    """INSERT INTO users(username,password_hash,name,role,initial_password,must_change_password)
       VALUES('2888888',?,'选项测试导师','mentor','2888888',1)""",
    (generate_password_hash('2888888'),),
).lastrowid
conn.execute(
    "INSERT INTO mentors(user_id,college,title,admission_category,quota) VALUES(?,?,?,?,3)",
    (mentor_user_id, '旧学院名', '老职称', '旧类别'),
)
# 另一位导师使用配置里的学院，用于校验排序优先级
second_mentor_user_id = conn.execute(
    """INSERT INTO users(username,password_hash,name,role,initial_password,must_change_password)
       VALUES('2888889',?,'配置学院导师','mentor','2888889',1)""",
    (generate_password_hash('2888889'),),
).lastrowid
conn.execute(
    "INSERT INTO mentors(user_id,college,title,admission_category,quota) VALUES(?,?,?,?,3)",
    (second_mentor_user_id, '信息学院', '教授', '专硕'),
)

student_user_id = conn.execute(
    """INSERT INTO users(username,password_hash,name,role,initial_password,must_change_password)
       VALUES('20268888',?,'选项测试学生','student','20268888',1)""",
    (generate_password_hash('20268888'),),
).lastrowid
conn.execute(
    "INSERT INTO students(user_id,major) VALUES(?,'旧专业')", (student_user_id,)
)
conn.commit()
conn.close()

with app.test_client() as admin:
    response = login(admin, 'admin', 'admin123')
    check('管理员登录', response.status_code == 302)

    got = admin.get('/api/admin/options')
    check('读取选项接口可用', got.status_code == 200)
    payload = got.get_json()
    check('默认学院下发', '人工智能学院' in payload['options']['colleges'])
    check('默认专业下发', '计算机科学与技术' in payload['options']['majors'])
    check('学位类别选项下发', payload['degree_options'] == ['学硕', '专硕'])
    check('旧学院被合并进候选项', '旧学院名' in payload['options']['colleges'])
    check('旧专业被合并进候选项', '旧专业' in payload['options']['majors'])
    check('旧职称被合并进候选项', '老职称' in payload['options']['titles'])
    check('旧招生类别被合并进候选项', '旧类别' in payload['options']['categories'])

    saved = post(admin, '/api/admin/options', {
        'colleges': ['信息学院', '工学院'],
        'majors': ['数据科学', '软件工程'],
        'categories': ['学硕', '专硕'],
        'titles': ['教授', '副教授'],
        'degree_map': {'数据科学': '学硕', '软件工程': '专硕'},
    })
    check('保存选项', saved.status_code == 200)

    after = admin.get('/api/admin/options').get_json()
    check('保存后学院顺序按配置', after['saved']['colleges'] == ['信息学院', '工学院'])
    check('保存后专业生效', after['saved']['majors'] == ['数据科学', '软件工程'])
    check('保存后学位映射生效', after['saved']['degree_map'] == {
        '数据科学': '学硕', '软件工程': '专硕'})
    check('旧取值仍可作为筛选项保留', '旧学院名' in after['options']['colleges'])

    check('学硕判定跟随配置（数据科学）', is_xueshu_major('数据科学'))
    check('学硕判定跟随配置（软件工程非学硕）', not is_xueshu_major('软件工程'))
    check('未配置专业沿用内置默认', is_xueshu_major('计算机科学与技术'))

    mentors = admin.get('/api/admin/mentors').get_json()
    check('导师接口下发配置学院', mentors['colleges'][:2] == ['信息学院', '工学院'])
    check('导师接口下发配置专业', mentors['majors'][:2] == ['数据科学', '软件工程'])
    check('导师接口下发配置职称', mentors['titles'][:2] == ['教授', '副教授'])

    # 只提交专业、不提交 degree_map 时，已有映射不能被清空
    again = post(admin, '/api/admin/options', {
        'colleges': ['信息学院'],
        'majors': ['数据科学', '软件工程'],
        'categories': ['学硕'],
        'titles': ['教授'],
    })
    check('缺少学位映射时沿用旧配置', again.get_json()['options']['degree_map'].get('数据科学') == '学硕')

    # 专业被移除后，其学位映射一并清理
    trimmed = post(admin, '/api/admin/options', {
        'colleges': ['信息学院'],
        'majors': ['数据科学'],
        'categories': ['学硕'],
        'titles': ['教授'],
        'degree_map': {'数据科学': '学硕'},
    })
    trimmed_map = trimmed.get_json()['options']['degree_map']
    check('移除专业后清理其学位映射', '软件工程' not in trimmed_map)

    bad = post(admin, '/api/admin/options', {
        'colleges': ['信息学院'], 'majors': ['数据科学'],
        'categories': ['专硕'], 'titles': ['教授'],
        'degree_map': {'数据科学': '院士'},
    })
    check('非法学位类别被丢弃', bad.get_json()['options']['degree_map']['数据科学'] == '')

    profile = admin.get('/api/admin/mentors?page_size=100').get_json()
    check('导师列表接口在选项改动后仍可用', profile['pagination']['total'] >= 1)

    # 学院排序跟随配置顺序：旧记录（旧学院名）应排在配置学院之后
    names = [m['college'] for m in profile['mentors']]
    check('学院按配置顺序排列', names.index('信息学院') < names.index('旧学院名'))

options_service.refresh()
print()
print(f'基础选项设置测试：{sum(checks)}/{len(checks)} 通过')
if not all(checks):
    raise SystemExit(1)
