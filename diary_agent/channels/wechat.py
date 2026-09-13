"""Reserved official WeChat Official Account transport; no personal-account automation.

TODO: after selecting and approving an Official Account, implement callback
challenge/signature verification, EncodingAESKey encryption/decryption, XML
text normalization, access-token refresh and permitted customer-service sends.
Official entry: https://developers.weixin.qq.com/doc/offiaccount/Getting_Started/Overview.html
"""

from .base import ChannelAdapter


class WeChatAdapter(ChannelAdapter):
    name = "wechat"

    def __init__(self, on_message, *, app_id: str = "", app_secret: str = "",
                 token: str = "", encoding_aes_key: str = ""):
        super().__init__(on_message)
        self.credentials = {"app_id": app_id, "app_secret": app_secret,
                            "token": token, "encoding_aes_key": encoding_aes_key}

    async def start(self) -> None:
        raise NotImplementedError(
            "WeChat adapter is not connected. Configure an official account, "
            "WECHAT_APP_ID, WECHAT_APP_SECRET, WECHAT_TOKEN, WECHAT_ENCODING_AES_KEY "
            "and HTTPS callbacks; TODO: encrypted callbacks, token refresh and message delivery."
        )

    async def stop(self) -> None:
        return None

    async def send_message(self, user_id: str, text: str, *, buttons=()) -> None:
        raise NotImplementedError("WeChat official message delivery is not implemented")
