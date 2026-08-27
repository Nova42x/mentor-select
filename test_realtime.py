# -*- coding: utf-8 -*-
"""V2实时互选端到端与并发验收测试。"""
import json
import io
import os
import sys
import threading
import openpyxl

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

os.environ.setdefault('MENTOR_TEMP_ACCOUNTS', 't001,t002')

import db
db.DB_PATH = os.path.join(BASE, 'data', 'test-realtime.db')
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)

from app import app
from matching_service import accept_request, MatchingError

checks = []


def check(name, condition):
    checks.append(bool(condition))
    print(('✅' if condition else '❌'), name)


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


def first_login_change_password(client, username, new_password):
    response = login(client, username, username)
    check(f'{username} 初始密码登录后进入修改密码页',
          response.status_code == 302 and '/password' in response.headers['Location'])
    response = post(client, '/api/password', {'old': username, 'new': new_password})
    check(f'{username} 首次修改密码', response.status_code == 200 and response.json.get('ok'))


with app.test_client() as admin:
    response = login(admin, 'admin', 'admin123')
    check('管理员登录', response.status_code == 302)

    student_csv = (
        '学号,姓名,专业\n'
        '20260001,实时学生甲,智能科学与技术(140500)\n'
        '20260002,实时学生乙,智能科学与技术\n'
        '20260003,实时学生丙,智能科学与技术\n'
        '20260004,并发学生甲,智能科学与技术\n'
        '20260005,并发学生乙,智能科学与技术'
    )
    response = post(admin, '/api/admin/import', {'kind': 'student', 'text': student_csv})
    check('导入学号账号且新增5名学生', response.json.get('added') == 5)

    mentor_csv = (
        '工号,姓名,学院,职称,招生类别,可招生专业,名额,启用状态\n'
        '2000001,实时导师甲,人工智能学院,教授,学硕,智能科学与技术,1\n'
        '2000002,实时导师乙,人工智能学院,副教授,学硕,智能科学与技术,2\n'
        '2000003,并发导师,人工智能学院,教授,学硕,智能科学与技术,1\n'
        't001,临时导师一,人工智能学院,特任副教授,学硕/专硕,智能科学与技术,2\n'
        't002,临时导师二,电子与电气工程学院,教授,学硕/专硕,智能科学与技术,6\n'
        '2000004,停用导师,电子与电气工程学院,教授,学硕,智能科学与技术,1,停用'
    )
    response = post(admin, '/api/admin/import', {'kind': 'mentor', 'text': mentor_csv})
    check('导入4个工号账号及2个批准的临时导师账号', response.json.get('added') == 6)
    check('全新数据库默认暂停学生申请',
          admin.get('/api/admin/overview').json['info']['open'] is False)
    overview_accounts = {m['username'] for m in admin.get('/api/admin/overview').json['mentors']}
    check('管理员导师名额表返回导师账号',
          {'2000001', 't001', 't002'}.issubset(overview_accounts))
    admin_page = admin.get('/admin/')
    check('管理员页面使用侧边栏并包含数据看板',
          admin_page.status_code == 200 and
          'admin-sider' in admin_page.get_data(as_text=True) and
          'data-tab="dashboard"' in admin_page.get_data(as_text=True) and
          '<div class="ph-title">数据导出</div>' not in admin_page.get_data(as_text=True) and
          'dashboardVisitorSearch' in admin_page.get_data(as_text=True) and
          'dashboardVisitorPageSize' in admin_page.get_data(as_text=True) and
          "['all', '全部']" in admin_page.get_data(as_text=True) and
          'student-template-toolbar' in admin_page.get_data(as_text=True) and
          "UI.bindSearch($('#studentSearch')" not in admin_page.get_data(as_text=True) and
          'mentor-template-toolbar' in admin_page.get_data(as_text=True) and
          'mentorCollege' in admin_page.get_data(as_text=True) and
          'mentorCategory' in admin_page.get_data(as_text=True) and
          "UI.bindSearch($('#mentorSearch')" not in admin_page.get_data(as_text=True) and
          "openDashboardDetail('matched')" in admin_page.get_data(as_text=True))
    mentor_template_page = admin.get(
        '/api/admin/mentors?q=实时&college=人工智能学院&category=学硕&major=智能科学与技术&status=active&page=1&page_size=100'
    ).json
    check('导师管理支持搜索、类别、专业、状态与后端分页',
          mentor_template_page['pagination']['page_size'] == 100 and
          mentor_template_page['pagination']['total'] == 2 and
          mentor_template_page['colleges'] == ['人工智能学院', '电子与电气工程学院'] and
          {row['username'] for row in mentor_template_page['mentors']} ==
          {'2000001', '2000002'})
    disabled_mentor_page = admin.get(
        '/api/admin/mentors?status=disabled&page=1&page_size=20'
    ).json
    check('导师管理可单独筛选停用账号',
          disabled_mentor_page['pagination']['total'] == 1 and
          disabled_mentor_page['mentors'][0]['username'] == '2000004')
    ordered_mentor_page = admin.get('/api/admin/mentors?page=1&page_size=100').json
    college_order = [row['college'] for row in ordered_mentor_page['mentors']]
    check('导师管理默认按完整学院名称分组排列',
          all(value in ('人工智能学院', '电子与电气工程学院') for value in college_order) and
          college_order == sorted(college_order,
                                  key=lambda value: {'人工智能学院': 0,
                                                     '电子与电气工程学院': 1}[value]))
    student_template_page = admin.get(
        '/api/admin/students?q=实时&page=1&page_size=100'
    ).json
    check('学生管理搜索与每页数量由后端统一分页',
          student_template_page['pagination']['page_size'] == 100 and
          student_template_page['pagination']['total'] == 3 and
          len(student_template_page['students']) == 3)
    student_status_page = admin.get(
        '/api/admin/students?status=unselected&account=active&page=1&page_size=20'
    ).json
    check('学生管理支持互选状态与账号状态筛选并返回概览统计',
          student_status_page['pagination']['total'] == 5 and
          student_status_page['summary']['total'] == 5 and
          student_status_page['summary']['matched'] == 0 and
          student_status_page['summary']['pending'] == 0 and
          student_status_page['summary']['unselected'] == 5 and
          all(row['selection_status'] == 'unselected'
              for row in student_status_page['students']))
    for account_role, account_label, expected_account, expected_password in (
            ('student', '学生', '20260001', '20260001'),
            ('mentor', '导师', '2000001', '2000001'),
            ('admin', '管理员', 'admin', 'admin123')):
        account_response = admin.get(f'/api/admin/export?type=accounts&role={account_role}')
        account_csv = account_response.data.decode('utf-8-sig')
        check(f'可导出{account_label}初始账号密码',
              account_response.status_code == 200 and
              expected_account in account_csv and expected_password in account_csv and
              '初始密码可用' in account_csv and
              account_response.headers.get('Cache-Control') == 'no-store')
    combined_response = admin.get('/api/admin/export?type=accounts_all')
    combined_book = openpyxl.load_workbook(io.BytesIO(combined_response.data), read_only=True)
    check('初始账号密码可合并导出为三工作表 Excel',
          combined_response.status_code == 200 and
          combined_book.sheetnames == ['学生账户', '导师账户', '管理员账户'] and
          combined_book['学生账户']['A2'].value == '20260001' and
          combined_book['导师账户']['A2'].value == '2000001' and
          combined_book['管理员账户']['A2'].value == 'admin' and
          combined_response.headers.get('Cache-Control') == 'no-store')
    combined_book.close()
    dashboard = admin.get('/api/admin/dashboard').json
    check('数据看板返回真实互选与访问统计',
          dashboard['stats']['students'] == 5 and
          dashboard['stats']['matched'] == 0 and
          dashboard['stats']['unselected'] == 5 and
          len(dashboard['daily']) == 7)
    check('访问明细关联来源IP和管理员账户', any(
        item['ip'] == '127.0.0.1' and item['username'] == 'admin'
        for item in dashboard['visitors']))
    unselected_detail = admin.get(
        '/api/admin/dashboard/detail?type=unselected&page=1&page_size=20'
    ).json
    check('看板未选择指标可下钻查看学生名单',
          unselected_detail['pagination']['total'] == 5 and
          len(unselected_detail['rows']) == 5 and
          unselected_detail['rows'][0]['username'] == '20260001')
    conn = db.get_db()
    for index in range(25):
        conn.execute(
            """INSERT INTO access_logs(username,display_name,role,ip,path,created_at)
               VALUES('','','',?, '/login', ?)""",
            (f'10.0.1.{index + 1}', f'2026-08-10 12:00:{index:02d}'))
    conn.execute(
        """INSERT INTO access_logs(username,display_name,role,ip,path,created_at)
           VALUES('20260001','实时学生甲','student','10.0.0.200','/student/','2026-08-10 12:01:00')"""
    )
    conn.commit()
    conn.close()
    visitor_page = admin.get('/api/admin/dashboard?role=anonymous&page=2&page_size=10').json
    check('访问明细支持后端分页和每页数量',
          visitor_page['visitors_pagination']['page'] == 2 and
          visitor_page['visitors_pagination']['page_size'] == 10 and
          visitor_page['visitors_pagination']['total'] >= 25 and
          len(visitor_page['visitors']) == 10)
    visitor_search = admin.get(
        '/api/admin/dashboard?role=student&q=10.0.0.200&page=1&page_size=20'
    ).json
    check('访问明细支持身份分类及IP账号姓名搜索',
          visitor_search['visitors_pagination']['total'] == 1 and
          visitor_search['visitors'][0]['username'] == '20260001' and
          visitor_search['visitors'][0]['role'] == 'student')
    visitor_detail = admin.get(
        '/api/admin/dashboard/detail?type=visits&role=student&page=1&page_size=20'
    ).json
    check('看板访问图可按身份下钻查看账户明细',
          visitor_detail['pagination']['total'] == 1 and
          visitor_detail['rows'][0]['username'] == '20260001')
    post(admin, '/api/admin/phase', {'action': 'open'})

    conn = db.get_db()
    mentor_rows = {r['username']: dict(r) for r in conn.execute(
        'SELECT m.id,m.user_id,u.username FROM mentors m JOIN users u ON u.id=m.user_id '
        "WHERE u.username IN ('2000001','2000002','2000003')"
    ).fetchall()}
    student_rows = {r['username']: dict(r) for r in conn.execute(
        'SELECT s.id,s.user_id,u.username FROM students s JOIN users u ON u.id=s.user_id '
        "WHERE u.username LIKE '2026000%'"
    ).fetchall()}
    inactive = conn.execute(
        """SELECT u.active user_active,m.active mentor_active
             FROM mentors m JOIN users u ON u.id=m.user_id WHERE u.username='2000004'"""
    ).fetchone()
    check('导师CSV可保留停用状态',
          inactive['user_active'] == 0 and inactive['mentor_active'] == 0)
    conn.close()

    policy_client = app.test_client()
    check('管理员可关闭首次登录强制改密',
          post(admin, '/api/admin/phase', {'action': 'password_change_off'}).json['info']['force_password_change'] is False)
    response = login(policy_client, '20260001', '20260001')
    check('关闭开关后初始密码登录可直接进入系统',
          response.status_code == 302 and '/password' not in response.headers['Location'])
    policy_client.get('/logout')
    check('管理员可重新开启首次登录强制改密',
          post(admin, '/api/admin/phase', {'action': 'password_change_on'}).json['info']['force_password_change'] is True)

    s1, s2, s3, s4, s5 = [app.test_client() for _ in range(5)]
    for client, username, pwd in zip(
            (s1, s2, s3, s4, s5),
            ('20260001', '20260002', '20260003', '20260004', '20260005'),
            ('student-a', 'student-b', 'student-c', 'student-d', 'student-e')):
        first_login_change_password(client, username, pwd)
    temp1, temp2 = app.test_client(), app.test_client()
    first_login_change_password(temp1, 't001', 'temp-mentor-12')
    first_login_change_password(temp2, 't002', 'temp-mentor-50')

    t1, t2, t3 = app.test_client(), app.test_client(), app.test_client()
    for client, username, pwd in zip(
            (t1, t2, t3), ('2000001', '2000002', '2000003'),
            ('mentor-a', 'mentor-b', 'mentor-c')):
        first_login_change_password(client, username, pwd)

    student_page_text = s1.get('/student/').get_data(as_text=True)
    check('手机号码位于提交申请表单且不再单独显示保存按钮',
          student_page_text.index('id="selectionForm"') <
          student_page_text.index('id="studentPhone"') and
          '保存手机号码' not in student_page_text and
          'window.savePhone' not in student_page_text)
    phone_response = post(s1, '/api/student/profile', {'phone': '+86 138-1234-5678'})
    check('学生可保存并规范化手机号码',
          phone_response.status_code == 200 and
          phone_response.json['phone'] == '13812345678' and
          s1.get('/api/student/me').json['me']['phone'] == '13812345678')
    check('个人介绍使用新提示并限制1000字但不展示建议字数',
          'maxlength="1000"' in student_page_text and
          '请简要介绍本科背景（院校专业）、学习与工作经历、兴趣方向及相关成果：科研、竞赛、大创、毕设。' in student_page_text and
          '建议 100–300 字' not in student_page_text and
          '500 字以内' not in student_page_text)

    mid1 = mentor_rows['2000001']['id']
    mid2 = mentor_rows['2000002']['id']
    mid3 = mentor_rows['2000003']['id']

    check('学生端不返回导师名额字段', all(
        'quota' not in m and 'remaining' not in m
        for m in s1.get('/api/mentors').json['mentors']))
    check('热门导师火苗标记默认关闭',
          s1.get('/api/mentors').json['mentor_hot_badge_enabled'] is False)
    check('导入时去除专业代码且仍能匹配导师',
          s1.get('/api/student/me').json['me']['major'] == '智能科学与技术')

    check('学生甲申请导师甲', post(s1, '/api/student/preference',
                                  {'mentor_id': mid1, 'intro': '甲简介'}).json.get('ok'))
    check('学生乙也可申请导师甲（申请数不实时限流）',
          post(s2, '/api/student/preference', {'mentor_id': mid1}).json.get('ok'))
    hot_on = post(admin, '/api/admin/phase', {'action': 'mentor_hot_badge_on'}).json
    hot_mentors = {m['id']: m for m in s3.get('/api/mentors').json['mentors']}
    check('管理员可开启学生端热门导师火苗标记',
          hot_on['info']['mentor_hot_badge_enabled'] is True)
    check('有效选择人数达到招生名额2倍时仅返回火苗标记',
          hot_mentors[mid1]['is_hot'] is True and
          'interest_count' not in hot_mentors[mid1] and
          'quota' not in hot_mentors[mid1])
    hot_off = post(admin, '/api/admin/phase', {'action': 'mentor_hot_badge_off'}).json
    hidden_mentors = s3.get('/api/mentors').json['mentors']
    check('关闭热门导师开关后学生端立即隐藏火苗',
          hot_off['info']['mentor_hot_badge_enabled'] is False and
          not any(m['is_hot'] for m in hidden_mentors))
    pending = t1.get('/api/mentor/applicants').json['applicants']
    check('导师看到两条待审核申请', len(pending) == 2)
    admin_student = admin.get('/api/admin/students?q=20260001').json['students'][0]
    check('管理员学生列表完整返回待审核导师姓名和账号',
          admin_student['round_info'][0]['mentor'] == '实时导师甲' and
          admin_student['round_info'][0]['mentor_username'] == '2000001')
    req1 = next(r for r in pending if r['sno'] == '20260001')
    response = post(t1, '/api/mentor/select',
                    {'request_id': req1['request_id'], 'decision': 'accept'})
    check('导师同意后立即配对并占满名额',
          response.json.get('ok') and response.json['taken'] == 1 and
          response.json['auto_cancelled'] == 1)
    mentor_match_page = admin.get(
        '/api/admin/matches?view=mentor&q=实时导师甲&page=1&page_size=10'
    ).json
    check('匹配管理导师视角支持聚合搜索与后端分页',
          mentor_match_page['pagination']['total'] == 1 and
          mentor_match_page['summary']['paired'] == 1 and
          mentor_match_page['mentor_rows'][0]['id'] == mid1 and
          mentor_match_page['mentor_rows'][0]['xueshu_matched'] == 1)
    mentor_match_detail = admin.get(f'/api/admin/matches/mentor/{mid1}').json
    check('匹配管理导师详情按需返回录取和待审核名单',
          mentor_match_detail['mentor']['taken'] == 1 and
          mentor_match_detail['accepted'][0]['student_no'] == '20260001')
    student_match_page = admin.get(
        '/api/admin/matches?view=student&student_status=matched&page=1&page_size=10'
    ).json
    check('匹配管理学生视角支持状态筛选与统一分页',
          student_match_page['pagination']['total'] == 1 and
          student_match_page['student_rows'][0]['mentor_id'] == mid1)
    matched_detail = admin.get(
        '/api/admin/dashboard/detail?type=matched&page=1&page_size=20'
    ).json
    check('看板已录指标可下钻查看配对名单',
          matched_detail['pagination']['total'] == 1 and
          matched_detail['rows'][0]['username'] == '20260001' and
          matched_detail['rows'][0]['mentor_username'] == '2000001')
    check('学生甲选择被锁定', s1.get('/api/student/status').json['state'] == 'matched')
    s2_status = s2.get('/api/student/status').json
    check('满额自动退回其他待审核学生',
          s2_status['state'] == 'selectable' and
          s2_status['history'][0]['status'] == 'auto_cancelled')
    check('已配对学生不能重新申请',
          post(s1, '/api/student/preference', {'mentor_id': mid2}).status_code == 400)

    check('学生丙申请导师乙',
          post(s3, '/api/student/preference', {'mentor_id': mid2}).json.get('ok'))
    req3 = t2.get('/api/mentor/applicants').json['applicants'][0]
    check('导师拒绝后学生可立即重选',
          post(t2, '/api/mentor/select', {'request_id': req3['request_id'],
                                         'decision': 'reject'}).json.get('ok') and
          s3.get('/api/student/status').json['state'] == 'selectable')

    sid1 = student_rows['20260001']['id']
    check('管理员可解除已锁定配对',
          post(admin, '/api/admin/match', {'student_id': sid1, 'mentor_id': None}).json.get('ok') and
          s1.get('/api/student/status').json['state'] == 'selectable')
    check('管理员可重新指定导师',
          post(admin, '/api/admin/match', {'student_id': sid1, 'mentor_id': mid2}).json.get('ok') and
          s1.get('/api/student/status').json['pairing']['mentor_id'] == mid2)
    conn = db.get_db()
    manual_pairing_id = conn.execute(
        'SELECT id FROM pairings WHERE student_id=?', (sid1,)
    ).fetchone()['id']
    conn.close()
    check('导师不能撤回管理员安排的配对',
          post(t2, '/api/mentor/pairing/cancel',
               {'pairing_id': manual_pairing_id}).status_code == 409)

    check('管理员暂停新申请',
          post(admin, '/api/admin/phase', {'action': 'close'}).json['info']['open'] is False)
    check('暂停后学生不能提交新申请',
          post(s2, '/api/student/preference', {'mentor_id': mid2}).status_code == 400)
    post(admin, '/api/admin/phase', {'action': 'open'})

    # 两个申请并发争抢同一导师的最后一个名额。
    post(s4, '/api/student/preference', {'mentor_id': mid3})
    post(s5, '/api/student/preference', {'mentor_id': mid3})
    conn = db.get_db()
    request_ids = [r['id'] for r in conn.execute(
        "SELECT id FROM selection_requests WHERE mentor_id=? AND status='pending' ORDER BY id",
        (mid3,)).fetchall()]
    mentor_user_id = mentor_rows['2000003']['user_id']
    conn.close()
    results = []
    result_lock = threading.Lock()

    def approve(request_id):
        local = db.get_db()
        try:
            accept_request(local, mentor_user_id, request_id)
            outcome = 'accepted'
        except MatchingError:
            outcome = 'not-accepted'
        finally:
            local.close()
        with result_lock:
            results.append(outcome)

    threads = [threading.Thread(target=approve, args=(rid,)) for rid in request_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    conn = db.get_db()
    pair_count = conn.execute(
        'SELECT COUNT(*) c FROM pairings WHERE mentor_id=?', (mid3,)
    ).fetchone()['c']
    pending_count = conn.execute(
        "SELECT COUNT(*) c FROM selection_requests WHERE mentor_id=? AND status='pending'",
        (mid3,)).fetchone()['c']
    winner = conn.execute(
        """SELECT p.id pairing_id,u.username sno,r.id request_id
             FROM pairings p JOIN students s ON s.id=p.student_id
             JOIN users u ON u.id=s.user_id JOIN selection_requests r ON r.id=p.request_id
             WHERE p.mentor_id=?""", (mid3,)
    ).fetchone()
    conn.close()
    check('并发抢最后名额不会超额且自动清空待审核',
          pair_count == 1 and pending_count == 0 and results.count('accepted') == 1)
    response = post(t3, '/api/mentor/pairing/cancel', {'pairing_id': winner['pairing_id']})
    winner_client = s4 if winner['sno'] == '20260004' else s5
    winner_status = winner_client.get('/api/student/status').json
    check('导师可撤回自己确认的配对', response.status_code == 200 and response.json.get('ok'))
    check('导师撤回后学生恢复选择且记录原因明确',
          winner_status['state'] == 'selectable' and
          winner_status['history'][0]['cancel_reason'] == 'mentor_withdrew_pairing')

    final_csv = admin.get('/api/admin/export?type=final').data.decode('utf-8-sig')
    history_csv = admin.get('/api/admin/export?type=preferences').data.decode('utf-8-sig')
    check('最终名单导出包含学生手机号并使用实时配对表头',
          '手机号' in final_csv and '13812345678' in final_csv and
          '配对时间' in final_csv and '匹配轮次' not in final_csv)
    check('申请明细导出包含满额自动退回状态',
          '系统自动退回' in history_csv and '导师名额已满' in history_csv)

print(f'\n实时互选测试：{sum(checks)}/{len(checks)} 通过')
if not all(checks):
    raise SystemExit(1)
