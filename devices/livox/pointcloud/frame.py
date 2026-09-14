from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(slots=True)
class PointCloudFrame:
    xyz: np.ndarray
    reflectivity: np.ndarray
    timestamp: int | None = None
    data_type: int | None = None

    @property
    def point_count(self) -> int:
        return int(self.xyz.shape[0])
