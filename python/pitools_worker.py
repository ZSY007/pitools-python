# pitools private worker: JSONL over its own stdin/stdout, spawned only by the
# pitools host adapter when the user opts into the Python core. It holds activity
# state only. It never opens network ports, runs commands, reads sessions or
# credentials, or writes files; stdout carries protocol frames only.
import json
import math
import sys
from pathlib import Path

if sys.version_info < (3, 11):
  sys.stdout.write(json.dumps({'protocol': 1, 'type': 'fatal', 'category': 'python_too_old'}) + '\n')
  sys.stdout.flush()
  sys.exit(3)

# Started with -I (isolated): the script directory is not on sys.path by default.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pitools_core import CORE_VERSION, PROTOCOL  # noqa: E402
from pitools_core.activity import PHASES, ActivityState  # noqa: E402

MAX_FRAME = 1024 * 1024
MAX_TEXT = 65536
MAX_BATCH_EVENTS = 64
MAX_BATCH_BYTES = 64 * 1024


class ProtocolError(Exception):
  pass


def _reject_constant(name):
  raise ProtocolError(f'non_finite_{name}')


def _int(message, key, optional=False):
  value = message.get(key)
  if value is None and optional:
    return None
  if isinstance(value, bool) or not isinstance(value, int) or not -2**53 < value < 2**53:
    raise ProtocolError(f'bad_{key}')
  return value


def _text(message, key, optional=False):
  value = message.get(key)
  if value is None and optional:
    return None
  if not isinstance(value, str) or len(value) > MAX_TEXT:
    raise ProtocolError(f'bad_{key}')
  return value


def _local(message):
  value = message.get('local')
  if not isinstance(value, list) or len(value) != 5 or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
    raise ProtocolError('bad_local')
  return tuple(value)


class Worker:
  def __init__(self, write):
    self.write = write
    self.ready = False
    self.state = ActivityState()
    self.epoch = -1
    self.seq = -1
    self.ignored = 0

  def send(self, payload):
    payload['protocol'] = PROTOCOL
    self.write(json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii') + b'\n')

  def hello(self, message):
    if message.get('version') != CORE_VERSION:
      self.send({'type': 'fatal', 'category': 'version_mismatch', 'version': CORE_VERSION})
      return False
    locale = message.get('locale')
    self.state = ActivityState(locale=locale if isinstance(locale, str) else '')
    self.ready = True
    self.send({'type': 'ready', 'version': CORE_VERSION, 'python': '.'.join(map(str, sys.version_info[:3])), 'features': ['event_batch']})
    return True

  def event(self, message):
    epoch, seq, op = _int(message, 'epoch'), _int(message, 'seq'), message.get('op')
    if op in ('reset', 'snapshot'):
      if epoch <= self.epoch:
        self.ignored += 1
        return
      self.epoch, self.seq = epoch, -1
    elif epoch != self.epoch or seq <= self.seq:
      self.ignored += 1  # Old branch/session or replayed frame: never apply.
      return
    self.seq = seq
    state = self.state
    if op == 'reset':
      state.reset(message.get('config'))
      return
    if op == 'snapshot':
      snapshot = message.get('state')
      if not isinstance(snapshot, dict):
        raise ProtocolError('bad_state')
      state.restore_snapshot(snapshot)
      return
    if op == 'configure':
      state.configure(message.get('config'))
      return
    now, local = _int(message, 'now'), _local(message)
    if op == 'begin':
      state.begin(now, local)
    elif op == 'turnStart':
      state.turn_start(now, local)
    elif op == 'streamStart':
      state.stream_start(now, local)
    elif op == 'delta':
      state.delta(_text(message, 'kind'), _text(message, 'text'), now, local)
    elif op == 'messageEnd':
      tokens = message.get('outputTokens')
      if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, (int, float))):
        raise ProtocolError('bad_outputTokens')
      state.message_end(_text(message, 'text'), _text(message, 'messageKey'), tokens, now, local)
    elif op == 'toolStart':
      state.tool_start(_text(message, 'id'), _text(message, 'name'), _text(message, 'detail', optional=True), now, local)
    elif op == 'toolEnd':
      state.tool_end(_text(message, 'id'), message.get('error') is True, now)
    elif op == 'finish':
      reason = message.get('reason')
      state.finish(now, reason if isinstance(reason, str) else '')
    else:
      raise ProtocolError('unknown_op')

  def event_batch(self, message):
    events = message.get('events')
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_BATCH_EVENTS:
      raise ProtocolError('bad_batch')
    # Validate the complete batch before changing state. No arbitrary nested
    # frames, commands or control events: only the existing delta contract.
    for event in events:
      if not isinstance(event, dict) or event.get('type') != 'event' or event.get('op') != 'delta':
        raise ProtocolError('bad_batch_event')
      for key in ('epoch', 'seq', 'now'):
        _int(event, key)
      _local(event)
      _text(event, 'kind')
      _text(event, 'text')
    for event in events:
      self.event(event)

  def view(self, message):
    # Always answer (with the worker's own epoch): the host drops stale epochs, and an
    # unanswered request would otherwise look like a hung worker.
    request, now = _int(message, 'id'), _int(message, 'now')
    _int(message, 'epoch')
    state = self.state
    wake = state.next_wake_at(now)
    assert state.phase in PHASES
    self.send({'type': 'view', 'epoch': self.epoch, 'id': request, 'line': state.line(now), 'phase': state.phase,
      'failure': bool(state.failure), 'live': state.live, 'nextWakeAt': wake if wake is None or math.isfinite(wake) else None})

  def handle(self, raw: bytes):
    message = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant)
    if not isinstance(message, dict) or message.get('protocol') != PROTOCOL:
      raise ProtocolError('protocol_mismatch')
    kind = message.get('type')
    if kind == 'event_batch' and len(raw) > MAX_BATCH_BYTES:
      raise ProtocolError('batch_too_large')
    if kind == 'hello':
      return self.hello(message)
    if not self.ready:
      raise ProtocolError('not_ready')
    if kind == 'event':
      self.event(message)
    elif kind == 'event_batch':
      self.event_batch(message)
    elif kind == 'view':
      self.view(message)
    else:
      raise ProtocolError('unknown_type')
    return True


def main():
  stdin, stdout = sys.stdin.buffer, sys.stdout.buffer

  def write(data: bytes):
    stdout.write(data)
    stdout.flush()

  worker = Worker(write)
  while True:
    raw = stdin.readline(MAX_FRAME + 2)
    if not raw:
      return 0  # Host closed the pipe: exit, never linger.
    if not raw.endswith(b'\n') and len(raw) > MAX_FRAME:
      worker.send({'type': 'fatal', 'category': 'frame_too_large'})
      return 2
    # Frames are split on LF only; U+2028/U+2029 inside JSON strings are data.
    raw = raw[:-1] if raw.endswith(b'\n') else raw
    raw = raw[:-1] if raw.endswith(b'\r') else raw
    if len(raw) > MAX_FRAME:
      worker.send({'type': 'fatal', 'category': 'frame_too_large'})
      return 2
    if not raw.strip():
      continue
    try:
      if worker.handle(raw) is False:
        return 3
    except (ProtocolError, ValueError, UnicodeDecodeError) as error:
      category = str(error) if isinstance(error, ProtocolError) else 'bad_json'
      worker.send({'type': 'error', 'category': category, 'epoch': worker.epoch})
    except Exception as error:  # Report the category only; never echo content.
      worker.send({'type': 'error', 'category': f'internal_{type(error).__name__}', 'epoch': worker.epoch})


if __name__ == '__main__':
  sys.exit(main())
