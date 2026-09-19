"""Temporary queue observations; no private corpus, provider or instruction writes."""
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import json
import sqlite3

import pytest

from self_improve import queue_history as q
from self_improve import store as store_module
from self_improve.store import Store

SETTINGS = {'mine_order': 'signal_then_recent', 'project_filter': '', 'dry_run': False,
            'cheap_call_cap': 80, 'strong_call_cap': 10, 'gate_call_cap': 78}


def incident(store, ident, status='new'):
    store.insert_incident({'id': ident, 'session_file': 'invented-session', 'signal_type': 'correction',
                           'status': status, 'ts': '1999-01-01T00:00:00Z', 'run_id': 'scan-not-mining'})


def fixture_store(path, initial=('new',) * 10):
    """Create legacy membership, then install observation at an invented clock."""
    with patch.object(store_module, 'MIGRATIONS', store_module.MIGRATIONS[:-1]):
        with closing(Store(path)) as old:
            old.insert('sessions', {'file_path': 'invented-session', 'source': 'claude'})
            for i, status in enumerate(initial):
                incident(old, f'baseline-{i}', status)
    store = Store(path, migrate=False)
    clock = ['2030-01-01T00:00:00.000Z']
    store.conn.create_function('current_timestamp', 0, lambda: clock[0].removesuffix('Z'))
    store._migrate()
    return store, clock


def anchor(store, clock, run_id='selected', phase='finish', when='2030-01-09T12:00:00.000Z', settings=None):
    clock[0] = when
    store.conn.execute("INSERT INTO runs(id,started,status) VALUES (?,'2030-01-09T00:00:00Z','ok') ON CONFLICT DO NOTHING", (run_id,))
    result = q.capture(store, run_id, phase=phase, settings=settings or SETTINGS)
    store.commit()
    return result


@pytest.fixture
def history(tmp_path):
    store, clock = fixture_store(tmp_path / 'state.db')
    yield store, clock
    store.close()


def test_upgrade_baseline_is_not_an_arrival_and_empty_is_measured(tmp_path):
    with closing(fixture_store(tmp_path / 'empty.db', ())[0]) as store:
        assert q.require_schema(store)['opening_count'] == 0
        assert store.query('SELECT * FROM queue_events') == []
    store, clock = fixture_store(tmp_path / 'baseline.db')
    with closing(store):
        anchor(store, clock)
        result = q.read_run(store, 'selected')
        assert result['queue_count'] == 10 and result['admissions'] == 0
        assert result['rates']['processed_per_day'] == 0
        assert result['scenario'] == {'state': 'steady', 'days': None, 'assumption': 'No finite drain estimate at these observed rates.'}


def test_actual_exits_common_window_and_conditional_days(history):
    store, clock = history
    clock[0] = '2030-01-02T00:00:00.000Z'  # included lower bound
    incident(store, 'new')
    for i in range(8):
        q.set_processed_status(store, f'baseline-{i}', 'dismissed', outcome='negative',
                               provenance={'run_id': 'actual-mining'})
    clock[0] = '2030-01-09T00:00:00.000Z'  # excluded upper bound
    incident(store, 'partial-day')
    q.set_processed_status(store, 'baseline-8', 'mined', outcome='new')
    snap = anchor(store, clock)
    got = q.read_run(store, 'selected')
    assert got['snapshot']['id'] == snap['id'] and got['queue_count'] == 3
    assert got['admissions'] == 1 and got['processed_exits'] == 8
    assert got['window']['days'] == 7 and got['window']['complete']
    assert got['rates']['net_drain_per_day'] == 1 and got['scenario']['days'] == 3
    assert {r['run_id'] for r in store.query('SELECT * FROM queue_processing')} == {'actual-mining', ''}
    assert 'scan-not-mining' not in json.dumps(got)


@pytest.mark.parametrize('mode',['delete_new', 'delete_dismissed', 'requeue', 'direct_exit', 'terminal_change'])
def test_maintenance_and_unattributed_changes_never_credit_processing(history, mode):
    store, clock = history
    clock[0] = '2030-01-03T00:00:00.000Z'
    if mode == 'delete_new':store.conn.execute("DELETE FROM incidents WHERE id='baseline-0'")
    elif mode == 'delete_dismissed':
        q.set_processed_status(store, 'baseline-0', 'dismissed', outcome='negative')
        store.conn.execute("DELETE FROM incidents WHERE id='baseline-0'")
    elif mode == 'direct_exit':store.update('incidents','id','baseline-0',{'status':'mined'})
    else:
        q.set_processed_status(store, 'baseline-0', 'mined', outcome='new')
        store.update('incidents','id','baseline-0',{'status':'new' if mode == 'requeue' else 'dismissed'})
    anchor(store, clock)
    got = q.read_run(store, 'selected')
    assert got['adjustments']['window'] == 1 and got['rates'] is None and got['scenario'] is None
    assert 'unattributed' in ' '.join(got['reasons'])


def test_tail_maintenance_invalidates_but_earlier_maintenance_ages_out(history):
    store, clock = history
    clock[0] = '2030-01-01T01:00:00.000Z'
    store.conn.execute("DELETE FROM incidents WHERE id='baseline-0'")
    anchor(store, clock, run_id='before-tail')
    assert q.read_run(store, 'before-tail')['rates'] is not None
    clock[0] = '2030-01-09T10:00:00.000Z'
    store.conn.execute("DELETE FROM incidents WHERE id='baseline-1'")
    anchor(store, clock)
    assert q.read_run(store, 'selected')['adjustments']['through_snapshot'] == 1
    assert q.read_run(store, 'selected')['rates'] is None


def test_duplicate_processing_and_transaction_rollback(history):
    store, clock = history
    clock[0] = '2030-01-03T00:00:00.000Z'
    with pytest.raises(RuntimeError):
        with store.transaction(write=True):
            q.set_processed_status(store,'baseline-0','mined',outcome='new')
            raise RuntimeError('abort content publication')
    assert store.query('SELECT * FROM queue_events') == []
    assert store.query('SELECT * FROM queue_processing') == []
    with store.transaction(write=True):
        first = q.set_processed_status(store,'baseline-0','mined',outcome='new')
        assert q.set_processed_status(store,'baseline-0','mined',outcome='new') is None
    assert len(store.query('SELECT * FROM queue_processing')) == 1
    assert store.query_one('SELECT event_id FROM queue_processing')['event_id'] == first


def test_snapshot_survives_stats_replacement_replay_and_new_activity(history):
    store, clock = history
    snap = anchor(store, clock)
    store.update('runs','id','selected',{'stats_json':'{}','finished':'','status':'running'})
    incident(store,'later')
    clock[0] = '2030-01-10T00:00:00.000Z'
    assert q.capture(store,'selected',phase='finish',settings=SETTINGS) == snap
    with pytest.raises(q.QueueHistoryError, match='replay settings'):
        q.capture(store,'selected',phase='finish',settings={**SETTINGS,'cheap_call_cap':2})
    store.commit()
    assert q.read_run(store,'selected')['queue_count'] == 10
    assert store.query_one("SELECT COUNT(*) n FROM incidents WHERE status='new'")['n'] == 11


def test_missing_and_partial_coverage_are_not_zero(history):
    store, clock = history
    store.insert('runs',{'id':'old','started':'1999-01-01T00:00:00Z'});store.commit()
    got=q.read_run(store,'old');assert got['queue_count'] is None and got['reasons']
    anchor(store,clock,when='2030-01-03T12:00:00.000Z')
    got=q.read_run(store,'selected');assert got['queue_count']==10 and got['rates'] is None
    assert not got['window']['complete'] and got['scenario'] is None
    store.conn.execute('DELETE FROM schema_migrations WHERE name=?',(q.MIGRATION,))
    assert q.read_run(store,'selected')['queue_count'] is None


@pytest.mark.parametrize('kind',['empty','growing','steady'])
def test_zero_and_nonshrinking_queues_have_no_finite_countdown(history,kind):
    store,clock=history;clock[0]='2030-01-03T00:00:00.000Z'
    if kind=='empty':
        for i in range(10):q.set_processed_status(store,f'baseline-{i}','dismissed',outcome='negative')
    elif kind=='growing':incident(store,'arrival')
    anchor(store,clock);got=q.read_run(store,'selected')
    assert got['scenario']['state']==kind and got['scenario']['days'] is None
    assert got['rates'] is not None


@pytest.mark.parametrize('trigger',['queue_observe_insert','queue_observe_update','queue_observe_delete','queue_processing_no_update'])
def test_missing_trigger_fails_before_read_or_capture(history,trigger):
    store,clock=history;anchor(store,clock)
    store.conn.execute('DROP TRIGGER '+trigger)
    with pytest.raises(q.QueueHistoryError,match='trigger'):q.read_run(store,'selected')
    store.conn.execute('BEGIN')
    with pytest.raises(q.QueueHistoryError,match='trigger'):q.capture(store,'selected',phase='start',settings=SETTINGS)
    store.conn.rollback()


def test_unknown_baseline_is_explicitly_incompatible(tmp_path):
    store,clock=fixture_store(tmp_path/'unknown.db',('future_queued',))
    with closing(store):
        anchor(store,clock);got=q.read_run(store,'selected')
        assert got['coverage']['opening_unknown_count']==1
        assert got['rates'] is None and 'Unknown incident statuses' in got['reasons'][0]


@pytest.mark.parametrize('damage',['clock','sequence','delta','count','profile','unknown_status'])
def test_corrupt_history_is_a_data_error(history,damage):
    store,clock=history;clock[0]='2030-01-03T00:00:00.000Z';incident(store,'arrival');anchor(store,clock)
    if damage in ('count','profile'):
        store.conn.execute('DROP TRIGGER queue_snapshots_no_update')
        store.conn.execute("UPDATE queue_snapshots SET "+("queue_count=99" if damage=='count' else "profile='future/2'"))
        store.conn.execute(q._expected_triggers()['queue_snapshots_no_update'])
    else:
        store.conn.execute('DROP TRIGGER queue_events_no_update')
        clause={'clock':"observed_at='2029-01-01T00:00:00Z'",'sequence':'seq=2','delta':'queue_delta=0','unknown_status':"new_status='unknown'"}[damage]
        store.conn.execute('UPDATE queue_events SET '+clause)
        store.conn.execute(q._expected_triggers()['queue_events_no_update'])
    with pytest.raises(q.QueueHistoryError):q.read_run(store,'selected')


def test_reader_is_selected_readonly_without_file_reads_or_migration(history):
    store,clock=history;anchor(store,clock);before=list(store.conn.iterdump())
    with closing(Store(store.db_path,read_only=True)) as reader, patch.object(Path,'read_text',side_effect=AssertionError('No source read')):
        with reader.transaction():assert q.read_run(reader,'selected')['queue_count']==10
    assert list(store.conn.iterdump())==before


def test_immutable_archives_and_caller_transaction(history):
    store,clock=history;anchor(store,clock)
    with pytest.raises(q.QueueHistoryError,match='caller write'):q.capture(store,'selected',phase='start',settings=SETTINGS)
    for table in q.TABLES:
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):
            # Coverage/snapshot tables have records; seed the others first below.
            if table=='queue_events':incident(store,'extra')
            if table=='queue_processing':q.set_processed_status(store,'baseline-0','mined',outcome='new')
            store.conn.execute('DELETE FROM '+table)
        store.conn.rollback()


def test_historical_receipts_validate_call_and_snapshot_membership(history):
    store,clock=history;clock[0]='2030-01-03T00:00:00.000Z'
    event=q.set_processed_status(store,'baseline-0','mined',outcome='new')
    anchor(store,clock)
    store.conn.execute('DROP TRIGGER queue_processing_no_update')
    store.conn.execute("UPDATE queue_processing SET call_id='nonexistent-call'")
    store.conn.execute(q._expected_triggers()['queue_processing_no_update'])
    with pytest.raises(q.QueueHistoryError,match='processing call'):q.read_run(store,'selected')
    store.conn.rollback()


def test_late_receipt_cannot_reclassify_an_anchored_unknown_exit(history):
    store,clock=history;clock[0]='2030-01-03T00:00:00.000Z'
    store.update('incidents','id','baseline-0',{'status':'mined'})
    event=store.query_one('SELECT id FROM queue_events')['id']
    anchor(store,clock)
    assert q.read_run(store,'selected')['rates'] is None
    store.insert('queue_processing',{'event_id':event,'profile':q.PROCESSING_PROFILE,'outcome':'new','run_id':'','command_id':'','call_id':''})
    with pytest.raises(q.QueueHistoryError,match='changed after'):q.read_run(store,'selected')


def test_snapshot_clock_reversal_fails_even_without_intervening_events(history):
    store,clock=history
    anchor(store,clock,phase='start',when='2030-01-10T12:00:00.000Z')
    anchor(store,clock,phase='finish',when='2030-01-09T12:00:00.000Z')
    with pytest.raises(q.QueueHistoryError,match='ordering'):q.read_run(store,'selected')
