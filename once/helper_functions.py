from once.logger import get_logger

logger = get_logger(__name__)


async def check_number_authorized(number: str) -> bool:
    logger.info(f"Checking if number {number} is authorized")
    ...
