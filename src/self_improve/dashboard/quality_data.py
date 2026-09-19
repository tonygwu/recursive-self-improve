"""Class evidence with separate delivery, observation, judgment and gate counts."""
import json

from .. import quality, eval_history, rule_revisions, rule_availability
from ..commands import CommandError, _hash
from ..execution_policy import TARGET_CLASSES, TARGET_CLASS, policy_snapshot
from .eval_data import outcome


def _observations(store):
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?',(rule_revisions.MIGRATION,)):
        return {}, False
    revisions = {r['id']:r for r in rule_revisions.retained_revisions(store)}
    latest = {}
    for row in store.query('SELECT * FROM rule_availability_observations ORDER BY observed_at,id'):
        record = rule_availability._validate_observation(rule_revisions.read_record(row,'rule_availability_observations'))
        revision = revisions.get(record['rule_revision_id'])
        if revision is None:
            raise CommandError('QualityDataError',record['id']+': observation has no delivered revision.',500)
        rule_availability._bind_revision(record,revision)
        key = (record['rule_revision_id'], record['working_copy_id'])
        old = latest.get(key, [])
        latest[key] = old+[record] if old and old[0]['observed_at']==record['observed_at'] else [record]
    by_subject = {}
    for (revision_id,copy_id), records in latest.items():
        revision = revisions[revision_id]
        subject = _hash([revision['application_id'],revision['contribution_hash']])
        statuses = {rule_availability._check_signature(r) for r in records}
        status = records[0]['status'] if len(statuses)==1 else 'unknown'
        by_subject.setdefault(subject,[]).append({'rule_revision_id':revision_id,'working_copy_id':copy_id,
            'observed_at':records[0]['observed_at'],'status':status,'observation_ids':[r['id'] for r in records]})
    return by_subject, True


def _rollbacks(store, subjects):
    from ..operations import operation_status
    from ..resolutions import completed
    found, unknown = set(), []
    for event in store.query("SELECT * FROM proposal_events WHERE event='rolled_back' ORDER BY ts,id"):
        try:
            note = json.loads(event['note'])
        except (ValueError,TypeError) as exc:
            raise CommandError('QualityDataError',event['id']+': unreadable rollback metadata.',500) from exc
        if not isinstance(note,dict):
            raise CommandError('QualityDataError',event['id']+': invalid rollback metadata.',500)
        if note.get('operation_id'):
            operation = operation_status(store,note['operation_id'])
            if operation['kind']!='rollback' or operation['state']!='completed':
                raise CommandError('QualityDataError',event['id']+': rollback has no matching completed operation.',500)
            source = operation['record']['rollback']['source']
            identity = _hash([source['application_id'],source['contribution_hash']])
            if (operation['kind']!='rollback' or operation['state']!='completed'
                    or source['application_id']!=note.get('application_id')
                    or (event['proposal_id'],note.get('applied_event_id')) not in {(m['proposal_id'],m['applied_event_id']) for m in source['affected_members']}):
                raise CommandError('QualityDataError',event['id']+': rollback has no matching completion.',500)
        elif note.get('resolution_proposal_id'):
            identity = _hash([note.get('application_id'),note.get('contribution_hash')])
            if identity not in subjects:
                unknown.append(event['id']); continue
            source = {**subjects[identity]['source'],'proposal':{'id':event['proposal_id']},
                      'affected_members':[{'proposal_id':event['proposal_id']} ]}
            if not completed(store,source):
                raise CommandError('QualityDataError',event['id']+': resolution has no matching completion.',500)
        else:
            unknown.append(event['id']); continue
        if identity not in subjects:
            unknown.append(event['id']); continue
        found.add(identity)
    return found, unknown


def classes(store, cfg):
    policy = policy_snapshot(store)
    schema = quality.available(store)
    population = quality.population(store,cfg)
    subjects = {r['source']['id']:r for r in population['subjects']}
    observations, observed_schema = _observations(store)
    rolled_back, unlinked_rollbacks = _rollbacks(store,subjects)
    evals = {c:[] for c in TARGET_CLASSES}
    eval_schema = eval_history.available(store)
    unknown_eval_classes = []
    if eval_schema:
        for row in store.query('SELECT id FROM eval_attempts ORDER BY created_at,id'):
            data = eval_history.detail(store,row['id'])
            cls = TARGET_CLASS.get(data['source']['proposal']['target_kind'])
            if cls is None:
                unknown_eval_classes.append(row['id']); continue
            evals[cls].append({'id':row['id'],'outcome':outcome(data)['code']})
    result = []
    for cls in TARGET_CLASSES:
        selected = [r for r in subjects.values() if r['source']['target_class']==cls]
        judgments = []
        for item in selected:
            history = quality.judgments(store,item['source']['id']) if schema else []
            judgments.append({'judgment':history[-1] if history else None})
        counts = quality._counts(judgments,len(selected))
        states = {key:0 for key in ('available','not_available','unknown','not_observed')}
        checks = []
        for item in selected:
            actual = observations.get(item['source']['id'],[])
            checks.extend(actual)
            status = ('not_observed' if not actual else 'available' if any(r['status']=='available' for r in actual)
                      else 'unknown' if any(r['status']=='unknown' for r in actual) else 'not_available')
            states[status] += 1
        valid = [e for e in evals[cls] if e['outcome'] in {'rule_helped','rule_failed','inconclusive'}]
        result.append({'target_class':cls,'policy':policy['classes'][cls],
            'applied_revisions':len(selected),'rolled_back_revisions':sum(r['source']['id'] in rolled_back for r in selected),
            'availability':states if observed_schema else None,'latest_observation_at':max((r['observed_at'] for r in checks),default=None),
            'quality':counts if schema else None,
            'evaluations':{'attempts':len(evals[cls]),'comparable':len(valid),
                'passed':sum(e['outcome']=='rule_helped' for e in valid),'outcomes':evals[cls]} if eval_schema else None,
            'sample_advice':{'applied':10 if cls=='skill' else 20,'useful_fraction':0.9,
                             'maximum_rollbacks':None if cls=='skill' else 1} if cls!='hook' else None})
    return {'classes':result,'policy_available':policy['available'],'quality_available':schema,
            'coverage':{'application_events':population['application_events'],
                'excluded_applications':population['excluded_applications'],'unlinked_rollback_events':unlinked_rollbacks,
                'unknown_evaluation_classes':unknown_eval_classes},
            'meaning':'Applied counts are verified contribution revisions, including later rollbacks and reapplications. Availability means the latest retained check in at least one known copy, not current presence or session receipt. Human judgments and comparable evaluation attempts are separate. Advice never locks a class switch.'}
