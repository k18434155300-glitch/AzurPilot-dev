"""抢占控制器的离线验证。

不依赖项目第三方依赖，用 stub 替换 ``module.logger`` / ``module.config.config``，
并通过文件路径直接加载 ``task_priority`` 以绕开包的 ``__init__``。

运行::

    python tests/test_preemption_offline.py
"""

import importlib.util
import os
import sys
import types


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TaskEnd(Exception):
    """替代 module.config.config.TaskEnd。"""


class PendingFunc:
    def __init__(self, command):
        self.command = command


class FakeConfig:
    """最小可用的配置替身。"""

    def __init__(self, data, pending=()):
        self.data = data
        self.pending_task = [PendingFunc(c) for c in pending]
        self.modified = {}
        self.updated = 0

    def get_next_task(self):
        # 真实实现会重算 pending_task；这里沿用预置值即可
        return None

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


def make_config(pending, enabled=True, allowlist='', interval=15):
    return FakeConfig(
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
        pending=pending,
    )


def test_disabled_by_default():
    """总开关关闭时绝不打扰任务执行。"""
    cfg = make_config(['OpsiScheduling'], enabled=False)
    ctl = PreemptionController(cfg)
    ctl.bind('Commission')
    ctl.check()  # 不抛异常即通过
    assert cfg.modified == {}


def test_higher_priority_preempts():
    """pending 中存在更高优先级任务 → 中断当前任务。"""
    cfg = make_config(['OpsiScheduling'])  # OpsiScheduling(1) 高于 Commission(2)
    ctl = PreemptionController(cfg)
    ctl.bind('Commission')
    try:
        ctl.check()
        raise AssertionError('应当触发抢占')
    except TaskEnd:
        pass
    assert ctl.triggered is True
    assert ctl.preempted_by == 'OpsiScheduling'


def test_lower_priority_keeps_running():
    """pending 中的任务优先级更低 → 不打断。"""
    cfg = make_config(['Research'])  # Research(3) 低于 OpsiScheduling(1)
    ctl = PreemptionController(cfg)
    ctl.bind('OpsiScheduling')
    ctl.check()
    assert ctl.triggered is False


def test_allowlist_narrows_scope():
    """白名单未包含当前任务时，即便有高优任务也不抢占。"""
    cfg = make_config(['OpsiScheduling'], allowlist='Dorm, Research')
    ctl = PreemptionController(cfg)
    ctl.bind('Commission')
    ctl.check()
    assert ctl.triggered is False


def test_reschedule_puts_task_back():
    """被抢占的任务应回写 NextRun，而不是被推迟到下一周期。"""
    cfg = make_config(['OpsiScheduling'])
    ctl = PreemptionController(cfg)
    ctl.bind('Commission')
    try:
        ctl.check()
    except TaskEnd:
        pass
    assert ctl.reschedule_current() is True
    stamp = cfg.modified.get('Commission.Scheduler.NextRun')
    assert stamp, 'NextRun 未被回置'
    assert cfg.updated == 1, '未触发配置写回'


def test_no_reschedule_without_trigger():
    """正常结束时不应篡改 NextRun。"""
    cfg = make_config(['Research'])
    ctl = PreemptionController(cfg)
    ctl.bind('OpsiScheduling')
    ctl.check()
    assert ctl.reschedule_current() is False
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
    print('=' * 40)
    sys.exit(_run_all())
