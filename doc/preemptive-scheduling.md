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

- 高频调用 → 必须节流（默认 15 秒一次，可配置）
- 可能在不理想的瞬间中断 → 提供**允许抢占的任务白名单**，且开关默认关闭

### 2.2 抢占判定

任务开始执行后，`Scheduler.NextRun` 已被推迟到下一周期，因此运行中的任务
**不在 pending 队列内**，`pending` 里全是其他候选任务。

判定条件：

```
存在 pending 任务 P，使得 priority(P) < priority(当前任务)   # 下标越小优先级越高
```

命中即触发抢占。

### 2.3 被抢占任务的处置

`except TaskEnd → return True` 会让主循环认为任务成功，若不干预，
该任务将被推迟整个成功间隔，等于被吞掉一次执行。

因此必须在检测到抢占后**回置 NextRun**：

```python
self.config.modified[f'{task}.Scheduler.NextRun'] = current_time().replace(microsecond=0)
```

使其重新进入 pending，待高优任务执行完毕后按优先级再次被选中。

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
    --interval 15 --allowlist "OpsiScheduling, Commission"

python tools/preemption_config.py --config E:/AzurPilot/config/ap.json --disable
```

**建议的上线顺序**：先只放行少量幂等任务（如 `Commission`、`Reward`），
观察到日志出现 `[抢占]` 记录且被中断任务能正常回位后，再移除白名单放开全部。

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
