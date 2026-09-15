"""Toss Phase 1의 오프라인 shadow 통계 경계.

이 패키지는 네트워크, 인증, 환경변수, 운영 설정을 읽지 않는다.
"""

from .shadow import ShadowManifest, build_shadow, summarize_pairs

__all__ = ("ShadowManifest", "build_shadow", "summarize_pairs")
