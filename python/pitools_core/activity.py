# Local, pure activity state. No filesystem (except its own JSON data), network,
# process, or Pi UI access. Port of activity.ts with JS-exact semantics.
# Data and mix_slot are derived from dsh-working-activity 0.5.1 (BSD-3-Clause).
# Copyright (c) 2026, chimney (ccch1mneyyy); see data/activity/LICENSE.
from __future__ import annotations

import json
import math
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ACTIVITY_VERSION = '0.1.12'
_DATA = Path(__file__).resolve().parent.parent.parent / 'data' / 'activity'
PHRASES = json.loads((_DATA / 'phrases.json').read_text(encoding='utf-8'))
FRAME_DATA = json.loads((_DATA / 'frames.json').read_text(encoding='utf-8'))
# JSON object order equals JS Object.keys order only without integer-like keys.
if any(re.fullmatch(r'(0|[1-9][0-9]*)', name) for name in FRAME_DATA['presets']):
  raise ValueError('frame preset names must not be integer-like')
FRAME_NAMES = list(FRAME_DATA['presets'])
DEFAULT_ACTIVITY = {'enabled': True, 'frames': 'moon8', 'lang': 'zh', 'narrate': True, 'contract': True, 'phrases': True}
PHASES = ('idle', 'waiting', 'thinking', 'tool', 'done')

# JS \s and String.prototype.trim whitespace; Python's \s/isspace differ (﻿, \x1c-\x1f).
_WS = '\t\n\v\f\r    -     　﻿'
_SAFE_RE = re.compile('[\u0000-\u0008\u000b-\u001f\u007f-\u009f​-‏ -‮⁠-⁩]')
_OSC_RE = re.compile(r'\x1b\][\s\S]*?(?:\x07|\x1b\\)')
_CSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
_CTRL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]')
_WS_RUN_RE = re.compile(f'[{_WS}]+')
_TRIM_RE = re.compile(f'^[{_WS}]+|[{_WS}]+\\Z')
_NARRATION_RE = re.compile('(?:^|\n)⏵[ \t]*([^\n⏵]*)')
_BOUNDARY_RE = re.compile(f'[。．!?！？;；]|\\.(?=[{_WS}]|\\Z|[A-Z][a-z])')
_TRAILING_RE = re.compile('[。．.!！,，、;；]+\\Z')
_TOOL_PREFIX_RE = re.compile(r'^(?:functions|tools)\.')
_M32 = 0xFFFFFFFF


def safe_text(value) -> str:
  text = '' if value is None else value if isinstance(value, str) else js_string(value)
  text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\t', '    ')
  return _SAFE_RE.sub('', text)


def js_string(value) -> str:
  """String(value) for the JSON scalars the host may forward."""
  if value is None:
    return 'null'
  if value is True:
    return 'true'
  if value is False:
    return 'false'
  if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
    return str(int(value))
  return str(value)


def _js_trim(text: str) -> str:
  return _TRIM_RE.sub('', text)


def _to_int32(value) -> int:
  if isinstance(value, bool):
    value = int(value)
  if not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
    return 0
  return int(value) & _M32  # int() truncates toward zero, like ToInt32.


def mix_slot(seed, slot) -> int:
  """Upstream's deterministic 32-bit avalanche (Math.imul/>>> exact)."""
  h = ((_to_int32(seed) * 0x9E3779B1) ^ (_to_int32(slot) * 0x85EBCA6B)) & _M32
  h = ((h ^ (h >> 15)) * 0x2545F491) & _M32
  h = ((h ^ (h >> 13)) * 0x9E3779B1) & _M32
  return (h ^ (h >> 16)) & _M32


def pick(entries, seed, slot=0) -> str:
  return entries[mix_slot(seed, slot) % len(entries)] if entries else ''


def _floor(value) -> int:
  return math.floor(value)


def activity_duration(ms) -> str:
  seconds = _floor(max(0, ms) / 1000)
  if seconds < 60:
    return f'{seconds}s'
  if seconds < 3600:
    return f'{seconds // 60}m{seconds % 60}s'
  return f'{seconds // 3600}h{seconds % 3600 // 60}m'


def _fixed1(value) -> str:
  # Number.prototype.toFixed(1): exact binary value, ties pick the larger n.
  return str(Decimal(value).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP))


def fragment(value) -> str:
  text = '' if value is None else js_string(value) if not isinstance(value, str) else value
  text = _CTRL_RE.sub(' ', _CSI_RE.sub('', _OSC_RE.sub('', text)))
  return _js_trim(_WS_RUN_RE.sub(' ', safe_text(text)))


def _wide(point: int) -> bool:
  return point >= 0x1100 and (point <= 0x115f or 0x2e80 <= point <= 0xa4cf or 0xac00 <= point <= 0xd7a3
    or 0xf900 <= point <= 0xfaff or 0xfe10 <= point <= 0xfe6f or 0xff01 <= point <= 0xff60
    or 0xffe0 <= point <= 0xffe6 or 0x1f000 <= point <= 0x1faff or point >= 0x20000)


def cut_columns(text: str, budget: int) -> str:
  result, columns = [], 0
  for char in text:  # Code points, like JS for...of (lone surrogates stay single).
    point = ord(char)
    width = 0 if unicodedata.category(char).startswith('M') or point == 0x200d else 2 if _wide(point) else 1
    if columns + width > budget:
      break
    result.append(char)
    columns += width
  return ''.join(result)


def _utf16_tail(text: str, units: int):
  """JS text.slice(-units) plus text[start - 1], measured in UTF-16 code units."""
  data = text.encode('utf-16-le', 'surrogatepass')
  length = len(data) // 2
  start = max(0, length - units)
  tail = data[start * 2:].decode('utf-16-le', 'surrogatepass')
  before = data[(start - 1) * 2:start * 2].decode('utf-16-le', 'surrogatepass') if start > 0 else None
  return start, tail, before


def extract_narration(visible_text):
  text = '' if visible_text is None else visible_text if isinstance(visible_text, str) else js_string(visible_text)
  start, tail, before = _utf16_tail(text, 300)
  candidate = None
  for match in _NARRATION_RE.finditer(tail):
    # Slicing the rolling buffer must not turn a mid-line marker into line-start.
    if match.start() == 0 and start > 0 and before != '\n':
      continue
    candidate = match.group(1)
  if candidate is None:
    return None
  clean = fragment(candidate)
  boundary = _BOUNDARY_RE.search(clean)
  if boundary:
    clean = clean[:boundary.start() + 1]
  clean = _js_trim(_TRAILING_RE.sub('', cut_columns(clean, 80)))
  return clean or None


def narration_contract(lang: str) -> str:
  return PHRASES['uiStrings']['en' if lang == 'en' else 'zh']['narrate-instruction']


def _compile_action(match: dict):
  pattern, flags = match['pattern'], match['flags']
  # Only the simple anchored ASCII alternations shipped upstream are translated.
  # re.ASCII + IGNORECASE matches JS non-unicode /i (no ſ/K folding); \Z is JS $.
  if flags != 'i' or not re.fullmatch(r'\^\([a-z_|-]+\)\$?', pattern):
    raise ValueError(f'unsupported tool action pattern: {pattern!r}/{flags}')
  return re.compile(pattern[:-1] + r'\Z' if pattern.endswith('$') else pattern, re.IGNORECASE | re.ASCII)


# Deliberately separate ordered regex tables for zh/en; do not use ByName.
_ACTION_TABLES = {lang: [(_compile_action(row['match']), row['actions']) for row in PHRASES['toolAction'][lang]] for lang in ('zh', 'en')}


def tool_action(name, lang: str, seed, slot) -> str:
  key = 'en' if lang == 'en' else 'zh'
  plain = _TOOL_PREFIX_RE.sub('', fragment(name))
  row = next((actions for regex, actions in _ACTION_TABLES[key] if regex.search(plain)), None)
  return pick(row if row is not None else PHRASES['toolFallback'][key], seed, slot)


def normalize_activity(value=None) -> dict:
  out = dict(DEFAULT_ACTIVITY)
  if not isinstance(value, dict):
    return out
  for key in ('enabled', 'narrate', 'contract', 'phrases'):
    if isinstance(value.get(key), bool):
      out[key] = value[key]
  frames = value.get('frames')
  if isinstance(frames, str) and (frames == 'random' or frames in FRAME_DATA['presets']):
    out['frames'] = frames
  if value.get('lang') in ('zh', 'en', 'auto'):
    out['lang'] = value['lang']
  return out


def _finite(value) -> bool:
  return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _num(value) -> str:
  return js_string(value) if isinstance(value, float) else str(value)


class ActivityState:
  """Same state machine as activity.ts. Wall-clock values come from the host.

  `local` is the host's local calendar for an event time: (year, month 1-12,
  day, weekday 0=Sunday like JS getDay, hour). Python never reads its own clock
  or time zone, so weekend/holiday/night choices cannot drift from Node.
  `locale` is the host's Intl locale, used only for lang=auto.
  """

  def __init__(self, config=None, locale: str = ''):
    self.locale = locale
    self.config = normalize_activity(config)
    self.preset_name = 'moon8'
    self._clear()
    self.configure(self.config)

  def _clear(self):
    self.phase = 'idle'
    self.started_at = 0
    self.started_local = None
    self.phase_at = 0
    self.thinking_phases = 0
    self.active: dict[str, dict] = {}
    self.completed = 0
    self.first_tool_at = None
    self.last_tool = None
    self.narration = ''
    self.last_narration = ''
    self.last_chunk_at = None
    self.done_text = ''
    self.end_at = None
    self.failure = False
    self.output_tokens = 0
    self.seen_messages: set[str] = set()

  def configure(self, config):
    self.config = normalize_activity(config)
    self.preset_name = FRAME_NAMES[mix_slot(self.started_at, 123) % len(FRAME_NAMES)] if self.config['frames'] == 'random' else self.config['frames']

  def reset(self, config=None):
    self._clear()
    self.configure(self.config if config is None else config)

  @property
  def live(self) -> bool:
    return self.phase not in ('idle', 'done')

  @property
  def lang(self) -> str:
    if self.config['lang'] != 'auto':
      return self.config['lang']
    return 'en' if self.locale.lower().startswith('en') else 'zh'

  def set_phase(self, phase: str, now):
    if phase == self.phase:
      return
    if phase == 'thinking':
      self.thinking_phases += 1
    self.phase = phase
    self.phase_at = now

  def begin(self, now, local=None):
    self.reset()
    self.started_at = now
    self.started_local = local
    self.set_phase('waiting', now)
    self.configure(self.config)

  def turn_start(self, now, local=None):
    if not self.live:
      self.begin(now, local)
    elif not self.active:
      self.stream_start(now, local)

  def stream_start(self, now, local=None):
    if not self.live:
      self.begin(now, local)
    self.narration = ''
    self.last_chunk_at = None
    if not self.active:
      self.set_phase('waiting', now)

  def delta(self, kind: str, visible_text, now, local=None):
    if not self.live:
      self.begin(now, local)
    if not self.active:
      self.set_phase('thinking', now)
    self.last_chunk_at = now
    if not self.config['narrate'] or not kind.startswith('text'):
      return
    narration = extract_narration(visible_text)
    if narration:
      self.narration = self.last_narration = narration

  def message_end(self, text, message_key, output_tokens, now, local=None):
    """`text` is the host's '\\n'.join of the message's text blocks; dedupe by message identity key."""
    if not self.live:
      self.begin(now, local)
    if not self.active:
      self.set_phase('thinking', now)
    narration = extract_narration(text) if self.config['narrate'] else None
    if narration:
      self.narration = self.last_narration = narration
      self.last_chunk_at = now
    if message_key not in self.seen_messages:
      self.seen_messages.add(message_key)
      if _finite(output_tokens) and output_tokens >= 0:
        self.output_tokens += output_tokens

  def tool_start(self, tool_id: str, name, detail, now, local=None):
    """`detail` is the host-selected raw command/path/query/url string, or None."""
    if not self.live:
      self.begin(now, local)
    if tool_id in self.active:
      return
    if self.first_tool_at is None:
      self.first_tool_at = now
    self.active[tool_id] = {
      'id': tool_id, 'name': fragment(name),
      'action': tool_action(name, self.lang, self.started_at, self.completed + len(self.active)),
      'detail': cut_columns(fragment(detail), 40) if isinstance(detail, str) else '', 'startedAt': now,
    }
    self.set_phase('tool', now)

  def tool_end(self, tool_id: str, error, now):
    tool = self.active.get(tool_id)
    if tool is None:
      return
    self.last_tool = {**tool, 'endedAt': now, 'error': error}
    self.failure = bool(error)
    self.completed += 1
    del self.active[tool_id]
    if not self.active:
      self.set_phase('thinking', now)

  def finish(self, now, reason=''):
    if self.phase in ('idle', 'done'):
      return
    self.end_at = now
    lang = self.lang
    if reason == 'aborted':
      prefix = '已中断' if lang == 'zh' else 'Interrupted'
    elif reason == 'error':
      prefix = '请求失败' if lang == 'zh' else 'Request failed'
    elif self.config['phrases']:
      prefix = pick(PHRASES['fail' if self.failure else 'done'][lang], self.started_at, 11)
    else:
      prefix = PHRASES['uiStrings'][lang]['done-prefix']
    self.failure = self.failure or reason == 'error'
    tokens = self.output_tokens
    usage = f" · ↓ {_fixed1(tokens / 1000) + 'k' if tokens >= 1000 else _num(tokens)} tokens" if tokens else ''
    self.done_text = f"{prefix} · {self.completed} {'工具' if lang == 'zh' else 'tools'} · {'总' if lang == 'zh' else 'total '}{activity_duration(now - self.started_at)}{usage}"
    self.active.clear()
    self.set_phase('done', now)

  def _rare(self) -> bool:
    return self.phase == 'thinking' and self.thinking_phases == 1 and mix_slot(self.started_at, 0x5EED) % 150 == 0

  def phrase(self, now) -> str:
    lang = self.lang
    if not self.config['phrases']:
      return PHRASES['uiStrings'][lang]['waiting-label' if self.phase == 'waiting' else 'thinking-label']
    rare = self._rare()
    rotate = 7500 if rare else 4000
    slot = max(0, _floor((now - self.phase_at) / rotate))
    if self.phase == 'waiting':
      return pick(PHRASES['waiting'][lang], self.started_at, slot)
    local = self.started_local
    if self.thinking_phases == 1 and slot == 0:
      year, month, day, weekday, _hour = local
      mmdd = f'{month:02d}-{day:02d}'
      holiday = PHRASES['lunarNewYear'][lang] if PHRASES['lunarNewYearDays'].get(f'{year}-{mmdd}') else PHRASES['holiday'][lang].get(mmdd)
      if holiday:
        return pick(holiday, self.started_at, slot)
      if rare:
        return pick(PHRASES['rare'][lang], self.started_at, slot)
      if weekday in (0, 6):
        return pick(PHRASES['weekend'][lang], self.started_at, slot)
    if rare:
      return pick(PHRASES['rare'][lang], self.started_at, slot)
    elapsed = self.phase_at + slot * rotate - self.started_at
    tier = next((row['pool'] for row in reversed(PHRASES['thinkingTiers'][lang]) if elapsed >= row['atMs']), None)
    night = local[4] < 6
    return pick(tier if tier is not None else [*PHRASES['thinking'][lang], *(PHRASES['thinkingNight'][lang] if night else [])], self.started_at, slot)

  def frame(self, now) -> str:
    preset = FRAME_DATA['presets'][self.preset_name]
    frames = preset['frames']
    return frames[_floor(max(0, now - self.started_at) / preset['intervalMs']) % len(frames)] if frames else ''

  def line(self, now) -> str:
    if not self.config['enabled']:
      return ''
    lang = self.lang
    if self.phase == 'idle':
      frames = FRAME_DATA['presets'][self.preset_name]['frames']
      return _js_trim(f"{frames[0] if frames else ''} ⏵ {'待机中 · 等待任务' if lang == 'zh' else 'Idle · ready for a task'}")
    if self.phase == 'done':
      narration = self.last_narration + ' · ' if self.config['narrate'] and self.last_narration else ''
      return _js_trim(f"{self.frame(self.started_at if self.end_at is None else self.end_at)} ⏵ {narration}{self.done_text}")
    frame = self.frame(now)
    narration = f'⏵ {self.narration}' if self.config['narrate'] and self.narration and self.last_chunk_at is not None and now - self.last_chunk_at <= 5000 else ''
    if self.active:
      tool = list(self.active.values())[-1]
      opening = pick(PHRASES['toolOpening'][lang], self.started_at, 0) + ' · ' if self.config['phrases'] and self.first_tool_at is not None and now - self.first_tool_at < 2500 else ''
      parallel = f" · {len(self.active)} {'并行' if lang == 'zh' else 'parallel'}" if len(self.active) > 1 else ''
      text = f"{narration + ' · ' if narration else ''}{opening}{tool['action']} {tool['detail'] or tool['name']} · {activity_duration(now - tool['startedAt'])}{parallel}"
    elif self.config['phrases'] and self.last_tool and now - self.last_tool['endedAt'] < 2500:
      ms = self.last_tool['endedAt'] - self.last_tool['startedAt']
      text = f"✓ {self.last_tool['action']} {self.last_tool['detail'] or self.last_tool['name']} · {str(_floor(ms)) + 'ms' if ms < 1000 else activity_duration(ms)}"
    else:
      text = f"{narration or self.phrase(now)} · {'总' if lang == 'zh' else 'total '}{activity_duration(now - self.started_at)}"
    return _js_trim(f'{frame} {text}')

  def next_wake_at(self, now):
    if not self.config['enabled'] or not self.live:
      return None
    preset = FRAME_DATA['presets'][self.preset_name]

    def following(anchor, interval):
      return anchor + (_floor(max(0, now - anchor) / interval) + 1) * interval

    tool = list(self.active.values())[-1] if self.active else None
    candidates = [following(self.started_at if tool is None else tool['startedAt'], 1000), following(self.phase_at, 7500 if self._rare() else 4000)]
    if len(preset['frames']) > 1:
      candidates.append(following(self.started_at, max(16, preset['intervalMs'])))
    if self.last_chunk_at is not None and self.narration:
      candidates.append(self.last_chunk_at + 5001)
    if self.last_tool:
      candidates.append(self.last_tool['endedAt'] + 2500)
    if self.first_tool_at is not None:
      candidates.append(self.first_tool_at + 2500)
    future = [at for at in candidates if at > now]
    return min(future) if future else None

  def restore_snapshot(self, data: dict):
    """Adopt the TS core's exported state, so a mid-task switch shows no fake idle/progress."""
    self._clear()
    self.config = normalize_activity(data.get('config'))
    phase = data.get('phase')
    self.phase = phase if phase in PHASES else 'idle'
    self.started_at = data.get('startedAt', 0)
    local = data.get('startedLocal')
    self.started_local = tuple(local) if isinstance(local, list) and len(local) == 5 else None
    self.phase_at = data.get('phaseAt', 0)
    self.thinking_phases = data.get('thinkingPhases', 0)
    self.active = {tool['id']: dict(tool) for tool in data.get('active') or []}
    self.completed = data.get('completed', 0)
    self.first_tool_at = data.get('firstToolAt')
    self.last_tool = data.get('lastTool')
    self.narration = data.get('narration', '')
    self.last_narration = data.get('lastNarration', '')
    self.last_chunk_at = data.get('lastChunkAt')
    self.done_text = data.get('doneText', '')
    self.end_at = data.get('endAt')
    self.failure = bool(data.get('failure'))
    self.output_tokens = data.get('outputTokens', 0)
    self.seen_messages = set(data.get('seenMessageKeys') or [])
    preset = data.get('presetName')
    self.preset_name = preset if preset in FRAME_DATA['presets'] else 'moon8'
