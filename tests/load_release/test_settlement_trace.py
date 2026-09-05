"""Per-settlement traces remain bounded and preserve client behavior."""

import asyncio
import json

import httpx
import pytest


async def test_trace_bounds_poll_history_and_excludes_payloads():
    from tests.load_release.approval_diagnostics import SettlementTrace

    response = httpx.Response(200, json={"status": "paused_for_approval", "secret": "canary"})

    class Client:
        async def get(self, *args, **kwargs):
            return response

        async def post(self, *args, **kwargs):
            return response

    trace = SettlementTrace(Client())
    for _ in range(80):
        assert await trace.get('/runs/canary', headers={'secret': 'canary'}) is response
    assert await trace.post('/approvals/canary/resolve', json={'secret': 'canary'}) is response
    snapshot = trace.snapshot()
    assert snapshot['request_count'] == 81
    assert len(snapshot['recent']) == 64
    assert snapshot['last_state'] == 'paused_for_approval'
    assert snapshot['totals']['get']['count'] == 80
    assert snapshot['totals']['post']['count'] == 1
    assert snapshot['recent'][-1]['method'] == 'post'
    assert snapshot['recent'][-1]['http_status'] == 200
    assert snapshot['elapsed_ms'] >= snapshot['totals']['get']['elapsed_ms'] >= 0
    assert 'canary' not in json.dumps(snapshot)


@pytest.mark.parametrize('error', [ValueError('secret-canary'), asyncio.CancelledError()])
async def test_trace_preserves_exception_identity(error):
    from tests.load_release.approval_diagnostics import SettlementTrace

    class Client:
        async def get(self, *args, **kwargs):
            raise error

    trace = SettlementTrace(Client())
    with pytest.raises(type(error)) as caught:
        await trace.get('/runs/canary')
    assert caught.value is error
    row, = trace.snapshot()['recent']
    assert row['outcome'] == type(error).__name__
    assert 'canary' not in json.dumps(trace.snapshot())


@pytest.mark.parametrize('body', [{'status': 'secret-canary'}, ['secret-canary'], None])
async def test_trace_does_not_record_unknown_response_state(body):
    from tests.load_release.approval_diagnostics import SettlementTrace

    class Client:
        async def get(self, *args, **kwargs):
            return httpx.Response(200, json=body)

    trace = SettlementTrace(Client())
    await trace.get('/runs/canary')
    assert trace.snapshot()['last_state'] is None
    assert 'canary' not in json.dumps(trace.snapshot())


async def test_install_attaches_only_failing_settlement_timeline(tmp_path, monkeypatch):
    from tests.load_release import approval_diagnostics, workload_probe

    error = AssertionError('secret-canary')
    original_targets = []

    class Client:
        async def get(self, *args, **kwargs):
            await asyncio.sleep(0)
            return httpx.Response(200, json={'status': 'running'})

    client = Client()
    target = workload_probe.Target(scope=None, client=client)

    async def settle(observed, profile, sequence, run_id, started):
        assert observed is not target
        original_targets.append(target.client)
        await observed.client.get('/runs/' + run_id)
        if sequence == 1:
            await observed.client.get('/runs/' + run_id)
            raise error
        return ['unchanged']

    async def database(self, dsn):
        return []

    monkeypatch.setattr(workload_probe, '_settle_run', settle)
    monkeypatch.setattr(approval_diagnostics.Diagnostics, 'database_waits', database)
    path = tmp_path / 'trace.jsonl'
    approval_diagnostics.install(monkeypatch, path, 'unused')
    results = await asyncio.gather(
        workload_probe._settle_run(target, 'overload', 0, 'canary', 0),
        workload_probe._settle_run(target, 'overload', 1, 'canary', 0),
        return_exceptions=True,
    )
    assert results[0] == ['unchanged']
    assert results[1] is error
    assert original_targets == [client, client]
    assert target.client is client
    records = [json.loads(line) for line in path.read_text().splitlines()]
    row = next(record for record in records if record['operation'] == 'settle_failure')
    assert row['sequence'] == 1
    assert row['settlement']['request_count'] == 2
    assert row['settlement']['last_state'] == 'running'
    assert 'canary' not in path.read_text()


@pytest.mark.parametrize('state', [
    'queued', 'running', 'paused_for_approval', 'waiting_interrupt', 'succeeded',
    'failed', 'terminated_by_policy', 'terminated_by_loop_guard', 'dead_letter',
])
async def test_trace_covers_public_status_contract(state):
    from tests.load_release.approval_diagnostics import SettlementTrace
    from zeroth.service.api.run_api import RunPublicStatus

    assert state in {item.value for item in RunPublicStatus}

    class Client:
        async def get(self, *args, **kwargs):
            return httpx.Response(200, json={'status': state})

    trace = SettlementTrace(Client())
    await trace.get('/runs/example')
    assert trace.snapshot()['last_state'] == state
    assert trace.snapshot()['recent'][-1]['state'] == state


async def test_unknown_state_clears_previous_observation():
    from tests.load_release.approval_diagnostics import SettlementTrace

    states = iter(['paused_for_approval', 'secret-canary'])

    class Client:
        async def get(self, *args, **kwargs):
            return httpx.Response(200, json={'status': next(states)})

    trace = SettlementTrace(Client())
    await trace.get('/runs/example')
    await trace.get('/runs/example')
    assert trace.snapshot()['last_state'] is None
    assert 'secret-canary' not in json.dumps(trace.snapshot())
