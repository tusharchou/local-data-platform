from logging import (
    getLogger,
    basicConfig,
    INFO
)


def log():
    basicConfig(level=INFO, format=
                """
                %(filename)s - %(funcName)s 
                - %(asctime)s - %(name)s 
                - %(levelname)s 
                - message : %(message)s
                """
            )

    return getLogger('loca_data_platform-demo')
