"""按姓名把导师账号迁移为正式工号，并同步初始密码与历史访问日志。

用法（先演练，确认无误后再加 --apply）：
    python rename_account.py --db data/mentor.db --map 张老师=2000001 --map 李老师=2000002
    python rename_account.py --db data/mentor.db --map 张老师=2000001 --map 李老师=2000002 --apply

安全约束：
    * 只处理 role='mentor' 的账号；
    * 新工号必须为7位数字，且未被其他账号占用；
    * 同名导师只迁移未改过登录密码的账号，避免覆盖用户自己设置的口令。
"""

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash


def rename(conn, name, new_username, apply_changes):
    rows = conn.execute(
        "SELECT id, username, initial_password, password_hash, must_change_password "
        "FROM users WHERE name=? AND role='mentor'",
        (name,),
    ).fetchall()
    if not rows:
        return f'{name}: 未找到该导师账号，跳过'
    if len(rows) > 1:
        return f'{name}: 存在 {len(rows)} 个同名导师账号，请人工确认，跳过'

    row = rows[0]
    uid, old_username = row['id'], row['username']
    if old_username == new_username:
        return f'{name}: 账号已是 {new_username}，无需修改'

    taken = conn.execute(
        'SELECT id, name FROM users WHERE username=? AND id!=?', (new_username, uid)
    ).fetchone()
    if taken:
        return f'{name}: 工号 {new_username} 已被「{taken["name"]}」占用，跳过'
    if not (len(new_username) == 7 and new_username.isdigit()):
        return f'{name}: 工号 {new_username} 不是7位数字，跳过'

    keeps_own_password = not check_password_hash(row['password_hash'], old_username)
    if not apply_changes:
        hint = '用户已自行改过密码，将保留原密码' if keeps_own_password else '密码同步为新工号'
        return f'{name}: {old_username} -> {new_username}（{hint}）[演练]'

    if keeps_own_password:
        conn.execute('UPDATE users SET username=? WHERE id=?', (new_username, uid))
    else:
        conn.execute(
            'UPDATE users SET username=?, password_hash=?, initial_password=?, '
            'must_change_password=1 WHERE id=?',
            (new_username, generate_password_hash(new_username), new_username, uid),
        )
    conn.execute('UPDATE access_logs SET username=? WHERE user_id=?', (new_username, uid))
    conn.execute('UPDATE audit_logs SET username=? WHERE user_id=?', (new_username, uid))
    return (
        f'{name}: {old_username} -> {new_username}（'
        f'{"保留原密码" if keeps_own_password else "密码同步为新工号"}）'
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='data/mentor.db')
    parser.add_argument('--map', action='append', default=[], metavar='姓名=工号')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()

    pairs = []
    for item in args.map:
        if '=' not in item:
            print(f'参数格式错误：{item}，应为 姓名=工号', file=sys.stderr)
            return 2
        name, username = item.split('=', 1)
        pairs.append((name.strip(), username.strip()))
    if not pairs:
        print('至少需要通过 --map 指定一个 姓名=工号', file=sys.stderr)
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        print(f'数据库不存在：{db_path}', file=sys.stderr)
        return 2

    if args.apply:
        backup = db_path.with_name(
            f'{db_path.stem}.rename-{time.strftime("%Y%m%d-%H%M%S")}{db_path.suffix}'
        )
        shutil.copy2(db_path, backup)
        print(f'已备份数据库：{backup}')

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for name, username in pairs:
            print(rename(conn, name, username, args.apply))
        if args.apply:
            violations = conn.execute('PRAGMA foreign_key_check').fetchall()
            if violations:
                conn.rollback()
                print('外键校验失败，已回滚：', [tuple(v) for v in violations], file=sys.stderr)
                return 1
            conn.commit()
            print('已提交修改')
        else:
            conn.rollback()
            print('演练结束，未写入；确认后加 --apply 执行')
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
