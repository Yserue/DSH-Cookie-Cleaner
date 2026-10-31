# DSH Cookie 自动清理器 v1.0.0

> 面向 Windows 的 DeepSeek Harness 桌面版 Cookie 清理工具。  
> 托盘常驻、低频检查、默认保留最新记录，避免随机端口导致的 `dsh-auth-*` Cookie 堆积，从而减少请求头超过 16 KB 后出现的 “Web authentication failed / 431”。

本版本是 **onedir 文件夹版**，不是单文件 EXE。发布给他人时，请压缩并发送整个 `dist\DSHCookieCleaner` 文件夹，不能只发其中的 `DSHCookieCleaner.exe`。

---

## 一、它解决什么问题

DeepSeek Harness 桌面版每次启动可能使用不同的本地端口，登录过程中会写入 `127.0.0.1` / `localhost` 下的 `dsh-auth-*` Cookie。长期使用后 Cookie 数量会不断增加，可能造成：

- 请求头越来越大，最终超过 16 KB；
- DSH 启动时报 “Web authentication failed” 或 HTTP 431；
- 手动清理又容易误删其他 Cookie。

本工具只处理同时满足以下条件的记录：

```text
host_key IN ('127.0.0.1', 'localhost')
AND name LIKE 'dsh-auth-%'
```

其他域名、其他名称的 Cookie 一律不动。

---

## 二、第一次使用

1. 将 `DSHCookieCleaner.zip` 解压到一个**普通文件夹**（例如 `D:\Tools\DSHCookieCleaner`）。  
   不要只把 exe 拖出来运行，exe 必须和 `_internal` 文件夹在一起。
2. 双击 `DSHCookieCleaner.exe`。
3. 第一次运行会弹出 **“DSH Cookie 清理工具 - 首次设置”** 窗口。
4. 按需修改下面的项目，然后点 **“保存并继续”**：
   - 备份存放位置（默认 `<程序目录>\backups`）
   - 日志存放位置（默认 `<程序目录>\log`）
   - 备份保留上限（默认 30 份）
   - 单个日志大小上限（默认 2 MB）
   - 历史日志保留数量（默认 3 个）
   - 触发清理阈值（默认 50 条）
   - 清理后保留最新 Cookie（默认 10 条）
   - 后台检查间隔（默认 2 小时）
   - 开机时自动在后台清理（可选）
5. 保存后会写入同目录的 `config.json`，并创建日志/备份目录，然后自动进入托盘后台模式。

> 数字输入框只接受数字。留空会使用默认值；输入字母、符号或其他非数字内容会提示 **“只能输入数字！”**，并且不会保存。

### 想重新打开设置

- 删除程序目录下的 `config.json`，再启动；或
- 在托盘图标上右键 → **设置**；或
- 运行 `DSHCookieCleaner.exe --setup`。

---

## 三、托盘模式和菜单

程序默认常驻系统托盘，不显示主窗口。右键托盘图标可以看到：

| 菜单项 | 作用 |
| --- | --- |
| 立即检查 | 立刻执行一次检查；不等待 2 小时周期 |
| 设置 | 打开配置窗口（左键双击托盘图标也可以） |
| 检测更新 | 在后台请求 GitHub Release API，发现新版时打开下载页并提示 |
| ⚠️ 彻底退出程序 | 停止后台线程、关闭托盘并退出 |

程序使用一个命名互斥锁防止重复启动。若已经有一个实例在运行，再次双击 exe 会在右下角提示 **“程序已在后台运行”**，然后安静退出，不会打开第二个清理进程。

---

## 四、后台检查、全屏和资源占用

- 后台定时器使用 `threading.Event().wait(timeout)`，不使用 `while True` 空转，也不使用 `time.sleep()` 轮询。
- 定时任务运行在 `daemon=True` 的守护线程中，不会阻塞 Windows 关机。
- 默认每 **2 小时**检查一次，可在设置中修改；保存后立即生效。
- 每次唤醒先检查 DSH 是否在运行：
  - 如果 DSH 正在运行：记录日志并跳过，不会复制、分析或写回任何 Cookies。
  - 如果检测到全屏前台窗口（通常是游戏）：推迟本次检查，避免抢占资源。
- 未达到阈值时只做只读计数，然后继续休眠。
- 达到阈值后才会复制到临时工作区、清理、备份并写回。

程序不会强制结束 DSH，也不会修改 DSH 的安装目录。

---

## 五、日志

默认日志：

```text
<程序目录>\log\clean_cookie.log
```

日志按设置自动轮换：

- 单个文件达到“单个日志大小上限”后轮换；
- 最多保留“历史日志保留数量”个 `.1`、`.2`… 文件；
- 使用 UTF-8 编码。

日志会记录：

- 程序目录、配置路径、日志路径、备份路径；
- 检查间隔、阈值、保留条数；
- DSH 进程检测结果；
- 清理前/清理后/删除条数；
- 备份文件名、旧备份清理数量；
- 完整的中文错误信息。

日志中不会写入 Cookie 值。

---

## 六、备份和恢复

清理前会先把原 `Cookies` 和可能存在的 `Cookies-journal` 复制到配置的备份目录：

```text
backups\Cookies.bak.YYYYMMDD_HHMMSS
backups\Cookies.bak.YYYYMMDD_HHMMSS-journal
```

- 备份目录可在设置中修改。
- 每次备份后按“主库 + journal”为一组扫描。
- 超过“备份保留上限”时，从最旧的组开始删除，直到不超过上限。
- 只删除备份目录下严格匹配 `Cookies.bak.*` 的普通文件，不删除其他文件，也不会跨目录删除。

### 恢复某次备份

1. 彻底退出 DSH（含托盘），确认任务管理器中没有 `DeepSeek Harness.exe`。
2. 彻底退出本工具（托盘右键 → ⚠️ 彻底退出程序）。
3. 进入备份目录，找到要恢复的 `Cookies.bak.YYYYMMDD_HHMMSS`。
4. 复制它，覆盖回：

```text
%APPDATA%\@deepseek-ai\dsh-desktop\Network\Cookies
```

5. 如果备份同时带有 `-journal`，把它复制为同目录下的 `Cookies-journal`。
6. 重新启动 DSH。

> 恢复前建议把当前 `Cookies` 再另存一份，避免恢复错文件。  
> 不要只恢复 journal 而不恢复主库，二者必须来自同一组备份。

---

## 七、配置文件和路径

配置文件固定在程序目录：

```text
<程序目录>\config.json
```

示例：

```json
{
  "backup_dir": "D:\\Tools\\DSHCookieCleaner\\backups",
  "log_dir": "D:\\Tools\\DSHCookieCleaner\\log",
  "backup_retention": 30,
  "log_max_mb": 2,
  "log_backup_count": 3,
  "threshold": 50,
  "keep": 10,
  "autostart": false,
  "check_interval_hours": 2
}
```

说明：

- 相对路径会按 `config.json` 所在目录解析。
- 修改日志目录或备份目录并保存时，程序会使用 `shutil.move` 把旧目录中的文件迁移到新目录，支持跨盘符。
- 如果新目录已有同名文件，会生成 `_1`、`_2` 等名称，不覆盖原文件。
- 迁移后旧目录可能保留为空目录，可手动删除。
- 自定义到其他盘符的路径，卸载脚本无法自动识别，需要你手动删除。

### Cookie 路径策略

主路径通过环境变量解析，不硬编码用户名：

```text
%APPDATA%\@deepseek-ai\dsh-desktop\Network\Cookies
```

仅当主路径不存在时，才回退到旧机器的固定路径，并在日志中明确警告：

```text
使用了硬编码回退路径，可能不是当前用户
```

---

## 八、卸载

1. 托盘右键 → **⚠️ 彻底退出程序**。
2. 双击程序目录中的 `uninstall.bat`。
3. 确认提示：

```text
确定要完全卸载并清理所有数据吗？(Y/N)
```

输入 `Y` 后，脚本会删除程序目录下的：

- `config.json`
- `log\`
- `backups\`
- 注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 中的 `DSHCookieCleaner` 自启动项

然后提示：

```text
程序本体请直接删除本文件夹即可
```

此时再删除整个 `DSHCookieCleaner` 文件夹即可，不留程序数据。

> 如果日志或备份被配置到了其他盘符，请手动删除那些目录。  
> `uninstall.bat` 不会删除 DSH 自己的 Cookies，也不会删除 DSH 安装目录。

---

## 九、升级（保留配置）

本工具是便携式目录版，升级不会覆盖用户数据：

1. 彻底退出旧版（托盘右键 → 彻底退出）。
2. 下载新版 `DSHCookieCleaner.zip`。
3. 解压后，把新文件夹中的内容覆盖到旧文件夹。
4. 保留原来的 `config.json`、`log\`、`backups\` 即可。

程序本体（`DSHCookieCleaner.exe` + `_internal`）和用户数据是分开的，因此覆盖程序文件不会丢失配置和历史备份。

> 覆盖前建议先备份整个文件夹；如果新版修改了配置字段，程序会为缺失字段使用默认值。

---

## 十、命令行参数（可选）

普通用户不需要命令行。高级用法：

```text
DSHCookieCleaner.exe                 默认：托盘后台模式
DSHCookieCleaner.exe --setup         重新打开首次设置窗口
DSHCookieCleaner.exe --once          只执行一次清理，不进入托盘
DSHCookieCleaner.exe --dry-run       只统计，不备份、不修改
DSHCookieCleaner.exe --silent        不显示结果弹窗（适合开机自启）
DSHCookieCleaner.exe --cookies-dir D:\test\Network
DSHCookieCleaner.exe --version
```

`--dry-run` 会自动按单次模式运行，不会启动托盘。  
`--once` 适合排障或计划任务；正常使用建议保持默认托盘模式。

---

## 十一、从源码构建

### 环境

- Windows 10/11 64 位
- Python 3.10 或更高（建议 3.12）
- 依赖见 `requirements.txt`：

```text
psutil>=5.9,<8
Pillow>=10,<13
pystray>=0.19,<0.20
```

安装：

```powershell
python -m pip install -r requirements.txt
```

### 构建

直接双击 `build.bat`，或在命令行运行：

```bat
build.bat
```

脚本会：

1. 检查 Python（`python` 或 `py -3`）；
2. 如缺少 `assets\app.ico`，调用 `make_icon.py` 生成一个占位图标；
3. 使用 onedir 模式打包：

```bat
pyinstaller --noconfirm --noconsole --onedir --icon=assets\app.ico --add-data "assets;assets" --name DSHCookieCleaner main.py
```

4. 把 `uninstall.bat` 复制到 `dist\DSHCookieCleaner`；
5. 检查 `dist\DSHCookieCleaner\DSHCookieCleaner.exe` 是否存在。

> 使用 onedir 而不是 onefile，是为了避免 PyInstaller 单文件在权限受限目录中解压临时文件失败，出现 `Could not create temporary directory!`。

### 当前构建产物

| 项目 | 值 |
| --- | --- |
| EXE | `dist\DSHCookieCleaner\DSHCookieCleaner.exe` |
| EXE 大小 | 2,923,455 字节 |
| EXE SHA256 | `F6977B751785278A6F9D7EB820C6D64700D15BB9C1507F72D85630D4551E8BB2` |
| 文件夹大小 | 46,814,296 字节 |
| 文件数 | 957 |
| 发布 ZIP | `dist\DSHCookieCleaner.zip` |
| ZIP 大小 | 20,239,381 字节 |
| ZIP SHA256 | `BD88CD446C44D7E2A1DBE5CEB5CEAAA50BE37620557607B1AAACBA71C66B74E4` |

> Python 版本、PyInstaller 版本和图标文件不同，哈希会变化；以你本地重新构建的结果为准。

---

## 十二、测试

项目自带 `test_clean.py`，只使用临时目录和临时 SQLite 数据库，不读取、不修改真实 Cookies。

运行：

```powershell
python test_clean.py -q
```

当前覆盖 **50 项**测试，包括：

- 49 条不清理、50/60 条触发清理并只保留最新 10 条；
- 其他 host / 其他名称的 Cookie 不变；
- 备份组轮换（35 组只保留 30 组）；
- 数据库表/列缺失、损坏数据库、权限错误；
- DSH 运行中不复制、不写回；
- DSH 在写回前启动时放弃写回；
- 配置读写、默认值、非数字校验、路径迁移不覆盖；
- 日志轮换参数；
- 更新检查的“有新版/已是最新/网络失败”；
- 托盘菜单项和单实例互斥逻辑。

---

## 十三、杀毒软件误报

PyInstaller 打包的 Python 程序，尤其是带托盘、会读写 `%APPDATA%` 的程序，可能被 360、火绒等杀毒软件误报。

处理建议：

1. 只从你信任的发布页下载 ZIP；
2. 核对 ZIP / EXE 的 SHA256；
3. 在杀毒软件中把整个 `DSHCookieCleaner` 文件夹加入信任区/白名单；
4. 不要为了“通过扫描”而关闭系统安全功能。

本工具不联网上传数据；只有你点击 **检测更新** 时才会访问 GitHub API。

---

## 十四、常见问题

### Q1. 双击没有窗口，是不是没启动？

这是托盘程序，默认不显示主窗口。请看右下角系统托盘；第一次运行会先弹设置窗口。

### Q2. 提示“请先从右下角托盘彻底退出 DSH 后再试”

说明检测到 `DeepSeek Harness.exe` 仍在运行。请：

1. 右键 DSH 托盘图标退出；
2. 在任务管理器确认没有 `DeepSeek Harness.exe`；
3. 再点“立即检查”。

程序不会强制结束 DSH。

### Q3. 为什么没有清理？

低于设置的阈值（默认 50 条）时只检查，不修改。  
也可能是 DSH 正在运行、当前处于全屏状态，或数据库被占用。

### Q4. 检测更新提示“网络连接失败，请稍后再试”

这是离线、超时、GitHub API 被拦截或仓库地址尚未配置时的统一提示。  
发布前请把 `main.py` 顶部的：

```python
GITHUB_REPO = "dsh-cookie-cleaner/DSHCookieCleaner"
```

改成你自己的 GitHub `用户名/仓库名`。程序不会自动下载安装包，只会打开 Release 页面。

### Q5. 能不能只发 exe？

不能。这是 onedir 程序，必须同时携带 `_internal` 文件夹。请发送整个 `dist\DSHCookieCleaner` 的 ZIP。

### Q6. 备份能自动删吗？

可以，按“备份保留上限”自动轮换最旧的组；但不会删除备份目录之外的文件。

### Q7. 会不会误删其他 Cookie？

不会。SQL 只匹配 `host_key IN ('127.0.0.1','localhost')` 且 `name LIKE 'dsh-auth-%'`，测试中专门验证了其他 host / 名称不受影响。

---

## 十五、安全边界

- 绝不在 DSH 运行时复制、分析或写回 Cookies；
- 复制前、备份前、写回前分别再次检测 DSH 进程；
- 写回使用临时工作库和原子替换；
- 只操作目标 `Network` 目录下的 `Cookies` / `Cookies-journal`；
- 不删除整个 `Network` 或 `dsh-desktop` 目录；
- 备份轮换只删除配置备份目录内严格匹配的备份组；
- 所有测试使用临时 SQLite 文件，不接触真实用户 Cookies；
- 日志不记录 Cookie 值。

---

## 十六、文件清单

```text
DSHCookieCleaner\
├─ DSHCookieCleaner.exe
├─ _internal\
├─ uninstall.bat
├─ config.json          （首次设置后生成）
├─ log\                 （首次设置后生成）
├─ backups\             （首次设置后生成）
└─ ...
```

源码目录还包含：

```text
main.py
test_clean.py
requirements.txt
build.bat
uninstall.bat
make_icon.py
README.md
LICENSE
.gitignore
assets\app.ico
```

## 许可证

MIT License，详见 `LICENSE`。
