"""실제 ExitManager 계산은 유지하며 checkpoint 후보의 파일 I/O를 분리한다."""
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from src.core.types import Position
from src.strategies.exit_manager import ExitConfig, ExitManager, ExitStage

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=ZoneInfo('Asia/Seoul'))


def test_memory_only_manager_never_creates_stage_files(tmp_path):
    directory = tmp_path / 'not-created'
    manager = ExitManager(ExitConfig(), persist=False, state_dir=directory, clock=lambda: NOW)
    manager.register_position(Position('005930', quantity=40, avg_price=Decimal('10000'),
                                       current_price=Decimal('10000'), entry_time=NOW))
    manager.update_price('005930', Decimal('10400'))
    assert manager.get_state('005930').remaining_quantity == 40
    assert not directory.exists()


def test_injected_clock_is_used_for_price_high_and_partial_fill(tmp_path):
    clock = [NOW]
    manager = ExitManager(ExitConfig(), persist=False, state_dir=tmp_path, clock=lambda: clock[0])
    manager.register_position(Position('005930', quantity=100, avg_price=Decimal('10000'),
                                       current_price=Decimal('10000'), entry_time=NOW))
    state = manager.get_state('005930')
    clock[0] += timedelta(days=1)
    manager.update_price('005930', Decimal('10300'))
    assert state.last_new_high_date == clock[0].date()
    state.pending_stage = ExitStage.FIRST
    state.pending_target_qty = 10
    manager.on_fill('005930', 4, Decimal('11000'))
    assert state.pending_since == clock[0]
    assert state.current_stage == ExitStage.NONE
    assert state.pending_filled_qty == 4
    manager.on_fill('005930', 6, Decimal('11000'))
    assert state.current_stage == ExitStage.FIRST


def test_default_persistence_remains_compatible(tmp_path):
    manager = ExitManager(ExitConfig(), state_dir=tmp_path, clock=lambda: NOW)
    manager.register_position(Position('005930', quantity=10, avg_price=Decimal('10000'),
                                       current_price=Decimal('10000'), entry_time=NOW))
    assert (tmp_path / 'exit_stages_2026-09-17.json').is_file()
