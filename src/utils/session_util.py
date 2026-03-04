"""
SessionUtil - engine.py의 session_util 임포트 브릿지

engine.py는 `from ..utils.session_util import SessionUtil` 형태로 사용.
실제 로직은 session.py의 KRSession에 위임.
"""

from src.core.types import MarketSession
from src.utils.session import KRSession

_kr_session = KRSession()


class SessionUtil:
    """engine.py에서 사용하는 정적 인터페이스 (KRSession 위임)"""

    @staticmethod
    def get_current_session() -> MarketSession:
        """현재 KR 시장 세션 반환"""
        return _kr_session.get_session()

    @staticmethod
    def is_trading_hours(config) -> bool:
        """KR 거래 가능 시간 여부

        Args:
            config: KRConfig 또는 enable_pre_market/enable_next_market 속성을 가진 객체
        """
        enable_pre = getattr(config, "enable_pre_market", True)
        enable_next = getattr(config, "enable_next_market", True)
        return _kr_session.is_trading_hours(
            enable_pre_market=enable_pre,
            enable_next_market=enable_next,
        )
