# Windows 服务器运行说明

适用目标：Windows Server 2016 / 2019 / 2022 / 2025，x64。
程序内含 Python 3.12、FastAPI、SQLite FTS5 和 jieba 中文词典，无需安装 Python、pip、Docker、数据库或联网下载依赖。
打包程序在本机 Windows x64 验证；各 Windows Server 版本仍需在目标服务器确认。

## 1. 解压并启动

将 ZIP 中全部文件解压到一个可写目录，例如 `C:\AML`。请先解压，不要在 ZIP 内直接双击。
双击 `start.cmd`，或在 PowerShell 中执行：

```powershell
cd C:\AML
.\aml-server.exe
```

首次运行自动在 EXE 同目录生成 `.env`，包含独立随机 `AML_API_KEY`，并创建 `data\memory.db` 和 `logs\`。
监听地址默认为 `0.0.0.0:8000`。控制台显示 Uvicorn 启动后可访问：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

返回 `status: ok` 表示正常。按 Ctrl+C 正常停止；关闭终端也会停止服务。
EXE 是控制台程序，不能直接用 `sc.exe create` 注册成 Windows 服务。

## 2. 修改配置与调用接口

用记事本打开自动生成的 `.env`，修改后重启。不要把密钥放在公开截图或仓库中。
系统环境变量优先于 `.env`。相对数据库路径始终相对于 EXE 所在目录，和当前终端目录无关。
支持 UTF-8（含 BOM）的 `.env`。

```powershell
notepad C:\AML\.env
# 临时更改端口（优先于 .env）：
C:\AML\aml-server.exe --port 8080
# 使用另一个配置文件：
C:\AML\aml-server.exe --env-file C:\AML\production.env
```

配置项见 `.env.example`。默认开启 Bearer 认证；`/health` 无需密钥。
使用 `.env` 中的实际密钥执行示例：

```powershell
$headers = @{ Authorization = 'Bearer 替换为.env中的AML_API_KEY' }
$body = @{
    request_id = [guid]::NewGuid().ToString()
    user_id = 'demo'; session_id = 'demo-session'
    messages = @(@{ role = 'user'; content = '我在上海使用 PostgreSQL 数据库。' })
} | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:8000/aml/add -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$query = @{ user_id = 'demo'; query = '数据库'; top_k = 5 } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/aml/search -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($query))
```

## 3. 远程访问

若需要其他机器访问，请在服务器管理员 PowerShell 中按需添加防火墙规则（端口须与配置一致）：

```powershell
New-NetFirewallRule -DisplayName 'AML Memory Server' -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

云服务器还需放行安全组端口。客户端地址为 `http://服务器IP:8000`。
公网生产部署应通过 IIS / 网关等配置 HTTPS，并保留 API Key 认证；程序默认提供 HTTP。

## 4. 开机自动运行（可选）

在“任务计划程序”创建任务：触发器选择“系统启动时”，操作选择启动 `C:\AML\aml-server.exe`，起始目录填 `C:\AML`。
选择“无论用户是否登录都要运行”，使用有该目录读写权限的账号，设置失败后重启，并取消运行时间限制。
首次先手动启动完成 `.env` 初始化并停止，再启用任务。停止后台程序应在任务计划程序中结束该任务。
请勿让计划任务与手动启动的程序同时使用同一端口。

## 5. 数据、日志与升级

- `data\memory.db`：持久化记忆；发布包不包含本机已有数据。
- `.env`：服务器专用配置及密钥。
- `logs\server.log`：启动和异常日志；`logs\http.log`：HTTP 访问及服务日志，各自按 10 MB 轮转，保留 5 份备份。
- 更新前停止程序，备份 `.env` 和整个 `data` 目录，再替换 EXE；已有数据库首次打开时可能自动迁移。
- 如果需要迁移开发机已有数据，先停止开发机服务，再单独复制数据库到服务器的 `data` 目录。

启动失败先检查日志、目录写权限、端口是否被占用及 `.env` 中的配置值。
单文件 EXE 启动时会将内部依赖解压到当前账号的临时目录，因此该目录也需可写；首次启动可能稍慢。
该包尚未做代码签名；文件校验可在 PowerShell 中运行 `Get-FileHash .\AML-Memory-Server-1.0.0-windows-x64.zip -Algorithm SHA256`，与旁边 `.sha256` 文件对比。

## 6. 开发机重新构建

在 Windows x64、Python 3.12 环境的项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

脚本默认使用项目 `.venv\Scripts\python.exe`；可通过 `-Python` 指定其他 Python 路径。
输出在 `dist` 下，包括 ZIP、校验文件及解压后的目录。发布采用文件白名单，不包含数据库、`.env`、日志或开发资料。
