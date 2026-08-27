#!/bin/bash
# 线上验证：学生列表重构（Codex 改动）已部署
set -e
B=http://127.0.0.1:8080
CK=/tmp/vck2.txt
rm -f $CK

echo "=== 1. 模板与样式包含新结构 ==="
grep -c 'student-table' /opt/mentor-select/templates/admin.html | xargs echo "admin.html student-table 次数:"
grep -c 'student-list-card' /opt/mentor-select/templates/admin.html | xargs echo "student-list-card 次数:"
grep -c '左右滑动' /opt/mentor-select/static/style.css | xargs echo "style.css 移动端提示次数:"
grep -c 'admin-container' /opt/mentor-select/static/style.css /opt/mentor-select/templates/admin.html

echo "=== 2. 管理员登录 → 学生页渲染 ==="
curl -s -c $CK -b $CK $B/login > /dev/null
TOK=$(curl -s -c $CK -b $CK $B/login | grep -o 'name="_csrf" value="[^"]*"' | head -1 | sed 's/.*value="//;s/"//')
curl -s -b $CK -c $CK -d "username=admin&password=admin123&_csrf=$TOK" $B/login > /dev/null
curl -s -b $CK -o /dev/null -w "admin页面: %{http_code}\n" $B/admin/
curl -s -b $CK $B/api/admin/students | python3 -c "import json,sys; d=json.load(sys.stdin); print('students 接口:', len(d['students']), '人 | 字段:', sorted(d['students'][0].keys()) if d['students'] else '无')"

echo "=== 3. 服务与数据 ==="
systemctl is-active mentor-select
ls /opt/mentor-select/data/
echo OK
