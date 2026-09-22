"""
连续作战（MULTIPLE SORTIES）交互。

出击准备界面底部的「连续作战」按钮会弹出设置窗：

    作战次数  [-]  10  [+]  [MAX]
    自动使用 [道具] 334                [出击]

点「出击」即开始连续作战，游戏会自己重复出击，直到次数用完。
与普通出击的区别是：**出击动作发生在弹窗里**，所以处理完本交互之后
调用方不应再点 `FLEET_PREPARATION`（立刻前往）。

注意（游戏内提示）：
- 连续作战默认开启「自律寻敌」「自律作战」
- 连续作战中停止「自律寻敌」会**自动终止**连续作战

因此使用前必须确保自动搜索是开启状态，否则连续作战会被立即打断。

资源说明：`CONTINUOUS_BATTLE_*` 目前只有国服（cn）截图，其余服务器
沿用国服坐标。
"""

from module.base.timer import Timer
from module.campaign.assets import (
    CONTINUOUS_BATTLE_ENTER, CONTINUOUS_BATTLE_MAX, CONTINUOUS_BATTLE_MINUS,
    CONTINUOUS_BATTLE_START
)
from module.logger import logger

# 游戏内「连续作战」的次数上限，即弹窗里 MAX 按钮对应的值（国服为 10）
CONTINUOUS_BATTLE_MAX_SORTIES = 10


class ContinuousBattle:
    """连续作战交互。由需要它的任务类混入（mixin）。"""

    def continuous_battle_start(self, count=CONTINUOUS_BATTLE_MAX_SORTIES, setting_timeout=5):
        """
        在舰队准备界面开启连续作战并直接出击。

        步骤：点「连续作战」开弹窗 -> 点「MAX」拉到上限 -> 按需用「-」
        下调到目标次数 -> 点「出击」。

        设置次数采用「先 MAX 再往下减」而不是直接调，是因为弹窗里显示的
        是上次的值（该设置会被游戏继承），直接增减无法知道起点；而先拉到
        上限再减，起点是确定的。

        Args:
            count (int): 作战次数。大于上限时按上限处理。
            setting_timeout (int): 等待弹窗出现的超时秒数。

        Returns:
            bool: True 表示已经通过弹窗出击，调用方不要再点 FLEET_PREPARATION；
                False 表示未能处理，调用方应按普通流程出击。
        """
        if not self.appear(CONTINUOUS_BATTLE_ENTER, offset=(20, 20)):
            logger.info('[连续作战] 未找到连续作战按钮，按普通流程出击')
            return False

        count = max(1, min(int(count), CONTINUOUS_BATTLE_MAX_SORTIES))

        logger.hr('连续作战', level=2)
        self.device.click(CONTINUOUS_BATTLE_ENTER)

        # 等待设置弹窗出现，「出击」按钮是弹窗独有的标志
        timeout = Timer(setting_timeout, count=setting_timeout).start()
        while 1:
            self.device.screenshot()

            if self.appear(CONTINUOUS_BATTLE_START, offset=(20, 20)):
                break
            if timeout.reached():
                logger.warning('[连续作战] 设置弹窗未出现，回退到普通出击')
                return False

        # 先点 MAX 拉到上限，再按需用「-」往下减
        if self.appear_then_click(CONTINUOUS_BATTLE_MAX, offset=(20, 20)):
            self.device.sleep((0.3, 0.5))
            self.device.screenshot()
            logger.info(f'[连续作战] 作战次数已拉到上限 {CONTINUOUS_BATTLE_MAX_SORTIES}')
        else:
            logger.warning('[连续作战] 未找到 MAX 按钮，沿用当前次数')

        step = CONTINUOUS_BATTLE_MAX_SORTIES - count
        if step > 0:
            if not self.appear(CONTINUOUS_BATTLE_MINUS, offset=(20, 20)):
                logger.warning('[连续作战] 未找到「-」按钮，次数可能未按预期设置')
            else:
                for _ in range(step):
                    self.device.click(CONTINUOUS_BATTLE_MINUS)
                    self.device.sleep((0.15, 0.25))
                self.device.screenshot()
                logger.info(f'[连续作战] 作战次数下调至 {count}')

        # 弹窗内出击
        self.appear_then_click(CONTINUOUS_BATTLE_START, offset=(20, 20))
        logger.info('[连续作战] 已在设置弹窗内出击')
        return True
