from logging import (
    getLogger,
    basicConfig,
    INFO
)


def log():
    basicConfig(level=INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    return getLogger('loca_data_platform')
