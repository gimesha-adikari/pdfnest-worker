from app.core.actor_heartbeat import heartbeat_key
from app.core.config import settings


def test_actor_heartbeat_key_is_environment_scoped():
    assert heartbeat_key() == settings.actor_heartbeat_key
    assert heartbeat_key().startswith("pdfnest:")
    assert heartbeat_key().endswith(":actor:heartbeat")
