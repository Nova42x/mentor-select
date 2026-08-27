# -*- coding: utf-8 -*-
"""为生产 SQLite 数据库创建一致性备份，并清理过期备份。"""
import os
import sqlite3
import time
from datetime import datetime

from db import DB_PATH


BACKUP_DIR = os.environ.get('MENTOR_BACKUP_DIR', '/var/backups/mentor-select')
RETENTION_DAYS = max(1, int(os.environ.get('MENTOR_BACKUP_RETENTION_DAYS', '30')))


def main():
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    target = os.path.join(BACKUP_DIR, f'mentor-{stamp}.db')
    with sqlite3.connect(DB_PATH, timeout=30) as source:
        with sqlite3.connect(target) as backup:
            source.backup(backup)
    os.chmod(target, 0o600)

    cutoff = time.time() - RETENTION_DAYS * 86400
    for name in os.listdir(BACKUP_DIR):
        path = os.path.join(BACKUP_DIR, name)
        if (name.startswith('mentor-') and name.endswith('.db') and
                os.path.isfile(path) and os.path.getmtime(path) < cutoff):
            os.remove(path)
    print(target)


if __name__ == '__main__':
    main()
