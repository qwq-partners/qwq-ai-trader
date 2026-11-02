"""E04 제한 실호출 인수 진입점 — 사용자 승인 아래서만 실행한다.

사용:
  venv/bin/python scripts/ops/model_comparison_acceptance.py prepare --experiment-id <ID> --day YYYY-MM-DD \
      --candidate '{"reasoning_effort":"low"}' --input-json <spec.json> --out <dir>      # 호출 0
  venv/bin/python scripts/ops/model_comparison_acceptance.py run --dir <dir> --approved-by <name>   # 1쌍 실호출

운영 worker/R1/_raw_clients 를 켜지 않으며 승인 디렉터리의 manifest·승인 요청서·입력 지문이 모두 맞을 때만 한 봉투를 보낸다.
영수증에는 프롬프트·응답 원문이 없다. 실패·불명 결과를 새 쌍으로 재시도하지 않는다.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.analytics.model_comparison_acceptance import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
