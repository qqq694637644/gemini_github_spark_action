# Windows 无 Docker 部署：内置个人 OAuth 授权服务器

本方案把 OAuth 授权服务器、JWT/JWKS、Refresh Token、MCP 和 GitHub Gateway 放在同一个 Uvicorn 进程中运行，不需要 Docker，也不需要外部身份提供商。

适用场景：单人使用、一个固定 Gemini 自定义连接应用、使用 fine-grained GitHub PAT、通过 HTTPS 反向代理公开。

## 1. 前置条件

Windows 上需要：

- Python 3.11 或更高版本
- Git
- Windows PowerShell 5.1（系统自带）或 PowerShell 7
- 已解析到此 Windows 主机的 HTTPS 域名
- 一个 GitHub PAT；网关默认不再额外限制仓库范围，实际可访问范围由 PAT 决定
- 一个反向代理；本文给出原生 Windows Caddy 配置，不使用 Docker

当前示例公网前缀：

```text
https://githubaction.giize.com/gemini_mcp
```

MCP URL：

```text
https://githubaction.giize.com/gemini_mcp/mcp
```

## 2. 获取代码

在当前 Windows PowerShell 中执行即可：

```powershell
Set-Location C:\
git clone https://github.com/qqq694637644/gemini_github_spark_action.git
Set-Location C:\gemini_github_spark_action
git fetch origin
git checkout gpt/migrate-gemini-spark
```

PR 合并后可改为：

```powershell
git checkout main
git pull
```

## 3. 从 Gemini 复制重定向 URI

在 Gemini 的“设置自定义连接应用程序”窗口中：

1. 填写 MCP URL：`https://githubaction.giize.com/gemini_mcp/mcp`
2. 展开“高级设置”
3. 点击“复制重定向 URI”
4. 暂时不要点“下一个”

重定向 URI 必须原样传给初始化脚本，不能自行猜测或修改。

## 4. 一键初始化 Python、OAuth 和 `.env`

执行策略只对当前 PowerShell 窗口生效：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

执行初始化。将 `<从Gemini复制的重定向URI>` 和 GitHub 用户名替换为真实值：

```powershell
.\scripts\setup_windows_builtin_oauth.ps1 `
  -PublicBaseUrl "https://githubaction.giize.com/gemini_mcp" `
  -RedirectUri "<从Gemini复制的重定向URI>" `
  -GitHubUsername "qqq694637644" `
  -Force
```

默认行为：

```text
ALLOW_ALL_REPOS=true
ALLOW_WORKFLOW_EDIT=true
ALLOW_DELETE_FILES=true
WORKSPACE_ALLOW_NETWORK=true
WORKSPACE_SHELL=powershell.exe
```

也就是说，项目范围由 GitHub PAT 自己决定，网关不再重复限制。如果以后确实需要第二层项目限制，再可选添加：

```powershell
-AllowedRepos "owner/repo-a,owner/repo-b"
```

脚本会安全提示输入：

- fine-grained GitHub PAT
- 个人 OAuth 授权密码（至少 16 个字符）

脚本会创建：

```text
.env
data/oauth-signing-key.pem
data/oauth.db
data/gemini-oauth-client.txt
.venv/
```

个人模式默认保留 Windows 原有文件权限，不主动重写 ACL。只有明确需要限制为当前用户、SYSTEM 和 Administrators 时，才添加 `-HardenAcl`。

旧版本脚本若造成 `oauth-signing-key.pem` 的 `PermissionError`，以管理员 PowerShell 执行：

```powershell
.\scripts\repair_windows_oauth_acl.ps1
```

查看 Gemini 需要填写的客户端 ID 和客户端密钥：

```powershell
Get-Content .\data\gemini-oauth-client.txt
```

不要把 `.env`、`data/oauth-signing-key.pem`、`data/oauth.db` 或客户端凭据提交到 Git。

重新生成配置时先备份，再使用：

```powershell
.\scripts\setup_windows_builtin_oauth.ps1 `
  -PublicBaseUrl "https://githubaction.giize.com/gemini_mcp" `
  -RedirectUri "<从Gemini复制的重定向URI>" `
  -GitHubUsername "qqq694637644" `
  -Force
```

`-Force` 会生成新的 OAuth 客户端密钥和密码哈希；Gemini 中的客户端密钥也必须同步更新。

## 5. 启动内置授权服务器和 MCP

在第一个 PowerShell 窗口中运行：

```powershell
Set-Location C:\gemini_github_spark_action
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\run_windows_builtin_oauth.ps1 -Port 8005
```

看到以下内容表示本地进程已启动：

```text
Uvicorn running on http://127.0.0.1:8005
```

保持此窗口运行。该脚本只启动一个 Uvicorn worker，确保授权码和 SQLite 状态一致。

本机检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8005/healthz
Invoke-RestMethod http://127.0.0.1:8005/.well-known/oauth-authorization-server
Invoke-RestMethod http://127.0.0.1:8005/oauth/jwks
```

`healthz` 中应显示：

```json
{"auth_mode":"builtin_oauth"}
```

## 6. 配置 Windows 原生 Caddy 反向代理

如果已有 Caddy，请不要覆盖整个 Caddyfile。把以下文件内容放入现有 `githubaction.giize.com { ... }` 站点块顶部，并置于任何 catch-all `handle` 或 `respond` 之前：

```text
deploy/windows/Caddyfile.gemini_mcp.fragment
```

片段负责三类路由：

```text
/gemini_mcp/*
/.well-known/oauth-protected-resource/gemini_mcp/mcp
/.well-known/oauth-authorization-server/gemini_mcp
```

假设 Caddyfile 位于 `C:\caddy\Caddyfile`，验证并重载：

```powershell
caddy validate --config C:\caddy\Caddyfile --adapter caddyfile
caddy reload --config C:\caddy\Caddyfile --adapter caddyfile
```

首次安装 Caddy 时，从 Caddy 官方下载 Windows 静态二进制并放到 `C:\caddy\caddy.exe`，然后以管理员 PowerShell 安装为服务：

```powershell
Set-Location C:\caddy
.\caddy.exe validate --config C:\caddy\Caddyfile --adapter caddyfile
sc.exe create caddy start= auto binPath= "C:\caddy\caddy.exe run --config C:\caddy\Caddyfile --adapter caddyfile"
sc.exe start caddy
```

如果已经有其他反向代理，必须实现等价的路径转发和 rewrite。仅转发 `/gemini_mcp/*` 不够；两个根级 `/.well-known/...` 地址也必须转发。

## 7. 检查公网 OAuth Discovery

在第二个 PowerShell 窗口执行：

```powershell
Set-Location C:\gemini_github_spark_action
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\check_windows_oauth.ps1 `
  -PublicBaseUrl "https://githubaction.giize.com/gemini_mcp"
```

成功输出应包含：

```text
health: HTTP 200
protected resource metadata: HTTP 200
authorization server metadata: HTTP 200
MCP OAuth challenge: HTTP 401
OAuth discovery is ready for Gemini.
```

也可手工检查：

```powershell
Invoke-RestMethod "https://githubaction.giize.com/.well-known/oauth-protected-resource/gemini_mcp/mcp"
Invoke-RestMethod "https://githubaction.giize.com/.well-known/oauth-authorization-server/gemini_mcp"
Invoke-RestMethod "https://githubaction.giize.com/gemini_mcp/oauth/jwks"
```

MCP 未认证请求必须返回 401，并且 `WWW-Authenticate` 中含 `resource_metadata=`。

## 8. 在 Gemini 中填写

回到 Gemini 自定义连接窗口：

```text
自定义应用链接:
https://githubaction.giize.com/gemini_mcp/mcp

客户端 ID:
读取 data/gemini-oauth-client.txt 中的 OAuth client ID

客户端密钥:
读取 data/gemini-oauth-client.txt 中的 OAuth client secret
```

点击“下一个”。浏览器会打开本项目提供的授权页面：

```text
https://githubaction.giize.com/gemini_mcp/oauth/authorize
```

输入初始化脚本中设置的“个人 OAuth 授权密码”，点击“允许”。

授权服务器会执行：

1. 校验客户端 ID、客户端密钥和精确重定向 URI
2. 强制 PKCE S256
3. 生成一次性授权码
4. 签发 RS256 JWT access token
5. 签发并轮换 refresh token
6. 默认把通配符 `*` 写入 token 的仓库 claim，由 GitHub PAT 决定实际访问范围

## 9. 完整只读验收

连接完成后执行：

```powershell
$env:MCP_SERVER_URL = "https://githubaction.giize.com/gemini_mcp/mcp"
$env:MCP_ACCESS_TOKEN = "<仅用于测试的OAuth access token>"
$env:MCP_TEST_OWNER = "qqq694637644"
$env:MCP_TEST_REPO = "gemini_github_spark_action"
$env:MCP_TEST_REF = "main"
.\.venv\Scripts\python.exe scripts\validate_remote_mcp.py
```

该流程会真实调用：

```text
prepareWorkspace(base_ref=main)
workspaceInspect
workspaceStatus
queryCiStatus
```

不会创建分支、不会修改仓库、不会推送。

## 10. 常见错误

### Gemini 仍提示“不支持认证方法”

检查：

```powershell
.\scripts\check_windows_oauth.ps1 -PublicBaseUrl "https://githubaction.giize.com/gemini_mcp"
```

最常见原因是反向代理没有转发根级 `/.well-known/...` 路由。

### `invalid_client`

Gemini 中填写的客户端 ID 或客户端密钥与 `data/gemini-oauth-client.txt` 不一致。

### `redirect_uri is not registered`

重新复制 Gemini 显示的重定向 URI，并用 `-Force` 重建配置。URI 必须逐字符一致。

### 授权页面密码错误

这里需要填写初始化时设置的“个人 OAuth 授权密码”，不是 GitHub PAT，也不是 OAuth 客户端密钥。

### 工具调用返回仓库未授权

个人默认配置不会限制项目范围。此时检查 GitHub PAT 是否实际拥有该仓库权限，以及 `.env` 中是否为 `ALLOW_ALL_REPOS=true`。

## 安全边界

- 只能通过 HTTPS 公开。
- GitHub PAT 决定可访问仓库；网关个人模式默认不重复设置项目白名单。
- 不要公开 8005；只允许本机反向代理访问。
- 不要把授权密码、客户端密钥或 PAT 发到聊天、日志或仓库。
- 备份 `data/oauth-signing-key.pem` 和 `data/oauth.db`；更换签名密钥会使现有 access token 失效。
