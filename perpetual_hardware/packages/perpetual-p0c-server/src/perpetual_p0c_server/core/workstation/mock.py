import logging
from .base import AbstractWorkstation

class MockWorkstation(AbstractWorkstation):

    async def close_all_outputs(self) -> None:
        pass

    async def open_all_outputs(self) -> None:
        pass

    async def open_output(self, output_box: int) -> None:
        logging.info(f"Open Output {output_box}")
        pass

    async def close_output(self, output_box: int) -> None:
        logging.info(f"Close Output {output_box}")
        pass

    async def open_input(self) -> None:
        pass

    async def close_input(self) -> None: 
        pass


