"""Entry-point contract for the subsidiary-matching worker."""
from app.subsidiary import config, main, queue


def test_subsidiary_queue_name_is_stable():
    assert config.QUEUE == "CS-subsidiary:matching"
    assert queue.config.QUEUE == "CS-subsidiary:matching"


def test_subsidiary_main_is_callable_without_side_effects_at_import():
    assert callable(main.main)
