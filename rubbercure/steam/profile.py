"""按制品给出的分段升温（升压）配方。

旧做法对所有制品只有一个目标压力，阀门一路拉到底：薄件升温太快容易过硫、
厚件芯部还没透表面已经过硫，段间停多久也全靠操作工看表。分段配方把一次
升温拆成若干段，每段给出目标压力、容差带和到点后的保持时长：压力进入
容差带并连续停够 ``hold_s`` 才允许进下一段；中途冲过上沿则退回上一段，
重新到点、重新计时。配方按制品型号登记，薄件厚件各用各的曲线。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from ..errors import SteamError


@dataclass(frozen=True)
class RampStage:
    """升温曲线上的一段。"""

    target_kpa: float
    hold_s: float
    tolerance_kpa: float

    def __post_init__(self) -> None:
        if self.target_kpa <= 0:
            raise SteamError("分段目标压力必须为正", component="steam")
        if self.hold_s < 0:
            raise SteamError("分段保持时长不能为负", component="steam")
        if self.tolerance_kpa < 0:
            raise SteamError("分段压力容差不能为负", component="steam")

    def band(self) -> tuple[float, float]:
        """返回 (下限, 上限)。"""

        return (self.target_kpa - self.tolerance_kpa, self.target_kpa + self.tolerance_kpa)

    def is_in_band(self, measured_kpa: float) -> bool:
        low, high = self.band()
        return low <= measured_kpa <= high

    def is_above(self, measured_kpa: float) -> bool:
        """压力是否冲过本段上沿（需要退回）。"""

        return measured_kpa > self.target_kpa + self.tolerance_kpa

    def as_dict(self) -> dict:
        low, high = self.band()
        return {
            "target_kpa": round(self.target_kpa, 3),
            "hold_s": round(self.hold_s, 3),
            "tolerance_kpa": round(self.tolerance_kpa, 3),
            "band_low_kpa": round(low, 3),
            "band_high_kpa": round(high, 3),
        }


@dataclass(frozen=True)
class RampProfile:
    """一个制品的分段升温配方。"""

    product_code: str
    stages: tuple[RampStage, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.product_code:
            raise SteamError("制品型号不能为空", component="steam")
        if not self.stages:
            raise SteamError(f"制品 {self.product_code} 的升温配方至少要有一段", component="steam")
        previous = None
        for index, stage in enumerate(self.stages):
            if previous is not None and stage.target_kpa <= previous:
                raise SteamError(
                    f"制品 {self.product_code} 的分段目标压力必须严格递增"
                    f"（第 {index} 段 {stage.target_kpa} 不高于上一段 {previous}）",
                    component="steam",
                )
            # 最后一段到点后由硫化计时接管保温，允许 0；中间段必须停够才放行。
            if index < len(self.stages) - 1 and stage.hold_s <= 0:
                raise SteamError(
                    f"制品 {self.product_code} 的第 {index + 1} 段必须给出正的保持时长",
                    component="steam",
                )
            previous = stage.target_kpa

    @property
    def final_target_kpa(self) -> float:
        return self.stages[-1].target_kpa

    def stage_at(self, index: int) -> RampStage:
        return self.stages[index]

    def as_dict(self) -> dict:
        return {
            "product_code": self.product_code,
            "stage_count": len(self.stages),
            "final_target_kpa": round(self.final_target_kpa, 3),
            "stages": [stage.as_dict() for stage in self.stages],
        }


class RampProfileBook:
    """按制品型号保存分段升温配方的注册册。"""

    def __init__(self) -> None:
        self._profiles: dict[str, RampProfile] = {}
        self._lock = threading.RLock()

    def register(self, profile: RampProfile) -> RampProfile:
        """登记（或覆盖）一个制品的升温配方。"""

        with self._lock:
            self._profiles[profile.product_code] = profile
        return profile

    def register_stages(
        self,
        product_code: str,
        stages: list[RampStage],
    ) -> RampProfile:
        """按制品型号与分段列表登记配方。"""

        return self.register(RampProfile(product_code=product_code, stages=tuple(stages)))

    def resolve(self, product_code: str) -> RampProfile:
        """解析制品的升温配方；未登记时报错。"""

        with self._lock:
            profile = self._profiles.get(product_code)
        if profile is None:
            raise SteamError(f"制品 {product_code} 尚未登记分段升温配方", component="steam")
        return profile

    def has(self, product_code: str) -> bool:
        with self._lock:
            return product_code in self._profiles

    def known_products(self) -> list[str]:
        with self._lock:
            return sorted(self._profiles)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                code: profile.as_dict()
                for code, profile in sorted(self._profiles.items())
            }
