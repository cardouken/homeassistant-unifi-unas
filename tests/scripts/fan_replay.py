import random

# only random.random() is used, its sequence for a seed is stable across Python versions

MODES = ("target_temp", "auto", "unas_managed", "unas_managed")


class Gen:
    def __init__(self, seed):
        self.rng = random.Random(seed)

    def chance(self, p):
        return self.rng.random() < p

    def between(self, low, high):
        return low + int(self.rng.random() * (high - low + 1))

    def pick(self, items):
        return items[int(self.rng.random() * len(items))]


def scenario(seed, ticks):
    g = Gen(seed)
    t = 1_700_000_000 + seed * 100_000
    drives = [g.between(36, 46) * 10 for _ in range(g.between(1, 7))]
    settings = {
        "mode": "target_temp",
        "min_temp": g.pick((35, 38, 40)),
        "max_temp": g.pick((45, 48, 50)),
        "min_fan": g.pick((51, 64, 80)),
        "max_fan": g.pick((200, 230, 255)),
        "target_temp": g.between(40, 45),
        "temp_metric": g.pick(("max", "avg")),
        "response_speed": g.pick(("relaxed", "balanced", "aggressive")),
    }
    sample_every = g.pick((5, 6, 30))
    since_sample = sample_every
    hw = g.between(51, 160)
    rows = []
    for _ in range(ticks):
        if not g.chance(0.004):
            t += g.pick((1, 1, 1, 1, 1, 2, 2, 3))
        else:
            t += g.pick((12, 40, 299, 301, 900))
        drift = 0
        if g.chance(0.01):
            drift = g.pick((-30, -15, 15, 30, 60))
        drives = [min(max(d + drift + g.between(-2, 2), 250), 640) for d in drives]

        if g.chance(0.012):
            settings["mode"] = g.pick(MODES + ("128", "0", "255", "300", "64"))
        elif settings["mode"] != "target_temp" and g.chance(0.03):
            settings["mode"] = "target_temp"
        if g.chance(0.004):
            settings["target_temp"] = g.between(38, 47)
        if g.chance(0.002):
            settings["temp_metric"] = g.pick(("max", "avg"))
        if g.chance(0.002):
            settings["response_speed"] = g.pick(("relaxed", "balanced", "aggressive"))
        if g.chance(0.002):
            settings["min_fan"] = g.pick((51, 64, 80))
        if g.chance(0.002):
            settings["max_fan"] = g.pick((200, 230, 255))

        new_hw = None
        if settings["mode"] == "unas_managed" and g.chance(0.2):
            hw = g.between(40, 200)
            new_hw = hw

        since_sample += 1
        sample = 0
        if since_sample >= sample_every:
            sample = 1
            since_sample = 0

        temps = sorted((d // 10 for d in drives), reverse=True)
        rows.append({
            "t": t, "sample": sample, "hw": new_hw, "settings": dict(settings), "temps": temps
        })
    return rows


def scenarios():
    return [scenario(seed, 400) for seed in range(1, 11)]
