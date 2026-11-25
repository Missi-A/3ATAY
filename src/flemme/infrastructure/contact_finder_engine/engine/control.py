"""Contrôle coopératif indépendant de Qt ; pause/arrêt entre les lectures réseau."""

import threading


class Control:
    def __init__(self, check_stop=None):
        self.cancelled = threading.Event()
        self.running = threading.Event()
        self.running.set()
        self.check_stop = check_stop

    def pause(self):
        self.running.clear()

    def resume(self):
        self.running.set()

    def cancel(self):
        self.cancelled.set()
        self.running.set()

    def check(self):
        if self.check_stop is not None:
            self.check_stop()
        while not self.running.wait(0.1):
            if self.cancelled.is_set():
                raise InterruptedError("Recherche arrêtée ; reprise disponible.")
        if self.cancelled.is_set():
            raise InterruptedError("Recherche arrêtée ; reprise disponible.")

    def wait(self, seconds):
        while seconds > 0:
            self.check()
            step = min(seconds, 0.1)
            if self.cancelled.wait(step):
                self.check()
            seconds -= step
