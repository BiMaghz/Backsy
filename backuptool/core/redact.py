import re

# Secrets that can leak through exception text (requests/boto3 embed the full URL in errors).
_PATTERNS = [
    # Telegram bot token inside API URLs: .../bot123456789:AAH.../sendDocument
    (re.compile(r'(bot)\d+:[A-Za-z0-9_-]{20,}'), r'\1<redacted>'),
    # S3 pre-signed URL query parameters
    (re.compile(r'(X-Amz-(?:Signature|Credential|Security-Token)=)[^&\s"\']+', re.IGNORECASE), r'\1<redacted>'),
    # Authorization headers
    (re.compile(r'(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}', re.IGNORECASE), r'\1<redacted>'),
]

def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
