from app.jobs.actors import (
    editor_compile_job,
    editor_extract_job,
    markup_highlight_job,
    markup_strikeout_job,
    markup_underline_job,
)


def test_actor_time_limits():
    assert editor_extract_job.options.get("time_limit") == 600_000
    assert editor_compile_job.options.get("time_limit") == 600_000
    assert markup_highlight_job.options.get("time_limit") == 900_000
    assert markup_underline_job.options.get("time_limit") == 900_000
    assert markup_strikeout_job.options.get("time_limit") == 900_000
