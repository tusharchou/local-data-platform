from local_data_platform import Worker


class Engine(Worker):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)