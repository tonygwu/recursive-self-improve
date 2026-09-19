"""Local delivery worker. Opening a view never starts this process or a model."""
from .apply import execute_next_command
from .delivery_lock import WriteBusy


def run_once(store, cfg):
    try:
        return execute_next_command(store, cfg)
    except WriteBusy as exc:
        return {'state': 'busy', 'error_code': 'WriteBusy', 'error_detail': str(exc)}


def serve(cfg, *, once=False, poll_seconds=1.0, operation_id=None):
    """Open the existing configured store without migration, then drain commands."""
    from contextlib import closing
    import json
    import time
    from .store import Store
    if poll_seconds <= 0:
        raise ValueError('poll_seconds must be positive')
    from .worker_services import check_runtime
    check_runtime(cfg)
    with closing(Store(cfg.state_path('state.db'), migrate=False)) as store:
        if operation_id is not None:
            from .apply import resume_write_operation
            try:
                result = resume_write_operation(store, cfg, operation_id)
            except WriteBusy as exc:
                result = {'state': 'busy', 'error_code': 'WriteBusy', 'error_detail': str(exc)}
            print(json.dumps({k: result[k] for k in ('id', 'kind', 'state', 'result', 'error_code', 'error_detail') if k in result}, ensure_ascii=False), flush=True)
            return 1 if result['state'] in {'failed', 'blocked', 'busy'} else 0
        while True:
            result = run_once(store, cfg)
            if result is not None:
                # Keep full retained evidence and file contents in the store,
                # not in routine service logs.
                summary = {k: result[k] for k in ('id', 'state', 'result', 'error_code', 'error_detail') if k in result}
                print(json.dumps(summary, ensure_ascii=False), flush=True)
            if once:
                return 1 if result and result['state'] in {'failed', 'blocked', 'busy'} else 0
            if result is None or result.get('state') == 'busy':
                time.sleep(poll_seconds)
