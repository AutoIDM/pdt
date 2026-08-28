import os

from sqlalchemy import MetaData, Table, create_engine, update


class State:
    def __init__(self):
        self.environment = os.getenv("MELTANO_ENVIRONMENT")
        assert self.environment

    def persist(self):
        sqlalchemy_engine = create_engine(
            f"{os.getenv('SQLALCHEMY_URL')}", pool_recycle=3600
        )
        with sqlalchemy_engine.connect() as db_connection:
            metadata = MetaData(schema="autoidm_state")
            send_once_notifications = Table(
                "send_once_notifications", metadata, autoload_with=db_connection
            )
            stmt = update(send_once_notifications).values(sent=True)
            db_connection.execute(stmt)

    @staticmethod
    def run():
        state = State()
        state.persist()
