import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _database_url() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("MYSQL_URL")
    if url:
        return url.replace("mysql://", "mysql+pymysql://", 1)
    return "mysql+pymysql://root:Admin%40123@localhost:3306/Rconsil"


DATABASE_URL = _database_url()

engine = create_engine(
    DATABASE_URL,
    echo=True,
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args={
        "connect_timeout": 10,
        "read_timeout": 300,
        "write_timeout": 300,
    },
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)
