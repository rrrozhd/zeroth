import dramatiq
from dramatiq.brokers.redis import RedisBroker

from econ_plane.config import settings

redis_broker = RedisBroker(host=settings.redis_host, port=settings.redis_port)
dramatiq.set_broker(redis_broker)
