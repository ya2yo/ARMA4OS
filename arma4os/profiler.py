"""Small rule classifier; temporal smoothing belongs to the selector."""

from .contracts import Observation, Profile


class RuleProfiler:
    def __init__(self, max_age_ms: int = 5000):
        self.max_age_ms = max_age_ms

    def profile(self, observation: Observation, at_ms: int) -> Profile:
        features = {}
        for key in ["cpu.utilization", "cpu.pressure"]:
            m = observation.metrics.get(key)
            if m and m.unit == "fraction" and m.usable(at_ms, self.max_age_ms) and 0 <= m.value <= 1:
                features[key] = m.value
        if len(features) != 2:
            return Profile("unknown", 0.0, features, "required CPU metrics missing or stale")
        if features["cpu.utilization"] >= 0.7 and features["cpu.pressure"] >= 0.05:
            return Profile("cpu_contended", 0.95, features, "high utilization and CPU pressure")
        return Profile("uncontended", 0.9, features, "no calibrated CPU contention signature")
