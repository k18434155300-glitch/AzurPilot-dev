# 抢占式任务调度改造方案

> 目标：高优先级任务进入待运行队列时，中断正在运行的低优先级任务，
> 并将被中断的任务放回待运行队列（而非丢弃或延后到下一周期）。

## 一、现状梳理

### 1.1 已具备的能力（无需改动）

| 能力 | 位置 |
|---|---|
| 优先级表配置 | `config/ap.json` → `General.YukikazeTaskManager.TaskPriorityAdjustment` |
| 优先级解析 | `module/config/task_priority.py::parse_task_priority`（`PRIORITY_SEPARATOR = "\n> "`，展平为一维序列表） |
| 调度层排序 | `module/config/config.py:333-338` `pending = f.apply(pending)` |
| UI 层排序 | `module/api/runtime_service.py:52` `pending → (0, order[name])` |
| 等待期切换 | `alas.py:1926 get_next_task()` 中 `wait_until()` 返回 False 即 `continue` 重算队列 |
| 优雅中断异常 | `alas.py:964` `except TaskEnd: return True` —— 抛出后任务按**成功**处理 |

结论：**排队顺序与等待期切换已经可用**，缺口只有一个 —— 任务执行期间的中断。

### 1.2 缺口

`alas.py:2219` 的 `self.run(...)` 是同步阻塞调用。任务一旦开始，
除非内部主动抛出 `TaskEnd`，否则外部无法介入。

## 二、设计要点

### 2.1 中断点选择

所有任务的循环结构都形如：

```python
while 1:
    self.device.screenshot()      # ← 必然反复经过
    if self.appear(X): ...
```

因此 `module/device/device.py:336 screenshot()` 是**事实上的统一心跳点**，
在此挂载抢占检查即可覆盖全部任务，无需逐个任务改造。

代价与取舍：

- 高频调用 → 必须节流（**默认 60 秒**一次，可配置）

  每次检查会经 `check_task_switch()` → `task_switched()` → `load()`，
  其中包含一次配置树 `deepcopy`（95 个任务组），几十毫秒量级，60 秒一次开销可忽略。

  **间隔取 60 是刻意的，不是保守**——见 2.1.2 与 2.1.3：大世界自动搜索与
  三油低耗本身就有「每场战斗」的检查点，且命中后会优雅退出战斗再中断。
  心跳间隔大于官方周期，才能确保官方机制优先触发，本控制器退居兜底。
- 可能在不理想的瞬间中断 → 提供**允许抢占的任务白名单**，且开关默认关闭

### 2.1.1 关键：复用官方中断路径，而非自造

项目**已经存在**一套任务切换机制，且正是「取消勾选后跑完收尾再切换」的实现：

```python
# module/config/config.py:738
def task_switched(self):
    prev = getattr(self, '_task_switch_owner', self.task)
    self.load()                # 重新载入配置
    new = self.get_next()      # 按优先级重算队列
    if prev == new:
        logger.info(f"[配置] 继续任务 `{new}`")
        return False
    logger.info(f"[配置] 切换任务 `{prev}` 到 `{new}`")
    return True

# :758
def check_task_switch(self, message=""):
    if getattr(self, '_disable_task_switch', False):
        return
    if self.task_switched():
        self.task_stop(message=message)      # → raise TaskEnd

# :720
@staticmethod
def task_stop(message=""):
    async_executor.flush(timeout=2.0)        # ← 等待异步收尾
    raise TaskEnd
```

三个要点：

1. **判定逻辑天然覆盖了抢占**。`get_next()` 会重算 pending 并按优先级排序，
   只要胜出的不是当前任务就返回 True。而 `Function.__eq__` 比对的是
   「命令 + NextRun」，所以高优先级任务一旦到期挤占队首，判定自动成立，
   **无需自行比较优先级**。
2. **`task_stop()` 会 `async_executor.flush(timeout=2.0)`**，等待异步任务落地后才抛
   `TaskEnd`。这就是「跑完收尾再切换」的来源 —— 直接抛 `TaskEnd` 会跳过它，
   可能导致异步任务（掉落统计、数据上报）不完整。
3. 任务内部的 `if self.config.task_switched():` 是设计者选定的安全点，
   但只分布在约 23 个文件里，**覆盖不全**，所以仍需心跳兜底。

因此本实现不在控制器里自行判定，而是节流后调用 `config.check_task_switch()`，
由官方路径完成「判定 → 收尾 → 中断」全流程。

### 2.1.2 更严格的一层：`_disable_task_switch`

官方保护比我们设想的「中断前先退出战斗」还要严格——**刷图期间干脆禁止中断**。

`module/os/tasks/scheduling.py:935` 在智能调度切换子任务时设置：

```python
self.config._disable_task_switch = task_name not in (
    self.TASK_NAME_HAZARD1_LEVELING,      # 侵蚀1练级
    self.TASK_NAME_MEOWFFICER_FARMING,    # 耄耋相接
)
```

而 `check_task_switch()` 开头就是：

```python
if getattr(self, '_disable_task_switch', False):
    logger.info('[配置] 任务切换检查已临时禁用')
    return
```

即：**除练级与耄耋相接外，大世界子任务执行期间不响应任何切换请求**，
包括本抢占。这带来两个结论：

* 心跳在刷图中触发时会被静默忽略，**不会把游戏丢在战斗界面**，
  无需再实现「先 `ensure_auto_search_exit()` 再中断」；
* 抢占真正生效的时机是**子任务边界**（调度层重新选择子任务的间隙），
  这正是「在长任务中间插入其他任务」想要的位置。

`_task_switch_owner` 则保证智能调度内部切换子任务时，
对外仍视作同一个主人任务（`OpsiScheduling`），不会被误判为任务已切换。

> 因此当前实现**不做**绕过 `_disable_task_switch` 的强制中断。
> 若未来确有需要，应作为独立开关并自行承担战斗状态残留的风险。

### 2.1.3 两个长耗时任务的官方检查点对照

需要被抢占的正是这两个任务，而它们**各自都已有细粒度且会优雅退出的检查点**：

| 任务 | 实现 | 检查点 | 检查粒度 | 中断方式 |
|---|---|---|---|---|
| 智能调度 Plus（含侵蚀1练级） | `module/os/map.py:1030` | 自动搜索战斗循环内 `combat_appear()` | 每场战斗（约 20–30 秒） | `interrupt_auto_search()` → 导航回主页面/地图页 → `TaskEnd` |
| 三油低耗 | `module/campaign/gems_farming.py:1176`（`run()` 的 `while 1` 末尾） | `task_switched()` | 每轮刷图（一场战斗级） | `ensure_auto_search_exit()` → `task_stop()` |

两者命中后都**先退出自动搜索战斗，再中断**，且粒度都在一场战斗级别——
远快于「刷完一整轮」。三油低耗另有 `os/tasks/hazard_leveling.py:153`
这类整轮级的检查点，但日常主要由上表两处生效。

因此本项目心跳**不应与它们争快**，而应退居兜底：

* 正常情况下官方检查点先命中，走优雅中断；
* 心跳只在任务卡死、或任务本身没有自动搜索循环（如主界面类任务）时才介入。

### 2.1.4 为何不做「战斗状态保护」

曾考虑过一个更保守的方案：抢占前先用当前截图做一次模板匹配
（`is_in_auto_search_menu()` 的本质就是一次 `match_luma`），
若正处在自动搜索战斗界面就跳过本次，等下一轮心跳，让中断尽量落在非战斗间隙。

技术上完全可行——抢占点恰好持有最新截图。**但最终没有采用**，理由是：

* **大世界练级是连续战斗**，「非战斗时刻」可能长时间不出现，
  抢占会形同虚设，甚至因为迟迟不触发而引入难以排查的行为；
* 项目自身的检查点（如 `hazard_leveling.py:153`）本就落在一轮结束附近，
  实践中同样靠近战斗，额外回避并无实质收益；
* 引入 UI 判定会增加与页面系统的耦合，反而可能带来新的判定错误。

结论：**不回避战斗状态，与官方行为保持一致**。
数据安全性由 `task_stop()` 的 `async_executor.flush(timeout=2.0)` 承接，
游戏状态则由下一个任务进入时的 `ui_goto` 自愈。

### 2.1.5 重入保护

`task_stop()` 的收尾、`ensure_auto_search_exit()` 内部都会调用
`device.screenshot()`，会再次进入本钩子。控制器用 `_in_check` 标志切断递归。

### 2.2 抢占判定：交给 `get_next()`

不由控制器自行比较优先级。`task_switched()` 内部调用 `get_next()`，
它会重新载入配置、重建 pending/waiting 队列并施加 `SCHEDULER_PRIORITY` 排序，
返回队首任务。控制器只在额度到达时对 bookeeper 发起一次询问。

这样做避开了重复实现可能遗漏的边界：`AzurLaneConfig.is_hoarding_task`、
错误队列（`error + pending`）、`ServerUpdate` 修正等。

### 2.3 被抢占任务的处置

`TaskEnd` 会让任务跳过末尾的 `task_delay()`，因此 `NextRun` 大概率仍是到期状态，
任务**自然留在 pending 队列**等待下次被选中 —— 此时无需干预，强行回置反而会
丢失「已到期多久」的信息。

但若任务在执行途中已经推迟过自己的 NextRun（部分刷图类任务会周期性更新），
中断就等于吞掉这一次执行。`ensure_requeued()` 只在这种情况下回置：

```python
if next_run > now:                     # 已被推迟到未来
    config.modified[f'{task}.Scheduler.NextRun'] = now
    config.update()
```

### 2.4 防抖

同一轮抢占后若立即重新选中被抢占的任务，可能来回震荡。措施：

- 抢占后强制 `continue`，重新走一轮 `get_next_task()`
- 被抢占任务回置的 NextRun 取 `now`，但下一轮中会与真正到期的更高优任务竞争，优先级表兜底

## 三、实现清单

| # | 文件 | 改动 |
|---|---|---|
| 1 | `module/runtime/preemption.py` | 新增：抢占判定器（节流、优先级比较、白名单） |
| 2 | `module/device/device.py` | `screenshot()` 内挂载检查钩子 |
| 3 | `alas.py` | loop 中设置/清理上下文；捕获抢占并处理 NextRun 回置 |

配置项（读取 `General.YukikazeTaskManager`，缺省值即关闭，保证向后兼容）：

- `TaskPreemptionEnabled`：总开关，**默认 false**
- `TaskPreemptionCheckInterval`：检查间隔秒数，默认 15
- `TaskPreemptionAllowlist`：允许被抢占的任务名列表，空表示不限

> 这三个字段尚未加入 UI 表单 —— `argument.yaml` / `args.json` 属于配置生成链的一环，
> 直接改动有兼容风险，改为直接读写 `config/ap.json`。缺省即关闭，向后兼容。

## 四、启用方式

在运行实例的配置 `config/ap.json` 中，给 `General.YukikazeTaskManager` 补上字段
（已存在时直接改写值）：

```json
"YukikazeTaskManager": {
  "TaskPriorityAdjustment": { "...": "保持原样" },
  "TaskPreemptionEnabled": true,
  "TaskPreemptionCheckInterval": 15,
  "TaskPreemptionAllowlist": "OpsiScheduling, Commission"
}
```

一键写入或关闭：

```bash
python tools/preemption_config.py --config E:/AzurPilot/config/ap.json --enable \
    --interval 5

python tools/preemption_config.py --config E:/AzurPilot/config/ap.json --disable
```

### 优先级调整

长耗时任务（不手动停止就会一直运行）应下调到表末尾，使其只在其他任务
都处理完时空闲运行：

```bash
python tools/priority_adjust.py --config E:/AzurPilot/config/ap.json --show

python tools/priority_adjust.py --config E:/AzurPilot/config/ap.json \
    --move-to-end OpsiScheduling ThreeOilLowCost --dry-run   # 先预览
```

优先级下调与抢占是配套的：下调只决定「排队时排在哪」，而**正在运行的长任务
能被后来者挤掉，靠的是抢占**。

### `TaskPreemptionAllowlist` 的定位

它只是**窄化影响范围**的手段（比如只想让某几个任务参与抢占），
**不是安全保护**。不要把「排除战斗任务」当作规避风险的方案——
需要被抢占的往往恰恰就是战斗任务，而安全性由下一节的机制保证。

## 五、验证

离线单元测试不依赖项目第三方依赖，可直接运行：

```bash
python tests/test_preemption_offline.py
```

覆盖七个场景：默认关闭不打扰、高优先级抢占、低优先级不打断、
白名单收敛范围、抢占后 NextRun 正确回置、仍在到期状态时不回置、
正常结束不篡改 NextRun。

## 六、风险与回滚

- 中断点落在 UI 切换或战斗过程中，可能导致界面状态残留。
  ALAS 任务入口普遍具备「先回主界面」的自愈逻辑，且后续 `Restart` 可兜底，
  但首次启用建议先在少量任务上验证。
- 全部改动受单一总开关控制，置 false 后行为完全等同改动前。
- 回滚：本仓库基线提交为 `85d0170`，`git revert` 或 `git reset --hard 85d0170` 即可。

## 七、部署与启动（更新源接管）

### 7.1 为什么必须动更新逻辑

源码改动写在受 git 跟踪的文件里（`alas.py`、`module/device/device.py`），
而启动器每次冷启动都会执行 `deploy/installer.py` → `git_install()` →
`git reset --hard origin/<Branch>`，本地改动会被直接抹掉。这条路径有两条：

| 路径 | 触发时机 | 是否抹掉本地改动 |
| --- | --- | --- |
| `deploy/installer.py` → `git_install()` | 双击 `alas-launcher.exe` 冷启动 | **会**，无条件 |
| `module/runtime/updater.py` → `check_update_loop()` | WebUI 运行期，每 `CheckUpdateInterval` 分钟 | 否，自带保护 |

两者的总闸都是同一个云端返回值 `https://alas-apiv2.nanoda.work/api/updata`，
返回 `None`（不可达）会 `raise ExecutionError` 直接终止启动，
因此**断网、hosts 屏蔽、防火墙拦截都不可行**——必须让它连得上，再从源头换掉货。

### 7.2 做法：把上游换成自己的仓库

`deploy/installer.py` 里唯一的「软开关」其实是 `config/deploy.yaml` 的
`Repository` 与 `Branch`：这条路径会用它们去 `git remote set-url`。
而 `config/deploy.yaml` 有两个天然优势：

- 被 `.gitignore:10`（`config/*.yaml`）排除，**不参与 `reset --hard`**；
- 位于 `config/` 目录内，**属于启动失败清理的白名单**，不会被误删。

所以不需要改 `deploy/git.py` 一行代码，改配置即可让"自动更新"拉取我们自己的仓库，
顺带实现「手动跟进上游」：

```yaml
Deploy:
  Git:
    Repository: E:/AzurPilot-master   # 本地源码仓库
    Branch: main                      # 该仓库的分支名
```

配套要点：

1. **先把 `.gitignore` 排除的文件补回源码仓库**：当初做基线快照时，
   `AGENTS.md`、`.claude/settings*.json`、`.cursor/rules/*.mdc` 被忽略规则排除。
   它们在上游是被跟踪的，`reset --hard` 会按「目标树里没有」把这几个文件从磁盘删掉。
   用 `git add -f` 补回（提交 `5431552`）。
2. **本地校验再双击**：完整模拟一次启动时的 git 序列，确认不炸（见 7.4）。
3. **`.venv`、`frontend/dist`、`node_modules`、`cache/`、`config/` 都是未跟踪或白名单内容**，
   `reset --hard` 不会碰它们，切换上游不需要重建虚拟环境。
4. `GitOverCdn` 默认为 `False`（`deploy/config.py:79`），不会走 CDN 那条旁路绕过 git。

### 7.3 两条启动方式

**方式一（推荐）：双击 `alas-launcher.exe`。**
启动器会自行探测并安装 Node.js（实测日志：
`Node.js runtime is available version="v24.21.0" executable=C:\Program Files\nodejs\node.exe`），
因此前端重建能力已经就位，不必手工装 Node。
另外 `frontend/dist/.source-fingerprint` 与源码指纹一致时，`ensure_frontend()`
会直接短路返回，**即便没有 Node 也能启动**。只有某次失败清理删掉 `frontend/`
导致 `dist` 不复存在时，才会真正触发 `npm ci && npm run build`。

**方式二：绕开启动器直启 WebUI。**

```bash
cd E:/AzurPilot
.venv/Scripts/python.exe gui.py        # 默认监听 0.0.0.0:25548
```

不经过 installer，因此不做任何 git 操作。适合临时验证，
但不能替代 7.2 的配置接管。

### 7.4 验证与排障

```bash
cd E:/AzurPilot
# 1. 确认部署配置已生效
.venv/Scripts/python.exe -c "from deploy.config import DeployConfig as D; print(D().Repository, D().Branch)"

# 2. 完整模拟启动时的 git 序列（reset 后应落在自己的提交上）
GIT=./.venv/Scripts/git/cmd/git.exe
"$GIT" fetch origin main && "$GIT" reset --hard origin/main && "$GIT" pull --ff-only origin main

# 3. 离线测试
.venv/Scripts/python.exe tests/test_preemption_offline.py
```

预期的端到端现象：WebUI 启动后日志出现

```
[GUI] WebUI 服务已就绪 (PID: ...)
... fetch origin main
From E:/AzurPilot-master
 * branch  main -> FETCH_HEAD
无更新
```

**排障：`fatal: fetch-pack: invalid index-pack output`。**
这是 `.git/objects/pack/` 下遗留了 `tmp_pack_*` 临时文件所致
（通常来自被中断的抓取）。删除它们后重跑即可：

```bash
rm -f .git/objects/pack/tmp_pack_*
```

### 7.5 手动跟进上游

配置接管后不再自动接收官方更新。需要跟进时在源码仓库里操作：

```bash
cd E:/AzurPilot-master
git remote add upstream https://github.com/wess09/AzurPilot   # 或官方镜像
git fetch upstream
git merge upstream/master                                     # 冲突集中在 alas.py / device.py
```

合并后运行实例会在下次冷启动时自动拉取。若官方改动与抢占钩子冲突，
重点看 `alas.py::loop()` 的 `try/finally` 与 `Device.screenshot()` 两处插入点。

回滚接管的办法：把 `config/deploy.yaml` 的 `Repository` 改回
`git://git.pull/AzurPilot`、`Branch` 改回 `master`，备份在
`config/deploy.yaml.repo-*.bak`。

### 7.6 排障实录：启动器卡在 `FETCH REPOSITORY BRANCH`（`0xC0000005`）

**症状**：界面停在 `FETCH REPOSITORY BRANCH`，日志里 `fetch` 每次都在约 2.8 秒后
以 `[ failure ], error_code: -1073741819` 结束，连续重试后放弃。

**错误码含义**：`-1073741819` 的无符号形式为 `3221225477`，即 `0xC0000005`
`STATUS_ACCESS_VIOLATION`——**git 子进程自身崩溃**，不是网络问题，也不是
分支或仓库地址写错。

**真正的根因：子进程 PATH 里没有 Git 运行时目录。**

把上游改成**本地目录** `E:/AzurPilot-master` 后，git 走的是 local transport：
它要经 `sh` 去启动对端的 `git-upload-pack`，而这两个程序**只能靠 PATH 定位**。
启动器拉起 Python 子进程时给的 PATH 里没有任何 Git 安装目录；恰好 `.venv`
自带的那份 Git 又是**残缺的**——只有 `cmd/` 与 `mingw64/`，**没有 `usr/bin`**，
也就是没有 `sh.exe`。`sh` 起不来，git 就以 `0xC0000005` 崩掉。

二分验证的证据链：

| 追加到 PATH 的目录 | 结果 |
| --- | --- |
| 无（原样） | 崩溃 `0xC0000005` |
| 仅 `usr/bin` | `rc=128`，`git-upload-pack: command not found` |
| `usr/bin` + `mingw64/bin` | **exit 0** |

**修法：`deploy/git.py` 执行命令前自行补齐 PATH。**
新增 `GitManager.git_runtime_path`（探测 Git for Windows 的 `mingw64/bin` 与
`usr/bin`）与 `execute()` 重写，把命令前缀成：

```
set "PATH=<mingw64/bin>;<usr/bin>;%PATH%" && "…/git.exe" fetch origin main
```

这样 git 不再依赖调用方给的环境变量。探测顺序为：`GitExecutable` 自身的安装根 →
`%ProgramFiles%\Git` → 硬编码的 `E:/Program Files/Git`、`C:/Program Files/Git` →
PATH 上 `git` 的安装根；同一安装根下两个目录齐全时直接采用（最自洽），
找不到就返回空列表、行为退回原样（对远端是 HTTP(S) 的场景无副作用）。

**端到端验证（A/B 对照，子进程用启动器同款最小环境）**：

| 组别 | 命令 | 结果 |
| --- | --- | --- |
| 对照组：不补 PATH | `git fetch origin main` | `3221225477`（= `0xC0000005`） |
| 实验组：`GitManager.execute()` | `fetch --progress` | `[ success ]` |
| 实验组：`GitManager.execute()` | `reset --hard origin/main` | `[ success ]` |
| 实验组：`GitManager.execute()` | `pull --ff-only origin main` | `[ success ]` |

**关键鉴别：手工终端跑通 ≠ 启动器能用。**

| 环境 | 结果 |
| --- | --- |
| 系统 git 2.55.0（bash，PATH 完整） | 成功 |
| `.venv` 内置 git 2.51.0（bash，PATH 完整） | 成功 |
| `.venv` 内置 git 2.51.0 + `os.system()`（PATH 完整） | 成功 |
| **启动器进程内（PATH 无 Git 目录）** | **崩溃 `0xC0000005`** |

结论只能从日志的 `error_code` 读取，不能只看手工命令的返回值。

**一个被推翻的中间结论（记录以免重蹈）**：最初判断为"运行实例 `.git` 太大"
（1.3 GB，含 988 MB 巨型 pack），并据此重建了 `.git`（1.3 GB → 354 MB，
`fsck` 干净）。**但 `fetch` 依然失败**——体积不是原因。重建本身无害且确实
省了空间，可它不是解法；真正的解法是上面补 PATH 那一处。

**两个相邻但独立的坑**：

- `fatal: fetch-pack: invalid index-pack output`：`.git/objects/pack/` 下有
  `tmp_pack_*` 残留（来自被中断的抓取），删掉重跑即可。
- `detected dubious ownership in repository`：用**系统** git 操作 `E:/AzurPilot`
  时会报（该目录属 `BUILTIN/Administrators`，当前用户是 `18434`），需要
  `-c safe.directory=*`。最终方案用的是 `.venv` 的 git + 系统 Git 的运行时目录，
  用不到它，但作为回退方案验证过可行。

**排查方法上的两个注意点**：

1. 模拟启动器环境时，**不要**在父进程里 `os.environ.clear()`——那样会连沙箱
   注入的标记变量一起清掉，导致后续子进程创建被拒（表现为任何命令都 `rc=1`
   且无输出）。正确做法是父进程环境不动，只把**子进程**的 `env` 换成最小集。
2. 崩溃时可能在 `.git/objects/pack/` 留下 `tmp_pack_*`，验证完顺手检查一次。
