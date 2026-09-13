"""Reserved QQ Open Platform bot transport.

TODO: approve a bot and its private-message scope; implement AppID/AppSecret
access-token exchange/refresh, official signed webhook handshake/verification,
C2C_MESSAGE_CREATE normalization, and reply delivery with the source message ID.
The legacy static bot Token authentication has been deprecated by QQ.
Official entry: https://bot.q.qq.com/wiki/develop/api-v2/
"""

from .base import ChannelAdapter


class QQAdapter(ChannelAdapter):
    name = "qq"

    def __init__(self, on_message, *, app_id: str = "", app_secret: str = ""):
        super().__init__(on_message)
        self.credentials = {"app_id": app_id, "app_secret": app_secret}

    async def start(self) -> None:
        raise NotImplementedError(
            "QQ adapter is not connected. Apply for an official QQ bot and private-message "
            "permissions, set QQ_APP_ID and QQ_APP_SECRET, and configure HTTPS callbacks; "
            "TODO: access-token refresh, webhook signatures and C2C replies."
        )

    async def stop(self) -> None:
        return None

    async def send_message(self, user_id: str, text: str, *, buttons=()) -> None:
        raise NotImplementedError("QQ official message delivery is not implemented")
