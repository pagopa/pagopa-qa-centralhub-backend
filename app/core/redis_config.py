from redis import Redis
import os 
from app.config import settings

redis_client = Redis.from_url(settings.redis_url)
