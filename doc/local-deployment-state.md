# 本地部署状态（本仓库作为运行实例的上游）

> 本文记录「本仓库 ↔ 运行实例 `E:\AzurPilot`」的关系与恢复方法。
> 本仓库是**唯一持久副本**：`E:\AzurPilot` 里除白名单目录外的内容都可能被启动器清理。

## 一、目录角色

| 路径 | 角色 | 是否会被启动器清理 |
| --- | --- | --- |
| `E:\AzurPilot-master`（本仓库） | **源码权威副本**，运行实例的上游 | 否，在 `E:\AzurPilot` 之外 |
| `E:\AzurPilot` | 运行实例 | 部分（仅白名单目录保留） |
| `E:\AzurPilot\config` | 真实配置与个人数据 | **否**，属白名单 |
| `E:\AzurPilot\.venv` | Python 虚拟环境 | 是（可重建） |

### 启动失败清理的白名单

启动器退出时只枚举 `E:\AzurPilot` 的**根目录一层**，仅保留 7 项：

```
alas-launcher.exe  bootstrap/  config/  deploy/  log/  unins000.dat  unins000.exe
```

其余顶层条目**整个删除**。注意：

- 不会递归进白名单目录内部，所以 `config/` 下的内容安全；
- `.workbuddy/`、`.claude/`、`.cursor/`、`.agent/` **不在白名单**，每次清理都会丢；
- `.venv/`、`frontend/`、`assets/`、`bin/`、`doc/`、`tools/`、`tests/` 会被删，
  但都能通过 git 拉取或脚本重建。

## 二、恢复运行实例的完整步骤

假设 `E:\AzurPilot` 被清空，只剩白名单目录：

```bash
cd E:/AzurPilot
GIT=./bootstrap/git/cmd/git.exe     # 白名单内，清理后仍在

# 1. 重建 git 并指向本仓库
"$GIT" init
"$GIT" remote add origin E:/AzurPilot-master
"$GIT" config --local http.sslVerify true
"$GIT" fetch --progress origin main
"$GIT" branch main origin/main
"$GIT" symbolic-ref HEAD refs/heads/main
"$GIT" reset --hard origin/main     # 此刻工作区为空，需要真检出

# 2. 重建虚拟环境（uv 配置在 config/deploy.yaml）
.venv/Scripts/python.exe -V || uv sync
```

前端不必手工构建：`deploy/frontend.py::ensure_frontend()` 会读取
`frontend/dist/.source-fingerprint` 与源码指纹比对，不一致时**自动**
执行 `npm ci && npm run build`；启动器会自行确保 Node.js 可用。

**别手工写 `.source-fingerprint`。** 它由 `source_fingerprint()` 对
`package.json`、`package-lock.json`、`index.html`、`vite.config.ts`、
`tsconfig.json` 以及 `src/`、`public/` 下全部文件内容做 SHA-256 得出。
写错只会让启动器去跑一次真构建，得不偿失。

## 三、当前生效的配置（运行实例）

配置在 `E:\AzurPilot\config\ap.json`（**白名单内，不会被清理**）。

### 任务优先级

`TaskPriorityAdjustment` 共 77 条，一维线性表，越靠前优先级越高。关键名次：

| 名次 | 任务 | 说明 |
| --- | --- | --- |
| 1 | `Restart` | |
| 3 | `Commission` | |
| 76 | `OpsiScheduling` | 智能调度Plus，**已下调** |
| 77 | `ThreeOilLowCost` | 三油低耗Plus，**已下调** |

两个长耗时任务被压到队尾，以便在其运行期间插入其他任务的处理。

调整工具：`tools/priority_adjust.py`（`--show` / `--move-to-end` / `--dry-run`）。

### 其他

- `Repository: E:/AzurPilot-master`、`Branch: main`（见
  `preemptive-scheduling.md` 第七章，这是「切断自动更新、改手动」的实现）。
- 运行环境：MuMuPlayer12 @ `127.0.0.1:16384`，bilibili 服，
  截图 `ADB_nc`，控制 `MaaTouch`。
- `.venv` 为 cpython-3.14；自带 git 在 `.venv/Scripts/git/`，但**不完整**
  （只有 `cmd/` 与 `mingw64/`，缺 `usr/bin` 即 `sh.exe`），所以执行 git 命令时
  会借用系统 Git for Windows 的运行时目录，见 `deploy/git.py::git_runtime_path`。

## 四、个人数据备份清单

`E:\AzurPilot\config\` 下这些**不在 git 里**，务必单独备份：

| 文件 | 内容 |
| --- | --- |
| `ap.json` | 全部任务配置，**含 LLM API Key，勿外泄** |
| `deploy.yaml` | 部署配置（仓库地址、路径等） |
| `azurstats_local.db` | 掉落/统计数据库 |
| `cl1_data.db` | 委托/关卡数据 |

时间戳快照存放在 `E:\AzurPilot\AzurPilot_Data_Backup\<时间>\`，
另有 `config/*.bak` 形式的单文件备份（由工具自动生成）。

## 五、已知坑位速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 卡在 `FETCH REPOSITORY BRANCH`，`error_code: -1073741819` | `0xC0000005`：子进程 PATH 无 Git 运行时目录，本地目录远端需 `sh`+`git-upload-pack` | 已由 `deploy/git.py::execute()` 自动补 PATH 修复；详见 `preemptive-scheduling.md` 7.6 |
| `fatal: fetch-pack: invalid index-pack output` | `.git/objects/pack/` 残留 `tmp_pack_*` | `rm -f .git/objects/pack/tmp_pack_*` |
| `git-upload-pack: command not found`（`rc=128`） | 只补了 `mingw64/bin`，缺 `usr/bin`（`sh.exe`） | 两个目录都要进 PATH |
| `detected dubious ownership in repository` | 用系统 git 操作属 `BUILTIN/Administrators` 的目录 | 加 `-c safe.directory=*`；现方案用 `.venv` git，用不到 |
| 启动器报找不到 Node.js | 它只探测标准路径（如 `C:\Program Files\nodejs`） | 接受其安装提议，或手工装到标准路径 |
| 日志时间戳与本地时间差 8 小时 | `log/*_launcher.txt` 用 UTC | 北京时间 = 日志时间 + 8 |
| `SAFE_DELETE_BULK_CONFIRM_REQUIRED` | WorkBuddy 会话注入的批量删除守卫 | 只影响会话内进程，与启动器无关 |
