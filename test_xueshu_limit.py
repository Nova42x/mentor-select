# -*- coding: utf-8 -*-
"""每位导师最多录取3名学硕规则回归测试。"""
import json
import os
import sys

from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import db

db.DB_PATH = os.path.join(BASE, 'data', 'test-xueshu-limit.db')
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)

from app import app
from matching_service import MatchingError, accept_request, admin_set_pairing


def token(client):
    with client.session_transaction() as sess:
        return sess['_csrf']


def post(client, url, payload=None, form=None):
    headers = {'X-CSRF-Token': token(client)}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        return client.post(url, headers=headers, data=json.dumps(payload))
    return client.post(url, headers=headers, data=form or {})


conn = db.get_db()
assert conn.execute(
    "SELECT value FROM settings WHERE key='xueshu_limit_enabled'"
).fetchone()['value'] == '1'

mentor_user_id = conn.execute(
    """INSERT INTO users(username,password_hash,name,role)
       VALUES('2999999',?,'学硕上限测试导师','mentor')""",
    (generate_password_hash('mentor-pass'),),
).lastrowid
mentor_id = conn.execute(
    "INSERT INTO mentors(user_id,quota,admission_category) VALUES(?,6,'学硕/专硕')",
    (mentor_user_id,),
).lastrowid

student_ids = []
for index in range(1, 7):
    user_id = conn.execute(
        """INSERT INTO users(username,password_hash,name,role)
           VALUES(?,?,?,'student')""",
        (f'2999000{index}', generate_password_hash('student-pass'), f'学硕学生{index}'),
    ).lastrowid
    student_ids.append(conn.execute(
        "INSERT INTO students(user_id,major) VALUES(?,'计算机科学与技术')",
        (user_id,),
    ).lastrowid)

zhuanshu_user_id = conn.execute(
    """INSERT INTO users(username,password_hash,name,role)
       VALUES('29990100',?,'专硕学生','student')""",
    (generate_password_hash('student-pass'),),
).lastrowid
zhuanshu_id = conn.execute(
    "INSERT INTO students(user_id,major) VALUES(?,'计算机技术')",
    (zhuanshu_user_id,),
).lastrowid

request_ids = []
for student_id in student_ids[:4]:
    request_ids.append(conn.execute(
        'INSERT INTO selection_requests(student_id,mentor_id) VALUES(?,?)',
        (student_id, mentor_id),
    ).lastrowid)
zhuanshu_request_id = conn.execute(
    'INSERT INTO selection_requests(student_id,mentor_id) VALUES(?,?)',
    (zhuanshu_id, mentor_id),
).lastrowid
conn.commit()
conn.close()

for request_id in request_ids[:3]:
    conn = db.get_db()
    result = accept_request(conn, mentor_user_id, request_id)
    conn.close()

assert result['xueshu_auto_cancelled'] == 1
conn = db.get_db()
fourth = conn.execute(
    'SELECT status,cancel_reason FROM selection_requests WHERE id=?',
    (request_ids[3],),
).fetchone()
assert (fourth['status'], fourth['cancel_reason']) == ('auto_cancelled', 'xueshu_limit')
assert conn.execute(
    'SELECT status FROM selection_requests WHERE id=?', (zhuanshu_request_id,)
).fetchone()['status'] == 'pending'

fifth_request_id = conn.execute(
    'INSERT INTO selection_requests(student_id,mentor_id) VALUES(?,?)',
    (student_ids[4], mentor_id),
).lastrowid
conn.commit()
conn.close()

conn = db.get_db()
try:
    accept_request(conn, mentor_user_id, fifth_request_id)
    raise AssertionError('第4名学硕不应被导师确认')
except MatchingError as exc:
    assert '3名学硕' in str(exc)
finally:
    conn.close()

conn = db.get_db()
admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()['id']
try:
    admin_set_pairing(conn, admin_id, student_ids[4], mentor_id)
    raise AssertionError('管理员不应绕过已开启的学硕上限')
except MatchingError as exc:
    assert '3名学硕' in str(exc)
finally:
    conn.close()

with app.test_client() as admin:
    admin.get('/login')
    assert post(admin, '/login', form={
        'username': 'admin', 'password': 'admin123',
    }).status_code == 302
    disabled = post(admin, '/api/admin/phase', {'action': 'xueshu_limit_off'})
    assert disabled.status_code == 200

conn = db.get_db()
admin_set_pairing(conn, admin_id, student_ids[4], mentor_id)
conn.close()

conn = db.get_db()
sixth_request_id = conn.execute(
    'INSERT INTO selection_requests(student_id,mentor_id) VALUES(?,?)',
    (student_ids[5], mentor_id),
).lastrowid
conn.commit()
conn.close()

with app.test_client() as admin:
    admin.get('/login')
    assert post(admin, '/login', form={
        'username': 'admin', 'password': 'admin123',
    }).status_code == 302
    enabled = post(admin, '/api/admin/phase', {'action': 'xueshu_limit_on'})
    assert enabled.status_code == 200 and enabled.json['cancelled'] == 1

conn = db.get_db()
assert conn.execute(
    'SELECT cancel_reason FROM selection_requests WHERE id=?', (sixth_request_id,)
).fetchone()['cancel_reason'] == 'xueshu_limit'
conn.close()

with app.test_client() as mentor:
    mentor.get('/login')
    assert post(mentor, '/login', form={
        'username': '2999999', 'password': 'mentor-pass',
    }).status_code == 302
    state = mentor.get('/api/mentor/me').json
    assert state['info']['xueshu_limit_enabled'] is True
    assert state['xueshu_taken'] == 4 and state['info']['xueshu_limit'] == 3

admin_html = open(os.path.join(BASE, 'templates', 'admin.html'), encoding='utf-8').read()
mentor_html = open(os.path.join(BASE, 'templates', 'mentor.html'), encoding='utf-8').read()
assert 'xueshuLimitSwitch' in admin_html
assert '每位导师最多录取' in mentor_html

print('xueshu limit regression: passed')
