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

覆盖六个场景：默认关闭不打扰、高优先级抢占、低优先级不打断、
白名单收敛范围、抢占后 NextRun 正确回置、正常结束不篡改 NextRun。

## 六、风险与回滚

- 中断点落在 UI 切换或战斗过程中，可能导致界面状态残留。
  ALAS 任务入口普遍具备「先回主界面」的自愈逻辑，且后续 `Restart` 可兜底，
  但首次启用建议先在少量任务上验证。
- 全部改动受单一总开关控制，置 false 后行为完全等同改动前。
- 回滚：本仓库基线提交为 `85d0170`，`git revert` 或 `git reset --hard 85d0170` 即可。
