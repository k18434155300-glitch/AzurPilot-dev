"""任务优先级调整工具。

用于把「长耗时 / 不手动停止就一直运行」的任务（如智能调度 Plus、三油低耗）
下调到优先级表末尾，使它们只在其他任务都处理完时空闲运行，
从而让出执行权给需要及时处理的短任务。

优先级文本在解析后是一维线性序列，工具会先展平、调整顺序、再重新格式化。
``>`` 只是视觉分隔符，重新排版不影响语义。

用法::

    # 查看当前顺序（含名次）
    python tools/priority_adjust.py --config E:/AzurPilot/config/ap.json --show

    # 预览调整结果，不写入
    python tools/priority_adjust.py --config E:/AzurPilot/config/ap.json \
        --move-to-end OpsiScheduling ThreeOilLowCost --dry-run

    # 实际写入（自动备份）
    python tools/priority_adjust.py --config E:/AzurPilot/config/ap.json \
        --move-to-end OpsiScheduling ThreeOilLowCost
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime


GROUP_PATH = ('General', 'YukikazeTaskManager')
KEY = 'TaskPriorityAdjustment'
SEPARATOR = '\n> '


def parse_priority(value):
    """展平为任务名列表，与 module/config/task_priority.py 的行为一致。"""
    if not value:
        return []
    text = str(value)
    text = re.sub(r"[＞﹥›˃ᐳ❯]", ">", text)
    tasks, seen = [], set()
    for raw_line in text.splitlines():
        line = raw_line.split('#', 1)[0].strip()
        if not line:
            continue
        for raw_task in line.split('>'):
            task = raw_task.strip()
            if task and task not in seen:
                seen.add(task)
                tasks.append(task)
    return tasks


def format_priority(tasks):
    return SEPARATOR.join(t for t in tasks if t)


def read_config(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def get_group(data, create=False):
    node = data
    for key in GROUP_PATH:
        if not isinstance(node, dict) or key not in node:
            if not create:
                return None
            node[key] = {}
        node = node[key]
    return node


def unwrap(value):
    """ap.json 中该字段可能是字符串，也可能是 {'value': ...}。"""
    if isinstance(value, dict):
        return value.get('value')
    return value


def move_to_end(tasks, names):
    """把指定任务按给定顺序移到末尾，返回 (新列表, 实际移动的任务)。"""
    moving = [n for n in names if n in tasks]
    rest = [t for t in tasks if t not in moving]
    return rest + moving, moving


def show(tasks, highlight=(), tail=None, title=None):
    """显示任务列表及其真实名次；tail 指定只显示末尾若干项。"""
    if title:
        print(title)
    start = 0
    if tail and len(tasks) > tail:
        start = len(tasks) - tail
        print('  ...')
    for index, name in enumerate(tasks[start:], start=start + 1):
        mark = ' ←' if name in highlight else ''
        print(f'  {index:>3}  {name}{mark}')


def main(argv=None):
    parser = argparse.ArgumentParser(description='任务优先级调整')
    parser.add_argument('--config', required=True, help='config/ap.json 路径')
    parser.add_argument('--show', action='store_true', help='显示当前顺序')
    parser.add_argument('--move-to-end', nargs='+', default=[], metavar='TASK',
                        help='将指定任务移到末尾（按参数顺序排列）')
    parser.add_argument('--dry-run', action='store_true', help='仅预览，不写入')
    parser.add_argument('--no-backup', action='store_true', help='写入前不备份')
    args = parser.parse_args(argv)

    if not os.path.isfile(args.config):
        raise SystemExit(f'配置文件不存在: {args.config}')

    data = read_config(args.config)
    group = get_group(data)
    if group is None or KEY not in group:
        raise SystemExit(f'未找到 {"/".join(GROUP_PATH)}.{KEY}')

    tasks = parse_priority(unwrap(group.get(KEY)))
    if not tasks:
        raise SystemExit('优先级表为空，无法调整')

    if not args.move_to_end:
        show(tasks)
        return 0

    unknown = [n for n in args.move_to_end if n not in tasks]
    for name in unknown:
        print(f'警告: 任务 `{name}` 不在优先级表中，已忽略')

    new_tasks, moving = move_to_end(tasks, args.move_to_end)
    if not moving:
        raise SystemExit('没有需要移动的任务')

    print(f'调整前（共 {len(tasks)} 项）：')
    show(tasks, tail=8)
    print('  ↓')
    print(f'调整后（共 {len(new_tasks)} 项）：')
    show(new_tasks, tail=8, highlight=moving)

    if args.dry_run:
        print('\n[dry-run] 未写入')
        return 0

    if not args.no_backup:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        dest = f'{args.config}.priority-{stamp}.bak'
        shutil.copy2(args.config, dest)
        print(f'\n已备份原配置: {dest}')

    if isinstance(group.get(KEY), dict):
        group[KEY]['value'] = format_priority(new_tasks)
    else:
        group[KEY] = format_priority(new_tasks)

    tmp = f'{args.config}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, args.config)
    print(f'已写入: {args.config}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
