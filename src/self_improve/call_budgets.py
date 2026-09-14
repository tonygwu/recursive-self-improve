"""Read-only cost bounds shared by job previews and evaluation preflights."""


def gate_calls_needed(cfg):
    return cfg.eval_scenarios * (1 + 2 * cfg.eval_trials)


def model_tier(cfg, model_class):
    if model_class == cfg.cheap_model_class:
        return 'cheap'
    if model_class == cfg.strong_model_class:
        return 'strong'
    raise ValueError(
        f"unknown model_class {model_class!r}; expected "
        f"{cfg.cheap_model_class!r} or {cfg.strong_model_class!r}"
    )


def call_pool(cfg, stage, model_class):
    tier = model_tier(cfg, model_class)
    return 'gate' if stage in cfg.gate_stages else tier
