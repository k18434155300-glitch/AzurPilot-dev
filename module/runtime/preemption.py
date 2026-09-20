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
        # task_stop() 的收尾与 ensure_auto_search_exit() 内部都会截图，
        # 会再次进入本钩子，用重入标志切断递归
        self._in_check = False
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

    def check(self):
        """心跳入口。命中抢占条件时抛出 TaskEnd。

        判定**完全交给** ``config.check_task_switch()``：
        ``task_switched()`` 会重新载入配置并按优先级重算队列，只有当胜出的
        任务不再是当前任务时才判定为切换。这样做的好处是：

        * 复用官方实现，不会遗漏 hoarding / error 队列 / ServerUpdate 等边界条件；
        * ``task_switched()`` 用 ``Function.__eq__`` 比对「命令 + NextRun」，
          天然覆盖了「低优先级到期任务挤占高优先级」的判定，无需自行比较；
        * 中断走 ``task_stop()`` —— 它会先 ``async_executor.flush(timeout=2.0)``
          等待异步任务收尾，再抛 TaskEnd，而不是粗暴硬切。
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
        if self._in_check:
            # 收尾流程（async_executor.flush / 退出战斗）内部截图会再次进入本钩子
            return

        from module.config.config import TaskEnd

        self._in_check = True
        try:
            self.config.check_task_switch(message='preempted')
        except TaskEnd:
            # 记录抢占事实后继续抛出，由 alas.run() 的 except TaskEnd 收口
            self.triggered = True
            raise
        finally:
            self._in_check = False

    # ------------------------------------------------------------------ 善后

    def ensure_requeued(self):
        """确保被抢占的任务回到待运行队列。

        ``TaskEnd`` 会让任务跳过末尾的 ``task_delay()``，因此 NextRun 通常
        仍是到期状态，任务自然留在 pending 队列等待下次被选中，此时无需干预。

        但若任务在执行途中已经推迟过自己的 NextRun（部分刷图类任务会周期性
        更新），中断就等于吞掉这一次执行。这种情况下才回置 NextRun，
        保证「放回排队列表」的语义成立。

        Returns:
            bool: 是否执行了回置。
        """
        if not self.triggered or not self.current_task:
            return False
        task = self.current_task
        try:
            raw = self.config.cross_get(keys=[task, 'Scheduler', 'NextRun'], default=None)
        except Exception:
            return False

        stamp = str(raw or '').replace('T', ' ').strip()
        moment = None
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
            try:
                moment = datetime.strptime(stamp, fmt)
                break
            except ValueError:
                continue
        if moment is None or moment <= datetime.now():
            # 仍处到期状态，已自然回到队列
            return False

        now_stamp = datetime.now().replace(microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
        self.config.modified[f'{task}.Scheduler.NextRun'] = now_stamp
        try:
            self.config.update()
        except Exception as e:
            from module.logger import logger

            logger.warning(f'[抢占] 回置 `{task}` 的 NextRun 失败: {e}')
            return False
        return True
