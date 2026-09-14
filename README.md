# 研究生导师实时互选系统（Mentor Select）

学生申请一名符合专业和招生类别的导师；导师同意后立即完成配对并锁定学生和名额。导师达到招生名额时，系统自动退回该导师其余待审核申请。管理员可以暂停新申请、解除配对或重新指定导师。

> 本仓库为从实际部署项目整理出的公开版本
> <img width="1913" height="913" alt="image" src="https://github.com/user-attachments/assets/a556ff26-1fd2-4e9b-b985-21ce0f9be83e" />
<img width="1886" height="910" alt="image" src="https://github.com/user-attachments/assets/1b868a23-b171-4f46-9723-36c0920d2a2c" />

## 技术栈

- Flask + Waitress
- SQLite（WAL、`busy_timeout`、关键操作使用 `BEGIN IMMEDIATE`）
- 原生 HTML/CSS/JavaScript，无 CDN 依赖
- Werkzeug 密码哈希、CSRF、登录限流、安全响应头和管理员操作日志

中小规模（数百名学生、数十名导师）无需引入消息队列或 WebSocket。页面每 7 秒刷新一次状态，关键操作后立即刷新。

## 实时互选规则
<img width="1886" height="908" alt="image" src="https://github.com/user-attachments/assets/15404b61-9312-442b-b13d-c4e22e6ce114" />

1. 学生同一时间只能有一个待审核申请。
2. 待审核期间学生可以撤回并重新选择。
3. 导师拒绝后学生立即恢复选择权限。
4. 导师同意后立即写入配对结果；学生和名额同时锁定。
5. 导师满额后，该导师其他待审核申请自动变为“导师满额自动退回”。
6. 已配对学生和导师不能自行取消，由管理员解除或改配。
7. 管理员释放名额后，不自动恢复以前退回的申请。
8. 学生可选导师同时受专业和学硕/专硕招生类别限制；管理员改配具有人工覆盖权限。

## 账号规则

- 学生：学号作为账号和初始密码。
- 导师：工号作为账号和初始密码。
- 管理员：首次初始化为 `admin / admin123`，部署后必须修改。
- 新导入的学生和导师首次登录必须修改初始密码，密码只保存哈希。
- 临时导师账号（未取得正式工号时）可通过环境变量 `MENTOR_TEMP_ACCOUNTS` 配置，逗号分隔，例如 `MENTOR_TEMP_ACCOUNTS=t001,t002`。

默认不创建演示数据。如确需本地演示数据，可设置环境变量 `SEED_DEMO=1` 后初始化数据库。

## 数据结构

- `selection_requests`：保存待审核、同意、拒绝、学生撤回、满额退回和管理员取消等完整申请历史。
- `pairings`：保存当前有效配对，每名学生最多一条。

旧版 `preferences`、`decisions`、`matches` 表保留用于迁移回滚，V2 不再写入。

## 基础选项设置

管理员可在「互选控制 → 基础选项设置」中维护四组下拉选项，所有相关表单与筛选器都从这里读取，不再写死在代码里：

- **学院**：同时决定导师列表的排序顺序。
- **专业**：每个专业可单独指定归属「学硕 / 专硕 / 不指定」，该归属直接驱动每位导师最多录取 3 名学硕的规则。
- **招生类别**、**职称**。

选项保存在 `settings` 表的 `options_*` 键中。首次部署时这些键为空，系统回落到 `options_service.DEFAULT_*` 内置默认值，页面表现与配置前一致，无需额外迁移；在界面上保存一次后即以数据库配置为准。

若数据库中已有历史取值（例如某导师的专业已被从配置里移除），`options_service.merge_observed()` 会把这些取值补回选项列表并排在最后，避免旧记录被静默改写或筛选不到。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
.\.venv\Scripts\python.exe -m waitress --host=127.0.0.1 --port=8080 app:app
```

打开 `http://127.0.0.1:8080`。

## 名单导入

管理员后台支持粘贴 CSV。

学生示例：

```csv
学号,姓名,专业
20260001,张三,计算机科学与技术(081200)
```

导入时会自动把 `计算机科学与技术(081200)` 规范为 `计算机科学与技术`。

导师示例：

```csv
工号,姓名,职称,招生类别,可招生专业,名额
2000123,李老师,教授,学硕/专硕,计算机科学与技术|计算机技术,3
```

## 测试

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe test_realtime.py
```

测试覆盖：首次强制改密、专业过滤、提交/撤回/拒绝/重选、即时配对、满额自动退回、管理员解除与改配、暂停新申请、CSV 导出，以及两个审批并发争抢最后一个名额。

基础选项设置（学院/专业/招生类别/职称的可配置化与学生学位类别联动）由 `test_options.py` 覆盖，共 25 项。

`test_flow.py`、`test_concurrent.py` 和 `test_scenarios.py` 保留为兼容入口，均运行同一套 V2 验收。

## 部署与运维

- 参考 `deploy/` 下的 systemd、nginx、fail2ban 与安全加固脚本；其中的 IP 和域名（`example.com`）为占位符，需替换为实际值。
- 部署/切换前使用 `backup_db.py` 备份当前数据库。
- 详细需求与验收清单见 `docs/requirements.md`。
