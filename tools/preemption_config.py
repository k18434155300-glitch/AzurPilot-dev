"""抢占式调度的配置开关工具。

直接在运行实例的 ``config/ap.json`` 中读写 ``General.YukikazeTaskManager``
下的三个字段。之所以不通过 UI 表单，是因为 ``argument.yaml`` / ``args.json``
属于配置生成链的一环，直接改动存在兼容风险。

用法::

    # 启用
    python tools/preemption_config.py --config E:/AzurPilot/config/ap.json \
        --enable --interval 15 --allowlist "OpsiScheduling, Commission"

    # 关闭（保留其余字段）
    python tools/preemption_config.py --config E:/AzurPilot/config/ap.json --disable

    # 仅查看当前状态
    python tools/preemption_config.py --config E:/AzurPilot/config/ap.json --show
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime


GROUP = ('General', 'YukikazeTaskManager')

KEY_ENABLE = 'TaskPreemptionEnabled'
KEY_INTERVAL = 'TaskPreemptionCheckInterval'
KEY_ALLOWLIST = 'TaskPreemptionAllowlist'


def load(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save(path, data, backup=True):
    if backup:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        dest = f'{path}.preemption-{stamp}.bak'
        shutil.copy2(path, dest)
        print(f'已备份原配置: {dest}')
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    print(f'已写入: {path}')


def group_of(data, create=True):
    """定位 General.YukikazeTaskManager，必要时创建中间层。"""
    node = data
    for key in GROUP:
        if not isinstance(node, dict):
            raise SystemExit(f'配置结构异常：{key} 不是对象')
        if key not in node or not isinstance(node.get(key), dict):
            if not create:
                return None
            node[key] = {}
        node = node[key]
    return node


def show(group):
    print('当前抢占配置：')
    for key, default in (
        (KEY_ENABLE, False),
        (KEY_INTERVAL, 5),
        (KEY_ALLOWLIST, '(不限)'),
    ):
        value = group.get(key, default)
        if isinstance(value, dict):
            value = value.get('value', default)
        print(f'  {key}: {value}')


def main(argv=None):
    parser = argparse.ArgumentParser(description='抢占式调度开关')
    parser.add_argument('--config', required=True, help='config/ap.json 路径')
    parser.add_argument('--enable', action='store_true', help='启用抢占')
    parser.add_argument('--disable', action='store_true', help='关闭抢占')
    parser.add_argument('--show', action='store_true', help='仅显示当前配置')
    parser.add_argument('--interval', type=int, default=60,
                        help='检查间隔秒数（默认 60。大世界自动搜索已有每场战斗约 '
                             '20–30 秒的检查点且会优雅中断，故本心跳定位为兜底，'
                             '间隔应大于官方周期）')
    parser.add_argument('--allowlist', default='', help='允许被抢占的任务，逗号分隔')
    parser.add_argument('--no-backup', action='store_true', help='写入前不备份')
    args = parser.parse_args(argv)

    if not os.path.isfile(args.config):
        raise SystemExit(f'配置文件不存在: {args.config}')
    if args.enable and args.disable:
        raise SystemExit('--enable 与 --disable 不能同时使用')

    data = load(args.config)
    group = group_of(data)

    if args.show or not (args.enable or args.disable):
        show(group)
        return 0

    if args.enable:
        group[KEY_ENABLE] = True
        group[KEY_INTERVAL] = max(1, args.interval)
        if args.allowlist:
            group[KEY_ALLOWLIST] = args.allowlist
        print('已启用抢占')
    else:
        group[KEY_ENABLE] = False
        print('已关闭抢占')

    save(args.config, data, backup=not args.no_backup)
    show(group)
    return 0


if __name__ == '__main__':
    sys.exit(main())
