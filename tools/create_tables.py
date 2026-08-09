import database.models
from database.db import Base, engine

Base.metadata.create_all(bind=engine)
print('created tables')
