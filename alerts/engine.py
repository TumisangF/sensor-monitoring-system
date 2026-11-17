"""Pure alert episode state machine; no database dependency."""
from dataclasses import dataclass


def breach(value, rule):
    if 'alert_max' in rule and value > rule['alert_max']:
        return 'exceeds_maximum', rule['alert_max']
    if 'alert_min' in rule and value < rule['alert_min']:
        return 'below_minimum', rule['alert_min']
    return None


@dataclass
class Episode:
    start: dict
    condition: str
    threshold: float


class AlertState:
    def __init__(self, rule):
        self.rule = rule
        self.open = None

    def feed(self, reading):
        """Yield ('open'|'close', episode, reading) for state transitions.

        A switch between below-min and above-max closes the prior episode and opens another.
        """
        condition = breach(reading['value'], self.rule)
        if self.open and (condition is None or condition[0] != self.open.condition):
            previous = self.open
            self.open = None
            yield 'close', previous, reading
        if condition and self.open is None:
            self.open = Episode(reading, condition[0], condition[1])
            yield 'open', self.open, reading
