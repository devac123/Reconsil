from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    Shared declarative base for all SQLAlchemy ORM models.

    All models in this project should inherit from this class so that
    Base.metadata.create_all() can discover and create every table.
    """
    pass
