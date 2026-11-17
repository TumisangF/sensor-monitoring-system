"""Loads and validates the alert limits and plausible ranges from a JSON config file."""
import json
import math
import os

DEFAULT_PATH = "/config/thresholds.json"
BOUNDS = ("alert_min", "alert_max", "valid_min", "valid_max")


def config_path():
    return os.getenv("THRESHOLDS_FILE", DEFAULT_PATH)


def _number(metric, key, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{metric}.{key} must be a finite number, got {value!r}")
    return float(value)


def load(path=None):
    """Return {metric: {unit, alert_min?, alert_max?, valid_min?, valid_max?}}.

    Raises ValueError with a clear message if the file is malformed or inconsistent.
    """
    path = path or config_path()
    with open(path) as handle:
        try:
            raw = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    metrics = raw.get("metrics") if isinstance(raw, dict) else None
    if not isinstance(metrics, dict) or not metrics:
        raise ValueError(f"{path} must contain a non-empty 'metrics' object")

    rules = {}
    for name, spec in metrics.items():
        if not isinstance(spec, dict):
            raise ValueError(f"{name} must be an object")
        rule = {"unit": str(spec.get("unit", ""))}
        for key in BOUNDS:
            if key in spec:
                rule[key] = _number(name, key, spec[key])
        if "alert_min" not in rule and "alert_max" not in rule:
            raise ValueError(f"{name} needs at least one of alert_min or alert_max")
        for lo, hi in (("alert_min", "alert_max"), ("valid_min", "valid_max")):
            if lo in rule and hi in rule and rule[lo] >= rule[hi]:
                raise ValueError(f"{name}.{lo} must be lower than {name}.{hi}")
        if "valid_min" in rule and "alert_min" in rule and rule["alert_min"] < rule["valid_min"]:
            raise ValueError(f"{name}.alert_min lies outside the valid range")
        if "valid_max" in rule and "alert_max" in rule and rule["alert_max"] > rule["valid_max"]:
            raise ValueError(f"{name}.alert_max lies outside the valid range")
        rules[name] = rule
    return rules
