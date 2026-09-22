"""
低耗轮换任务。

与「三油低耗」（ThreeOilLowCost）的区别：

===========  ==================  ===========================
             三油低耗            低耗轮换
===========  ==================  ===========================
旗舰         1 级普通航母        1 级普通航母
先锋         百级普通驱逐        **1 级**普通驱逐
出击         每轮普通出击        **连续作战**连打一批
心情         需要管理、靠轮换    **不检测**
退役         被动（船坞满弹窗）  **主动**（建造 → 退役）
===========  ==================  ===========================

整队都是 1 级掉落船、一批打完就整套换新，所以既不存在心情耗尽，
也不需要在退役时抢救候选船。

循环流程：

    编队（1 级航母 + 1 级驱逐）      <- 任务开始时做一次
        |
    进图 -> 舰队选择界面点「连续作战」-> 连打一批
        |
    替换编队                        <- 与上面的「编队」是同一个操作
        |
    退役                            <- 主动进退役界面
        |
    （回到「打一批」）

顺序要点：**先替换编队，再退役**。换下来的旧船只有被替换之后才回到船坞，
一键退役才选得中它们；反过来先退役的话旧船还在舰队里，会一直堆在船坞。

结束判定：一批打完后会弹出「合计获得奖励」结算框，见
`LowCostCampaignBase.handle_total_rewards()`。
"""

from module.base.decorator import cached_property
from module.base.timer import Timer
from module.campaign.assets import BUILD_RETIRE, TOTAL_REWARDS, TOTAL_REWARDS_QUIT
from module.campaign.gems_farming import GemsCampaignOverride, GemsEmotion, GemsFarming
from module.campaign.run import CampaignRun
from module.exception import CampaignEnd
from module.handler.continuous_battle import ContinuousBattle
from module.logger import logger
from module.retire.assets import IN_RETIREMENT_CHECK
from module.ui.page import page_build


class LowCostCampaignBase(GemsCampaignOverride, ContinuousBattle):
    """低耗轮换的战役覆写。

    负责两件事：

    - 认得「合计获得奖励」结算框——一批结束的标志
    - 在舰队准备界面开启「连续作战」并直接出击
    """

    def handle_continuous_battle_setting(self):
        """在舰队准备界面开启连续作战并直接出击。

        次数取自 `LowCostRotation_SortiesCount`（默认 10，即游戏上限）。

        ⚠️ 这个方法必须挂在**战役对象**上：调用它的是
        `MapOperation.enter_map()`，那里 `self` 是 Campaign 而非 Task。
        若写在任务类里，永远命中不到，会静默退回 `MapOperation` 的默认实现
        （返回 False）→ 直接点「立刻前往」，连续作战形同不存在。

        Returns:
            bool: True 表示已在弹窗内出击，调用方跳过点「立刻前往」。
        """
        return self.continuous_battle_start(
            count=self.config.LowCostRotation_SortiesCount)

    def handle_total_rewards(self):
        """处理「合计获得奖励」结算框。

        连续作战一批打完后弹出它，出现即表示本批结束。奖励是逐项加载的，
        点「离开」需要多按几次才能真正退出、回到出击界面；这里每次进循环
        点一下，由上层循环驱动重复点击。

        Returns:
            bool: True 表示点击了离开，需要重新截图。
        """
        if self.appear(TOTAL_REWARDS, offset=(20, 20), interval=2):
            logger.info('[低耗轮换] 出现合计奖励结算框，本批结束')
            # 置位后由任务侧的 triggered_stop_condition() 消费，
            # 让本轮结束、交回 run() 换编队
            self.config.LOW_COST_BATCH_FINISHED = True
            self.device.click(TOTAL_REWARDS_QUIT)
            self.interval_reset(TOTAL_REWARDS)
            return True
        return False

    def handle_battle_status(self, drop=None):
        """处理战斗结算画面，处理完顺手清掉点击记录。

        **这里的结算必须点**。曾按「中间结算游戏会自己过」的假设改成不处理，
        实测立刻失败：`auto_search_combat_status()` 里的界面静止不动，
        `stuck_timer`（60 秒）超时抛 `GameStuckError`，任务被判
        「游戏状态无法推进」而重启游戏。也就是说连续作战中途的结算
        并不会自动略过，仍然需要脚本推进。

        但点了会撞上另一个保护：点击频率检测只看**最近 15 次**点击、
        两个按钮各满 6 次即抛 `GameTooManyClickError`，而连续作战每场
        固定 3 次点击（结算 + 经验 ×2），约 5 场就误报。
        所以每处理完一次就清空记录，把连续作战的正常节奏与「脚本卡死」
        区分开——真正的卡死仍有 `stuck_record_check` 兜底。

        Args:
            drop: 掉落记录对象，透传给父类。

        Returns:
            bool: 父类的处理结果。
        """
        result = super().handle_battle_status(drop=drop)
        if result:
            self.device.click_record_clear()
        return result

    def handle_exp_info(self):
        """处理经验结算画面，处理完顺手清掉点击记录。理由同 `handle_battle_status()`。

        Returns:
            bool: 父类的处理结果。
        """
        result = super().handle_exp_info()
        if result:
            self.device.click_record_clear()
        return result

    def handle_retirement(self):
        """打一批的过程中弹出「船坞已满」：处理掉弹窗后直接停止任务。

        三油低耗在同一个检测点（handle_retirement）认出弹窗后退役完继续打；
        低耗轮换则直接收工——此时**战斗还在进行**，换编队（要出战斗）和
        退役（要回主界面）都做不了，硬做只会卡住。

        弹窗本身仍必须先由父类处理掉，否则弹窗挡着界面同样会卡死。

        Raises:
            TaskEnd: 由 `config.task_stop()` 抛出，终止当前任务。
        """
        dock_full = self.retirement_appear()
        result = super().handle_retirement()

        if dock_full:
            logger.info('[低耗轮换] 打一批过程中船坞已满，战斗未结束无法收尾，停止任务')
            self.config.task_stop()

        return result


class LowCostRotation(GemsFarming):
    """低耗轮换：1 级编队 + 连续作战，一批一换。

    连续作战的交互（点按钮、设次数、出击）由战役侧的
    `LowCostCampaignBase` 负责——它的调用方是 `MapOperation.enter_map()`，
    那里的 `self` 是 Campaign 对象而不是任务对象，所以不能写在本类里。
    """

    # 只覆写「是否连装备一起换」这两项：1 级船用默认装备即可，不切装备。
    # 「是否换船」仍由配置决定（`GemsFarming_ChangeVanguard/ChangeFlagship`，
    # 默认 ship_equip 含换船），所以本任务保留对 GemsFarming 参数组的引用，
    # 以便在界面上调整船型筛选（CommonCVFilter/CommonDDFilter）与等级范围。

    @property
    def change_vanguard_equip(self):
        """是否连装备一起换。1 级船用默认装备即可，不切。"""
        return False

    @property
    def change_flagship_equip(self):
        """是否连装备一起换。同上，不切。"""
        return False

    def load_campaign(self, name, folder='campaign_main'):
        """加载战役：注入结算框处理，并强制忽略心情。

        Args:
            name (str): 战役文件名。
            folder (str): 战役文件夹名。
        """
        # 先按 CampaignRun 的方式建出基础 campaign 对象
        CampaignRun.load_campaign(self, name, folder)

        class LowCostCampaign(LowCostCampaignBase, self.module.Campaign):

            @cached_property
            def emotion(self) -> GemsEmotion:
                return GemsEmotion(config=self.config)

        self.campaign = LowCostCampaign(device=self.campaign.device, config=self.campaign.config)
        # 1 级船每批都换新，不存在心情不足
        self.campaign.config.override(Emotion_Mode='ignore')

    def dock_filter_set(self, sort='level', index='all', faction='all',
                        rarity='all', extra='no_limit', wait_loading=True):
        """把取船时的「可突破」筛选换成「不限」。

        低耗轮换要的是 1 级船，而**1 级船不能限界突破**，于是但凡勾上
        `extra='can_limit_break'`，它们就会被整批排除，表现为「船坞里
        明明有船却一艘都找不到」。

        这个坑在 `GemsFarming` 里有两处（gems_farming.py）：

            # CV 侧 562 行：AllowHighFlagshipLevel 为真时才勾可突破
            extra = 'can_limit_break' if AllowHighFlagshipLevel else 'enhanceable'
            # DD 侧 664 行：无条件写死可突破
            extra = 'can_limit_break'

        驱逐侧那条是写死的，所以在低耗场景下**必然**找不到 1 级驱逐。
        这里统一改判为 `no_limit`；等级范围本来就由
        `ShipScanner(level=...)` 单独限定，不需要靠 `extra` 收窄。

        退役流程用的 `not_level_max`、强化流程用的 `enhanceable` 都不在
        替换范围内，行为不受影响。
        """
        if extra == 'can_limit_break':
            extra = 'no_limit'
        return super().dock_filter_set(sort=sort, index=index, faction=faction,
                                       rarity=rarity, extra=extra,
                                       wait_loading=wait_loading)

    def get_common_rarity_cv(self, lv=None, emotion=0):
        """取普通航母作旗舰，不关心心情。

        等级上限取自 `LowCostRotation_FlagshipLevelMax`（默认 1，即只挑
        1 级船）。船坞里没有 1 级航母时可以放宽这个值。

        这里临时把 `GemsFarming_CommonCV` 置为 `'custom'`，原因是
        `find_custom_candidates()` 里有个坑：

            if ship_type == 'cv' and self.config.GemsFarming_CommonCV != 'custom':
                filter_string = self.config.COMMON_CV_FILTER      # 内置的 4 种航母名单
            else:
                filter_string = self.config.GemsFarming_CommonCVFilter

        也就是说 **CV 侧只有在 `CommonCV` 选 `custom` 时，界面上配的
        `CommonCVFilter` 才会生效**；否则永远走内置的
        `bogue > ranger > langley > hermes`，想换的船根本搜不到。
        （DD 侧没有这个问题，始终读 `CommonDDFilter`。）

        另外可选船型受 `TEMPLATE_COMMON_CV` 限制——只有上述 4 种航母有模板，
        配上名单外的名字会 KeyError。都找不到时会回退到「任意普通航母」，
        那一步不依赖模板，所以只要船坞里有普通航母就能换上。

        Args:
            lv (int): 等级上限。为 None 时读配置。
            emotion (int): 传 0 表示不检查心情。
        """
        if lv is None:
            lv = self.config.LowCostRotation_FlagshipLevelMax

        # 两处临时覆盖：
        # 1. CommonCV 置 custom —— 否则界面上配的 CommonCVFilter 不生效
        # 2. 关掉 AllowHighFlagshipLevel —— 它会把等级写死成满级
        #    （CN 100 / 其它 70），让上面的等级上限彻底失效。
        #    那个开关是三油低耗用来刷百级航母的，与低耗目标冲突。
        origin_cv = self.config.GemsFarming_CommonCV
        origin_high = self.config.GemsFarming_AllowHighFlagshipLevel

        self.config.override(
            GemsFarming_CommonCV='custom',
            GemsFarming_AllowHighFlagshipLevel=False,
        )
        try:
            return super().get_common_rarity_cv(lv=lv, emotion=0)
        finally:
            self.config.override(
                GemsFarming_CommonCV=origin_cv,
                GemsFarming_AllowHighFlagshipLevel=origin_high,
            )

    def get_common_rarity_dd(self, emotion=0):
        """取普通驱逐作先锋，不关心心情。

        等级上限用本任务自己的 `LowCostRotation_VanguardLevelMax`（默认 1），
        **不用** `GemsFarming_VanguardLevelMin/Max`——后者默认 (1, 125) 会命中
        `GemsFarming.get_common_rarity_dd()` 里的回退逻辑：

            if min_level <= 1 and max_level >= 125:
                max_level = 100 (CN) / 70
                min_level = max_level

        结果反手去挑满级驱逐，与低耗目标完全相反。这里显式压到
        (1, VanguardLevelMax)，并且避开 125 这个触发值。

        Args:
            emotion (int): 传 0 表示不检查心情。
        """
        level_max = int(self.config.LowCostRotation_VanguardLevelMax)
        level_max = max(1, min(level_max, 124))   # 124 是上限，避开回退阈值 125

        origin_min = self.config.GemsFarming_VanguardLevelMin
        origin_max = self.config.GemsFarming_VanguardLevelMax
        self.config.override(GemsFarming_VanguardLevelMin=1,
                             GemsFarming_VanguardLevelMax=level_max)
        try:
            return super().get_common_rarity_dd(emotion=0)
        finally:
            self.config.override(GemsFarming_VanguardLevelMin=origin_min,
                                 GemsFarming_VanguardLevelMax=origin_max)

    def flagship_change_with_emotion(self, ship):
        """当前旗舰已符合要求时，跳过更换确认。

        `get_common_rarity_cv()` 会**优先返回「已经在当前编队里、且满足
        条件」的船**（源码注释原文：不需要更换当前舰船），此时候选的
        `fleet` 字段就等于 `fleet_to_attack`。基类拿到它之后仍会走一遍
        `_ship_change_confirm()`，等于对着同一艘船白点一下；这里直接返回。

        Args:
            ship (list[Ship]): 候选舰船。
        """
        if ship and all(s.fleet == self.fleet_to_attack for s in ship):
            logger.info('[低耗轮换] 当前旗舰已符合要求，跳过更换')
            return
        super().flagship_change_with_emotion(ship)

    def vanguard_change_with_emotion(self, ship):
        """当前先锋已符合要求时，跳过更换确认。理由同旗舰。

        Args:
            ship (list[Ship]): 候选舰船。
        """
        if ship and all(s.fleet == self.fleet_to_attack for s in ship):
            logger.info('[低耗轮换] 当前先锋已符合要求，跳过更换')
            return
        super().vanguard_change_with_emotion(ship)

    def triggered_stop_condition(self, oil_check=True):
        """一批打完结束本轮；其余交给标准的停止条件。

        与三油低耗同思路：先判断自己的触发条件，再交给 super() 处理
        石油、物资、运行次数、等级上限等通用条件——这样石油耗尽时才会
        正常推迟任务，而不是一直刷下去。
        区别是本任务没有 32 级与心情这两个条件，只有「一批是否打完」。

        停止条件与三油低耗用同一套配置项（StopCondition 组：石油上限、
        石油停止下限、物资上限、等级上限、获得新舰船、活动 PT、出击次数
        等），这里不再做增删。

        唯一区别是跳过 `GemsFarming` 那一层，直接调 `CampaignRun` 的版本：
        低耗轮换用 1 级船、每批换新，不需要 32 级与心情这两个触发条件。
        （「出击次数」在三油低耗里同样被 override 锁成 0 且界面隐藏，
        两侧行为一致，无需额外处理。）

        Args:
            oil_check (bool): 是否检查石油/物资等资源限制。

        Returns:
            bool: 是否结束本轮。
        """
        if self.config.LOW_COST_BATCH_FINISHED:
            logger.hr('[低耗轮换] 本批结束，准备换编队', level=1)
            return True

        return CampaignRun.triggered_stop_condition(self, oil_check=oil_check)

    def change_fleet(self) -> bool:
        """换整套 1 级编队：1 级普通航母（旗舰）+ 1 级普通驱逐（先锋）。

        任务开始时的初次编队与每批结束后的替换编队都是这个操作。

        Returns:
            bool: 是否成功换上新船。False 表示船坞里找不到符合条件的
                1 级船——多半是船型筛选（CommonCVFilter / CommonDDFilter）
                或前排等级范围配得太窄。调用方据此停止任务，不能拿旧编队
                继续打下一批。
        """
        logger.hr('[低耗轮换] 更换整套 1 级编队', level=1)
        self.hard_mode_override()

        vanguard_success = True
        if self.change_vanguard:
            vanguard_success = self.vanguard_change()

        flagship_success = True
        if self.change_flagship and vanguard_success:
            flagship_success = self.flagship_change()

        return bool(vanguard_success and flagship_success)

    def _change_fleet_safely(self) -> bool:
        """调用 `change_fleet()`，把异常也归入「换不上船」。

        `change_fleet()` 会走到 `vanguard_change()` / `flagship_change()`，
        那条链路（进出编队页、船坞筛选、模板匹配）可能抛各种异常。
        直接外抛会被顶层记成「未处理异常」并触发重启，还可能被反复重试；
        统一按「换不上船」处理，交由调用方走 `task_delay + task_stop`，
        留下一份可读的告警而不是一份堆栈。

        Returns:
            bool: 是否成功换上新船；异常时返回 False。
        """
        try:
            return self.change_fleet()
        except Exception as e:
            logger.error(f'[低耗轮换] 更换编队时发生异常，按「换不上船」处理：{e}')
            return False

    def retire_after_rotation(self):
        """主动退役：主界面 ->「建造」-> 点左下角「退役」-> 退役 -> 退出。

        退役分两步，顺序固定：

        1. `retire_handler()` 一键退役，清掉船坞里的杂鱼
           （其内部还会退役报废的旗舰，即打过、等级升上去的白皮航母）
        2. `retire_low_cost_vanguards()` 退役本轮换下来的先锋
           （等级 2~99 的白皮驱逐；1 级的留着下一批用）

        必须在 `change_fleet()` 之后调用：换下来的旧船这时才回到船坞，
        一键退役才能选中它们。

        关于「退役」入口的点击方式：**不使用模板匹配**。
        它是左侧栏的半透明标签，会透出建造界面的背景图，而背景随活动
        更替会变化，模板匹配的结果不稳定。建造界面的其他元素是固定的，
        所以这里先用 `page_build`（BUILD_CHECK）确认已进入建造界面，
        再直接点标签的固定坐标即可——界面布局不变，坐标就是可靠的。
        """
        logger.hr('[低耗轮换] 退役', level=1)
        self.ui_ensure(page_build)

        # 不做 appear(BUILD_RETIRE)：背景会变，模板不可靠；
        # 已确认处于建造界面，直接点标签位置
        self.device.click(BUILD_RETIRE)

        # 等待进入退役界面，超时则跳过本次退役
        timeout = Timer(5, count=5).start()
        while 1:
            self.device.screenshot()

            if self.appear(IN_RETIREMENT_CHECK, offset=(20, 20)):
                break
            if timeout.reached():
                logger.warning('[低耗轮换] 未进入退役界面，跳过本次退役')
                return

        # 先一键退役。其内部已包含「退役报废的旗舰」（打过、等级升上去的
        # 白皮航母），这一步负责清掉船坞里的杂鱼。
        self._retire_handler()

        # 再退役本轮换下来的先锋：1 级驱逐打完一批会升级，属于用完即弃的
        # 一次性船，不清掉会一直堆在船坞里。放在一键退役之后做。
        self.retire_low_cost_vanguards()

        self._retirement_quit()

    def run(self, name, folder='campaign_main', mode='normal', total=0):
        """
        一批一批地打：编队 -> 连续作战 -> 替换编队 -> 退役 -> 循环。

        Args:
            name (str): 战役文件名。
            folder (str): 战役文件夹名。
            mode (str): `normal` 或 `hard`。
            total (int): 总运行次数限制。
        """
        # 先把战役加载出来。初次编队会走到 hard_mode_override()，它要读
        # self.campaign.config.Campaign_Mode；而 self.campaign 平时是
        # CampaignRun.run() 内部才创建的，放在编队之前就是 None。
        name, folder = self.handle_stage_name(name, folder, mode=mode)
        self.config.override(Campaign_Name=name, Campaign_Event=folder)
        self.load_campaign(name, folder)

        # 连续作战全程「长时间没有任何点击」，会反复踩中设备层的卡死判定：
        #
        #   1. stuck_timer（60s 无点击）—— 一场战斗全程无点击，打满一分钟就误报。
        #      它只能靠点击重置（screenshot 不重置它）；本以为能兜底的
        #      `stuck_long_wait_list`（PAUSE / BATTLE_STATUS_S 等）实际是失效的：
        #      `detect_record_add()` 全项目没有任何调用点，detect_record 恒为空集，
        #      豁免分支永远进不去（device.py:87 / 400 / 471）。
        #   2. stuck_timer_long（195s）—— 到了之后会**绕过豁免直接报错**，是硬线。
        #   3. _stuck_image_timer（30s 画面不变）—— 战斗加载、结算画面会长时间静止。
        #   4. click_record（15 次窗，两按钮各 6 次）—— 每场固定 3 次点击，约 5 场就爆。
        #
        # 调度器对卡死类异常的处理是「重启游戏，重复则重启模拟器」，
        # 而这四处每一次触发都只是连续作战的正常节奏，纯属误报。
        # 所以整个任务期间直接**关掉这三套检测**——这批任务的界面节奏本来就是
        # 已知的，不需要通用保护兜底——退出时原样还原。
        #
        # 代价：游戏真卡死（崩溃、断网）时本任务不会自动重启恢复，需要人工介入。
        original_checks = (
            self.device.click_record_check,
            self.device.stuck_record_check,
            self.device._check_image_stuck,
        )
        self.device.disable_stuck_detection()                   # 关掉前两个
        self.device._check_image_stuck = lambda *a, **kw: None  # 关掉画面不变检测
        try:
            self._rotation_loop(name, folder, mode, total)
        finally:
            (self.device.click_record_check,
             self.device.stuck_record_check,
             self.device._check_image_stuck) = original_checks

    def _rotation_loop(self, name, folder, mode, total):
        """分批循环主体。

        单独抽成方法，是为了让 `run()` 能用 `try/finally` 包住卡死阈值的放宽，
        即使异常退出也能还原。

        Args:
            name (str): 战役文件名。
            folder (str): 战役文件夹名。
            mode (str): `normal` 或 `hard`。
            total (int): 总运行次数限制。
        """
        # 初次编队。找不到 1 级船就别开打——否则会拿不合要求的旧编队刷下去。
        if not self._change_fleet_safely():
            logger.critical(
                '[低耗轮换] 船坞里找不到可用的 1 级船，无法编队。'
                '请检查「紧急委托」组的船型筛选（CommonCVFilter / CommonDDFilter）'
                '与前排等级范围。')
            # 只 task_stop() 的话，调度器会因为 NextRun 已过期而立刻重跑，
            # 变成「失败 -> 重启 -> 再失败」的空转。先推迟再停。
            self.config.task_delay(minute=30)
            self.config.task_stop()

        while 1:
            # 每轮开始前复位标记，用来区分「一批打完」与「资源/次数条件退出」
            self.config.LOW_COST_BATCH_FINISHED = False

            # 「打一批 + 收尾」整段屏蔽任务切换（抢占）。
            #
            # 抢占的钩子挂在 `Device.screenshot()`，本来会在任意截图点生效；
            # 而战役侧不像大世界那样会设 `_disable_task_switch`，所以不加这段的话，
            # 抢占会打断在「一批刚打完、收尾还没做」的当口，留下半拉状态：
            # 打过的旧船继续占着编队、也没退役。
            #
            # 屏蔽后切换只会落在收尾之后，每次让路都留下干净状态。
            # 取舍：高优先级任务最多要等「一批 + 收尾」的时间。
            with self.config.temporary(_disable_task_switch=True):
                # 打一批：进图后在舰队选择界面点「连续作战」，
                # 由游戏自己连打，直到弹出「合计获得奖励」结算框
                try:
                    CampaignRun.run(self, name=name, folder=folder, total=total)
                except CampaignEnd:
                    # 一批打完（或撤退）属于正常结束
                    pass

                # 收尾：先替换编队、再退役（顺序不能反）。
                # 即便本轮是因为石油/物资/次数等条件提前退出的，也先把收尾做完——
                # 一批打完后旧船必须清掉，否则会一直堆在船坞里。
                #
                # 换不上船就停下：此时旧船还挂在编队里，退役也退不掉它，
                # 继续循环只会拿打过的船反复刷。
                if not self._change_fleet_safely():
                    logger.critical(
                        '[低耗轮换] 船坞里找不到可用的 1 级船，无法更换编队，停止任务。'
                        '请检查「紧急委托」组的船型筛选（CommonCVFilter / CommonDDFilter）'
                        '与前排等级范围。')
                    # 同上：先推迟，避免调度器立刻重跑造成空转
                    self.config.task_delay(minute=30)
                    self.config.task_stop()

                self.retire_after_rotation()

            # —— 收尾已完成，从这里起才响应抢占 ——
            if self.config.task_switched():
                self.campaign.ensure_auto_search_exit()
                self.config.task_stop()

            # 收尾做完后，再判断是不是该收工了
            if not self.config.LOW_COST_BATCH_FINISHED:
                logger.info('[低耗轮换] 达到停止条件（资源或运行次数），已完成收尾，结束任务')
                break
