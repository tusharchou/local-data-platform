from local_data_platform import Base


class Catalog(Base):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
