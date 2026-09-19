"""抢占式任务调度控制器。

解决的问题
----------
当前调度器已经支持「按优先级排序待运行队列」和「等待期切换」，
但一旦任务开始执行，主循环 ``alas.py:2219`` 的 ``self.run(...)`` 是同步阻塞调用，
外部无法介入。因此低优先级任务在执行期间即使让位给高优先级任务，也只能等它跑完。

实现方式
--------
``device.screenshot()`` 是所有任务循环必然反复经过的心跳点，在此挂载检查即可
覆盖全部任务，无需逐个任务改造。命中抢占条件时抛出 :class:`TaskEnd`，
由 ``alas.py:964`` 捕获并按「成功」处理，随后主循环把任务放回待运行队列。

安全性
------
* 总开关默认关闭，未启用时 :meth:`PreemptionController.check` 是廉价的空操作。
* 判定仅在节流间隔到达时进行，默认 15 秒一次，不增加每帧开销。
* 可通过白名单限定只允许特定任务被抢占。

配置（位于 ``General.YukikazeTaskManager``，全部可选）
------------------------------------------------------
``TaskPreemptionEnabled``        bool，总开关，缺省 false
``TaskPreemptionCheckInterval``  int，检查间隔秒数，缺省 15
``TaskPreemptionAllowlist``      str，允许被抢占的任务，逗号分隔；空表示不限
"""

from datetime import datetime


DEFAULT_CHECK_INTERVAL = 15

_GROUP = ('General', 'YukikazeTaskManager')


def _camp(value):
    """把配置值规整为驼峰任务名，兼容 {'value': x} 形式的参数容器。"""
    if isinstance(value, dict):
        value = value.get('value')
    if not isinstance(value, str):
        return ''
    return value.split('.')[0].replace('-', '').replace('_', '').strip()


class PreemptionController:
    """运行期抢占判定器。每个 Alas 实例持有一个，随任务切换重置。"""

    def __init__(self, config):
        self.config = config
        self.current_task = ''
        self.enabled = False
        self.interval = DEFAULT_CHECK_INTERVAL
        self.allowlist = set()
        self._last_check_monotonic = 0.0
        self.triggered = False
        self.preempted_by = ''
        self._load_settings()

    # ------------------------------------------------------------------ 配置

    def _setting(self, key, default):
        node = getattr(self.config, 'data', None) or {}
        for step in _GROUP:
            if not isinstance(node, dict):
                return default
            node = node.get(step)
        if not isinstance(node, dict) or key not in node:
            return default
        value = node[key]
        if isinstance(value, dict):
            value = value.get('value', value.get('Value'))
        return value if value is not None else default

    def _load_settings(self):
        self.enabled = bool(self._setting('TaskPreemptionEnabled', False))
        try:
            self.interval = max(1, int(self._setting('TaskPreemptionCheckInterval', DEFAULT_CHECK_INTERVAL)))
        except (TypeError, ValueError):
            self.interval = DEFAULT_CHECK_INTERVAL
        raw = self._setting('TaskPreemptionAllowlist', '') or ''
        if isinstance(raw, (list, tuple)):
            names = [_camp(v) for v in raw]
        else:
            names = [_camp(v) for v in str(raw).replace('\n', ',').split(',')]
        self.allowlist = {n for n in names if n}

    # ------------------------------------------------------------------ 生命周期

    def bind(self, task):
        """任务开始前调用，记录上下文并重置上一轮的抢占状态。"""
        self.current_task = _camp(task)
        self.triggered = False
        self.preempted_by = ''
        self._last_check_monotonic = 0.0
        self._load_settings()

    def release(self):
        """任务结束后调用。"""
        self.current_task = ''
        self.triggered = False
        self.preempted_by = ''

    # ------------------------------------------------------------------ 判定

    def _order(self):
        """返回 {任务名: 优先级下标}，下标越小优先级越高。"""
        from module.config.task_priority import parse_task_priority

        priority = parse_task_priority(
            self._setting('TaskPriorityAdjustment', '')
        )
        return {name: index for index, name in enumerate(priority)}

    def check(self):
        """心跳入口。命中抢占条件时抛出 TaskEnd。

        未启用、间隔未到、或无更高优先级任务时静默返回。
        """
        if not self.enabled or not self.current_task:
            return

        import time

        now_mono = time.monotonic()
        if self._last_check_monotonic and now_mono - self._last_check_monotonic < self.interval:
            return
        self._last_check_monotonic = now_mono

        if self.allowlist and self.current_task not in self.allowlist:
            return

        order = self._order()
        if not order:
            return

        candidates = self._pending_tasks()
        if not candidates:
            return

        current_index = order.get(self.current_task, len(order))
        for name in candidates:
            rank = order.get(name, len(order))
            if rank < current_index:
                self._fire(name)
        return

    def _pending_tasks(self):
        """重算队列并返回当前处于待运行(pending)状态的任务名。"""
        try:
            self.config.get_next_task()
        except Exception:  # 计算失败时保守地不抢占
            return []
        pending = getattr(self.config, 'pending_task', None) or []
        names = []
        for func in pending:
            name = _camp(getattr(func, 'command', ''))
            if name and name != self.current_task:
                names.append(name)
        return names

    def _fire(self, name):
        from module.config.config import TaskEnd
        from module.logger import logger

        self.triggered = True
        self.preempted_by = name
        logger.info(f'[抢占] 高优先级任务 `{name}` 已到期，中断当前任务 `{self.current_task}`')
        logger.info(f'[抢占] `{self.current_task}` 将被放回待运行队列')
        raise TaskEnd

    # ------------------------------------------------------------------ 善后

    def reschedule_current(self):
        """把被抢占的任务放回待运行队列。

        若不处理，``except TaskEnd → return True`` 会让调度器按成功处理，
        任务被推迟整个成功间隔，等于吞掉一次执行。
        """
        if not self.triggered or not self.current_task:
            return False
        task = self.current_task
        stamp = datetime.now().replace(microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
        self.config.modified[f'{task}.Scheduler.NextRun'] = stamp
        try:
            self.config.update()
        except Exception:  # 写入失败不应阻断主循环
            from module.logger import logger

            logger.warning(f'[抢占] 回置 `{task}` 的 NextRun 失败')
        return True
