"""Bounded timing-only records for opt-in live subtitle measurements."""

from collections import OrderedDict
import math
import time


TRACE_LIMIT = 128
MAX_CLIENT_LATENCY_MS = 600_000


def clock_ms():
    """Return high-resolution monotonic time in milliseconds."""
    return time.perf_counter() * 1000


def clock_resolution_ms():
    return time.get_clock_info('perf_counter').resolution * 1000


def trace_durations(trace):
    start = trace.get('speech_started_ms')
    values = {
        'speech_to_segment_ms': _duration(trace.get('segmented_ms'), start),
        'queue_ms': _duration(trace.get('dequeued_ms'), trace.get('enqueued_ms')),
        'lock_ms': _duration(trace.get('inference_started_ms'), trace.get('dequeued_ms')),
        'asr_ms': _duration(trace.get('asr_finished_ms'), trace.get('inference_started_ms')),
        'translation_ms': _duration(trace.get('translation_finished_ms'), trace.get('translation_started_ms')),
        'server_total_ms': _duration(trace.get('result_ready_ms'), start),
    }
    return {key: round(value, 3) if value is not None else None for key, value in values.items()}


def _duration(end, start):
    if end is None or start is None:
        return None
    return max(0.0, end - start)


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class SessionMetrics:
    """Keep aggregate counters and at most the newest 128 timing-only segments."""

    def __init__(self):
        self.counters = {
            'segmented': 0,
            'emitted': 0,
            'dropped_queue': 0,
            'skipped_stale': 0,
            'empty_asr': 0,
            'asr_failed': 0,
            'dropped_audio_frames': 0,
            'dropped_audio_samples': 0,
        }
        self.events = OrderedDict()

    def record(self, trace, outcome):
        event = {
            'segment_id': trace['segment_id'],
            'outcome': outcome,
            'source': trace.get('source', 'live'),
            'timestamps': {key: value for key, value in trace.items() if key.endswith('_ms') and finite_number(value)},
            'timings': trace_durations(trace),
        }
        self.events[trace['segment_id']] = event
        self.events.move_to_end(trace['segment_id'])
        while len(self.events) > TRACE_LIMIT:
            self.events.popitem(last=False)
        return event

    def rendered(self, message, sent_at_ms):
        segment_id = message.get('segment_id')
        if not isinstance(segment_id, int) or isinstance(segment_id, bool) or segment_id < 1:
            return None
        event = self.events.get(segment_id)
        if not event or event['outcome'] != 'emitted':
            return None

        browser_received = message.get('browser_received_server_ms')
        first_readable = message.get('first_readable_server_ms')
        final = message.get('final_server_ms')
        uncertainty = message.get('clock_uncertainty_ms')
        if not all(finite_number(value) for value in (browser_received, first_readable, final, uncertainty)):
            return None
        if uncertainty < 0 or uncertainty > MAX_CLIENT_LATENCY_MS:
            return None
        if not (sent_at_ms - uncertainty <= browser_received <= first_readable <= final):
            return None
        speech_started = event['timestamps'].get('speech_started_ms', final)
        first_readable_delta = first_readable - speech_started
        final_delta = final - speech_started
        if min(first_readable_delta, final_delta) < -uncertainty:
            return None
        if final_delta > MAX_CLIENT_LATENCY_MS:
            return None

        delivery_delta = browser_received - sent_at_ms
        event['outcome'] = 'rendered'
        event['render_method'] = (
            message.get('render_method')
            if message.get('render_method') in ('fully_visible_animation_frame', 'transition_end')
            else 'unknown'
        )
        event['timestamps'].update({
            'browser_received_ms': round(browser_received, 3),
            'first_readable_ms': round(first_readable, 3),
            'final_ms': round(final, 3),
            'clock_uncertainty_ms': round(uncertainty, 3),
        })
        event['timings'].update({
            'delivery_ms': round(max(0.0, delivery_delta), 3),
            'delivery_lower_ms': round(max(0.0, delivery_delta - uncertainty), 3),
            'delivery_upper_ms': round(max(0.0, delivery_delta + uncertainty), 3),
            'display_ms': round(first_readable - browser_received, 3),
            'first_readable_ms': round(max(0.0, first_readable_delta), 3),
            'first_readable_lower_ms': round(max(0.0, first_readable_delta - uncertainty), 3),
            'first_readable_upper_ms': round(max(0.0, first_readable_delta + uncertainty), 3),
            'final_ms': round(max(0.0, final_delta), 3),
            'final_lower_ms': round(max(0.0, final_delta - uncertainty), 3),
            'final_upper_ms': round(max(0.0, final_delta + uncertainty), 3),
            'clock_uncertainty_ms': round(uncertainty, 3),
        })
        return event

    def increment_dropped_audio(self, frames, samples):
        if not isinstance(frames, int) or isinstance(frames, bool) or not 0 < frames <= 100_000:
            return False
        if not isinstance(samples, int) or isinstance(samples, bool) or not 0 < samples <= 100_000_000:
            return False
        self.counters['dropped_audio_frames'] += frames
        self.counters['dropped_audio_samples'] += samples
        return True

    def payload(self):
        return {'counters': dict(self.counters), 'events': list(self.events.values())}
