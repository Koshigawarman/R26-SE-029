import os
from typing import Union


def get_ssl_verify_setting() -> Union[bool, str]:
    """
    Returns the requests verify setting for OpenAI-compatible providers.

    Default is secure verification. For local certificate-chain issues, set:
    OPENAI_COMPATIBLE_VERIFY_SSL=false

    Or point to a CA bundle:
    OPENAI_COMPATIBLE_CA_BUNDLE=/path/to/cacert.pem
    """

    verify_ssl = os.getenv("OPENAI_COMPATIBLE_VERIFY_SSL", "true").lower()
    if verify_ssl in {"false", "0", "no", "off"}:
        return False

    ca_bundle = os.getenv("OPENAI_COMPATIBLE_CA_BUNDLE", "").strip()
    if ca_bundle:
        return ca_bundle

    return True
