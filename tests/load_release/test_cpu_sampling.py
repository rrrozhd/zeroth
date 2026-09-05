"""CPU sampling retains code metadata and leaves signal ownership intact."""
import json
import subprocess
import sys


def test_real_cpu_sampling_is_private_and_restores_signal_after_error():
    script = '''
import json, signal, time
from tests.load_release.cpu_sampling import CPUSampler
previous = signal.getsignal(signal.SIGPROF)
sampler = CPUSampler()
error = ValueError("exception-secret-canary")
def busy():
    secret = "local-secret-canary"
    until = time.process_time() + .15
    while time.process_time() < until:
        pass
    return secret
try:
    with sampler:
        busy()
        raise error
except ValueError as caught:
    assert caught is error
assert signal.getsignal(signal.SIGPROF) == previous
assert signal.getitimer(signal.ITIMER_PROF) == (0, 0)
print(json.dumps(sampler.snapshot()))
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)
    assert row['status'] == 'sampled'
    assert row['total'] > 0
    assert any(frame['function'] == 'busy' for stack in row['stacks'] for frame in stack['frames'])
    assert 'secret-canary' not in result.stdout


def test_existing_signal_owner_is_preserved():
    script = '''
import signal
from tests.load_release.cpu_sampling import CPUSampler
handler = lambda *args: None
signal.signal(signal.SIGPROF, handler)
signal.setitimer(signal.ITIMER_PROF, 60, 60)
with CPUSampler() as sampler:
    assert sampler.snapshot()["status"] == "existing-owner"
assert signal.getsignal(signal.SIGPROF) is handler
assert signal.getitimer(signal.ITIMER_PROF)[0] > 59
signal.setitimer(signal.ITIMER_PROF, 0)
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_sample_storage_and_stack_depth_are_bounded():
    from types import SimpleNamespace
    from tests.load_release.cpu_sampling import CPUSampler

    sampler = CPUSampler()
    frame = None
    for _ in range(40):
        frame = SimpleNamespace(f_code=SimpleNamespace(co_filename='code.py', co_name='work'),
                                f_lineno=1, f_back=frame)
    for line in range(600):
        frame.f_lineno = line
        sampler.sample(None, frame)
    row = sampler.snapshot()
    assert row['total'] == 600
    assert row['dropped'] == 88
    assert len(row['stacks']) == 512
    assert all(len(stack['frames']) == 24 for stack in row['stacks'])
