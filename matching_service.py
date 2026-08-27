# -*- coding: utf-8 -*-
"""实时互选的原子事务服务。所有改变配对结果的操作都集中在这里。"""
import re


XUESHU_LIMIT = 3
XUESHU_MAJORS = frozenset({'计算机科学与技术', '智能科学与技术', '人工智能'})


class MatchingError(Exception):
    """可安全展示给用户的业务异常。"""


def pairing_count(conn, mentor_id):
    return conn.execute(
        'SELECT COUNT(*) c FROM pairings WHERE mentor_id=?', (mentor_id,)
    ).fetchone()['c']


def is_xueshu_major(major):
    """识别本批次学硕专业，兼容导入数据中的六位专业代码后缀。"""
    normalized = re.sub(
        r'\s*[（(]\d{6}[）)]\s*$', '', str(major or '').strip()
    ).strip()
    return normalized in XUESHU_MAJORS


def xueshu_limit_enabled(conn):
    row = conn.execute(
        "SELECT value FROM settings WHERE key='xueshu_limit_enabled'"
    ).fetchone()
    # 新规则默认开启；兼容尚未执行初始化迁移的既有数据库。
    return row is None or str(row['value']) == '1'


def xueshu_pairing_count(conn, mentor_id):
    rows = conn.execute(
        """SELECT s.major FROM pairings p JOIN students s ON s.id=p.student_id
           WHERE p.mentor_id=?""", (mentor_id,)
    ).fetchall()
    return sum(1 for row in rows if is_xueshu_major(row['major']))


def _cancel_pending_xueshu_for_mentor(conn, mentor_id, operated_by):
    rows = conn.execute(
        """SELECT r.id,s.major FROM selection_requests r
           JOIN students s ON s.id=r.student_id
           WHERE r.mentor_id=? AND r.status='pending'""", (mentor_id,)
    ).fetchall()
    ids = [row['id'] for row in rows if is_xueshu_major(row['major'])]
    if not ids:
        return 0
    placeholders = ','.join('?' * len(ids))
    cur = conn.execute(
        f"""UPDATE selection_requests
            SET status='auto_cancelled', cancel_reason='xueshu_limit',
                operated_by=?, responded_at=datetime('now','localtime'),
                updated_at=datetime('now','localtime')
            WHERE status='pending' AND id IN ({placeholders})""",
        (operated_by, *ids),
    )
    return cur.rowcount


def cancel_pending_xueshu_at_limit(conn, operated_by=None):
    """启用规则时，退回已达到学硕上限导师的剩余学硕待审核申请。"""
    mentor_ids = [row['mentor_id'] for row in conn.execute(
        'SELECT DISTINCT mentor_id FROM pairings'
    ).fetchall()]
    cancelled = 0
    for mentor_id in mentor_ids:
        if xueshu_pairing_count(conn, mentor_id) >= XUESHU_LIMIT:
            cancelled += _cancel_pending_xueshu_for_mentor(
                conn, mentor_id, operated_by)
    return cancelled


def _cancel_pending_for_full_mentor(conn, mentor_id, operated_by):
    """导师满额后退回其余待审核申请，返回受影响学生数。"""
    cur = conn.execute(
        """UPDATE selection_requests
           SET status='auto_cancelled', cancel_reason='mentor_full',
               operated_by=?, responded_at=datetime('now','localtime'),
               updated_at=datetime('now','localtime')
           WHERE mentor_id=? AND status='pending'""",
        (operated_by, mentor_id),
    )
    return cur.rowcount


def accept_request(conn, mentor_user_id, request_id):
    """导师同意申请并立即锁定配对；名额刚好占满时自动退回其他申请。"""
    try:
        conn.execute('BEGIN IMMEDIATE')
        mentor = conn.execute(
            """SELECT m.* FROM mentors m JOIN users u ON u.id=m.user_id
               WHERE m.user_id=? AND m.active=1 AND u.active=1""",
            (mentor_user_id,),
        ).fetchone()
        if not mentor:
            raise MatchingError('导师资料不存在或账号已停用')
        req = conn.execute(
            """SELECT * FROM selection_requests
               WHERE id=? AND mentor_id=?""",
            (request_id, mentor['id']),
        ).fetchone()
        if not req:
            raise MatchingError('申请不存在')
        if req['status'] == 'accepted':
            pairing = conn.execute(
                'SELECT 1 FROM pairings WHERE student_id=? AND mentor_id=?',
                (req['student_id'], mentor['id']),
            ).fetchone()
            if pairing:
                taken = pairing_count(conn, mentor['id'])
                conn.commit()
                return {'taken': taken, 'remaining': max(0, mentor['quota'] - taken),
                        'auto_cancelled': 0, 'idempotent': True}
        if req['status'] != 'pending':
            raise MatchingError('该申请已处理，请刷新页面查看最新状态')
        if conn.execute('SELECT 1 FROM pairings WHERE student_id=?',
                        (req['student_id'],)).fetchone():
            raise MatchingError('该学生已经完成配对')
        student = conn.execute(
            'SELECT major FROM students WHERE id=?', (req['student_id'],)
        ).fetchone()
        is_xueshu = bool(student and is_xueshu_major(student['major']))
        xueshu_taken = xueshu_pairing_count(conn, mentor['id'])
        if (is_xueshu and xueshu_limit_enabled(conn) and
                xueshu_taken >= XUESHU_LIMIT):
            raise MatchingError(f'您已录取{XUESHU_LIMIT}名学硕，不能继续录取学硕学生')
        taken = pairing_count(conn, mentor['id'])
        if taken >= mentor['quota']:
            raise MatchingError(f'招生名额已满（{taken}/{mentor["quota"]}）')
        conn.execute(
            """INSERT INTO pairings(student_id,mentor_id,request_id,source,operated_by)
               VALUES(?,?,?,'request',?)""",
            (req['student_id'], mentor['id'], req['id'], mentor_user_id),
        )
        conn.execute(
            """UPDATE selection_requests
               SET status='accepted', cancel_reason='', operated_by=?,
                   responded_at=datetime('now','localtime'),
                   updated_at=datetime('now','localtime')
               WHERE id=? AND status='pending'""",
            (mentor_user_id, req['id']),
        )
        taken += 1
        auto_cancelled = 0
        xueshu_auto_cancelled = 0
        if taken >= mentor['quota']:
            auto_cancelled = _cancel_pending_for_full_mentor(
                conn, mentor['id'], mentor_user_id)
        elif is_xueshu and xueshu_limit_enabled(conn):
            xueshu_taken += 1
            if xueshu_taken >= XUESHU_LIMIT:
                xueshu_auto_cancelled = _cancel_pending_xueshu_for_mentor(
                    conn, mentor['id'], mentor_user_id)
                auto_cancelled += xueshu_auto_cancelled
        conn.commit()
        return {'taken': taken, 'remaining': max(0, mentor['quota'] - taken),
                'auto_cancelled': auto_cancelled,
                'xueshu_auto_cancelled': xueshu_auto_cancelled,
                'idempotent': False}
    except MatchingError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise


def reject_request(conn, mentor_user_id, request_id):
    try:
        conn.execute('BEGIN IMMEDIATE')
        mentor = conn.execute(
            'SELECT id FROM mentors WHERE user_id=?', (mentor_user_id,)
        ).fetchone()
        if not mentor:
            raise MatchingError('导师资料不存在')
        req = conn.execute(
            'SELECT * FROM selection_requests WHERE id=? AND mentor_id=?',
            (request_id, mentor['id']),
        ).fetchone()
        if not req:
            raise MatchingError('申请不存在')
        if req['status'] == 'rejected':
            conn.commit()
            return {'idempotent': True}
        if req['status'] != 'pending':
            raise MatchingError('该申请已处理，请刷新页面查看最新状态')
        conn.execute(
            """UPDATE selection_requests
               SET status='rejected', cancel_reason='mentor_rejected', operated_by=?,
                   responded_at=datetime('now','localtime'),
                   updated_at=datetime('now','localtime') WHERE id=?""",
            (mentor_user_id, request_id),
        )
        conn.commit()
        return {'idempotent': False}
    except MatchingError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise


def mentor_cancel_pairing(conn, mentor_user_id, pairing_id):
    """导师撤回自己通过学生申请建立的配对，学生恢复为可选择状态。"""
    try:
        conn.execute('BEGIN IMMEDIATE')
        mentor = conn.execute(
            'SELECT id FROM mentors WHERE user_id=?', (mentor_user_id,)
        ).fetchone()
        if not mentor:
            raise MatchingError('导师资料不存在')
        pairing = conn.execute(
            'SELECT * FROM pairings WHERE id=? AND mentor_id=?',
            (pairing_id, mentor['id']),
        ).fetchone()
        if not pairing:
            raise MatchingError('配对记录不存在，请刷新页面查看最新状态')
        if pairing['source'] != 'request' or not pairing['request_id']:
            raise MatchingError('管理员安排的配对只能由管理员调整')
        conn.execute('DELETE FROM pairings WHERE id=?', (pairing['id'],))
        conn.execute(
            """UPDATE selection_requests
               SET status='rejected', cancel_reason='mentor_withdrew_pairing',
                   operated_by=?, responded_at=datetime('now','localtime'),
                   updated_at=datetime('now','localtime')
               WHERE id=? AND status='accepted'""",
            (mentor_user_id, pairing['request_id']),
        )
        conn.commit()
        return {'student_id': pairing['student_id']}
    except MatchingError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise


def cancel_student_request(conn, student_id, student_user_id):
    try:
        conn.execute('BEGIN IMMEDIATE')
        req = conn.execute(
            "SELECT id FROM selection_requests WHERE student_id=? AND status='pending'",
            (student_id,),
        ).fetchone()
        if not req:
            if conn.execute('SELECT 1 FROM pairings WHERE student_id=?',
                            (student_id,)).fetchone():
                raise MatchingError('已经配对成功，如需调整请联系管理员')
            raise MatchingError('当前没有可撤回的申请')
        conn.execute(
            """UPDATE selection_requests
               SET status='student_cancelled', cancel_reason='student_withdrew',
                   operated_by=?, responded_at=datetime('now','localtime'),
                   updated_at=datetime('now','localtime') WHERE id=?""",
            (student_user_id, req['id']),
        )
        conn.commit()
    except MatchingError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise


def admin_set_pairing(conn, admin_user_id, student_id, mentor_id, note=''):
    """管理员解除或改配。目标导师仍必须有剩余名额。"""
    try:
        conn.execute('BEGIN IMMEDIATE')
        student = conn.execute(
            'SELECT id,major FROM students WHERE id=?', (student_id,)
        ).fetchone()
        if not student:
            raise MatchingError('学生不存在')
        current = conn.execute(
            'SELECT * FROM pairings WHERE student_id=?', (student_id,)
        ).fetchone()
        if mentor_id is None:
            if not current:
                conn.commit()
                return {'removed': False, 'auto_cancelled': 0}
            conn.execute('DELETE FROM pairings WHERE id=?', (current['id'],))
            if current['request_id']:
                conn.execute(
                    """UPDATE selection_requests SET status='admin_cancelled',
                       cancel_reason='admin_unpaired', operated_by=?,
                       responded_at=datetime('now','localtime'),
                       updated_at=datetime('now','localtime') WHERE id=?""",
                    (admin_user_id, current['request_id']),
                )
            conn.commit()
            return {'removed': True, 'auto_cancelled': 0}

        mentor = conn.execute(
            """SELECT m.* FROM mentors m JOIN users u ON u.id=m.user_id
               WHERE m.id=? AND m.active=1 AND u.active=1""",
            (mentor_id,),
        ).fetchone()
        if not mentor:
            raise MatchingError('目标导师不存在或已停用')
        if current and current['mentor_id'] == mentor_id:
            conn.commit()
            return {'removed': False, 'auto_cancelled': 0, 'idempotent': True}
        is_xueshu = is_xueshu_major(student['major'])
        xueshu_taken = xueshu_pairing_count(conn, mentor_id)
        if (is_xueshu and xueshu_limit_enabled(conn) and
                xueshu_taken >= XUESHU_LIMIT):
            raise MatchingError(
                f'目标导师已录取{XUESHU_LIMIT}名学硕，不能继续安排学硕学生')
        taken = pairing_count(conn, mentor_id)
        if taken >= mentor['quota']:
            raise MatchingError(f'目标导师名额已满（{taken}/{mentor["quota"]}）')
        if current:
            conn.execute('DELETE FROM pairings WHERE id=?', (current['id'],))
            if current['request_id']:
                conn.execute(
                    """UPDATE selection_requests SET status='admin_cancelled',
                       cancel_reason='admin_reassigned', operated_by=?,
                       responded_at=datetime('now','localtime'),
                       updated_at=datetime('now','localtime') WHERE id=?""",
                    (admin_user_id, current['request_id']),
                )
        conn.execute(
            """UPDATE selection_requests SET status='admin_cancelled',
               cancel_reason='admin_assigned_elsewhere', operated_by=?,
               responded_at=datetime('now','localtime'),
               updated_at=datetime('now','localtime')
               WHERE student_id=? AND status='pending'""",
            (admin_user_id, student_id),
        )
        conn.execute(
            """INSERT INTO pairings(student_id,mentor_id,source,operated_by,note)
               VALUES(?,?,'manual',?,?)""",
            (student_id, mentor_id, admin_user_id, (note or '')[:200]),
        )
        taken += 1
        auto_cancelled = 0
        xueshu_auto_cancelled = 0
        if taken >= mentor['quota']:
            auto_cancelled = _cancel_pending_for_full_mentor(conn, mentor_id, admin_user_id)
        elif is_xueshu and xueshu_limit_enabled(conn):
            xueshu_taken += 1
            if xueshu_taken >= XUESHU_LIMIT:
                xueshu_auto_cancelled = _cancel_pending_xueshu_for_mentor(
                    conn, mentor_id, admin_user_id)
                auto_cancelled += xueshu_auto_cancelled
        conn.commit()
        return {'removed': bool(current), 'auto_cancelled': auto_cancelled,
                'xueshu_auto_cancelled': xueshu_auto_cancelled,
                'idempotent': False}
    except MatchingError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
