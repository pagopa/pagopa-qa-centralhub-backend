from __future__ import annotations

from abc import ABC, abstractmethod


class BaseIntegration(ABC):
    @abstractmethod
    async def sync(self) -> None: ...

    @abstractmethod
    async def health_check(self) -> bool: ...
