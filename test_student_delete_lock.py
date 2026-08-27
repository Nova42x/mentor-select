# -*- coding: utf-8 -*-
"""学生删除失败时的 SQLite 写锁回归测试。"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import db

db.DB_PATH = os.path.join(BASE, 'data', 'test-student-delete-lock.db')
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)

import app as app_module

app = app_module.app


def token(client):
    with client.session_transaction() as sess:
        return sess['_csrf']


def post(client, url, payload=None, form=None):
    headers = {'X-CSRF-Token': token(client)}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        return client.post(url, headers=headers, data=json.dumps(payload))
    return client.post(url, headers=headers, data=form or {})


def assert_write_lock_free():
    conn = db.get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.rollback()
    finally:
        conn.close()


with app.test_client() as client:
    client.get('/login')
    response = post(client, '/login', form={
        'username': 'admin', 'password': 'admin123',
    })
    assert response.status_code == 302

    conn = db.get_db()
    cur = conn.execute(
        """INSERT INTO users(username,password_hash,initial_password,name,role)
           VALUES('20269999','unused','unused','删除测试学生','student')"""
    )
    user_id = cur.lastrowid
    student_id = conn.execute(
        "INSERT INTO students(user_id,major) VALUES(?, '智能科学与技术')",
        (user_id,),
    ).lastrowid
    conn.execute(
        """INSERT INTO access_logs(user_id,username,display_name,role,ip,path)
           VALUES(?, '20269999', '删除测试学生', 'student', '127.0.0.1', '/')""",
        (user_id,),
    )
    conn.commit()
    conn.close()

    response = post(client, '/api/admin/student/delete', {'id': student_id})
    assert response.status_code == 200, response.get_data(as_text=True)
    conn = db.get_db()
    log = conn.execute(
        "SELECT user_id,username FROM access_logs WHERE username='20269999'"
    ).fetchone()
    assert log and log['user_id'] is None
    assert not conn.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone()
    conn.close()
    assert_write_lock_free()

    original_delete = app_module._delete_student

    def failing_delete(conn, _sid):
        conn.execute("UPDATE settings SET value='dirty' WHERE key='sys_title'")
        raise RuntimeError('forced delete failure')

    app_module._delete_student = failing_delete
    try:
        response = post(client, '/api/admin/student/delete', {'id': 999999})
        assert response.status_code == 500
    finally:
        app_module._delete_student = original_delete

    conn = db.get_db()
    title = conn.execute("SELECT value FROM settings WHERE key='sys_title'").fetchone()['value']
    conn.close()
    assert title != 'dirty'
    assert_write_lock_free()

print('student delete lock regression: passed')
