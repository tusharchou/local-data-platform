from logging import (
    getLogger,
    basicConfig,
    INFO
)


def log():
    basicConfig(level=INFO, format=
                """
                 %(message)s
                """
            )

    return getLogger('loca_data_platform-demo')
