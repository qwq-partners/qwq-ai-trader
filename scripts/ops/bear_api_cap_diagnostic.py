"""E04 Bear API 진단: prepare·check-ledger는 호출 0, run은 별도 승인 지문이 필요하다."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.analytics.bear_api_cap_diagnostic import main  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(main())
