import threading
import time


class EventTriggers:
    def __init__(self):
        self.events = {
            "cheques": threading.Event(),
            "incoming": threading.Event(),
            "outgoing": threading.Event(),
        }
        self.last_trigger_time = {
            key: 0.0 for key in self.events
        }
        self.debounce_seconds = 1.0  # Время между триггерами

    def trigger_once(self, name):
        now = time.monotonic()
        if now - self.last_trigger_time[name] >= self.debounce_seconds:
            self.events[name].set()
            self.last_trigger_time[name] = now

    def wait_and_clear(self, name, timeout=0.5):
        event = self.events[name]
        triggered = event.wait(timeout)
        if triggered:
            event.clear()
        return triggered
