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
from module.campaign.assets import BUILD_RETIRE, OCR_OIL_CHECK, TOTAL_REWARDS, TOTAL_REWARDS_QUIT
from module.campaign.gems_farming import GemsCampaignOverride, GemsEmotion, GemsFarming
from module.campaign.run import CampaignRun
from module.exception import CampaignEnd
from module.handler.continuous_battle import ContinuousBattle
from module.logger import logger
from module.retire.assets import IN_RETIREMENT_CHECK
from module.ui.page import page_build, page_main


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
            # 置位仅作为可选信号：这个框通常会被父类的
            # handle_battle_status() 抢先点掉（点击区域重合），
            # 本方法未必执行，判停不能依赖它（见 _rotation_loop）
            self.config.LOW_COST_BATCH_FINISHED = True
            self.device.click(TOTAL_REWARDS_QUIT)
            self.interval_reset(TOTAL_REWARDS)
            return True
        return False

    # 中间各场的结算（BATTLE_STATUS_S / EXP_INFO_S）**沿用父类处理**，
    # 也就是照常点击。理由：
    #   · 点击本身不是问题——它是「不卡死」的原因（stuck_timer 靠点击重置），
    #     卡死检测现已整体关闭，点击不再有任何副作用；
    #   · 点了能推进流程，万一游戏某次真的停在结算界面上，不会干等；
    #   · 顺带恢复 `drop.handle_add()`，中间各场的掉落统计也能记录。
    # 需要区别对待的只有末尾的「合计获得奖励」，见 `handle_total_rewards()`。

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

        # 这里**不做**额外的 dock_reset()。
        #
        # 日志显示 `dock_filter.set()` 自身就会在同一个弹窗里先重置再设置
        # （旗舰那段：同一弹窗内先点 FILTER_INDEX_0_0(all) 再点 _0_1(cv)），
        # 而下面的 super() 调用默认 sort='level'，sort 也会被一并设回去。
        # 多加一次 dock_reset() 只会白开一次筛选弹窗——实测编队阶段因此出现
        # 两轮筛选操作，且分不清是先锋还是旗舰那一步在做。

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

    def _wait_oil_icon(self, timeout=5) -> bool:
        """等主界面资源栏出现石油图标，返回是否在超时前出现。

        石油 / 物资都靠主界面顶部的资源栏读取。界面未就绪时
        （例如 `page_main_white` 白屏过渡态）`OCR_OIL_CHECK` 位置的颜色
        对不上，`_get_num()` 只是打个 warning 就**降级用错误参数做 OCR**，
        读出乱码（实测读成 2），再被
        `get_oil() < max(OilLimitHardFloor, OilLimit)` 判成「石油上限」。
        所以判停之前先确认资源栏真的可读。

        Args:
            timeout (int): 秒。

        Returns:
            bool: 石油图标是否可见。
        """
        timer = Timer(timeout, count=timeout).start()
        while 1:
            self.device.screenshot()
            if self.appear(OCR_OIL_CHECK, offset=(10, 2)):
                return True
            if timer.reached():
                return False

    def triggered_stop_condition(self, oil_check=True):
        """在父类判停之前，先确认资源栏可读。

        `CampaignRun.run()` 的循环开头（run.py:504）会调用本方法，且：
        * 它在 `campaign.run()` **之前**，所以面对的是上一次操作留下的界面；
        * 那时的界面可能还没就绪（实测：`ui_goto(page_main)` 已经返回，
          界面却仍是 `page_main_white`）。

        此时 `get_oil()` 读不到值会返回乱码，而父类的判定是
        `get_oil() < 上限` → 乱码小于阈值 → 误报「触发停止条件: 石油上限」
        并把任务推迟 120~240 分钟（日志 09:01 实例）。

        所以：**资源栏不可读时直接跳过石油 / 物资判定**，本轮不做这个决定，
        交给下一次检查——比拿一个乱码当真要安全。

        Args:
            oil_check (bool): 是否检查石油 / 物资等资源限制。

        Returns:
            bool: 是否触发停止条件。
        """
        if oil_check and not self._wait_oil_icon():
            logger.warning('[低耗轮换] 资源栏不可读（界面未就绪），跳过本次石油/物资判定')
            return False

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

    def retire_gems_farming_flagships(self, keep_one=True) -> int:
        """低耗轮换下强制「不保留」，把待退的普通航母全部退掉。

        父类默认 `keep_one=True`（至少留一艘普通航母供后续编队用），但它的
        判否分支是 `if len(ships) < 2: break`——**只剩 1 艘待退航母时会直接
        跳过，一艘都不退**。

        而这恰好是本任务的典型场景：退役范围是 `level=(2, 100)`，刚编进队的
        那艘 1 级航母本就不在候选里，于是候选往往只剩「替换下来的高等级航母」
        这一艘 → 被 break 掉，退不掉、一直堆在船坞。

        低耗轮换不需要保留：下一批要用的 1 级航母刚刚已经编好队，
        且按等级它也不会被选中。

        Args:
            keep_one (bool): 父类参数；本任务启用时强制为 False。

        Returns:
            int: 退役的舰船数量。
        """
        if self.config.is_task_enabled('LowCostRotation'):
            keep_one = False
        return super().retire_gems_farming_flagships(keep_one=keep_one)

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
        if not self._retirement_enter():
            return

        # 先一键退役。其内部已包含「退役报废的旗舰」（打过、等级升上去的
        # 白皮航母），这一步负责清掉船坞里的杂鱼。
        self._retire_handler()

        # 「退役废弃旗舰」结束时界面**已经退出了退役界面**（20:13 实测）。
        # 而退役先锋还要继续操作船坞里的排序 / 筛选开关——界面不对时这些开关
        # 会被判定为 unknown，于是反复点击（先是 Favourite_filter，后是
        # Dork_sorting，都刷屏到日志结束）。这里重新进入一次。
        self._retirement_enter()

        # 再退役本轮换下来的先锋：1 级驱逐打完一批会升级，属于用完即弃的
        # 一次性船，不清掉会一直堆在船坞里。放在一键退役之后做。
        self.retire_low_cost_vanguards()

        self._retirement_quit()

        # 退役收尾后回到主界面。
        #
        # `_retirement_quit()` 只是关掉退役弹窗，界面此时停在「建造」页；
        # 不回主界面的话，下一轮无论是继续出击还是响应抢占都无从导航
        # （实测就卡在这里，既不继续战斗也不响应抢占）。
        self.ui_goto(page_main)
        # 再等一次界面稳定。`ui_goto` 刚返回时可能还停在 page_main_white
        # （白屏过渡态），此时资源栏尚未渲染——紧接着的停止条件检查要读
        # 石油/物资，读不到会返回 0，于是「0 < 石油下限」被误判成
        # 「触发停止条件: 石油上限」（22:15 实测：无石油图标 / OCR_OIL 读 0 /
        # 界面为 page_main_white），任务提前收工。
        self.ui_ensure(page_main)

    def _retirement_enter(self) -> bool:
        """确保处于退役界面。

        进入方式见 `retire_after_rotation()` 的说明：不用模板匹配「退役」入口
        （它是半透明标签、会透出随活动变化的背景），而是确认已在建造界面后
        直接点标签的固定坐标。

        退役流程里会调用两次：第一次是正常进入；第二次是在「退役废弃旗舰」
        之后——那一步结束时界面已经退出了退役界面，而退役先锋还要继续操作
        船坞开关。

        Returns:
            bool: 是否已处于退役界面。
        """
        self.device.screenshot()
        if self.appear(IN_RETIREMENT_CHECK, offset=(20, 20)):
            return True

        self.ui_ensure(page_build)
        self.device.click(BUILD_RETIRE)

        timeout = Timer(5, count=5).start()
        while 1:
            self.device.screenshot()
            if self.appear(IN_RETIREMENT_CHECK, offset=(20, 20)):
                return True
            if timeout.reached():
                logger.warning('[低耗轮换] 未进入退役界面，跳过本次退役')
                return False

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

        # 卡死/连击检测的豁免**只在「打一批」期间**生效，见
        # `_continuous_battle_guards_off()`。
        #
        # 收尾（换编队、退役）都是常规 UI 操作、没有静默期，检测保持开启——
        # 否则像「开关状态判定为 unknown 导致反复点击」这类问题不会报错，
        # 只会永久卡住（18:17 实测：Favourite_filter 刷屏到日志结束）。
        self._rotation_loop(name, folder, mode, total)

    def _continuous_battle_guards_off(self):
        """进入连续作战：关掉会误报的卡死/连击检测，并把截图放宽到 1 秒。

        连续作战整批由游戏自己连打、脚本只是旁观，期间：

          1. stuck_timer（60s 无点击）—— 单场战斗全程无点击必然触发。它只能靠
             点击重置，而 stuck_long_wait_list 的豁免实际失效（detect_record_add()
             全项目无调用点，device.py:87 / 400 / 471）
          2. stuck_timer_long（195s）—— 到了会绕过豁免直接报错，是硬线
          3. _stuck_image_timer（30s 画面不变）—— 加载 / 结算画面长时间静止
          4. click_record（15 次窗，两按钮各 6 次）—— 每场固定 3 次点击

        **只在「打一批」期间关**：收尾是常规 UI 操作、没有静默期，检测必须
        开回来，否则「反复点击」不会超时报错，只会永久卡住（18:17 实测）。

        Returns:
            tuple: 还原所需的原方法，交给 `_continuous_battle_guards_restore()`。
        """
        device = self.device
        original = (
            device.click_record_check,
            device.stuck_record_check,
            device._check_image_stuck,
            device.screenshot_interval_set,
        )

        device.disable_stuck_detection()                    # 前两个
        device._check_image_stuck = lambda *a, **kw: None   # 画面不变检测

        original_set = device.screenshot_interval_set

        def slow_screenshot_interval(interval=None):
            # 改配置行不通：screenshot_interval_set(None) 会把值 limit 到 0.3 以内
            # （screenshot.py:237），只有传具体数字才不设限（250-252 行）
            if interval is None or interval == 'combat':
                interval = 1.0
            original_set(interval)

        device.screenshot_interval_set = slow_screenshot_interval
        return original

    def _continuous_battle_guards_restore(self, original):
        """还原 `_continuous_battle_guards_off()` 关掉的东西。

        Args:
            original (tuple): `_continuous_battle_guards_off()` 的返回值。
        """
        (self.device.click_record_check,
         self.device.stuck_record_check,
         self.device._check_image_stuck,
         self.device.screenshot_interval_set) = original

    def _rotation_loop(self, name, folder, mode, total):
        """分批循环主体。

        单独抽成方法，是为了让 `run()` 更清晰；检测豁免的开关由本方法
        按阶段控制（只在「打一批」期间关闭）。

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
                # 由游戏自己连打，直到弹出「合计获得奖励」结算框。
                #
                # total 固定传 1：CampaignRun.run() 的退出条件是
                # `if total and self.run_count >= total`（run.py:456），
                # 传 0 等于不设上限——它会打完这轮接着打下一次，
                # 永远不返回，本任务的换编队与退役就再也轮不到。
                # 传 1 让它在本轮（一次连续作战）结束后交还控制权。
                # 打一批之前先回到主界面。
                #
                # `CampaignRun.run()` 的循环开头就会检查停止条件
                # （run.py:504），而那个检查要读石油 / 物资——两者都依赖
                # 主界面顶部的资源栏。此时界面若是上一次操作留下的
                # （例如初次编队刚结束、停在编队页），`OCR_OIL_CHECK` 的
                # 颜色检查会失败，`_get_num()` 便降级用错误参数做 OCR，
                # 读出乱码（实测读成 2），再被
                # `get_oil() < max(OilLimitHardFloor, OilLimit)` 误判成
                # 「触发停止条件: 石油上限」，任务被推迟 120~240 分钟。
                #
                # 日志（09:01）：意外的OCR_OIL_CHECK颜色 → 无石油图标
                # → [OCR_OIL] 2 → 触发停止条件: 石油上限。
                self.ui_ensure(page_main)

                # 豁免只覆盖这一句——连续作战全程静默，通用保护必然误报
                guards = self._continuous_battle_guards_off()
                try:
                    CampaignRun.run(self, name=name, folder=folder, total=1)
                except CampaignEnd:
                    # 一批打完（或撤退）属于正常结束
                    pass
                finally:
                    # 收尾之前恢复检测。换编队 / 退役都是常规 UI 操作，
                    # 需要靠检测把「开关 unknown 导致反复点击」暴露出来，
                    # 否则它不会超时报错，只会永久卡住（18:17 实测）。
                    self._continuous_battle_guards_restore(guards)

                # 本轮（一次连续作战）到此结束。
                #
                # 顺带说明：「合计获得奖励」框通常不是本任务的
                # `handle_total_rewards()` 处理的——它与普通结算框点击区域重合，
                # 父类的 `handle_battle_status()` 会先命中并点掉（点了离开、
                # 回到出击界面）。所以 `LOW_COST_BATCH_FINISHED` 未必会被置位，
                # 外层判停不能只依赖它，见下面的 explicit 检查。

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

            # 收尾做完后，再判断是不是该收工了。
            #
            # 这里走的也是 `self.triggered_stop_condition()`，即本任务的覆写版
            # ——与 `CampaignRun.run()` 循环开头那次（run.py:504）共用同一套
            # 「资源栏不可读就跳过判定」的保护（见该方法的说明）。
            #
            # 注：`LOW_COST_BATCH_FINISHED` 不参与判停——「合计获得奖励」框
            # 常被父类的 `handle_battle_status()` 抢先处理，标记不置位。
            # 「本批是否打完」由 `total=1` 保证（CampaignRun.run() 打一轮
            # 就返回），两者各司其职。
            if self.triggered_stop_condition(oil_check=True):
                logger.info('[低耗轮换] 达到停止条件（资源或运行次数），已完成收尾，结束任务')
                break
