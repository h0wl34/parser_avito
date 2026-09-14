from loguru import logger

from models import Item
from .base import Notifier


class CompositeNotifier(Notifier):
    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    def notify(self, ad: Item = None, message: str = None):
        """Notify all configured backends and report whether any delivery worked.

        Historically this method swallowed every backend exception and returned
        None, so callers could not distinguish a successful notification from a
        complete delivery failure. Returning a boolean is backward compatible
        for existing callers that ignore the return value and lets durable
        consumers avoid acknowledging an event too early.
        """
        if not self.notifiers:
            return False

        delivered = False
        for notifier in self.notifiers:
            try:
                notifier.notify(ad=ad, message=message)
                delivered = True
            except Exception as e:
                logger.exception(
                    f"Ошибка {e} отправки уведомления через {notifier.__class__.__name__}"
                )
        return delivered


class NullNotifier(Notifier):
    def notify(self, ad: Item = None, message: str = None):
        # A no-op is not a successful delivery. Durable consumers must keep a
        # valuable event pending until a real notifier is configured.
        return False
