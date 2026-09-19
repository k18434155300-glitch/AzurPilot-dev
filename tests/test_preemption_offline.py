"""抢占控制器的离线验证。

不依赖项目第三方依赖，用 stub 替换 ``module.logger`` / ``module.config.config``，
并通过文件路径直接加载 ``task_priority`` 以绕开包的 ``__init__``。

设计说明
--------
抢占判定本身由 ``config.check_task_switch()`` 负责（它会载入最新配置并按优先级
重算队列），本控制器的职责仅限「在何时、对哪些任务允许做这次检查」。
因此这里用 ``FakeConfig`` 模拟官方判定结果，重点验证节流、白名单与善后语义。

运行::

    python tests/test_preemption_offline.py
"""

import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STAMP = '%Y-%m-%d %H:%M:%S'


class TaskEnd(Exception):
    """替代 module.config.config.TaskEnd。"""


class FakeConfig:
    """模拟 AzurLaneConfig 中与抢占判定相关的行为。"""

    def __init__(self, data, next_run=None):
        self.data = data
        self.modified = {}
        self.updated = 0
        self.checks = 0
        self.stopped = []
        self.current_command = None
        self.switch_to = None          # 模拟 get_next() 胜出的任务
        self.next_run_map = next_run or {}

    # 以下模拟 PreemptionController 会用到的官方接口
    def check_task_switch(self, message=''):
        self.checks += 1
        if self.switch_to and self.switch_to != self.current_command:
            self.stopped.append(message)
            raise TaskEnd(message)
        return False

    def cross_get(self, keys=None, default=None):
        if isinstance(keys, list) and len(keys) == 3 and keys[2] == 'NextRun':
            return self.next_run_map.get(keys[0])
        return default

    def update(self):
        self.updated += 1


def _install_stubs():
    """把 preemption 模块运行所需的符号塞进 sys.modules。"""
    if 'module' not in sys.modules:
        sys.modules['module'] = types.ModuleType('module')

    logger_mod = types.ModuleType('module.logger')

    class _Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

    logger_mod.logger = _Logger()
    sys.modules['module.logger'] = logger_mod

    config_mod = types.ModuleType('module.config.config')
    config_mod.TaskEnd = TaskEnd
    sys.modules['module.config.config'] = config_mod
    sys.modules['module.config'] = types.ModuleType('module.config')

    # task_priority 顶层会 import deep_*，但 parse_task_priority 本身用不到它们
    deep_mod = types.ModuleType('module.config.deep')

    def deep_get(data, keys=None, default=None):
        return default

    def deep_iter(data, depth=1):
        return iter(())

    deep_mod.deep_get = deep_get
    deep_mod.deep_iter = deep_iter
    sys.modules['module.config.deep'] = deep_mod

    # 从文件直接加载 task_priority，避免触发 module.config.__init__
    path = os.path.join(ROOT, 'module', 'config', 'task_priority.py')
    spec = importlib.util.spec_from_file_location('module.config.task_priority', path)
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)
    sys.modules['module.config.task_priority'] = tp


def _load_preemption():
    path = os.path.join(ROOT, 'module', 'runtime', 'preemption.py')
    spec = importlib.util.spec_from_file_location('module.runtime.preemption', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['module.runtime.preemption'] = mod
    spec.loader.exec_module(mod)
    return mod


_install_stubs()
preemption = _load_preemption()
PreemptionController = preemption.PreemptionController

PRIORITY = "Restart\n> OpsiScheduling\n> Commission\n> Research\n> Dorm"


def make_config(pending=(), enabled=True, allowlist='', interval=15, next_run=None):
    cfg = FakeConfig(
        {
            'General': {
                'YukikazeTaskManager': {
                    'TaskPriorityAdjustment': PRIORITY,
                    'TaskPreemptionEnabled': enabled,
                    'TaskPreemptionCheckInterval': interval,
                    'TaskPreemptionAllowlist': allowlist,
                }
            }
        },
        next_run=next_run,
    )
    cfg.switch_to = pending[0] if pending else None
    return cfg


def _bound(cfg, current):
    ctl = PreemptionController(cfg)
    ctl.bind(current)
    cfg.current_command = current
    return ctl


def test_disabled_by_default():
    """总开关关闭时绝不打扰任务执行。"""
    cfg = make_config(['OpsiScheduling'], enabled=False)
    ctl = _bound(cfg, 'Commission')
    ctl.check()  # 不抛异常即通过
    assert cfg.checks == 0, '未启用时不应调用官方判定'
    assert cfg.modified == {}


def test_switch_triggered_by_official_path():
    """官方判定要求切换时，控制器应如实抛出 TaskEnd 并记录。"""
    cfg = make_config(['OpsiScheduling'])
    ctl = _bound(cfg, 'Commission')
    try:
        ctl.check()
        raise AssertionError('应当触发抢占')
    except TaskEnd:
        pass
    assert ctl.triggered is True
    assert cfg.stopped == ['preempted']


def test_no_switch_keeps_running():
    """官方判定仍是当前任务 → 不打断。"""
    cfg = make_config(['Commission'])
    ctl = _bound(cfg, 'Commission')
    ctl.check()
    assert ctl.triggered is False


def test_allowlist_narrows_scope():
    """白名单未包含当前任务时，即便要求切换也不触发。"""
    cfg = make_config(['OpsiScheduling'], allowlist='Dorm, Research')
    ctl = _bound(cfg, 'Commission')
    ctl.check()
    assert cfg.checks == 0, '白名单未命中时不应调用官方判定'
    assert ctl.triggered is False


def test_no_requeue_when_still_due():
    """NextRun 仍处到期状态时无需回置，任务已自然在队列中。"""
    past = (datetime.now() - timedelta(minutes=5)).strftime(STAMP)
    cfg = make_config(['OpsiScheduling'], next_run={'Commission': past})
    ctl = _bound(cfg, 'Commission')
    try:
        ctl.check()
    except TaskEnd:
        pass
    assert ctl.ensure_requeued() is False
    assert cfg.modified == {}, '不应篡改仍有效的到期时间'


def test_requeue_when_next_run_delayed():
    """任务中途已推迟 NextRun → 必须回置，否则本次执行会被吞掉。"""
    future = (datetime.now() + timedelta(hours=3)).strftime(STAMP)
    cfg = make_config(['OpsiScheduling'], next_run={'Commission': future})
    ctl = _bound(cfg, 'Commission')
    try:
        ctl.check()
    except TaskEnd:
        pass
    assert ctl.ensure_requeued() is True
    written = cfg.modified.get('Commission.Scheduler.NextRun')
    assert written, 'NextRun 未被回置'
    assert datetime.strptime(written, STAMP) <= datetime.now()
    assert cfg.updated == 1, '未触发配置写回'


def test_no_requeue_without_trigger():
    """正常结束时不应触碰 NextRun。"""
    future = (datetime.now() + timedelta(hours=3)).strftime(STAMP)
    cfg = make_config(['Commission'], next_run={'Commission': future})
    ctl = _bound(cfg, 'Commission')
    ctl.check()
    assert ctl.ensure_requeued() is False
    assert cfg.modified == {}


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failures = []
    for fn in tests:
        try:
            fn()
            print(f'  PASS  {fn.__name__}')
        except AssertionError as e:
            failures.append((fn.__name__, str(e) or '断言失败'))
            print(f'  FAIL  {fn.__name__}: {e}')
    print()
    if failures:
        print(f'{len(failures)}/{len(tests)} 项失败')
        return 1
    print(f'全部 {len(tests)} 项通过')
    return 0


if __name__ == '__main__':
    print('抢占控制器离线验证')
    print('=' * 44)
    sys.exit(_run_all())
