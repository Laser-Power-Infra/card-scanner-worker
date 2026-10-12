"""Entry-point contract for the profile-collection worker."""
from app.profile import main, queue


def test_profile_queue_name_matches_the_nextjs_publisher():
    assert queue.QUEUE == "CS-profile:collection"


def test_profile_main_is_callable_without_side_effects_at_import():
    assert callable(main.main)


def test_profile_tags_cover_every_combined_worker_tag():
    from app.profile import handlers
    assert set(handlers.TAGS) == {
        "research", "linkedin", "whatsapp", "instagram", "facebook", "twitter",
    }
