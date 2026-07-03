from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str
    redis_url: str
    partition_count: int = 16

    class Config:
        env_file = ".env"

settings = Settings()